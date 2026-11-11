"""Low-level async serial open/resolve helpers built on pyserial-asyncio-fast.

Kept separate from the bridge so the device-resolution and pyserial-config
mapping live in one place. ``resolve_device`` re-resolves a stable hardware
identity (VID/PID/serial) to a current device path so COMx / ttyUSB* renumbering
doesn't break a mapping across reconnects. The port class, the transport and the stream
on top of them are serial_compat's (which also holds ser2net's fixes for pyserial and
pyserial-asyncio-fast).
"""
from __future__ import annotations

import sys
from typing import Any

from ..config import SerialSettings, url_port

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


def resolve_device(settings: SerialSettings) -> str:
    """Return the device path to open, preferring a stable-id match if configured."""
    want = {k: v for k, v in (settings.match or {}).items() if v}
    if want:
        from serial.tools import list_ports

        for p in list_ports.comports():
            if _match_port(p, want):
                return p.device
        # match not currently present -> fall back to the literal path (will error
        # at open if truly absent, which the supervisor reports as device-missing)
    return settings.port


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


async def open_serial(settings: SerialSettings):
    """Open the serial port asynchronously.

    Returns (reader, writer, serial_instance, device_path). Raises
    serial.SerialException / OSError if the device can't be opened, ValueError for a
    URL port (refuse_url).
    """
    device = resolve_device(settings)
    refuse_url(device)  # the one place every serial port is opened
    kwargs: dict[str, Any] = port_settings(settings)
    # `exclusive` is POSIX-only in pyserial; Windows ports are exclusive anyway.
    if sys.platform != "win32":
        kwargs["exclusive"] = settings.exclusive

    import serial.rs485

    from . import serial_compat

    reader, writer = await serial_compat.open_stream(device, SERIAL_STREAM_LIMIT, **kwargs)
    transport: Any = writer.transport  # serial_compat.Transport
    ser = transport.serial

    # apply RTS/DTR start state (only when explicitly on/off). Some backends/adapters
    # don't support explicit line control (ENOTTY etc.) — that's expected; anything
    # else is surfaced to the caller (logged by the supervisor).
    try:
        if settings.rts_on_open in ("on", "off"):
            ser.rts = settings.rts_on_open == "on"
        if settings.dtr_on_open in ("on", "off"):
            ser.dtr = settings.dtr_on_open == "on"
    except (OSError, ValueError, AttributeError, _serial_exc()):
        pass

    # RS-485 (best-effort; hardware/OS dependent)
    adv = settings.advanced
    if adv.rs485_enabled:
        try:
            ser.rs485_mode = serial.rs485.RS485Settings(
                rts_level_for_tx=adv.rs485_rts_level_for_tx,
                rts_level_for_rx=not adv.rs485_rts_level_for_tx,
                delay_before_tx=(adv.rs485_delay_before_tx_ms / 1000.0) or None,
                delay_before_rx=(adv.rs485_delay_after_tx_ms / 1000.0) or None,
            )
        except (OSError, ValueError, AttributeError, NotImplementedError, _serial_exc()):
            pass

    return reader, writer, ser, device


def _serial_exc():
    """serial.SerialException, resolved lazily so this module imports without pyserial."""
    import serial
    return serial.SerialException
