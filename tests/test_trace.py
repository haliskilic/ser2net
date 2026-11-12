"""Traffic traces: confined to data/traces, written off the event loop, rotated.

Before v2.8 a mapping's ``trace_both`` was a free file path, and any operator could
point it at a file the service may write (config.json, a startup script) to have raw
network bytes appended to it. Checks:
  * legacy paths (POSIX, Windows, "../") only keep their sanitised last component and
    the trace goes to <data>/traces/<mapping id>-<name>; nothing is written elsewhere;
  * a v2.7.1 config with such paths loads unchanged otherwise and says what moved;
  * write() only queues: a slow disk can't stall the caller, and what can't be
    written is reported in the trace instead of silently missing;
  * rotation keeps at most trace_keep old files of about trace_max_mb each, and never cuts
    a file in place (a download still reading it gets all of it, also with keep=0); the
    rotated files past trace_keep (it was lowered) are deleted when the trace starts;
  * a file that can't be opened or written pauses the trace instead of ending its writer
    thread: the status says why, nothing is queued meanwhile, the writer tries again,
    and the trace then says how many bytes are missing; a removed traces folder is made
    again; a thread that dies anyway is named in all.log;
  * a download of a trace being written sends it as it was when the download began:
    exactly its Content-Length (a FileResponse sent the lines added meanwhile too, and
    the server broke the download off);
  * a trace's files are deleted by its writer thread, in turn with what it writes: the
    file it writes is closed first (no deleted file is kept open) and begun again with
    the next traffic;
  * below the free-space floor (shutil.disk_usage faked, no disk is filled) a trace pauses
    at its start and while it writes, says why, and goes on by itself with 10% to spare;
  * with trace_max_age_days set (0, the default, keeps them), the hourly maintenance
    deletes the trace files not written for that long, a running trace's through its
    writer, and has a running trace whose file holds older traffic begin a new file; a
    hand-edited value out of range is read as 0 or the maximum, with a note;
  * deleting a mapping deletes its traces; the start-up prunes the traces of mappings
    that no longer exist (a replace import or a restore leaves them), and those removals
    close the gone mappings' runners and log files at once.
Cross-platform; no serial hardware (the runner's serial port doesn't exist).

Run: python3 tests/test_trace.py
"""
import asyncio
import contextlib
import errno
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collections import namedtuple
from unittest import mock

from app import storage
from app.config import AppConfig, ConfigError, MappingConfig, MappingOptions, trace_file_name
from app.engine.bridge import MappingRunner, RunnerStatus, Tracer, trace_path

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "config-2.7.1.json")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_names():
    cases = {
        "/var/log/ser2net/plc1.trace": "plc1.trace",
        "C:\\ser2net\\traces\\console.log": "console.log",
        "../../config.json": "config.json",
        "..": "trace.log",
        ".bashrc": "bashrc",
        "data/trace-plc1.log": "trace-plc1.log",
        "sıcaklık ölçüm.log": "sıcaklık_ölçüm.log",
        "a<b>c:d|e?f*.log": "a_b_c_d_e_f_.log",
        "": "",
        "   ": "",
    }
    for raw, want in cases.items():
        got = trace_file_name(raw)
        assert got == want, f"{raw!r} -> {got!r}, want {want!r}"
        if got:
            assert os.path.basename(got) == got and not got.startswith(".")
    assert MappingOptions.from_dict({"trace_both": "/etc/cron.d/x"}).trace_both == "x"
    print("trace names: legacy paths keep only a sanitised file name  OK")


def test_validation():
    m = MappingConfig.from_dict({"name": "t", "serial": {"port": "COM9"},
                                 "network": {"port": 4999, "bind_ip": "127.0.0.1"}})
    m.validate()  # defaults: 10 MB, 3 kept
    assert (m.options.trace_max_mb, m.options.trace_keep) == (10.0, 3)
    for field, bad in (("trace_max_mb", 0), ("trace_max_mb", 5000), ("trace_keep", -1),
                       ("trace_keep", 51), ("trace_keep", 1.5)):
        setattr(m.options, field, bad)
        try:
            m.validate()
        except ConfigError:
            pass
        else:
            raise AssertionError(f"{field}={bad!r} accepted")
        m.options = MappingOptions()
    print("trace size / rotation settings validated (defaults 10 MB x 3)  OK")


