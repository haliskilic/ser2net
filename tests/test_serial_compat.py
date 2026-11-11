"""ser2net's own serial transport (app/engine/serial_compat.py) and the upstream code it builds on.

serial_compat.Transport is pyserial-asyncio-fast's SerialTransport with ser2net's fixes, and
open_stream builds it for every port (nothing in pyserial-asyncio-fast is patched for the
whole process any more). Checked here, without a serial device:
  - a write the port takes only part of (pyserial 3.5 returns partial counts) is finished
    from the buffer, and the transport stops watching for writability once the buffer is
    empty; upstream's own transport keeps watching and hits its `assert data` on the empty
    buffer. That half fails once upstream fixes it: the override can then go;
  - a write the OS refuses (EAGAIN) is kept in the buffer with the writer armed, and goes
    out once the port has room (POSIX PTY);
  - reads take up to READ_SIZE bytes (the upstream attribute it sets still exists);
  - ports opened through serial_io.open_serial get this transport (POSIX PTY);
  - a missing upstream member makes it fall back to upstream's transport, and versions
    other than the checked ones are named in a start-up warning;
  - the upstream code each override replaces or relies on is pinned by a hash of its
    source: a pyserial / pyserial-asyncio-fast release that changes it fails here, so the
    override is looked at again before the bump is merged.
Runs on Windows too (the transport's timer-polled write path), the PTY parts on POSIX only.

Run: python3 tests/test_serial_compat.py
"""
import ast
import asyncio
import hashlib
import importlib.util
import os
import socket
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import serial
import serial_asyncio_fast as saf

from app.engine import serial_compat

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


class FakePort:
    """What the transport uses of a pyserial port. ``takes`` scripts the next write()s: an
    int is how many bytes it takes, an exception is raised; later writes take everything."""

    def __init__(self):
        self.timeout = self.write_timeout = None
        self.takes = []
        self.written = bytearray()
        self.in_waiting = 0
        self.out_waiting = 0
        self.is_open = True
        # a descriptor for the POSIX transport to watch: always writable, never readable
        self._a, self._b = socket.socketpair()

    def fileno(self):
        return self._a.fileno()

    def write(self, data):
        take = self.takes.pop(0) if self.takes else len(data)
        if isinstance(take, BaseException):
            raise take
        self.written += data[:take]
        return min(take, len(data))

    def read(self, size=1):
        return b""

    def flush(self):
        pass

    def reset_output_buffer(self):
        pass

    def close(self):
        self.is_open = False
        self._a.close()
        self._b.close()


async def _partial_write(cls, takes):
    """Write 100 bytes through a ``cls`` transport whose port first takes ``takes``; return
    (bytes the port got, bytes left in the buffer, writer still armed, loop errors)."""
    loop = asyncio.get_running_loop()
    errors = []
    loop.set_exception_handler(lambda _loop, ctx: errors.append(ctx))
    port = FakePort()
    port.takes = list(takes)
    t = cls(loop, asyncio.Protocol(), port)
    await asyncio.sleep(0.01)  # connection_made, the reader
    t.write(b"x" * 100)
    first_take = takes[0] if isinstance(takes[0], int) else 0
    assert t.get_write_buffer_size() == 100 - first_take, t.get_write_buffer_size()
    assert t._has_writer, "the rest must wait for the port to be writable"
    for _ in range(40):
        await asyncio.sleep(0.005)
    result = (bytes(port.written), t.get_write_buffer_size(), bool(t._has_writer), list(errors))
    t.abort()
    await asyncio.sleep(0.05)
    return result


def test_partial_write_finishes_and_stops_watching():
    for takes in ([10], [BlockingIOError()], [10, 0, 30]):
        written, left, armed, errors = asyncio.run(_partial_write(serial_compat.Transport, takes))
        assert written == b"x" * 100 and left == 0, (takes, written, left)
        assert not armed, f"{takes}: the writer must be removed once the buffer is empty"
        assert not errors, f"{takes}: the event loop reported {errors}"
    print("Transport: a partly taken / refused write is finished from the buffer, then the "
          "writer is removed  OK")


