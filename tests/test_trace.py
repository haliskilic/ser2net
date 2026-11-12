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
    a file in place (a download still reading it gets all of it, also with keep=0);
  * a file that can't be opened or written pauses the trace instead of ending its writer
    thread: the status says why, nothing is queued meanwhile, the writer tries again,
    and the trace then says how many bytes are missing; a removed traces folder is made
    again; a thread that dies anyway is named in all.log;
  * deleting a mapping deletes its traces.
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
    t = Tracer(path, _Opts(max_mb=0.002, keep=0, hexdump=False))  # ~2 KB per file
    t.write("ser>net", b"o" * 1000)
    await wait_for(lambda: os.path.isfile(path) and os.path.getsize(path) > 1000)
    with open(path, "rb") as reader:
        seen = os.fstat(reader.fileno()).st_size
        for _ in range(6):
            t.write("ser>net", b"n" * 1000)  # passes the size: a new file begins
        await t.aclose()
        got = reader.read(seen)
    assert len(got) == seen and got.endswith(b"o" * 1000 + b"\n"), (len(got), seen)
    assert os.path.getsize(path) < 4200 and not os.path.exists(path + ".1")
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
        await test_tracer_never_blocks(os.path.join(tmp, "b"))
        await test_keep0_never_cuts_in_place(os.path.join(tmp, "d"))
        await test_tracer_survives_failures(os.path.join(tmp, "e"))
        await test_tracer_folder_removed(os.path.join(tmp, "f"))
        test_thread_errors_logged()
        await test_runner_confined(os.path.join(tmp, "c"))
        test_state_files(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nPASS: traces confined to data/traces, off-loop writer, rotation, failures")


if __name__ == "__main__":
    asyncio.run(main())