def test_legacy_config_notes():
    cfg = AppConfig.from_dict(json.load(open(FIXTURE, encoding="utf-8")))
    names = {m.name: m.options.trace_both for m in cfg.mappings}
    assert names["PLC-1 raw"] == "plc1.trace" and names["Modbus gateway"] == "modbus.log"
    # the two absolute paths, and the bare name: 2.7.1 wrote it in its working directory
    assert len(cfg.load_notes) == 3, cfg.load_notes
    assert all("trace file is now traces/" in n for n in cfg.load_notes), cfg.load_notes
    assert any("now traces/aaaa00000006-modbus.log" in n for n in cfg.load_notes)
    cfg.validate()
    # written by 2.8.0 or later (it has trace_max_mb): a bare name was in traces/ already
    again = AppConfig.from_dict(cfg.to_dict())
    assert not again.load_notes, again.load_notes
    print("v2.7.1 config: trace paths and names mapped into data/traces, a note per move  OK")


class _Opts:
    def __init__(self, **kw):
        self.trace_hexdump = kw.get("hexdump", True)
        self.trace_timestamp = True
        self.trace_max_mb = kw.get("max_mb", 10.0)
        self.trace_keep = kw.get("keep", 3)


async def test_tracer_format_and_rotation(tmp):
    path = os.path.join(tmp, "rot.log")
    t = Tracer(path, _Opts(max_mb=0.05, keep=2))       # ~52 KB per file
    chunk = bytes(range(256)) * 4                      # 1 KB -> ~3 KB per hex line
    for _ in range(200):
        t.write("ser>net", chunk)
    t.write("net>ser", b"hello")
    await t.aclose()
    files = sorted(f for f in os.listdir(tmp) if f.startswith("rot.log"))
    assert files == ["rot.log", "rot.log.1", "rot.log.2"], files
    for f in files[1:]:
        size = os.path.getsize(os.path.join(tmp, f))
        assert 50_000 <= size <= 60_000, (f, size)
    tail = open(path, encoding="utf-8").read().splitlines()[-1]
    assert tail.endswith("net>ser [5] 68 65 6c 6c 6f"), tail
    print("tracer: hex lines, rotation keeps 2 old files of ~50 KB  OK")

    path0 = os.path.join(tmp, "keep0.log")
    t = Tracer(path0, _Opts(max_mb=0.01, keep=0, hexdump=False))
    for _ in range(100):
        t.write("ser>net", b"x" * 500)
    await t.aclose()
    assert not os.path.exists(path0 + ".1")
    assert os.path.getsize(path0) < 11_000
    print("tracer: keep=0 starts the file over (bounded size)  OK")


async def test_tracer_prunes_past_keep(tmp):
    """trace_keep lowered from 6 to 2: the trace's rotated files 3..6 go when it starts
    (logged); other names that only look alike stay."""
    path = os.path.join(tmp, "k.log")
    names = ["k.log", "k.log.1", "k.log.2", "k.log.3", "k.log.6", "k.log.old", "k.log.4x",
             "k.log.\u0663", "kk.log.5", "other-k.log.5"]
    for n in names:
        with open(os.path.join(tmp, n), "wb") as fh:
            fh.write(b"x")
    logs = []
    t = Tracer(path, _Opts(keep=2), log=logs.append)
    await asyncio.sleep(0)
    await t.aclose()
    left = sorted(os.listdir(tmp))
    assert left == sorted(set(names) - {"k.log.3", "k.log.6"}), left
    assert any(ln == f"trace: deleted 2 rotated file(s) of {path} past the 2 kept"
               for ln in logs), logs
    logs.clear()
    t = Tracer(path, _Opts(keep=2), log=logs.append)
    await t.aclose()
    assert not any("rotated file(s)" in ln for ln in logs), logs
    print("tracer: rotated files past a lowered trace_keep deleted at start (logged)  OK")


class _QuickTracer(Tracer):
    RETRY_S = 0.05
    SPACE_CHECK_BYTES = 4096