def test_upstream_write_ready_still_broken():
    """The bug Transport._write_ready works around (pyserial-asyncio-fast PR #36). Once a
    release fixes it, this fails: delete the override and this test."""
    written, left, armed, errors = asyncio.run(_partial_write(saf.SerialTransport, [10]))
    assert written == b"x" * 100 and left == 0, (written, left)
    fixed = not armed and not errors
    assert not fixed, ("pyserial-asyncio-fast's own _write_ready removes the writer now: "
                       "serial_compat.Transport._write_ready is no longer needed")
    assert armed, "upstream's transport keeps watching for writability with nothing to write"
    print(f"upstream SerialTransport {saf.__version__}: still keeps the writer armed after "
          f"the buffer emptied ({len(errors)} loop error(s)), the override is needed  OK")


def test_read_size():
    port = FakePort()
    loop = asyncio.new_event_loop()
    try:
        fresh = saf.SerialTransport(loop, asyncio.Protocol(), port)
        assert hasattr(fresh, "_max_read_size"), \
            "upstream renamed _max_read_size: Transport's read size would do nothing"
        ours = serial_compat.Transport(loop, asyncio.Protocol(), FakePort())
        assert ours._max_read_size == serial_compat.READ_SIZE > fresh._max_read_size
        assert port.timeout == 0 and port.write_timeout == 0
    finally:
        loop.close()
    print(f"Transport reads up to {serial_compat.READ_SIZE} bytes (upstream "
          f"{fresh._max_read_size})  OK")


def test_no_process_wide_patch():
    """pyserial-asyncio-fast's own class is left as it is (spy://-style users of it, other
    libraries in the process): the fixes live in the subclass."""
    from app.engine import serial_io  # noqa: F401 - importing it must not patch anything
    assert saf.SerialTransport._write_ready is not serial_compat.Transport._write_ready
    assert not hasattr(saf.SerialTransport, "_ser2net_write_ready_patched")
    assert serial_compat.transport_class() is serial_compat.Transport
    print("pyserial-asyncio-fast's SerialTransport is not patched; ports get the subclass  OK")


def test_fallback_and_version_note():
    assert serial_compat.unverified_note() == "", serial_compat.unverified_note()
    with mock.patch.object(saf, "__version__", "0.17"):
        note = serial_compat.unverified_note()
    assert note.startswith("WARNING: serial ports: pyserial-asyncio-fast 0.17 (checked: "
                           "0.16)"), note
    assert "its own transport" not in note, note
    saved = saf.SerialTransport.__dict__["_write_data"]
    delattr(saf.SerialTransport, "_write_data")  # a release without what Transport builds on
    try:
        assert serial_compat.transport_class() is saf.SerialTransport
        with mock.patch.object(serial, "__version__", "3.6"):
            note = serial_compat.unverified_note()
    finally:
        saf.SerialTransport._write_data = saved
    assert serial_compat.transport_class() is serial_compat.Transport
    assert "pyserial 3.6 (checked: 3.5)" in note and "its own transport is used" in note, note
    print("an unchecked version is named at start; without the upstream members the "
          "fixes build on, upstream's transport is used  OK")


def test_requirements_pin_the_checked_versions():
    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as fh:
        pins = {line.split("==")[0].strip().lower(): line.split("==")[1].split(";")[0].strip()
                for line in fh if "==" in line and not line.lstrip().startswith("#")}
    for name, _mod, version in serial_compat.VERIFIED:
        assert pins.get(name) == version, (f"requirements.txt pins {name} {pins.get(name)}, "
                                           f"serial_compat.VERIFIED says {version}")
    print("requirements.txt pins exactly the versions serial_compat was checked with  OK")


# ------------------------------------------------------------------ POSIX PTY parts
class Pty:
    def __init__(self):
        self.master, slave = os.openpty()
        self.path = os.ttyname(slave)
        os.close(slave)

    def recv(self, n, timeout=3.0):
        import select
        p = select.poll()
        p.register(self.master, select.POLLIN)
        got = b""
        end = time.monotonic() + timeout
        while len(got) < n and p.poll(max(0.0, end - time.monotonic()) * 1000):
            got += os.read(self.master, n - len(got))
        return got

    def close(self):
        if self.master is not None:
            os.close(self.master)
            self.master = None


