"""ser2netweb's own serial transport (ser2netweb/engine/serial_compat.py) and the upstream code it builds on.

serial_compat.Transport is pyserial-asyncio-fast's SerialTransport with ser2netweb's fixes, and
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
  - a baud rate without a termios constant (250000, 74880, ...) opens and can be switched
    to while termios refuses pyserial 3.5's BOTHER placeholder, as glibc 2.42+ does for
    the standalone binary, with the input speed the same where tcsetattr leaves the input
    speed's bits at 38400 (musl, Alpine); a setting the OS refuses is a readable
    SerialException (Linux PTY, rates read back with TCGETS2);
  - Windows writes (serial_compat.OverlappedWrites, driven through a stand-in for
    pyserial's serial.win32 on any OS): each write has its own OVERLAPPED and keeps its data
    until it completed, at most the write window in flight (then BlockingIOError); purged
    writes count as done, a failed one is raised by the next write, a close cancels and
    waits for them (and never frees one the driver did not let go of);
  - the transport's Windows branch (tests/_windows_transport.py, in a child process; off
    Windows its timer-driven code is loaded as on Windows): drain() waits for the device,
    a graceful close with data left completes, a failing port is closed once, an idle
    port is polled every POLL_IDLE_WAIT_S and a busy one every 0.5 ms (with the read
    polls per second and CPU of 1 / 8 / 32 idle ports measured and printed: on the
    Windows legs that is the selector loop's real rate);
  - Windows only (the windows-latest CI legs): WindowsSerial is the class ports are opened
    with (SER2NETWEB_WINDOWS_SERIAL=pyserial: pyserial's), and OverlappedWrites works against
    a real overlapped handle (a named pipe nobody reads, then reads in order; a cancel);
  - a missing upstream member makes it fall back to upstream's transport, and versions
    other than the checked ones are named in a start-up warning;
  - the upstream code each override replaces or relies on is pinned by a hash of its
    source: a pyserial / pyserial-asyncio-fast release that changes it fails here, so the
    override is looked at again before the bump is merged;
  - without pyserial-asyncio-fast (a distribution that doesn't package it), Transport
    builds on ser2netweb/engine/serial_transport.py: that copy is 0.16's transport, by a
    hash of its text and against an installed 0.16, and everything above runs once more
    on it in a child process where pyserial-asyncio-fast doesn't import;
  - in a distribution's build (its recipe wrote ser2netweb/distro.json into this tree, its
    check step runs this file against the distribution's packages): upstream code that
    differs from the pinned releases' (another release, the distribution's patch, as
    Fedora's pyserial 3.5 has one for custom baud rates) is named, not failed, as
    serial_compat runs on it with a start-up note; the Windows-only modules a package
    leaves out off Windows (openSUSE's pyserial has no serialwin32) are skipped; a pyserial
    that no longer hands BOTHER to termios skips the stand-in's own check. The built-in
    copy's hash and every behaviour above stay strict there too.
Runs on Windows too (the transport's timer-polled write path), the PTY parts on POSIX only.

Run: python3 tests/test_serial_compat.py
"""
import ast
import asyncio
import contextlib
import hashlib
import importlib.util
import io
import os
import socket
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ser2netweb import envvars, layout

# test_without_pyserial_asyncio_fast's child: as if pyserial-asyncio-fast weren't installed
WITHOUT_SAF_ENV = "SER2NETWEB_TEST_WITHOUT_SAF"
if envvars.get(WITHOUT_SAF_ENV) == "1":
    sys.modules["serial_asyncio_fast"] = None  # (importing it fails)

import serial

from _fake_win32 import FakeWin32
from ser2netweb.engine import serial_compat, serial_transport

# pyserial-asyncio-fast, or where it isn't installed (as on CI's job "minimum
# dependencies", or in the child of test_without_pyserial_asyncio_fast) the copy of its
# transport built into ser2netweb: everything below holds for both
saf = serial_compat._saf

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
# a distribution's build: its recipe wrote ser2netweb/distro.json into this tree (layout.DISTRO)
# and the dependencies are the distribution's packages
DISTRO_BUILD = layout.distro_info() is not None
# pyserial's modules for Windows alone, which a distribution's package may leave out
WINDOWS_ONLY = ("serial.serialwin32", "serial.win32")


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
    from ser2netweb.engine import serial_io  # noqa: F401 - importing it must not patch anything
    assert saf.SerialTransport._write_ready is not serial_compat.Transport._write_ready
    assert not hasattr(saf.SerialTransport, "_ser2net_write_ready_patched")
    assert serial_compat.transport_class() is serial_compat.Transport
    print("pyserial-asyncio-fast's SerialTransport is not patched; ports get the subclass  OK")