async def test_tracer_low_space(tmp):
    """A disk of 8 GiB (floor: 5%, 410 MB) with 100 MB free: the trace pauses at its start
    (status, log), counts what it misses, stays paused just above the floor and goes on
    with 10% to spare; then pauses again while writing, before the disk fills up."""
    usage = namedtuple("usage", "total used free")
    total = 8 * 1024 ** 3
    free = [100 * 1024 ** 2]
    fake = lambda _path: usage(total, total - free[0], free[0])  # noqa: E731
    floor = storage.floor(total)
    assert floor == int(total * 0.05), floor
    path = os.path.join(tmp, "low.log")
    status, logs = RunnerStatus(), []
    with mock.patch.object(storage.shutil, "disk_usage", fake):
        t = _QuickTracer(path, _Opts(hexdump=False), log=logs.append, status=status)
        assert t.error.startswith("the disk is almost full (100 MB free; traces pause below "
                                  "410 MB)"), t.error
        assert status.trace_error == t.error and not os.path.exists(path)
        t.write("ser>net", b"a" * 300)
        free[0] = floor + 10 * 1024 ** 2           # above the floor, not by 10%
        await asyncio.sleep(0.3)
        assert t.error, "went on without anything to spare"
        free[0] = int(floor * 1.1) + 1
        await wait_for(lambda: not t.error)
        t.write("ser>net", b"b" * 100)
        await wait_for(lambda: os.path.exists(path) and b"b" * 100 in open(path, "rb").read())
        text = open(path, "rb").read()
        assert b"!! 300 bytes not traced (the disk was almost full)" in text, text
        free[0] = floor - 1                        # gets low while it writes
        for _ in range(60):
            t.write("ser>net", b"c" * 100)
        await wait_for(lambda: bool(t.error))
        assert status.trace_error.startswith("the disk is almost full"), status.trace_error
        size = os.path.getsize(path)
        assert size < 4096 + 2000, size            # stopped after one check's worth
        await t.aclose()
    assert sum("trace not written" in ln for ln in logs) == 1, logs  # rate-limited
    assert any("trace written again" in ln for ln in logs), logs
    print("tracer: below the free-space floor it pauses (at start and while writing), says "
          "why and goes on by itself with 10% to spare  OK")


async def test_tracer_renew(tmp):
    """renew_if_older: a file whose traffic begins before the time given is rotated (as at
    its size) in the writer's turn, the next traffic goes into a new one; the time is the
    file's first line, or without timestamps when it was last written."""
    old = time.time() - 10 * 86400
    path = os.path.join(tmp, "r.log")
    with open(path, "w") as fh:
        fh.write(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(old)) + " ser>net x\n")
    t = Tracer(path, _Opts(hexdump=False))
    assert await asyncio.wrap_future(t.renew_if_older(old - 60)) is False, "not that old"
    assert await asyncio.wrap_future(t.renew_if_older(time.time() - 7 * 86400)) is True
    assert open(path + ".1").read().endswith(" ser>net x\n") and os.path.getsize(path) == 0
    t.write("ser>net", b"new")
    assert await asyncio.wrap_future(t.renew_if_older(time.time() - 7 * 86400)) is False
    await t.aclose()
    assert open(path, "rb").read().endswith(b"ser>net new\n")
    assert t.renew_if_older(time.time()) is None, "writer ended"
    plain = os.path.join(tmp, "p.log")
    with open(plain, "wb") as fh:
        fh.write(b"ser>net no time\n")
    os.utime(plain, (old, old))
    t = Tracer(plain, _Opts(hexdump=False))
    assert await asyncio.wrap_future(t.renew_if_older(time.time() - 7 * 86400)) is True
    await t.aclose()
    print("tracer: a file holding traffic older than asked begins anew (first line's time, "
          "else when last written)  OK")


class _SlowTracer(Tracer):
    QUEUE_MAX_BYTES = 64 * 1024

    def _append(self, lines):
        time.sleep(0.05)  # a slow disk
        super()._append(lines)


async def test_tracer_never_blocks(tmp):
    path = os.path.join(tmp, "slow.log")
    t = _SlowTracer(path, _Opts(hexdump=False))
    t0 = time.perf_counter()
    for _ in range(2000):
        t.write("ser>net", b"y" * 1024)  # 2 MB against a 64 KB queue and a slow disk
    took = time.perf_counter() - t0
    await t.aclose()
    assert took < 0.5, f"write() blocked the caller for {took:.2f}s"
    content = open(path, "rb").read()
    assert b"bytes not traced (the trace file could not keep up)" in content, content[-300:]
    print(f"tracer: 2000 writes against a slow disk returned in {took * 1000:.0f} ms; "
          "the loss is written into the trace  OK")


async def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


