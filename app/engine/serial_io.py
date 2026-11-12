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
import sys
import weakref
from typing import Any, Optional

from ..config import SerialSettings, url_port
from . import portdiag, portlist

# StreamReader high-water for the serial side. The default (64 KB) makes the reader
# pause/resume frequently under load; we read promptly in the bridge and apply our
# own per-client backpressure, so a larger buffer keeps the serial read flowing.
SERIAL_STREAM_LIMIT = 1024 * 1024


def _match_port(p: Any, want: dict[str, Any]) -> bool:
    def hexid(v: Any) -> str | None:
        return f"{v:04x}" if isinstance(v, int) else None

    if want.get("vid") and hexid(getattr(p, "vid", None)) != str(want["vid"]).lower():
        return False
    if want.get("pid") and hexid(getattr(p, "pid", None)) != str(want["pid"]).lower():
        return False
    if want.get("serial_number") and getattr(p, "serial_number", None) != want["serial_number"]:
        return False
    if want.get("location") and getattr(p, "location", None) != want["location"]:
        return False
    return True


def _match_listed(p: dict[str, Any], want: dict[str, Any]) -> bool:
    """_match_port for a port of portlist's list (its IDs are hex strings already)."""
    for key in ("vid", "pid"):
        if want.get(key) and (p.get(key) or "") != str(want[key]).lower():
            return False
    for key in ("serial_number", "location"):
        if want.get(key) and p.get(key) != want[key]:
            return False
    return True


def resolve_device(settings: SerialSettings,
                   ports: Optional[list[dict[str, Any]]] = None) -> str:
    """Return the device path to open, preferring a stable-id match if configured: from
    ``ports`` (portlist's list) when given, else by enumerating the ports (blocking)."""
    want = {k: v for k, v in (settings.match or {}).items() if v}
    if want:
        if ports is not None:
            for d in ports:
                if _match_listed(d, want):
                    return d["device"]
        else:
            from serial.tools import list_ports

            for p in list_ports.comports():
                if _match_port(p, want):
                    return p.device
        # match not currently present -> fall back to the literal path (will error
        # at open if truly absent, which the supervisor reports as device-missing)
    return settings.port


async def _resolve(settings: SerialSettings) -> str:
    """resolve_device without blocking the event loop: from the port list the PortWatcher
    keeps (fresh within a poll or two), else enumerated in a thread."""
    if not any((settings.match or {}).values()):
        return settings.port
    ports = portlist.recent()
    if ports is not None:
        return resolve_device(settings, ports)
    return await asyncio.to_thread(resolve_device, settings)


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
    device = await _resolve(settings)
    refuse_url(device)  # the one place every serial port is opened
    found: list[portdiag.Note] = await portdiag.about_port(device)
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