def test_fallback_and_version_note():
    built_in = ("serial ports: pyserial-asyncio-fast is not installed — its transport (0.16), "
                "built into ser2netweb, is used")
    assert serial_compat.unverified_note() == (built_in if serial_compat.BUILT_IN else ""), \
        serial_compat.unverified_note()
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
    assert "built into ser2netweb" not in note or serial_compat.BUILT_IN, note
    # another pyserial than the checked one: the note about the built-in copy stays
    with mock.patch.object(serial, "__version__", "3.6"), \
            mock.patch.object(serial_compat, "BUILT_IN", True):
        note = serial_compat.unverified_note()
    assert note.startswith("WARNING: serial ports: pyserial 3.6 (checked: 3.5) — "), note
    assert note.endswith(f"; pyserial-asyncio-fast is not installed — its transport "
                         f"({saf.__version__}), built into ser2netweb, is used"), note
    print("an unchecked version is named at start (with the note about the built-in copy); "
          "without the upstream members the fixes build on, upstream's transport is used  OK")


def test_broken_pyserial_asyncio_fast():
    """The copy stands in only for a pyserial-asyncio-fast that isn't installed: one that
    is there but fails to import (here: a module of its own is missing, and an error of its
    own) stops the import with that error, as --about reports it."""
    import subprocess
    import tempfile
    code = ("import sys\nsys.path.insert(0, sys.argv[1])\n"
            "try:\n    from ser2netweb.engine import serial_compat\n"
            "except ImportError as e:\n    print('ERROR', e)\n"
            "else:\n    print('BUILT_IN', serial_compat.BUILT_IN)\n")
    for body, error in (("import s2n_test_missing_dependency\n",
                         "ModuleNotFoundError: No module named 's2n_test_missing_dependency'"),
                        ("raise ImportError('cannot import name SerialTransport')\n",
                         "ImportError: cannot import name SerialTransport")):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "serial_asyncio_fast"))
            with open(os.path.join(tmp, "serial_asyncio_fast", "__init__.py"), "w",
                      encoding="utf-8") as fh:
                fh.write(body)
            env = {k: v for k, v in os.environ.items() if k != WITHOUT_SAF_ENV}
            env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
            out = subprocess.run([sys.executable, "-c", code, tmp], capture_output=True,
                                 text=True, timeout=120, env=env, cwd=ROOT)
        assert out.returncode == 0, out.stderr[-2000:]
        assert out.stdout.startswith("ERROR pyserial-asyncio-fast is installed but failed to "
                                     f"import: {error} ("), out.stdout
    print("a pyserial-asyncio-fast that is installed but fails to import: the error, not the "
          "built-in copy  OK")


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
    from ser2netweb.config import SerialSettings
    from ser2netweb.engine import serial_io

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


# ------------------------------------------------------------------ custom baud rates
def _bother_refused():
    """termios.tcsetattr as it answers on glibc 2.42+ in a program built for an older glibc
    (the standalone binary): the BOTHER placeholder pyserial 3.5 gives as the speed for a
    rate without a termios constant is refused with EINVAL."""
    import termios

    from serial import serialposix
    real = termios.tcsetattr

    def tcsetattr(fd, when, attrs):
        if serialposix.BOTHER in (attrs[4], attrs[5]):
            raise termios.error(22, "Invalid argument")
        return real(fd, when, attrs)

    return mock.patch.object(termios, "tcsetattr", tcsetattr)


