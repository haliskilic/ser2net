"""Serial port enumeration + live (hotplug-aware) port list.

Baseline is privilege-free polling of ``serial.tools.list_ports.comports()`` on a
timer, diffed against the last snapshot — works identically on Windows and Linux
and needs no elevation just to *list* ports. (Opening a port for forwarding does
need dialout-group membership on Linux; that surfaces in the engine, not here.)

An optional event-driven layer (pyudev on Linux, WM_DEVICECHANGE on Windows) can
be layered later to merely *trigger* an immediate rescan; the polling loop remains
the always-correct source of truth.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from typing import Any, Callable, Optional

from ..config import canonical_device

BY_ID_DIR = "/dev/serial/by-id"


def _normalize(p: Any) -> dict[str, Any]:
    """Normalize a pyserial ListPortInfo into a JSON-friendly dict.

    Several fields are platform-dependent and may be None; the UI must tolerate
    that (don't rely on a stable schema across OSes).
    """
    def hexid(v: Any) -> Optional[str]:
        return f"{v:04x}" if isinstance(v, int) else None

    return {
        "device": p.device,
        "name": getattr(p, "name", None) or p.device,
        "description": (getattr(p, "description", None) or "").strip() or None,
        "hwid": getattr(p, "hwid", None),
        "vid": hexid(getattr(p, "vid", None)),
        "pid": hexid(getattr(p, "pid", None)),
        "serial_number": getattr(p, "serial_number", None),
        "manufacturer": getattr(p, "manufacturer", None),
        "product": getattr(p, "product", None),
        "location": getattr(p, "location", None),
    }


def _by_id_links(by_id_dir: str = BY_ID_DIR) -> dict[str, str]:
    """Linux: real device path -> its stable /dev/serial/by-id/... name (udev)."""
    try:
        names = sorted(os.listdir(by_id_dir))
    except OSError:
        return {}
    out: dict[str, str] = {}
    for n in names:
        link = os.path.join(by_id_dir, n)
        out.setdefault(os.path.realpath(link), link)
    return out


def has_hardware(device: str, sys_class: str = "/sys/class/tty") -> Optional[bool]:
    """Linux 8250 ports: the kernel registers /dev/ttyS0..31 whether or not a UART is
    there; sysfs 'type' is 0 (PORT_UNKNOWN) when none is. False = no hardware behind
    the name, True = a UART answered, None = can't tell (not a ttyS port, not Linux)."""
    name = os.path.basename(device)
    if not sys.platform.startswith("linux") or not name.startswith("ttyS"):
        return None
    try:
        with open(os.path.join(sys_class, name, "type"), encoding="ascii") as fh:
            return fh.read().strip() != "0"
    except OSError:
        return None


def snapshot() -> list[dict[str, Any]]:
    """Synchronous one-shot enumeration (blocking; call via executor in async code).
    Each port also gets ``by_id`` (Linux udev stable name, else None), ``hardware``
    (see has_hardware), ``warnings`` (what else may use it: portdiag.port_warnings) and
    ``access`` (whether this process may open it; None: can't tell, Windows)."""
    from serial.tools import list_ports

    from . import portdiag

    links = _by_id_links() if sys.platform.startswith("linux") else {}
    ports = []
    for p in list_ports.comports():
        d = _normalize(p)
        d["by_id"] = links.get(os.path.realpath(d["device"]))
        d["hardware"] = has_hardware(d["device"])
        d["warnings"] = portdiag.port_warnings(d["device"])
        d["access"] = portdiag.access(d["device"])
        ports.append(d)
    ports.sort(key=lambda d: d["device"])
    return ports


def visible(ports: list[dict[str, Any]], show_all: bool = False) -> list[dict[str, Any]]:
    """Ports worth offering in the UI: without the kernel's placeholder ttyS ports
    that have no UART behind them (unless show_all)."""
    return list(ports) if show_all else [p for p in ports if p.get("hardware") is not False]


def names(port: dict[str, Any]) -> list[str]:
    """The names a listed port can be set as: its device path and its udev links."""
    return [n for n in (port.get("device"), port.get("by_id"), port.get("by_path")) if n]


def find(ports: list[dict[str, Any]], device: str,
         resolve: bool = False) -> Optional[dict[str, Any]]:
    """The listed port ``device`` names: its device path or a udev link of it (by-id,
    by-path), in any spelling Windows opens alike (canonical_device: com3 is COM3). With
    ``resolve``, also a link elsewhere that leads to it (/dev/serial0, a udev SYMLINK; only
    for this machine's own list, as it looks at this machine's files)."""
    if not device or not isinstance(device, str):
        return None
    key = canonical_device(device)
    for p in ports:
        if key in (canonical_device(n) for n in names(p)):
            return p
    if resolve and device.startswith("/"):
        real = os.path.realpath(device)
        if real != device:
            return next((p for p in ports if p.get("device") == real), None)
    return None


def used(ports: list[dict[str, Any]], devices: Any) -> set[str]:
    """The device paths of the listed ``ports`` that one of ``devices`` (the ports running
    mappings have open, under the names they were set as) names (find)."""
    return {p["device"] for d in devices if (p := find(ports, d)) is not None}


def same_port(a: str, b: str) -> bool:
    """Whether the port names ``a`` and ``b`` open the same port on this machine: equal in
    the canonical spelling (canonical_device), or POSIX paths that lead to one device (a
    by-id or by-path link and the device itself)."""
    if not a or not b:
        return False
    if canonical_device(a) == canonical_device(b):
        return True
    return a.startswith("/") and b.startswith("/") and os.path.realpath(a) == os.path.realpath(b)


async def async_snapshot() -> list[dict[str, Any]]:
    """Run the blocking enumeration in a thread so the event loop never stalls."""
    return await asyncio.to_thread(snapshot)


# the last list a PortWatcher took: (time.monotonic(), ports)
_recent: tuple[float, list[dict[str, Any]]] = (0.0, [])


def recent(max_age: float = 5.0) -> Optional[list[dict[str, Any]]]:
    """The port list a running PortWatcher took at most ``max_age`` seconds ago, None
    when there is none that fresh (no watcher: enumerate instead)."""
    taken, ports = _recent
    if not taken or time.monotonic() - taken > max_age:
        return None
    return list(ports)


def _remember(ports: list[dict[str, Any]]) -> None:
    global _recent
    _recent = (time.monotonic(), list(ports))


class PortWatcher:
    """Polls the serial port list on an interval and tracks the current snapshot.

    ``get()`` returns the latest list. ``version`` increments on every change, so
    the UI can poll cheaply and the watcher can notify a callback (e.g. to push a
    refresh). Cross-platform, privilege-free.
    """

    def __init__(self, interval: float = 2.0, on_change: Optional[Callable[[list[dict[str, Any]]], None]] = None):
        self.interval = interval
        self._on_change = on_change
        self._ports: list[dict[str, Any]] = []
        self._version = 0
        self._task: Optional[asyncio.Task] = None
        self._rescans: set = set()   # keep refs to fire-and-forget rescan tasks
        self._stop = asyncio.Event()

    @property
    def version(self) -> int:
        return self._version

    def get(self) -> list[dict[str, Any]]:
        return list(self._ports)

    async def start(self) -> None:
        # prime synchronously so the first UI render has data
        self._ports = await async_snapshot()
        _remember(self._ports)
        self._version = 1
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="port-watcher")
        # optional event-driven layer (just triggers an immediate rescan); polling
        # above remains the always-correct source of truth if this can't start.
        self._loop = asyncio.get_running_loop()
        self._event_thread: Optional[threading.Thread] = None
        self._start_event_watcher()

    async def stop(self) -> None:
        global _recent
        _recent = (0.0, [])
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _trigger_rescan(self) -> None:
        # keep a reference so the task isn't garbage-collected mid-flight (RUF006)
        if self._stop.is_set():
            return
        task = asyncio.ensure_future(self.refresh_now())
        self._rescans.add(task)
        task.add_done_callback(self._rescans.discard)

    def _start_event_watcher(self) -> None:
        """Best-effort hotplug events; silently no-op (polling continues) if the
        platform library is missing or the source can't be created."""
        if sys.platform.startswith("linux"):
            try:
                import pyudev  # optional
                ctx = pyudev.Context()
                mon = pyudev.Monitor.from_netlink(ctx)
                mon.filter_by("tty")

                def run():
                    for _dev in iter(lambda: mon.poll(timeout=1.0), False):
                        if self._stop.is_set():
                            break
                        if _dev is not None:
                            self._loop.call_soon_threadsafe(self._trigger_rescan)

                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                self._event_thread = thread
            except Exception:
                pass  # libudev/pyudev unavailable or blocked -> polling only
        elif sys.platform == "win32":
            try:
                import win32gui  # noqa: F401  (pywin32, optional)
                from . import _win_hotplug  # optional helper module

                self._event_thread = _win_hotplug.start(self._loop, self._trigger_rescan,
                                                        self._stop)
            except Exception:
                pass  # pywin32 / helper unavailable -> polling only

    async def refresh_now(self) -> list[dict[str, Any]]:
        """Force an immediate rescan (used by the UI 'Refresh' button)."""
        new = await async_snapshot()
        self._apply(new)
        return self.get()

    def _apply(self, new: list[dict[str, Any]]) -> None:
        _remember(new)  # (before on_change: a mapping it wakes resolves its port from it)
        if {p["device"] for p in new} != {p["device"] for p in self._ports}:
            self._ports = new
            self._version += 1
            if self._on_change:
                try:
                    self._on_change(self.get())
                except Exception:
                    pass
        else:
            # keep metadata fresh even when the device set is unchanged
            self._ports = new

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self.interval)
                new = await async_snapshot()
                self._apply(new)
            except asyncio.CancelledError:
                raise
            except Exception:
                # transient enumeration errors must not kill the watcher
                continue
