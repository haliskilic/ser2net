"""Serial port enumeration + the live port list.

Listing ports is privilege-free polling of ``serial.tools.list_ports.comports()``, which
works the same on Windows and Linux and needs no elevation just to *list* ports. (Opening a
port for forwarding does need the serial group on Linux; that surfaces in the engine, not
here.)

PortWatcher keeps the list. Taking it is a full enumeration (sysfs or SetupAPI, a few ms
per port), so it is not taken at every poll: every couple of seconds a cheap check
(port_signature: a few directory reads, a registry key on Windows) tells whether a port
came or went, and only then is the list taken again; also when the UI or the API asks for
one older than a couple of seconds (PortWatcher.current), and at least every minute
(what else may use a port changes without the ports changing).
"""
from __future__ import annotations

import asyncio
import glob
import os
import stat
import sys
import time
from typing import Any, Callable, Optional

from ..config import canonical_device
from ..textfold import natural_key

BY_ID_DIR = "/dev/serial/by-id"
BY_PATH_DIR = "/dev/serial/by-path"
SYS_CLASS_TTY = "/sys/class/tty"
PROC_TTY_DRIVERS = "/proc/tty/drivers"


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


def _links(link_dir: str) -> dict[str, str]:
    """Linux: real device path -> its udev link in ``link_dir`` (the first by name when it
    has several, as by-path's usb- and usbv2- forms)."""
    try:
        names = sorted(os.listdir(link_dir))
    except OSError:
        return {}
    out: dict[str, str] = {}
    for n in names:
        link = os.path.join(link_dir, n)
        out.setdefault(os.path.realpath(link), link)
    return out


def _by_id_links(by_id_dir: str = BY_ID_DIR) -> dict[str, str]:
    """Linux: real device path -> its /dev/serial/by-id/... name (udev: by vendor, model
    and serial number)."""
    return _links(by_id_dir)


def _identities(ports: list[dict[str, Any]], udev: bool) -> None:
    """Mark each port with ``same_identity``: the other listed ports that have its USB
    identity (VID:PID and serial number, in any letter case), which finding a device by its
    USB identity can't tell from it (identical adapters without a serial number of their
    own, a multi-port chip's channels); and ``ambiguous``: a USB port without a by-id name
    of its own (Linux, where udev makes them). A by-id name is made from vendor, model and
    serial number, so identical adapters without their own serial number get one name, which
    udev gives to one of them: the one that has it may change at a reboot or a re-plug.
    Compared across the listed ports, not by a missing serial number (a lone adapter without
    one is unambiguous; clones that all carry one serial number are not)."""
    def ident(p: dict[str, Any]) -> tuple:
        return p.get("vid"), p.get("pid"), str(p.get("serial_number") or "").casefold()

    groups: dict[tuple, list[dict[str, Any]]] = {}
    for p in ports:
        if p.get("vid") and p.get("pid"):
            groups.setdefault(ident(p), []).append(p)
    for p in ports:
        usb = bool(p.get("vid") and p.get("pid"))
        twins = [q for q in groups.get(ident(p), []) if q is not p] if usb else []
        p["same_identity"] = sorted(q["device"] for q in twins)
        p["ambiguous"] = bool(udev and usb and (not p.get("by_id")
                                                 or any(not q.get("by_id") for q in twins)))


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