def _musl_input_bits():
    """termios.tcsetattr as musl's leaves the port (Alpine): the input speed's bits
    (CIBAUD) at the 38400 placeholder, where glibc's let the input follow the output."""
    import array
    import fcntl
    import termios

    from serial import serialposix
    real = termios.tcsetattr

    def tcsetattr(fd, when, attrs):
        real(fd, when, attrs)
        buf = array.array("i", [0] * 64)
        fcntl.ioctl(fd, serialposix.TCGETS2, buf)
        buf[2] = (buf[2] & ~termios.CIBAUD) | (0o17 << 16)  # (the kernel's B38400)
        fcntl.ioctl(fd, serialposix.TCSETS2, buf)

    return mock.patch.object(termios, "tcsetattr", tcsetattr)


def _speeds(fd):
    """(input, output) baud rate the kernel has for ``fd`` (TCGETS2)."""
    import array
    import fcntl

    from serial import serialposix
    buf = array.array("i", [0] * 64)
    fcntl.ioctl(fd, serialposix.TCGETS2, buf)
    return buf[9], buf[10]


async def _open_at(path, rate):
    from ser2netweb.config import SerialSettings
    from ser2netweb.engine import serial_io

    _reader, writer, ser, _dev = await serial_io.open_serial(SerialSettings(port=path,
                                                                            baudrate=rate))
    try:
        return _speeds(ser.fd)
    finally:
        writer.transport.abort()
        await asyncio.sleep(0.05)


def test_custom_baud_rates_on_new_glibc():
    if not sys.platform.startswith("linux"):
        print("SKIP (part): the custom baud rate checks are Linux only (TCSETS2)")
        return
    import termios

    from serial import serialposix
    pty = Pty()
    try:
        with _bother_refused():
            try:
                serialposix.Serial(pty.path, 250000).close()
            except termios.error:
                pass
            else:
                stand_in_unused()
            ser = serial_compat.PosixSerial(pty.path, 250000, timeout=0, write_timeout=0)
            try:
                assert _speeds(ser.fd) == (250000, 250000), _speeds(ser.fd)
                for rate in (74880, 31250, 7200, 9600, 115200, 100000):  # e.g. RFC 2217
                    ser.baudrate = rate
                    assert ser.baudrate == rate and _speeds(ser.fd) == (rate, rate), \
                        (rate, _speeds(ser.fd))
            finally:
                ser.close()
            assert asyncio.run(_open_at(pty.path, 250000)) == (250000, 250000)
        with _musl_input_bits():  # the input speed too, where tcsetattr left it at 38400
            ser = serial_compat.PosixSerial(pty.path, 250000, timeout=0, write_timeout=0)
            try:
                assert _speeds(ser.fd) == (250000, 250000), _speeds(ser.fd)
                ser.baudrate = 74880
                assert _speeds(ser.fd) == (74880, 74880), _speeds(ser.fd)
            finally:
                ser.close()
        with mock.patch.object(termios, "tcsetattr",
                               side_effect=termios.error(22, "Invalid argument")):
            try:
                serial_compat.PosixSerial(pty.path, 19200, timeout=0, write_timeout=0)
            except serial.SerialException as e:
                assert f"could not configure {pty.path} for 19200 baud, 8N1: Invalid " \
                       "argument" == str(e), str(e)
            else:
                raise AssertionError("a refused setting must raise SerialException")
    finally:
        pty.close()
    print("PosixSerial: 250000 / 74880 / 31250 / 7200 / 100000 baud open and switch where "
          "glibc 2.42+ refuses pyserial's BOTHER placeholder, the input speed too where "
          "tcsetattr left it at 38400 (musl); a refused setting is a readable "
          "SerialException  OK")


def stand_in_unused(distro_build=None):
    """pyserial opened a custom rate under the stand-in: it no longer hands BOTHER to
    termios.tcsetattr. pyserial 3.5's release does (the pins): an error, as the stand-in
    must refuse what glibc 2.42+ refuses. In a distribution's build its package may be
    patched not to (Fedora's): noted, and PosixSerial is checked on its own."""
    if DISTRO_BUILD if distro_build is None else distro_build:
        print("NOTE: this pyserial doesn't hand BOTHER to termios (the distribution's patch): "
              "the stand-in's own check is skipped, PosixSerial is checked alone")
        return
    raise AssertionError("the stand-in must refuse BOTHER as glibc 2.42+ does")