async def test_keep0_never_cuts_in_place(tmp):
    """keep=0 begins a new file: a reader that opened the old one (a download) still gets
    every byte it saw (POSIX: the old file is deleted, not cut; Windows: it can't be while
    open, so the trace goes on in it)."""
    path = os.path.join(tmp, "k0.log")
    logs = []
    t = Tracer(path, _Opts(max_mb=0.002, keep=0, hexdump=False), log=logs.append)  # ~2 KB
    t.write("ser>net", b"o" * 1000)
    await wait_for(lambda: os.path.isfile(path) and os.path.getsize(path) > 1000)
    with open(path, "rb") as reader:
        seen = os.fstat(reader.fileno()).st_size
        for _ in range(6):
            t.write("ser>net", b"n" * 1000)  # passes the size: a new file begins
        await t.aclose()
        got = reader.read(seen)
    assert len(got) == seen and got.endswith(b"o" * 1000 + b"\n"), (len(got), seen)
    assert not os.path.exists(path + ".1")
    if os.name == "nt":  # can't be deleted while the reader has it open
        assert any("continuing in the same file" in ln for ln in logs), logs
    else:
        assert os.path.getsize(path) < 4200 and not logs, (os.path.getsize(path), logs)
    print("tracer: keep=0 begins a new file, a reader of the old one gets all of it  OK")


class _FailingTracer(Tracer):
    """A trace whose file can't be opened while `broken` (no file descriptor left)."""
    RETRY_S = 0.05
    broken = False

    def _reopen(self):
        if self.broken:
            raise OSError(errno.EMFILE, "Too many open files", self.path)
        return super()._reopen()


class _WriteCrashTracer(Tracer):
    """Its first write fails with an error that is no OSError."""
    RETRY_S = 0.05
    crashed = False

    def _write(self, lines):
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("unexpected")
        super()._write(lines)


async def test_tracer_survives_failures(tmp):
    """Both opens of a rotation fail (the old code then wrote to a closed file, and its
    writer thread died with every later chunk queued until the mapping restarted)."""
    path = os.path.join(tmp, "fail.log")
    logs = []
    status = RunnerStatus()
    t = _FailingTracer(path, _Opts(max_mb=0.001, keep=1, hexdump=False),  # ~1 KB per file
                       log=logs.append, status=status)
    t.broken = True
    for _ in range(3):
        t.write("ser>net", b"a" * 600)  # the second passes the size: the reopen fails
    await wait_for(lambda: t.error)
    assert "Too many open files" in t.error and status.trace_error == t.error, t.error
    assert status.as_dict()["trace_error"] == t.error
    assert any("trace not written" in ln and "trying again" in ln for ln in logs), logs
    for _ in range(100):
        t.write("ser>net", b"b" * 100)  # paused: counted, not queued
    assert t._queued == 0 and t._q.qsize() == 0, (t._queued, t._q.qsize())
    assert t._thread.is_alive()
    t.broken = False
    await wait_for(lambda: not t.error)  # the writer tried the file again by itself
    t.write("net>ser", b"after")
    await t.aclose()
    assert not t._thread.is_alive() and status.trace_error == ""
    content = open(path, "rb").read()
    # the third chunk (lost with the failed rotation, or counted) and the 100 paused ones
    assert b"10600 bytes not traced (the trace file could not be written)" in content, content
    assert content.endswith(b"net>ser after\n"), content[-80:]
    assert any("trace written again" in ln for ln in logs), logs
    assert sum("trace not written" in ln for ln in logs) == 1, logs  # at most every 10 min

    path = os.path.join(tmp, "crash.log")
    t = _WriteCrashTracer(path, _Opts(hexdump=False), log=logs.append)
    t.write("ser>net", b"lost")
    await wait_for(lambda: t.crashed and not t.error)
    t.write("ser>net", b"kept")
    await t.aclose()
    content = open(path, "rb").read()
    assert b"4 bytes not traced" in content and content.endswith(b"ser>net kept\n"), content
    print("tracer: a failed reopen or write pauses the trace (status says why, nothing "
          "queued), the writer tries again and the trace says what is missing  OK")


