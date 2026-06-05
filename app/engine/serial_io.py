"""Low-level async serial open/resolve helpers built on pyserial-asyncio-fast.

Kept separate from the bridge so the device-resolution and pyserial-config
mapping live in one place. ``resolve_device`` re-resolves a stable hardware
identity (VID/PID/serial) to a current device path so COMx / ttyUSB* renumbering
doesn't break a mapping across reconnects.
"""
from __future__ import annotations

import sys
from typing import Any

from ..config import SerialSettings

_patched = False


def _patch_serial_asyncio_fast() -> None:
    """Fix a writer-deregistration bug in serial_asyncio_fast 0.16.

    Upstream's SerialTransport._write_ready() drains the write buffer but never
    calls _remove_writer() once the buffer empties. The fd-writer (POSIX
    add_writer / Windows polled _poll_write) therefore stays armed, and the next
    writable event re-enters _write_ready() with an empty buffer, hitting
    `assert data, "Write buffer should not be empty"` and busy-looping. This only
    manifests when a serial write hits EAGAIN/partial (i.e. under sustained or
    concurrent load — exactly the many-ports case), so we patch it to deregister
    the writer when the buffer is empty, mirroring CPython's selector transport.
    """
    global _patched
    if _patched:
        return
    import serial_asyncio_fast as saf

    T = saf.SerialTransport
    if getattr(T, "_ser2net_write_ready_patched", False):
        _patched = True
        return

    def _write_ready(self) -> None:
        if not self._write_buffer:
            self._remove_writer()
            return
        if len(self._write_buffer) == 1:
            data = self._write_buffer.pop()
        else:
            data = b"".join(self._write_buffer)
            self._write_buffer.clear()
        self._write_data(data)
        self._maybe_resume_protocol()  # may append to the buffer
        if not self._write_buffer and not self._closing:
            self._remove_writer()

    T._write_ready = _write_ready
    T._ser2net_write_ready_patched = True
    _patched = True


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


async def open_serial(settings: SerialSettings):
    """Open the serial port asynchronously.

    Returns (reader, writer, serial_instance, device_path). Raises
    serial.SerialException / OSError if the device can't be opened.
    """
    import serial_asyncio_fast

    _patch_serial_asyncio_fast()
    device = resolve_device(settings)
    kwargs: dict[str, Any] = dict(
        url=device,
        baudrate=settings.baudrate,
        bytesize=settings.bytesize,
        parity=settings.parity,
        stopbits=settings.stopbits,
        xonxoff=(settings.flowcontrol == "xonxoff"),
        rtscts=(settings.flowcontrol == "rtscts"),
        dsrdtr=(settings.flowcontrol == "dsrdtr"),
    )
    # `exclusive` is POSIX-only in pyserial; Windows ports are exclusive anyway.
    if sys.platform != "win32":
        kwargs["exclusive"] = settings.exclusive

    reader, writer = await serial_asyncio_fast.open_serial_connection(**kwargs)
    ser = writer.transport.serial

    # apply RTS/DTR start state (only when explicitly on/off)
    try:
        if settings.rts_on_open in ("on", "off"):
            ser.rts = settings.rts_on_open == "on"
        if settings.dtr_on_open in ("on", "off"):
            ser.dtr = settings.dtr_on_open == "on"
    except Exception:
        pass  # not all backends/adapters support explicit line control

    # RS-485 (best-effort; hardware/OS dependent)
    adv = settings.advanced
    if adv.rs485_enabled:
        try:
            import serial.rs485

            ser.rs485_mode = serial.rs485.RS485Settings(
                rts_level_for_tx=adv.rs485_rts_level_for_tx,
                rts_level_for_rx=not adv.rs485_rts_level_for_tx,
                delay_before_tx=(adv.rs485_delay_before_tx_ms / 1000.0) or None,
                delay_before_rx=(adv.rs485_delay_after_tx_ms / 1000.0) or None,
            )
        except Exception:
            pass

    return reader, writer, ser, device