def serial_devices(drivers: str = PROC_TTY_DRIVERS,
                   stat_device: Callable[[str], Any] = os.stat) -> list[str]:
    """Linux: the device nodes of the kernel's serial drivers, whatever they are called (the
    drivers of type "serial" in /proc/tty/drivers, their nodes by the major and minor numbers
    each one lists). pyserial 3.5 looks for seven name patterns only (not ttymxc / ttyLP on
    i.MX, ttyTHS / ttyTCU on Jetson, ttyMSM, ttyPS / ttyUL on Xilinx, ttyMV, ttyAML, ttyMAX,
    ttyMI, ttyGS, vendor USB drivers such as ttyCH343USB) and drops the ports on the platform
    bus, which before Linux 6.5 are a Raspberry Pi's ttyS0 and other SoC UARTs. Its master
    branch reads /proc/tty/drivers the same way: this can go once a pyserial release after
    3.5 is pinned. Blocking (one small read, a glob and a stat per node)."""
    if sys.platform == "win32":  # (no /proc, no device numbers)
        return []
    try:
        with open(drivers, encoding="ascii", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    found: set[str] = set()
    for line in lines:
        # name, node prefix, major, minors ("64-111" or one number), type
        fields = line.split()
        if len(fields) < 5 or fields[-1] != "serial" or not fields[1].startswith("/"):
            continue
        try:
            major = int(fields[2])
            low, _sep, high = fields[3].partition("-")
            minors = range(int(low), int(high or low) + 1)
        except ValueError:
            continue
        for node in glob.glob(glob.escape(fields[1]) + "*"):
            try:
                st = stat_device(node)
            except OSError:
                continue
            if (stat.S_ISCHR(st.st_mode) and os.major(st.st_rdev) == major
                    and os.minor(st.st_rdev) in minors):
                found.add(node)
    return sorted(found)


def _ports() -> list[Any]:
    """pyserial's port list, and on Linux the serial ports it misses (serial_devices), with
    the same details (its sysfs reader)."""
    from serial.tools import list_ports

    listed = list(list_ports.comports())
    if sys.platform.startswith("linux"):
        from serial.tools.list_ports_linux import SysFS

        have = {os.path.realpath(p.device) for p in listed}
        for node in serial_devices():
            real = os.path.realpath(node)
            if real not in have:
                have.add(real)
                listed.append(SysFS(node))
    return listed


def snapshot() -> list[dict[str, Any]]:
    """Synchronous one-shot enumeration (blocking; call via executor in async code).
    Each port also gets ``by_id`` and ``by_path`` (its Linux udev names: by serial number,
    by USB socket; else None), ``same_identity`` and ``ambiguous`` (_identities),
    ``hardware`` (see has_hardware), ``warnings`` (what else may use it:
    portdiag.port_warnings) and ``access`` (whether this process may open it; None: can't
    tell, Windows). On Linux it also has the serial ports pyserial 3.5 misses
    (serial_devices); the kernel's placeholders among them are hardware False, as pyserial's
    own."""
    from . import portdiag

    linux = sys.platform.startswith("linux")
    by_id = _links(BY_ID_DIR) if linux else {}
    by_path = _links(BY_PATH_DIR) if linux else {}
    ports = []
    for p in _ports():
        d = _normalize(p)
        real = os.path.realpath(d["device"])
        d["by_id"] = by_id.get(real)
        d["by_path"] = by_path.get(real)
        d["hardware"] = has_hardware(d["device"])
        d["warnings"] = portdiag.port_warnings(d["device"])
        d["access"] = portdiag.access(d["device"])
        ports.append(d)
    ports.sort(key=lambda d: d["device"])
    _identities(ports, udev=bool(by_id or by_path))
    return ports


def visible(ports: list[dict[str, Any]], show_all: bool = False) -> list[dict[str, Any]]:
    """Ports worth offering in the UI, in the order people count them (COM2 before COM10,
    natural_key): without the kernel's placeholder ttyS ports that have no UART behind
    them (unless show_all). The REST API keeps snapshot()'s order."""
    shown = list(ports) if show_all else [p for p in ports if p.get("hardware") is not False]
    return sorted(shown, key=lambda p: natural_key(str(p.get("device") or "")))


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


def choice(ports: list[dict[str, Any]], selected: str,
           links: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """What the Device field shows for the configured port ``selected``: ``port``, the
    listed port it names (find), and ``as``, how: "device", "by_id", "by_path" (that name of
    the port), "alias" (another name of it: another spelling, a link elsewhere), None (not
    listed). ``links``: {name: the device it leads to}, worked out on the machine the ports
    are on (a link such as /dev/serial0 is not one of the port's own names)."""
    port = find(ports, selected)
    if port is None and links and links.get(selected):
        port = next((p for p in ports if p.get("device") == links[selected]), None)
    how = None
    if port is not None:
        how = next((k for k in ("device", "by_id", "by_path") if port.get(k) == selected),
                   "alias")
    return {"port": port, "as": how}


def links(ports: list[dict[str, Any]], configured: Any) -> dict[str, str]:
    """{name: the listed device it leads to} for each of the ``configured`` ports that is a
    link to a listed port but not one of its own names (/dev/serial0 -> /dev/ttyS0, a udev
    SYMLINK): for choice(). Reads this machine's files, so only for its own port list."""
    out: dict[str, str] = {}
    for name in configured:
        if isinstance(name, str) and name.startswith("/") and find(ports, name) is None:
            port = find(ports, name, resolve=True)
            if port is not None:
                out[name] = port["device"]
    return out


def used(ports: list[dict[str, Any]], devices: Any) -> set[str]:
    """The device paths of the listed ``ports`` (this machine's) that one of ``devices`` (the
    ports running mappings have open, under the names they were set as) names, also through
    a link such as /dev/serial0 (find with resolve)."""
    return {p["device"] for d in devices if (p := find(ports, d, resolve=True)) is not None}


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


def port_signature(sys_class: str = SYS_CLASS_TTY,
                   link_dirs: tuple[str, ...] = (BY_ID_DIR, BY_PATH_DIR)) -> Optional[tuple]:
    """What changes when a serial port comes or goes, read without enumerating the ports:
    Linux the tty devices in sysfs (and where each one sits, which a USB adapter in another
    socket changes) and the /dev/serial links udev makes a moment later; Windows the COM
    ports the registry lists; elsewhere the tty and cu nodes in /dev. None when it can't be
    read (then the list is taken at every poll). Blocking (a few directory reads)."""
    try:
        if sys.platform.startswith("linux"):
            ttys = []
            for name in sorted(os.listdir(sys_class)):
                try:
                    ttys.append((name, os.readlink(os.path.join(sys_class, name))))
                except OSError:  # (not a link: a stand-in tree, an odd sysfs)
                    ttys.append((name, ""))
            links = []
            for d in link_dirs:
                try:
                    links.append(tuple(sorted(os.listdir(d))))
                except OSError:  # no such links (no udev: a container), or none yet
                    links.append(())
            return tuple(ttys), tuple(links)
        if sys.platform == "win32":
            return _serialcomm()
        return tuple(sorted(n for n in os.listdir("/dev") if n.startswith(("tty", "cu."))))
    except OSError:
        return None


def _serialcomm() -> tuple:
    """Windows: the COM ports HKLM\\HARDWARE\\DEVICEMAP\\SERIALCOMM lists (each driver
    adds its ports there), as (value name, COM name) pairs; () when the key is not there (no
    port yet)."""
    import winreg  # type: ignore[import-not-found,unused-ignore]

    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,  # type: ignore[attr-defined,unused-ignore]
                             r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except FileNotFoundError:
        return ()
    found = []
    with key:
        i = 0
        while True:
            try:
                name, value, _kind = winreg.EnumValue(key, i)  # type: ignore[attr-defined,unused-ignore]
            except OSError:  # no more values
                break
            found.append((str(name), str(value)))
            i += 1
    return tuple(sorted(found))


class PortWatcher:
    """Keeps the serial port list (see the module docstring for when it is taken).

    ``get()`` returns the latest list, ``current()`` one at most ``max_age`` seconds old.
    ``version`` increments whenever the set of ports changes, and ``on_change`` is called
    then (Supervisor.ports_changed: mappings waiting for a port try again). Cross-platform,
    privilege-free.
    """

    # how old a list the UI or the API may be shown (current)
    FRESH = 2.0

    def __init__(self, interval: float = 2.0,
                 on_change: Optional[Callable[[list[dict[str, Any]]], None]] = None,
                 full_interval: float = 60.0,
                 signature: Optional[Callable[[], Optional[tuple]]] = None):
        self.interval = interval
        self.full_interval = full_interval
        self._on_change = on_change
        self._signature = signature or port_signature
        self._ports: list[dict[str, Any]] = []
        self._version = 0
        self._taken = 0.0   # time.monotonic() when the list was taken
        self._sig: Optional[tuple] = None  # port_signature then
        self._task: Optional[asyncio.Task] = None
        self._scanning: Optional[asyncio.Future] = None  # a list being taken now
        self._stop = asyncio.Event()

    @property
    def version(self) -> int:
        return self._version

    def get(self) -> list[dict[str, Any]]:
        return list(self._ports)

    async def current(self, max_age: float = FRESH) -> list[dict[str, Any]]:
        """The list, taken again first when it is older than ``max_age`` seconds (one
        enumeration for any number of callers at once)."""
        if time.monotonic() - self._taken > max_age:
            await self._rescan()
        return self.get()

    async def start(self) -> None:
        # take the list now, so the first UI render has data
        await self._rescan()
        self._version = 1
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="port-watcher")

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

    async def _rescan(self) -> None:
        scan = self._scanning
        if scan is None or scan.done():
            scan = self._scanning = asyncio.ensure_future(self._scan())
        # (a caller that goes away, a closed request, does not cancel it for the others)
        await asyncio.shield(scan)

    async def _scan(self) -> None:
        sig, new = await asyncio.to_thread(self._take)
        self._sig, self._taken = sig, time.monotonic()
        self._apply(new)

    def _take(self) -> tuple[Optional[tuple], list[dict[str, Any]]]:
        # the signature before the list: a port that comes while it is taken is seen next time
        try:
            sig = self._signature()
        except Exception:
            sig = None
        return sig, snapshot()

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
                try:
                    sig = await asyncio.to_thread(self._signature)
                except Exception:
                    sig = None
                if (sig is None or sig != self._sig
                        or time.monotonic() - self._taken >= self.full_interval):
                    await self._rescan()
                else:
                    _remember(self._ports)  # still the ports there are (portlist.recent)
            except asyncio.CancelledError:
                raise
            except Exception:
                # transient enumeration errors must not kill the watcher
                continue