# ------------------------------------------------------------------ Windows writes
def test_overlapped_writes():
    win32 = FakeWin32()
    w = serial_compat.OverlappedWrites(win32, 7, window=1000)
    first, second = bytes(range(200)) * 3, b"b" * 600
    assert w.write(first) == 600 and w.write(second) == 400, "the window is 1000 bytes"
    assert w.pending() == 1000
    try:
        w.write(b"c")
    except BlockingIOError:
        pass
    else:
        raise AssertionError("past the window a write must raise BlockingIOError")
    sent = [x[1] for x in win32.writes]
    assert sent[0] is first, "the data WriteFile got must be the object kept alive"
    assert len({id(x[0]) for x in win32.writes}) == 2, "each write needs its own OVERLAPPED"
    assert len({x[0].hEvent for x in win32.writes}) == 2, "... and its own event"
    assert all(d is x for d, (_ov, x) in zip(sent, w._pending)), "kept until completed"
    assert win32.complete(600) == 600 and w.pending() == 400
    assert win32.writes[0][0].hEvent in win32.closed, "a completed write's event is closed"
    assert w.write(b"d" * 700) == 600
    win32.purge()  # an RFC 2217 purge, reset_output_buffer(): the writes are aborted
    assert w.pending() == 0, "purged writes count as done"
    assert w.write(b"e" * 10) == 10
    win32.fail_next()
    try:
        w.write(b"f")
    except serial.SerialException as e:
        assert "write failed" in str(e), e
    else:
        raise AssertionError("a write that failed must be raised by the next one")
    assert w.write(b"g") == 1, "only once"
    for code, raised in ((win32.ERROR_INVALID_USER_BUFFER, BlockingIOError),
                         (win32.ERROR_OPERATION_ABORTED, BlockingIOError),
                         (5, serial.SerialException)):
        win32.refuse = code
        try:
            w.write(b"h")
        except raised:
            pass
        else:
            raise AssertionError(f"WriteFile error {code} must raise {raised.__name__}")
        assert max(win32.closed) == max(win32.signaled), "the unused event must be closed"
    win32.refuse = None
    print("OverlappedWrites: own OVERLAPPED / event / data per write, at most the window in "
          "flight, purged writes done, a failed one raised once, refusals mapped  OK")


def test_overlapped_writes_cancel():
    win32 = FakeWin32()
    w = serial_compat.OverlappedWrites(win32, 7, window=1000)
    w.write(b"a" * 300)
    w.write(b"b" * 300)
    w.cancel(wait_s=0.5)
    assert w.pending() == 0 and not win32.pending()
    assert all(x[0].hEvent in win32.closed for x in win32.writes)
    abandoned = len(serial_compat._ABANDONED)
    win32.ignore_cancel = True  # a driver that does not let go of a write
    w.write(b"c" * 300)
    t0 = time.monotonic()
    w.cancel(wait_s=0.2)
    assert time.monotonic() - t0 < 1.0
    assert len(serial_compat._ABANDONED) == abandoned + 1, "an unfinished write must be kept"
    assert win32.writes[-1][0].hEvent not in win32.closed
    serial_compat._ABANDONED.pop()
    print("OverlappedWrites: a close cancels the writes in flight and waits for them; one "
          "the driver keeps is never freed  OK")


def test_windows_transport_branch():
    import subprocess
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_windows_transport.py")
    out = subprocess.run([sys.executable, helper], capture_output=True, text=True, timeout=120)
    sys.stdout.write("".join(f"  {line}\n" for line in out.stdout.splitlines()))
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-3000:]
    print("the transport's Windows branch passes (tests/_windows_transport.py)  OK")


def test_windows_serial_class():
    if os.name != "nt":
        print("SKIP (part): WindowsSerial / a real overlapped handle are checked on Windows "
              "(CI windows-latest)")
        return
    assert serial_compat.serial_class() is serial_compat.WindowsSerial
    with mock.patch.dict(os.environ, {serial_compat.WINDOWS_SERIAL_ENV: "pyserial"}):
        assert serial_compat.serial_class() is serial.Serial
    port = serial_compat.WindowsSerial()
    try:
        port.write(b"x")
    except serial.PortNotOpenError:
        pass
    else:
        raise AssertionError("a closed port must refuse a write")
    try:
        serial_compat.WindowsSerial("COM250")
    except serial.SerialException:
        pass
    else:
        raise AssertionError("COM250 should not exist on a CI runner")
    print("WindowsSerial is the class ports are opened with; SER2NETWEB_WINDOWS_SERIAL=pyserial "
          "keeps pyserial's  OK")
    _overlapped_writes_on_a_pipe()