async def test_tracer_folder_removed(tmp):
    """The traces folder removed while a trace runs: the next rotation makes it again."""
    if os.name == "nt":
        print("tracer: removed traces folder  SKIP (Windows can't remove an open file)")
        return
    folder = os.path.join(tmp, "traces")
    path = os.path.join(folder, "x.log")
    logs = []
    t = Tracer(path, _Opts(max_mb=0.001, keep=2, hexdump=False), log=logs.append)
    t.write("ser>net", b"first")
    await wait_for(lambda: os.path.getsize(path) > 0)
    shutil.rmtree(folder)
    for _ in range(4):
        t.write("ser>net", b"c" * 600)
    t.write("net>ser", b"end")
    await t.aclose()
    assert not t.error and os.path.isfile(path + ".1"), (t.error, logs)
    assert open(path, "rb").read().endswith(b"net>ser end\n")
    if os.name == "posix":
        assert (os.stat(folder).st_mode & 0o777) == 0o700, oct(os.stat(folder).st_mode)
    print("tracer: a traces folder removed while tracing is made again (owner only)  OK")


async def _download(response, path, method="GET"):
    """Send `response` through h11 as uvicorn does (its declared length is enforced);
    after the first body chunk the trace writer appends to `path`. Returns (headers,
    body, error)."""
    import h11
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(f"{method} /t HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert isinstance(conn.next_event(), h11.Request)
    headers, body, chunks = {}, [], 0

    async def send(msg):
        nonlocal chunks
        if msg["type"] == "http.response.start":
            headers.update({k.decode(): v.decode() for k, v in msg["headers"]})
            conn.send(h11.Response(status_code=msg["status"], headers=msg["headers"]))
        elif msg["type"] == "http.response.body":
            if msg.get("body") and method != "HEAD":
                conn.send(h11.Data(data=msg["body"]))
                body.append(msg["body"])
                chunks += 1
                if chunks == 1:  # a batch of traffic is written meanwhile
                    with open(path, "ab") as fh:
                        fh.write(b"x" * 5000)
            if not msg.get("more_body"):
                conn.send(h11.EndOfMessage())

    async def receive():
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    scope = {"type": "http", "method": method, "path": "/t", "headers": [],
             "asgi": {"version": "3.0", "spec_version": "2.4"}}
    try:
        await response(scope, receive, send)
    except h11.LocalProtocolError as e:
        return headers, b"".join(body), str(e)
    return headers, b"".join(body), ""


async def test_live_download(tmp):
    from starlette.responses import FileResponse

    from app.web.routes.mappings import snapshot_response
    path = os.path.join(tmp, "abc123-live.log")
    original = bytes(range(256)) * 781 + b"end\n"  # 199940 bytes: four 64 KiB reads
    with open(path, "wb") as fh:
        fh.write(original)
    headers, body, error = await _download(
        FileResponse(path, media_type="text/plain; charset=utf-8", filename="x"), path)
    assert error == "Too much data for declared Content-Length", (error, len(body))
    with open(path, "wb") as fh:
        fh.write(original)
    fh = open(path, "rb")
    headers, body, error = await _download(
        snapshot_response(fh, "abc123-live.log", "text/plain; charset=utf-8"), path)
    assert not error and body == original, (error, len(body))
    assert headers["content-length"] == str(len(original)) and fh.closed
    assert headers["content-disposition"] == 'attachment; filename="abc123-live.log"'
    assert os.path.getsize(path) == len(original) + 5000  # what was added stays in the file
    fh = open(path, "rb")
    headers, body, error = await _download(
        snapshot_response(fh, "sıcaklık.log", "text/plain; charset=utf-8", head=True), path,
        method="HEAD")
    assert not error and body == b"" and fh.closed
    assert headers["content-length"] == str(len(original) + 5000)
    assert headers["content-disposition"] == \
        "attachment; filename*=utf-8''s%C4%B1cakl%C4%B1k.log", headers
    print("download of a live trace: exactly the size it had when it began (a FileResponse "
          "broke off: too much data for its Content-Length)  OK")


def _deleted_but_open(path) -> list:
    """This process's descriptors of `path` after it was deleted (Linux; [] elsewhere)."""
    if not sys.platform.startswith("linux"):
        return []
    out = []
    for fd in os.listdir("/proc/self/fd"):
        with contextlib.suppress(OSError):
            if os.readlink(f"/proc/self/fd/{fd}") == f"{path} (deleted)":
                out.append(fd)
    return out


async def test_tracer_remove_files(tmp):
    path = os.path.join(tmp, "rm.log")
    t = Tracer(path, _Opts(max_mb=0.002, keep=1, hexdump=False))  # ~2 KB per file
    for _ in range(4):
        t.write("ser>net", b"before" * 100)
    await wait_for(lambda: os.path.isfile(path + ".1"))
    t.write("ser>net", b"queued")  # written before the deletion: deleted with it
    done = await asyncio.wrap_future(t.remove_files([path, path + ".1", path + ".9"]))
    assert done == {path: None, path + ".1": None, path + ".9": None}, done
    assert not os.path.exists(path) and not os.path.exists(path + ".1")
    assert not _deleted_but_open(path), "the writer kept the deleted file open"
    t.write("ser>net", b"after")
    await t.aclose()
    assert open(path, "rb").read().endswith(b"ser>net after\n")
    assert b"before" not in open(path, "rb").read() and b"queued" not in open(path, "rb").read()
    assert t.remove_files([path]) is None, "the writer has ended: files like any other"
    print("tracer: its files are deleted by the writer, in turn; the file it writes is closed "
          "first and begun again with the next traffic  OK")


def test_thread_errors_logged():
    from app import runtime
    logs = []
    before = threading.excepthook
    try:
        runtime._log_thread_errors(logs.append)
        with contextlib.redirect_stderr(io.StringIO()) as err:  # Python's own report
            th = threading.Thread(target=lambda: 1 / 0, name="trace:demo")
            th.start()
            th.join()
    finally:
        threading.excepthook = before
    assert len(logs) == 1 and "thread 'trace:demo' stopped by an error: ZeroDivisionError" \
        in logs[0] and "test_trace.py:" in logs[0], logs
    assert "ZeroDivisionError" in err.getvalue()
    print("a thread that dies is named in all.log, not only on stderr  OK")


async def test_runner_confined(tmp):
    """A mapping whose trace_both points at another file: the runner writes into the
    traces dir only. The serial device doesn't exist; the network side still starts."""
    victim = os.path.join(tmp, "victim", "config.json")
    os.makedirs(os.path.dirname(victim))
    with open(victim, "w") as fh:
        fh.write("{}")
    tdir = os.path.join(tmp, "traces")
    port = free_port()
    m = MappingConfig.from_dict({
        "name": "evil", "serial": {"port": "/dev/ser2net-no-such-port" if os.name != "nt" else "COM250"},
        "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1", "port": port},
        "options": {"trace_both": victim, "trace_hexdump": False},
    })
    logs = []
    r = MappingRunner(m, logger=logs.append, trace_dir=tdir)
    await r.start()
    try:
        expected = trace_path(tdir, m)
        assert expected == os.path.join(tdir, f"{m.id}-config.json"), expected
        assert os.path.isfile(expected), os.listdir(tdir)
        assert r._tracer is not None
        r._tracer.write("net>ser", b"PAYLOAD")
        assert any(f"tracing traffic to {expected}" in ln for ln in logs), logs
    finally:
        await r.stop()
    assert open(victim).read() == "{}", "the legacy path was written to"
    assert b"PAYLOAD" in open(expected, "rb").read()
    no_dir = MappingRunner(m, logger=logs.append)  # no data dir -> no trace at all
    await no_dir.start()
    await no_dir.stop()
    assert any("trace not written: no data directory" in ln for ln in logs), logs
    print("runner: trace goes to traces/<id>-<name>, the old target file is untouched  OK")


