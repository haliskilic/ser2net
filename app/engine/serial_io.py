"""Low-level async serial open/resolve helpers built on pyserial-asyncio-fast.

Kept separate from the bridge so the device-resolution and pyserial-config
mapping live in one place. ``resolve_device`` re-resolves a stable hardware
identity (VID/PID/serial) to a current device path so COMx / ttyUSB* renumbering
doesn't break a mapping across reconnects. The port class, the transport and the stream
on top of them are serial_compat's (which also holds ser2net's fixes for pyserial and
pyserial-asyncio-fast).
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import sys
import weakref
from typing import Any, Optional

from ..config import SerialSettings, identity_text, url_port, usb_identity
from . import portdiag, portlist

# StreamReader high-water for the serial side. The default (64 KB) makes the reader
# pause/resume frequently under load; we read promptly in the bridge and apply our
# own per-client backpressure, so a larger buffer keeps the serial read flowing.
SERIAL_STREAM_LIMIT = 1024 * 1024


def _match_listed(p: dict[str, Any], want: dict[str, Any]) -> bool:
    """Whether port ``p`` of portlist's list (IDs as hex strings) has the USB identity
    ``want`` (config.usb_identity: only known keys). A serial number is compared in any
    letter case (Linux and Windows may read one adapter's apart)."""
    for key in ("vid", "pid"):
        if want.get(key) and (p.get(key) or "") != str(want[key]).lower():
            return False
    if want.get("serial_number") and (
            str(p.get("serial_number") or "").casefold() != str(want["serial_number"]).casefold()):
        return False
    if want.get("location") and p.get("location") != want["location"]:
        return False
    return True


class ForeignDevice(OSError):
    """The port a mapping is set to is another USB device now, and the one it is bound to is
    not there: it is not opened (errno ENODEV: the state is device-missing)."""


def _enumerate() -> list[dict[str, Any]]:
    """The serial ports with their USB identities, enumerated now (blocking)."""
    from serial.tools import list_ports

    return [portlist._normalize(p) for p in list_ports.comports()]


def resolve(settings: SerialSettings,
            ports: Optional[list[dict[str, Any]]] = None) -> tuple[str, list[str]]:
    """The device to open for ``settings``, and the ports that have the USB identity it is
    bound to (sorted; [] for a port opened by its name). ``ports`` is portlist's list; without
    it the ports are enumerated (blocking).

    Of several ports with that identity (identical adapters without their own serial number,
    the channels of an FT4232H), the configured port is opened when it is one of them (also
    through a /dev/serial/by-id or by-path link), else the first one no other mapping of this
    process has open. When no port has it and the configured port is now another USB device,
    ForeignDevice is raised instead of opening that one; a configured port that is not there
    or has no USB identity is opened by its name (and fails as missing when it is not
    there)."""
    want = usb_identity(settings.match)
    if not want:
        return settings.port, []
    listed = _enumerate() if ports is None else ports
    matches = sorted(p["device"] for p in listed if p.get("device") and _match_listed(p, want))
    if matches:
        mine = next((d for d in matches if portlist.same_port(settings.port, d)), None)
        if mine is None:
            free = [d for d in matches if not portdiag.holder(d)]
            mine = (free or matches)[0]
        return mine, matches
    here = portlist.find(listed, settings.port, resolve=True)
    if here is not None and here.get("vid"):
        raise ForeignDevice(
            errno.ENODEV,
            f"could not open port {settings.port}: it is another USB device now "
            f"({identity_text(here)}), not the one this mapping is bound to "
            f"({identity_text(want)}), so it is not opened; plug that one in, or bind the "
            "mapping to this one in its form")
    return settings.port, []


def resolve_device(settings: SerialSettings,
                   ports: Optional[list[dict[str, Any]]] = None) -> str:
    """The device path to open for ``settings`` (resolve)."""
    return resolve(settings, ports)[0]


async def _resolve(settings: SerialSettings) -> tuple[str, list[str]]:
    """resolve without blocking the event loop: from the port list the PortWatcher keeps
    (fresh within a poll or two), else enumerated in a thread."""
    if not settings.identity:
        return settings.port, []
    ports = portlist.recent()
    if ports is not None:
        return resolve(settings, ports)
    return await asyncio.to_thread(resolve, settings)


def refuse_url(device: str) -> None:
    """Raise ValueError for a port written as a URL (socket://, rfc2217://, spy://, loop://
    ...): pyserial would hand it to a URL handler, which opens a network connection or a
    file instead of a serial device. Edits refuse such ports (MappingConfig.validate_input);
    this keeps a stored one (hand-edited, restored, older config) from ever being opened."""
    if url_port(device):
        raise ValueError(f"serial port {device!r} is a URL, not a serial device; "
                         "URL ports are not opened")


def port_settings(settings: SerialSettings) -> dict[str, Any]:
    """The pyserial settings a mapping's port is opened with: baud rate, data bits, parity,
    stop bits and flow control (in the order pyserial applies them)."""
    return dict(
        baudrate=settings.baudrate,
        bytesize=settings.bytesize,
        parity=settings.parity,
        stopbits=settings.stopbits,
        xonxoff=(settings.flowcontrol == "xonxoff"),
        rtscts=(settings.flowcontrol == "rtscts"),
        dsrdtr=(settings.flowcontrol == "dsrdtr"),
    )


_LINE_STATES = {"on": True, "off": False}  # rts_on_open / dtr_on_open; "keep": pyserial's


async def open_serial(settings: SerialSettings):
    """Open the serial port asynchronously.

    Returns (reader, writer, serial_instance, device_path). Raises
    serial.SerialException / OSError if the device can't be opened, ValueError for a
    URL port (refuse_url).

    RTS / DTR set to on or off are in that state from the moment the port opens, and are
    set again once it is open (pyserial skips them quietly when the port has no modem
    lines, a PTY for one). A setting the port did not take (that, or RS-485 mode) does not
    fail the open: open_notes(serial_instance) has it (portdiag.Note) for the caller to
    report, with what else may use the port (portdiag.port_notes: the kernel console, a
    login prompt). A refused RS-485 mode is also taken back, as pyserial keeps it and would
    fail every later change of the port's settings with it (an RFC 2217 client's baud
    rate)."""
    device, matches = await _resolve(settings)
    refuse_url(device)  # the one place every serial port is opened
    found: list[portdiag.Note] = await portdiag.about_port(device)
    if len(matches) > 1 and not portlist.same_port(settings.port, device):
        # a guess: identical adapters (no serial number of their own) can't be told apart
        found.append(portdiag.Note("identity_shared", (
            identity_text(settings.match), len(matches), device)))
    kwargs: dict[str, Any] = port_settings(settings)
    # `exclusive` is POSIX-only in pyserial; Windows ports are exclusive anyway.
    if sys.platform != "win32":
        kwargs["exclusive"] = settings.exclusive

    import serial.rs485

    from . import serial_compat

    rts, dtr = _LINE_STATES.get(settings.rts_on_open), _LINE_STATES.get(settings.dtr_on_open)
    adv = settings.advanced
    keep_out = {"tiocexcl": bool(adv.tiocexcl),
                "respect_uucp_locks": bool(adv.respect_uucp_locks)}
    reader, writer = await serial_compat.open_stream(device, SERIAL_STREAM_LIMIT, rts=rts,
                                                     dtr=dtr, keep_out=keep_out, **kwargs)
    transport: Any = writer.transport  # serial_compat.Transport
    ser = transport.serial

    for line, want in (("RTS", rts), ("DTR", dtr)):
        if want is not None:
            try:
                setattr(ser, line.lower(), want)
            except Exception as e:  # ENOTTY / EINVAL: no modem lines; Windows: refused
                found.append(portdiag.Note("line", (line, want, portdiag.reason(e))))

    # RS-485 (hardware/OS dependent): never fatal, so a port without it still opens
    if adv.rs485_enabled:
        try:
            ser.rs485_mode = serial.rs485.RS485Settings(
                rts_level_for_tx=adv.rs485_rts_level_for_tx,
                rts_level_for_rx=not adv.rs485_rts_level_for_tx,
                delay_before_tx=(adv.rs485_delay_before_tx_ms / 1000.0) or None,
                delay_before_rx=(adv.rs485_delay_after_tx_ms / 1000.0) or None,
            )
        except Exception as e:  # Linux: ENOTTY on most USB adapters; Windows: delays
            with contextlib.suppress(Exception):
                ser.rs485_mode = None
            found.append(portdiag.Note("rs485", (portdiag.reason(e),)))

    if found:
        _NOTES[ser] = found
    return reader, writer, ser, device


# what open_serial found about a port it opened (a setting it did not take, ...), by port
_NOTES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def open_notes(ser: Any) -> list[portdiag.Note]:
    """What open_serial found about the open port ``ser`` (portdiag.Note), [] if nothing
    (or for a port it did not open)."""
    try:
        return list(_NOTES.get(ser, ()))
    except TypeError:  # (a stand-in that can't be weakly referenced)
        return []