def _overlapped_writes_on_a_pipe():
    """OverlappedWrites with the real serial.win32, on the write end of a named pipe opened
    for overlapped I/O (as a COM port is): writes pend while nobody reads, the window fills,
    the reader gets every byte in order, and a cancel ends the writes still in flight."""
    import ctypes
    from ctypes import wintypes

    from serial import win32
    k32 = ctypes.WinDLL("kernel32")
    create_pipe = k32.CreateNamedPipeW
    create_pipe.restype = wintypes.HANDLE
    create_pipe.argtypes = [wintypes.LPCWSTR] + [wintypes.DWORD] * 6 + [ctypes.c_void_p]
    name = rf"\\.\pipe\ser2net-test-{os.getpid()}"
    outbound, overlapped_io = 0x00000002, 0x40000000
    server = create_pipe(name, outbound | overlapped_io, 0, 1, 4096, 4096, 0, None)
    assert server and server != win32.INVALID_HANDLE_VALUE, ctypes.WinError()
    client = win32.CreateFile(name, win32.GENERIC_READ, 0, None, win32.OPEN_EXISTING, 0, 0)
    assert client != win32.INVALID_HANDLE_VALUE, ctypes.WinError()
    try:
        w = serial_compat.OverlappedWrites(win32, server, window=32 * 1024)
        sent = bytearray()
        for i in range(64):
            chunk = bytes([i]) * 2048
            try:
                n = w.write(chunk)
            except BlockingIOError:
                break
            sent += chunk[:n]
        else:
            raise AssertionError("nobody reads the pipe: the window must fill")
        assert w.pending() > 0, "writes to a pipe nobody reads must still be in flight"
        got = bytearray()
        buf = ctypes.create_string_buffer(65536)
        n = wintypes.DWORD()
        while len(got) < len(sent):
            assert win32.ReadFile(client, buf, len(buf), ctypes.byref(n), None), ctypes.WinError()
            got += buf.raw[:n.value]
        assert got == sent, "the reader must get every byte, in order"
        for _ in range(100):
            if not w.pending():
                break
            time.sleep(0.01)
        assert w.pending() == 0, "every write has completed once it was read"
        for _ in range(64):
            try:
                w.write(b"z" * 2048)
            except BlockingIOError:
                break
        abandoned = len(serial_compat._ABANDONED)
        w.cancel()
        assert w.pending() == 0 and len(serial_compat._ABANDONED) == abandoned, \
            "a cancelled write on a pipe must end within the wait"
    finally:
        win32.CloseHandle(client)
        win32.CloseHandle(server)
    print("OverlappedWrites on a real overlapped handle: pending writes, the window, every "
          "byte in order, a cancel  OK")


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
    # open_stream sets RTS / DTR before open(), serial_io takes a refused RS-485 mode back:
    # these setters keep the value, then apply it to a port that is open
    "serial.serialutil": {
        "SerialBase.rts": "9f3a188b4b33271c",
        "SerialBase.dtr": "c48d7fc73ab93a12",
        "SerialBase.rs485_mode": "8a1451338376f8b9",
    },
    "serial.serialposix": {             # PosixSerial
        "Serial.open": "4981135238de1222",
        "Serial.close": "af4f4e008d2ab69e",
        "Serial.read": "170b5f1411e03b35",
        "Serial.write": "d3b1411cb6cb8ed4",
        "Serial.cancel_read": "dcd213634ed388d5",
        "Serial.cancel_write": "a0686d9cef203a4e",
        "Serial._reconfigure_port": "750c32d9514f897d",
        "PlatformSpecific._set_special_baudrate[plat[:5] == 'linux']": "9d227870e19188c0",
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
        'SerialTransport._poll_write[os.name == "nt"]': "dc4e21ed43e1ca26",
        'SerialTransport._ensure_writer[os.name == "nt"]': "a60d57465e66d3ab",
        'SerialTransport._remove_writer[os.name == "nt"]': "aaaedc0e83fb6660",
        'SerialTransport._poll_read[os.name == "nt"]': "b21bc71108e803e7",
        'SerialTransport._ensure_reader[os.name == "nt"]': "ebf610378a97b332",
        'SerialTransport._remove_reader[os.name == "nt"]': "883bdd2e4eaacbf9",
    },
    "serial.serialwin32": {             # WindowsSerial
        "Serial.__init__": "5cd96869f9600813",
        "Serial.open": "68bea852df972437",
        "Serial.write": "2d85a095d12f6e57",
        "Serial._close": "eeca74a1fce8029d",
        "Serial.cancel_write": "1d765c0bbdf0a5dd",
        "Serial.flush": "aae40f7c68346341",
        "Serial.out_waiting": "c25a96fb088c0035",
        "Serial.reset_output_buffer": "8268fcf03953e692",
    },
}