def test_state_files(tmp):
    from app.config import ConfigStore
    from app.state import AppState
    data = os.path.join(tmp, "data")
    st = AppState(ConfigStore(os.path.join(data, "config.json")), AppConfig())
    assert st.traces_dir == os.path.join(data, "traces") and os.path.isdir(st.traces_dir)
    for name in ("abc123-x.log", "abc123-x.log.1", "other9-y.log"):
        with open(os.path.join(st.traces_dir, name), "w") as fh:
            fh.write(name)
        time.sleep(0.02)
    got = [os.path.basename(p) for p in st.trace_files("abc123")]
    assert got == ["abc123-x.log.1", "abc123-x.log"], got
    st.delete_mapping_traces("abc123")
    assert os.listdir(st.traces_dir) == ["other9-y.log"]
    print("state: a mapping's trace files are listed and deleted with it  OK")

    st.config.mappings = [MappingConfig.from_dict({"id": "other9", "name": "o",
                                                   "serial": {"port": "COM9"},
                                                   "network": {"port": 4999}})]
    for name in ("other9-y.log.1", "gone77-z.log", "gone77-z.log.3", "nodash"):
        with open(os.path.join(st.traces_dir, name), "wb") as fh:
            fh.write(b"t" * 1000)
    os.makedirs(os.path.join(st.traces_dir, "gone77-dir"))
    assert st.traced_mapping_ids() == {"other9", "gone77"}, st.traced_mapping_ids()
    logs = []
    st.log = logs.append
    st.prune_orphan_traces()
    left = sorted(os.listdir(st.traces_dir))
    assert left == ["gone77-dir", "other9-y.log", "other9-y.log.1"], left
    assert logs == ["removed 3 trace file(s) (0.0 MB) of mappings that no longer exist"], logs
    st.prune_orphan_traces()
    assert len(logs) == 1, "nothing to prune: nothing logged"
    print("state: start-up prunes the traces no configured mapping owns, logged  OK")


