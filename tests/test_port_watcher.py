"""The serial port list is enumerated when it may have changed, not at every poll.

Checks (stand-in snapshot and signature; nothing is enumerated for real):
  * port_signature reads what changes when a port comes or goes: on Linux the sysfs tty
    entries (and where they point) and the /dev/serial link directories (a stand-in tree);
    on Windows the registry's SERIALCOMM values (a stand-in winreg, so it runs anywhere);
  * PortWatcher polls the signature and takes the list again only when it changed, when it
    can't be read, and at least every full_interval; an unchanged poll keeps the list fresh
    for the engine (portlist.recent);
  * current(max_age) takes the list when it is older than that, once for any number of
    callers at once, and not when it is fresh; on_change is called when the set of ports
    changes (what wakes the mappings waiting for a port).

Run: python3 tests/test_port_watcher.py
"""
import asyncio
import os
import sys
import tempfile
import types
from unittest import mock

from _server import ROOT  # noqa: F401  (sys.path setup)

from ser2netweb.engine import portlist


def test_signature_linux_tree():
    if not sys.platform.startswith("linux"):
        print("port_signature (sysfs): not Linux, skipped")
        return
    d = tempfile.mkdtemp(prefix="s2n_sig_")
    sys_tty, by_id, by_path = (os.path.join(d, n) for n in ("tty", "by-id", "by-path"))
    os.makedirs(sys_tty)
    os.symlink("../../devices/platform/serial8250/tty/ttyS0", os.path.join(sys_tty, "ttyS0"))

    def sig():
        return portlist.port_signature(sys_tty, (by_id, by_path))

    first = sig()
    assert first == ((("ttyS0", "../../devices/platform/serial8250/tty/ttyS0"),), ((), ())), first
    assert sig() == first, "nothing changed"
    os.symlink("../../devices/pci0000:00/usb1/1-1/1-1:1.0/ttyUSB0/tty/ttyUSB0",
               os.path.join(sys_tty, "ttyUSB0"))
    plugged = sig()
    assert plugged != first
    os.makedirs(by_id)
    os.symlink("/dev/ttyUSB0", os.path.join(by_id, "usb-1a86_USB_Serial-if00-port0"))
    linked = sig()
    assert linked != plugged, "udev's link, made a moment after the tty, counts"
    os.remove(os.path.join(sys_tty, "ttyUSB0"))  # the same name in another USB socket
    os.symlink("../../devices/pci0000:00/usb1/1-2/1-2:1.0/ttyUSB0/tty/ttyUSB0",
               os.path.join(sys_tty, "ttyUSB0"))
    assert sig() != linked, "a tty that sits elsewhere now counts"
    assert portlist.port_signature(os.path.join(d, "missing")) is None
    print("port_signature: sysfs ttys (and where they sit) and the /dev/serial links  OK")


def test_signature_windows_registry():
    class Key:
        def __init__(self, values):
            self.values = values

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    values = [("\\Device\\VCP0", "COM5"), ("\\Device\\Serial0", "COM1")]
    fake = types.SimpleNamespace(HKEY_LOCAL_MACHINE=object())
    fake.OpenKey = lambda root, path: Key(list(values))

    def enum(key, i):
        if i >= len(key.values):
            raise OSError(259, "No more data is available")
        return key.values[i][0], key.values[i][1], 1

    fake.EnumValue = enum
    with mock.patch.dict(sys.modules, {"winreg": fake}):
        assert portlist._serialcomm() == (("\\Device\\Serial0", "COM1"), ("\\Device\\VCP0", "COM5"))

        def missing(root, path):
            raise FileNotFoundError(2, "The system cannot find the file specified")

        fake.OpenKey = missing
        assert portlist._serialcomm() == (), "no key: no ports yet"
    print("port_signature (Windows): the registry's COM ports, () without the key  OK")


async def _watcher():
    lists = {"ports": [{"device": "/dev/ttyS0"}]}
    sig = {"value": ("a",)}
    scans = []
    changes = []

    def snapshot():
        scans.append(1)
        return [dict(p) for p in lists["ports"]]

    def signature():
        if isinstance(sig["value"], Exception):
            raise sig["value"]
        return sig["value"]

    async def polls(n, w):
        for _ in range(n):
            await asyncio.sleep(w.interval * 1.5)

    async def until(cond, timeout=10.0):  # (a busy machine may run the polls late)
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not cond() and loop.time() < end:
            await asyncio.sleep(0.01)
        return cond()

    with mock.patch.object(portlist, "snapshot", snapshot):
        w = portlist.PortWatcher(interval=0.02, on_change=changes.append, full_interval=3600,
                                 signature=signature)
        await w.start()
        try:
            assert len(scans) == 1 and w.get() == [{"device": "/dev/ttyS0"}] and w.version == 1
            await polls(5, w)
            assert len(scans) == 1, f"nothing changed, yet {len(scans)} scans"
            assert portlist.recent(max_age=0.5) == [{"device": "/dev/ttyS0"}], \
                "an unchanged poll keeps the list fresh for the engine"
            # a port comes: one scan, on_change with the new list
            lists["ports"] = [{"device": "/dev/ttyS0"}, {"device": "/dev/ttyUSB0"}]
            sig["value"] = ("a", "b")
            assert await until(lambda: w.version == 2), (len(scans), w.version)
            await polls(3, w)
            assert len(scans) == 2, len(scans)
            assert changes and [p["device"] for p in changes[-1]] == ["/dev/ttyS0", "/dev/ttyUSB0"]
            # the UI asks: a fresh list is served as it is, an old one is taken again, once
            # for all who ask at the same time
            await w.current()
            assert len(scans) == 2
            got = await asyncio.gather(*(w.current(max_age=0.0) for _ in range(5)))
            assert len(scans) == 3 and all(len(g) == 2 for g in got), len(scans)
            # a signature that can't be read: the list is taken at every poll
            sig["value"] = None
            before = len(scans)
            assert await until(lambda: len(scans) >= before + 3), (before, len(scans))
            sig["value"] = OSError("sysfs gone")
            before = len(scans)
            assert await until(lambda: len(scans) >= before + 3), (before, len(scans))
        finally:
            await w.stop()
        # at least every full_interval, even when nothing seems to change
        scans.clear()
        sig["value"] = ("same",)
        w = portlist.PortWatcher(interval=0.02, full_interval=0.05, signature=signature)
        await w.start()
        try:
            assert await until(lambda: len(scans) >= 3), len(scans)
        finally:
            await w.stop()
        assert portlist.recent() is None, "a stopped watcher's list is not used"


def test_watcher_scans_on_change_and_demand():
    asyncio.run(_watcher())
    print("PortWatcher: enumerates when the signature changes, can't be read, at the full "
          "interval and on demand (once for concurrent callers); unchanged polls keep "
          "portlist.recent fresh  OK")


def main():
    test_signature_linux_tree()
    test_signature_windows_registry()
    test_watcher_scans_on_change_and_demand()
    print("\nPASS: the port list is enumerated when it may have changed")


if __name__ == "__main__":
    main()