def pin_check(pins, distro_build, platform=sys.platform):
    """(problems, notes, functions checked) of `pins` against the installed code. A
    difference is a problem, except in a distribution's build, where code of the
    distribution's package that differs from the pinned release's is a note, and so is a
    Windows-only module it left out off Windows; ser2netweb's own copy of the transport
    is held to its hashes everywhere."""
    problems, notes, checked = [], [], 0
    for module, wanted in pins.items():
        # (without pyserial-asyncio-fast: its copy in ser2netweb, the same code, which leaves
        # out connection_for_serial: what open_stream mirrors, not code that runs)
        name_here = saf.__name__ if module == "serial_asyncio_fast" else module
        ours = name_here.startswith("ser2netweb.")
        lenient = distro_build and not ours
        if importlib.util.find_spec(name_here) is None:
            if lenient and module in WINDOWS_ONLY and platform != "win32":
                notes.append(f"{module} is not installed (the distribution's package leaves "
                             "it out off Windows): its pins are skipped")
            else:
                problems.append(f"{module}: not installed")
            continue
        found = upstream_functions(name_here)
        for name, want in wanted.items():
            if name_here != module and name not in found:
                continue
            got = digest(found[name]) if name in found else "(gone)"
            checked += 1
            if got != want:
                (notes if lenient else problems).append(f"{module} {name}: {got} (pinned {want})")
    return problems, notes, checked


def test_upstream_pins():
    problems, notes, checked = pin_check(PINS, DISTRO_BUILD)
    assert not problems, ("upstream code serial_compat builds on has changed; review the "
                          "matching override, then update PINS:\n  " + "\n  ".join(problems))
    for note in notes:
        print(f"NOTE (a distribution's build): {note}")
    print(f"{checked} upstream functions serial_compat builds on are checked"
          + (f", {len(notes)} not as pinned (the distribution's packages: the notes above)"
             if notes else ", the pinned ones")
          + (" (pyserial-asyncio-fast's: in the built-in copy)" if serial_compat.BUILT_IN else "")
          + "  OK")


def test_portable_in_a_distribution_build():
    """What a distribution's build lets through (pin_check, stand_in_unused), and only
    there: a function that differs, a Windows-only module left out off Windows. Code that
    is gone or a module that is missing on its own platform, and the built-in copy's
    hashes, fail in either."""
    pins = {"serial.serialutil": {"SerialBase.rts": "0" * 16},
            "serial.nonexistent_for_the_test": {"Serial.open": "0" * 16}}
    problems, notes, _n = pin_check(pins, False)
    assert len(problems) == 2 and not notes, (problems, notes)
    windows_only = WINDOWS_ONLY + ("serial.nonexistent_for_the_test",)
    saved = globals()["WINDOWS_ONLY"]
    globals()["WINDOWS_ONLY"] = windows_only
    try:
        problems, notes, _n = pin_check(pins, True, platform="linux")
        assert not problems and len(notes) == 2, (problems, notes)
        problems, notes, _n = pin_check(pins, True, platform="win32")
        assert problems == ["serial.nonexistent_for_the_test: not installed"], problems
    finally:
        globals()["WINDOWS_ONLY"] = saved
    copy = {"serial_asyncio_fast": {"SerialTransport.write": "0" * 16}}
    problems, _notes, _n = pin_check(copy, True)
    assert bool(problems) == serial_compat.BUILT_IN, problems   # (ours: strict)
    with contextlib.redirect_stdout(io.StringIO()) as out:
        stand_in_unused(True)
    assert out.getvalue().startswith("NOTE: this pyserial doesn't hand BOTHER"), out.getvalue()
    try:
        stand_in_unused(False)
    except AssertionError:
        pass
    else:
        raise AssertionError("outside a distribution's build the stand-in must refuse BOTHER")
    print("a distribution's build: differing upstream code and the Windows-only modules a "
          "package leaves out are named, not failed; the rest as everywhere "
          f"({'this is one' if DISTRO_BUILD else 'this is none'})  OK")