async def _eagain_through_open_serial():
    from app.config import SerialSettings
    from app.engine import serial_io

    pty = Pty()
    writer = None
    try:
        reader, writer, ser, _dev = await serial_io.open_serial(
            SerialSettings(port=pty.path, baudrate=115200))
        t = writer.transport
        assert type(t) is serial_compat.Transport, type(t)
        assert t._max_read_size == serial_compat.READ_SIZE
        real_write = os.write

        def refused(fd, data):
            if fd == ser.fd:
                raise BlockingIOError(11, "Resource temporarily unavailable")
            return real_write(fd, data)

        with mock.patch.object(serial_compat.os, "write", refused):
            writer.write(b"k")  # the port's buffer is full right now
            assert t.get_write_buffer_size() == 1, t.get_write_buffer_size()
            assert t._has_writer, "the writer must be armed for the kept byte"
        await writer.drain()
        got = await asyncio.to_thread(pty.recv, 1)
        assert got == b"k", got
        for _ in range(20):
            if not t._has_writer:
                break
            await asyncio.sleep(0.01)
        assert t.get_write_buffer_size() == 0 and not t._has_writer
    finally:
        if writer is not None:
            writer.transport.abort()
            await asyncio.sleep(0.05)
        pty.close()


def test_eagain_keeps_the_data_posix():
    if os.name != "posix":
        print("SKIP (part): PTY write checks are POSIX only")
        return
    asyncio.run(_eagain_through_open_serial())
    print("open_serial: the port gets Transport; a write the OS refuses (EAGAIN) keeps the "
          "byte with the writer armed, and it arrives once there is room  OK")


# ------------------------------------------------------------------ upstream pins
def upstream_functions(module_name):
    """{qualified name: source} of every function in ``module_name``'s file, read with ast
    (works for Windows-only modules and branches on any OS; nothing is imported). A
    function in an `if` branch gets the condition appended: Class.name[os.name == "nt"]."""
    origin = importlib.util.find_spec(module_name).origin
    with open(origin, encoding="utf-8") as fh:
        text = fh.read()
    out = {}

    def walk(body, prefix, label=""):
        for node in body:
            if isinstance(node, ast.ClassDef):
                walk(node.body, f"{prefix}{node.name}.", label)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[f"{prefix}{node.name}{label}"] = ast.get_source_segment(text, node)
            elif isinstance(node, ast.If):
                cond = ast.get_source_segment(text, node.test)
                walk(node.body, prefix, f"[{cond}]")
                walk(node.orelse, prefix, f"[not {cond}]")

    walk(ast.parse(text).body, "")
    return out


def digest(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


# What serial_compat overrides, replaces or relies on, at the pinned releases
# (pyserial 3.5, pyserial-asyncio-fast 0.16). A changed hash means upstream changed that
# code: look at serial_compat's matching override (and whether upstream fixed what it
# works around), then put the new hash here.
PINS = {
    "serial.serialposix": {             # PosixSerial
        "Serial.open": "4981135238de1222",
        "Serial.close": "af4f4e008d2ab69e",
        "Serial.read": "170b5f1411e03b35",
        "Serial.write": "d3b1411cb6cb8ed4",
        "Serial.cancel_read": "dcd213634ed388d5",
        "Serial.cancel_write": "a0686d9cef203a4e",
    },
    "serial_asyncio_fast": {            # Transport, open_stream
        "SerialTransport.__init__": "0813f41f9c2b76d2",
        "SerialTransport.write": "df4f58b471807471",
        "SerialTransport._write_ready": "d7791362f8ef94cb",
        "SerialTransport._write_data": "808fd1aaa019ac12",
        "SerialTransport._read_ready": "7cb053e996ee1dac",
        "SerialTransport._close": "5960a2bd8a1001ff",
        "SerialTransport._abort": "1e1a86cd965c2613",
        "SerialTransport._call_connection_lost": "7e1a4f40149e1a9b",
        "connection_for_serial": "f03b285bf68e39db",
    },
}


def test_upstream_pins():
    changed = []
    for module, pins in PINS.items():
        found = upstream_functions(module)
        for name, want in pins.items():
            got = digest(found[name]) if name in found else "(gone)"
            if got != want:
                changed.append(f"{module} {name}: {got} (pinned {want})")
    assert not changed, ("upstream code serial_compat builds on has changed; review the "
                         "matching override, then update PINS:\n  " + "\n  ".join(changed))
    print(f"{sum(map(len, PINS.values()))} upstream functions serial_compat builds on are "
          "the pinned ones  OK")


if __name__ == "__main__":
    test_partial_write_finishes_and_stops_watching()
    test_upstream_write_ready_still_broken()
    test_read_size()
    test_no_process_wide_patch()
    test_fallback_and_version_note()
    test_requirements_pin_the_checked_versions()
    test_eagain_keeps_the_data_posix()
    test_upstream_pins()
    print("\nPASS: serial_compat's transport and the upstream code it builds on")