def test_retention_setting():
    from app.config import TRACE_DAYS_MAX
    assert AppConfig().trace_max_age_days == 0 and AppConfig.from_dict({}).trace_max_age_days == 0
    assert AppConfig.from_dict({"trace_max_age_days": 30}).to_dict()["trace_max_age_days"] == 30
    for raw, used in ((-1, 0), (TRACE_DAYS_MAX + 1, TRACE_DAYS_MAX), ("nope", 0)):
        cfg = AppConfig.from_dict({"trace_max_age_days": raw})
        assert cfg.trace_max_age_days == used and cfg.load_notes, (raw, cfg.load_notes)
        cfg.validate()
    print("trace_max_age_days: 0 (keep) by default; a stored value out of range is read as 0 "
          "or the maximum, with a note  OK")


async def test_retention(tmp):
    from app.config import ConfigStore
    from app.state import AppState
    data = os.path.join(tmp, "data")
    st = AppState(ConfigStore(os.path.join(data, "config.json")), AppConfig())
    logs = []
    st.log = logs.append
    m = MappingConfig.from_dict({
        "id": "ret001", "name": "r", "serial": {"port": "/dev/ser2net-no-such-port"
                                                if os.name != "nt" else "COM250"},
        "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                    "port": free_port()},
        "options": {"trace_both": "t.log"}})
    st.config.mappings = [m]
    await st.supervisor.apply_mapping(m)
    tracer = st.supervisor.get_runner("ret001").tracer
    try:
        week, now = time.time() - 8 * 86400, time.time()
        # Windows can't change the time of the file the trace has open (and its writer
        # deletes it like any other then): it stays recent there
        active = week if os.name != "nt" else now
        for name, when in (("ret001-t.log", active), ("ret001-t.log.1", week),
                           ("ret001-recent.log", now), ("gone01-x.log", week)):
            path = os.path.join(st.traces_dir, name)
            if not os.path.exists(path):
                with open(path, "wb") as fh:
                    fh.write(b"x")
            os.utime(path, (when, when))
        assert await st.run_trace_maintenance() == 0 and len(os.listdir(st.traces_dir)) == 4
        st.config.trace_max_age_days = 7
        n = 3 if active == week else 2
        assert await st.run_trace_maintenance() == n
        assert sorted(os.listdir(st.traces_dir)) == (
            ["ret001-recent.log"] if n == 3 else ["ret001-recent.log", "ret001-t.log"]), \
            os.listdir(st.traces_dir)
        assert logs[-1] == f"trace maintenance: deleted {n} trace file(s) not written for 7 days"
        assert not _deleted_but_open(tracer.path), "the file being written was left open"
        assert tracer.active and not tracer.error
        assert await st.run_trace_maintenance() == 0
        assert sum("trace maintenance" in ln for ln in logs) == 1, logs
        # written into all the time, so never "not written for 7 days": begun anew once its
        # traffic is that old, and the rotated file goes 7 days after its last write
        tracer.write("ser>net", b"payload")
        await wait_for(lambda: os.path.exists(tracer.path) and os.path.getsize(tracer.path))
        tracer._begun = time.time() - 8 * 86400  # (its first line came 8 days ago)
        assert await st.run_trace_maintenance() == 0
        assert logs[-1] == ("trace maintenance: 1 trace(s) begun in a new file, as theirs held "
                            "traffic from more than 7 days ago"), logs[-1]
        assert b"payload" in open(tracer.path + ".1", "rb").read()
        assert await st.run_trace_maintenance() == 0  # its new file is young: nothing more
        assert sum("begun in a new file" in ln for ln in logs) == 1, logs
    finally:
        await st.supervisor.stop_all()
        st.delete_mapping_log("ret001")
    print("retention: trace files not written for N days are deleted (the one being written "
          "by its writer), recent ones kept; 0 keeps all  OK")