# ser2netweb/engine/serial_transport.py, the copy of pyserial-asyncio-fast 0.16's transport for
# an installation without it: everything from upstream's module logger to the end of its
# SerialTransport class, by a hash of that text (a change to the copy fails here)
COPY_FROM = "_LOGGER = logging.getLogger(__name__)"
COPY_DIGEST = "c4c386f3996d7d35"


def test_built_in_copy():
    with open(serial_transport.__file__, encoding="utf-8") as fh:
        text = fh.read()
    copied = text[text.index(COPY_FROM):].rstrip()
    assert digest(copied) == COPY_DIGEST, (f"ser2netweb/engine/serial_transport.py's copied part "
                                           f"changed: {digest(copied)} (pinned {COPY_DIGEST})")
    assert serial_transport.__version__ == "0.16" == dict(
        (n, v) for n, _m, v in serial_compat.VERIFIED)["pyserial-asyncio-fast"]
    spec = None if serial_compat.BUILT_IN else importlib.util.find_spec("serial_asyncio_fast")
    if spec is None or saf.__version__ != serial_transport.__version__:
        print(f"SKIP (part): the copy against an installed pyserial-asyncio-fast "
              f"{serial_transport.__version__} (not installed here)")
    else:
        with open(spec.origin, encoding="utf-8") as fh:
            theirs = fh.read()
        end = theirs.index("\nasync def create_serial_connection(")
        assert copied == theirs[theirs.index(COPY_FROM):end].rstrip(), \
            "serial_transport.py's copy differs from the installed pyserial-asyncio-fast"
        print(f"the built-in copy is pyserial-asyncio-fast {saf.__version__}'s transport, "
              "character for character  OK")
    print(f"the built-in copy of the transport is the pinned text ({COPY_DIGEST})  OK")


def test_without_pyserial_asyncio_fast():
    """This file once more, in a child process where pyserial-asyncio-fast doesn't import
    (as on a distribution that doesn't package it): Transport builds on the built-in copy,
    and every check above holds there too, the Windows branch's included."""
    if serial_compat.BUILT_IN:
        assert serial_compat.Transport.__mro__[1] is serial_transport.SerialTransport
        assert "built into ser2netweb, is used" in serial_compat.unverified_note()
        print("without pyserial-asyncio-fast: Transport builds on the built-in copy (this "
              "run)  OK")
        return
    import subprocess
    out = subprocess.run([sys.executable, os.path.abspath(__file__)], capture_output=True,
                         text=True, timeout=300, env={**os.environ, WITHOUT_SAF_ENV: "1"})
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    assert "Transport builds on the built-in copy (this run)  OK" in out.stdout, out.stdout
    print("without pyserial-asyncio-fast (a child process): every check passes on the "
          "built-in copy  OK")


if __name__ == "__main__":
    test_partial_write_finishes_and_stops_watching()
    test_upstream_write_ready_still_broken()
    test_read_size()
    test_no_process_wide_patch()
    test_fallback_and_version_note()
    test_broken_pyserial_asyncio_fast()
    test_requirements_pin_the_checked_versions()
    test_eagain_keeps_the_data_posix()
    test_custom_baud_rates_on_new_glibc()
    test_overlapped_writes()
    test_overlapped_writes_cancel()
    test_windows_transport_branch()
    test_windows_serial_class()
    test_upstream_pins()
    test_portable_in_a_distribution_build()
    test_built_in_copy()
    test_without_pyserial_asyncio_fast()
    print("\nPASS: serial_compat's transport and the upstream code it builds on")