async def test_forget_mappings(tmp):
    """A replace import / restore removes mappings without deleting them one by one: their
    runner (with its trace writer) and log file are closed at once, their files stay."""
    import logging

    from app.config import ConfigStore
    from app.state import AppState
    data = os.path.join(tmp, "data")
    st = AppState(ConfigStore(os.path.join(data, "config.json")), AppConfig())
    st.log = lambda _m: None
    keep, gone = (MappingConfig.from_dict({
        "id": mid, "name": mid, "serial": {"port": "/dev/ser2net-no-such-port"
                                           if os.name != "nt" else "COM250"},
        "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                    "port": free_port()},
        "options": {"trace_both": "t.log"}}) for mid in ("keep01", "gone01"))
    st.config.mappings = [keep, gone]
    for m in (keep, gone):
        await st.supervisor.apply_mapping(m)
    tracer = st.supervisor.get_runner("gone01").tracer
    assert tracer is not None and tracer._thread.is_alive()
    files = sorted(os.path.basename(p) for p in st.trace_files("gone01"))
    st.config.mappings = [keep]  # what a replace import does
    await st.forget_mappings({"gone01"})
    try:
        assert st.supervisor.get_runner("gone01") is None and not tracer._thread.is_alive()
        assert "gone01" not in st._mapping_loggers and \
            not logging.getLogger("ser2net.map.gone01").handlers
        assert logging.getLogger("ser2net.map.keep01").handlers, "the kept mapping logs on"
        assert files == ["gone01-t.log"] and \
            [os.path.basename(p) for p in st.trace_files("gone01")] == files, "files stay"
        assert os.path.isfile(os.path.join(st.logs_dir, "gone01.log"))
    finally:
        await st.supervisor.stop_all()
        st.delete_mapping_log("keep01")
    print("state: forget_mappings stops what is left of removed mappings and closes their "
          "log files; their files stay  OK")


async def main():
    test_names()
    test_validation()
    test_legacy_config_notes()
    tmp = tempfile.mkdtemp(prefix="s2n_trace_")
    try:
        os.makedirs(os.path.join(tmp, "a"))
        os.makedirs(os.path.join(tmp, "b"))
        os.makedirs(os.path.join(tmp, "c"))
        for sub in ("d", "e", "f"):
            os.makedirs(os.path.join(tmp, sub))
        await test_tracer_format_and_rotation(os.path.join(tmp, "a"))
        os.makedirs(os.path.join(tmp, "k"))
        await test_tracer_prunes_past_keep(os.path.join(tmp, "k"))
        await test_tracer_never_blocks(os.path.join(tmp, "b"))
        await test_keep0_never_cuts_in_place(os.path.join(tmp, "d"))
        await test_tracer_survives_failures(os.path.join(tmp, "e"))
        os.makedirs(os.path.join(tmp, "l"))
        await test_tracer_low_space(os.path.join(tmp, "l"))
        os.makedirs(os.path.join(tmp, "m"))
        await test_tracer_renew(os.path.join(tmp, "m"))
        await test_tracer_folder_removed(os.path.join(tmp, "f"))
        test_thread_errors_logged()
        os.makedirs(os.path.join(tmp, "g"))
        await test_live_download(os.path.join(tmp, "g"))
        os.makedirs(os.path.join(tmp, "i"))
        await test_tracer_remove_files(os.path.join(tmp, "i"))
        await test_runner_confined(os.path.join(tmp, "c"))
        test_state_files(tmp)
        os.makedirs(os.path.join(tmp, "h"))
        await test_forget_mappings(os.path.join(tmp, "h"))
        test_retention_setting()
        os.makedirs(os.path.join(tmp, "j"))
        await test_retention(os.path.join(tmp, "j"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nPASS: traces confined to data/traces, off-loop writer, rotation, failures")


if __name__ == "__main__":
    asyncio.run(main())
