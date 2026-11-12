"""Times in all.log, stdout, the mapping logs and audit.log carry their UTC offset.

  * clock.stamp(): the server's local time with its offset ("2026-11-06 12:00:00+03:00"),
    in any zone; the hour repeated when daylight saving time ends gives two different
    stamps; clock.parse_stamp() reads it back exactly, and a line without an offset (as
    versions before 2.9 wrote) as local time, as before;
  * a reader that only knows the old format (line[:19], local time: the log trim and the
    sign-ins of 2.8.x) still reads every new line;
  * all.log / stdout / logs/<id>.log lines and audit records start with the stamp; the
    trace line format is unchanged;
  * the log trim keeps or drops a new line by its offset, an old line as before, in one
    file with both kinds; the sign-ins are read from old and new audit records;
  * the audit viewer and its CSV export show old and new records as they are written,
    and the audit, log and trace panels name the server's zone;
  * the hex view says its times are in the browser's zone.
The parts that set a time zone (TZ + time.tzset: this process only, never the system
clock) run on POSIX; on Windows the same checks run in the machine's own zone.

Run: python3 tests/test_time_stamps.py
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import logging
import os
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import clock                                            # noqa: E402
from app.config import AppConfig, ConfigStore                    # noqa: E402
from app.engine.bridge import Tracer                             # noqa: E402
from app.signins import SignIns                                  # noqa: E402
from app.state import AppState, _trim_log_file, write_audit_record  # noqa: E402
from app.web import auth                                         # noqa: E402
from app.web.server import build_app                             # noqa: E402

STAMP = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[+-]\d\d:\d\d")
OLD = "%Y-%m-%d %H:%M:%S"
PW = "time-stamps-1"
# 2026-10-25: daylight saving time ends in Europe (03:00 CEST -> 02:00 CET)
FALL_BACK = (1792888200.0, 1792891800.0)     # 00:30 and 01:30 UTC: 02:30 in Berlin twice
CAN_SET_ZONE = hasattr(time, "tzset")


@contextlib.contextmanager
def zone(name: str):
    """This process's local time zone (TZ) for a while; the system clock is untouched."""
    saved = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


def old_reader(line: str) -> float:
    """How 2.8.x reads a line's time (the log trim, the audit log's sign-ins)."""
    return time.mktime(time.strptime(line[:19], OLD))


def _close_loggers():
    for name in list(logging.root.manager.loggerDict):
        if name == "ser2net" or name.startswith("ser2net."):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_stamp():
    now = time.time()
    s = clock.stamp(now)
    assert STAMP.fullmatch(s), s
    assert clock.parse_stamp(s + " a message") == int(now), (s, now)
    assert s[:19] == time.strftime(OLD, time.localtime(now)), "the local time comes first"
    assert old_reader(s + " message") == int(now), "a 2.8 reader misreads a new line"
    old = time.strftime(OLD, time.localtime(now))
    assert clock.parse_stamp(old + " old line") == old_reader(old), "an old line"
    assert clock.parse_stamp(old + "\t10.0.0.1\tlogin") == old_reader(old), "an old audit record"
    for bad in ("", "no time here", "2026-13-01 00:00:00 x", "2026-11-06 12:00"):
        assert clock.parse_stamp(bad) is None, bad
    assert clock.zone().startswith("UTC"), clock.zone()
    print(f"stamp: local time + UTC offset ({s}, zone {clock.zone()}), read back exactly; "
          "old lines read as local time, as before  OK")
    if not CAN_SET_ZONE:
        print("SKIP: the other time zones (TZ) - no time.tzset() on this platform")
        return
    t = 1793000000.0     # 2026-10-26 07:33:20 UTC
    for name, text, label in (("UTC", "2026-10-26 07:33:20+00:00", "UTC"),
                              ("Europe/Istanbul", "2026-10-26 10:33:20+03:00", "UTC+03:00"),
                              ("Europe/Berlin", "2026-10-26 08:33:20+01:00", "UTC+01:00 (CET)"),
                              ("America/St_Johns", "2026-10-26 05:03:20-02:30", "UTC-02:30"),
                              ("Asia/Kolkata", "2026-10-26 13:03:20+05:30", "UTC+05:30")):
        with zone(name):
            assert clock.stamp(t) == text, (name, clock.stamp(t))
            assert clock.parse_stamp(text) == t and old_reader(text) == t, name
            assert clock.zone(t).startswith(label), (name, clock.zone(t))
    with zone("Europe/Berlin"):
        a, b = (clock.stamp(x) for x in FALL_BACK)
        assert (a, b) == ("2026-10-25 02:30:00+02:00", "2026-10-25 02:30:00+01:00"), (a, b)
        assert [clock.parse_stamp(x) for x in (a, b)] == list(FALL_BACK)
        # without the offset both read as one of the two instants (what 2.8 wrote)
        assert old_reader(a) == old_reader(b) and old_reader(a) in FALL_BACK
    print("stamp: UTC, +03:00, +01:00/+02:00, -02:30, +05:30; the repeated hour at the end of "
          "daylight saving time gives two stamps, read back apart  OK")


def test_files():
    d = tempfile.mkdtemp(prefix="s2n-stamps-")
    try:
        st = AppState(ConfigStore(os.path.join(d, "config.json")), AppConfig())
        st.log("hello from the log")
        log = st.make_mapping_logger(type("M", (), {"name": "PLC", "id": "m1"})())
        log("a mapping line")
        line = [ln for ln in _read(st.log_path).split("\n") if "hello from the log" in ln][0]
        assert STAMP.match(line) and line[25:] == " hello from the log", line
        mline = _read(st.mapping_log_path("m1")).split("\n")[0]
        assert STAMP.match(mline) and mline.endswith(" a mapping line"), mline
        assert st.read_mapping_log("m1") == [mline]
        fields = write_audit_record(st.audit_path, "10.0.0.1", "mapping_save", "PLC", "admin")
        assert STAMP.fullmatch(fields[0]), fields
        rec = st.read_audit()[0]
        assert rec["time"] == fields[0] and rec["action"] == "mapping_save", rec
        print("all.log, mapping log and audit records start with the stamp  OK")

        # the trace line format is unchanged: local time without an offset
        class Options:
            trace_hexdump, trace_timestamp, trace_max_mb, trace_keep = False, True, 1, 0
        tracer = Tracer(os.path.join(d, "t.trace"), Options())
        out = tracer._format(1793000000.0, "ser>net", b"ok").decode()
        assert out == time.strftime(OLD + " ", time.localtime(1793000000.0)) + "ser>net ok\n", out
        asyncio.run(tracer.aclose())
        print("trace lines keep 'YYYY-MM-DD HH:MM:SS dir data' (no offset)  OK")
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)


def test_trim_and_sign_ins():
    """One file with lines from 2.8 (no offset) and from now (offset): the trim drops and
    keeps each by its own time, the sign-ins are read from both kinds of audit records."""
    d = tempfile.mkdtemp(prefix="s2n-trim-")
    try:
        now = time.time()
        p = os.path.join(d, "m.log")

        def old(age):
            return time.strftime(OLD, time.localtime(now - age))
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(f"{old(20 * 86400)} OLD-GONE\n{clock.stamp(now - 18 * 86400)} NEW-GONE\n"
                     f"{old(3 * 86400)} OLD-KEPT\n{clock.stamp(now - 86400)} NEW-KEPT\n"
                     f"{clock.stamp(now)} LAST\n")
        assert _trim_log_file(p, now - 15 * 86400, 100 * 1024 * 1024)
        text = _read(p)
        assert "GONE" not in text and all(w in text for w in ("OLD-KEPT", "NEW-KEPT", "LAST")), text
        # what 2.8.x does with the file this version wrote: it reads every line's time
        assert all(old_reader(ln) for ln in text.split("\n") if ln), text
        print("log trim: old and new lines in one file, each by its own time; 2.8 reads them  OK")

        if CAN_SET_ZONE:   # the offset is what counts, not the local time before it
            with zone("UTC"):
                p2 = os.path.join(d, "z.log")
                with open(p2, "w", encoding="utf-8", newline="") as fh:
                    # 2026-06-09 00:00 at +03:00 is 2026-06-08 21:00 UTC: before the cutoff
                    fh.write("2026-06-09 00:00:00+03:00 EAST\n2026-06-08 23:00:00+00:00 HERE\n")
                cutoff = clock.parse_stamp("2026-06-08 22:00:00+00:00")
                assert _trim_log_file(p2, cutoff, 1 << 20)
                assert _read(p2) == "2026-06-08 23:00:00+00:00 HERE\n", _read(p2)
            print("log trim: a line's offset decides its time (a line from another zone)  OK")

        seen = SignIns()
        seen.load([
            {"time": old(10 * 86400), "action": "login", "user": "ann", "detail": "viewer, local"},
            {"time": clock.stamp(now - 86400), "action": "login", "user": "bob",
             "detail": "viewer, ldap"},
        ])
        ann = type("U", (), {"username": "ann", "source": "local"})()
        bob = type("U", (), {"username": "bob", "source": "ldap"})()
        assert int(seen.last(ann)) == int(now - 10 * 86400), seen.last(ann)
        assert int(seen.last(bob)) == int(now - 86400), seen.last(bob)
        print("sign-ins: read from audit records with and without the offset  OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)


async def _get(app, path: str, cookie: str = "") -> tuple[int, str]:
    path, _, query = path.partition("?")
    headers = [(b"host", b"127.0.0.1:8080")] + ([(b"cookie", cookie.encode())] if cookie else [])
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": query.encode(), "root_path": "",
             "headers": headers, "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8080)}
    out = {"status": 0, "body": b""}
    sent = asyncio.Event()

    async def receive():
        if not sent.is_set():
            sent.set()
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    await asyncio.wait_for(app(scope, receive, send), 30)
    return out["status"], out["body"].decode("utf-8", "replace")


def test_viewers():
    d = tempfile.mkdtemp(prefix="s2n-viewers-")
    try:
        cfg = AppConfig.from_dict({
            "users": [{"username": "admin", "password_hash": auth.hash_password(PW),
                       "role": "admin", "pwd_version": 1}],
            "mappings": [{"id": "plc1", "name": "PLC", "serial": {"port": "/dev/null-x"},
                          "options": {"trace_file": "plc.trace"}}]})
        st = AppState(ConfigStore(os.path.join(d, "config.json")), cfg)
        with open(st.audit_path, "w", encoding="utf-8") as fh:
            fh.write("2026-01-05 10:00:00\t10.0.0.1\tmapping_save\tv2.7 record\n"
                     "2026-02-05 10:00:00\t10.0.0.2\tmapping_save\tv2.8 record\tadmin\n")
        new = write_audit_record(st.audit_path, "10.0.0.3", "mapping_save", "v2.9 record", "admin")
        st.make_mapping_logger(cfg.mappings[0])("a line of the mapping")
        os.makedirs(st.traces_dir, exist_ok=True)
        with open(os.path.join(st.traces_dir, "plc1-plc.trace"), "w") as fh:
            fh.write(time.strftime(OLD) + " ser>net hello\n")
        app = build_app(st)
        session = auth.issue_session(cfg.secret_key, 3600, "admin", 1)
        cookie = f"ser2net_session={session}"
        here = clock.zone()

        status, page = asyncio.run(_get(app, "/settings/audit", cookie))
        assert status == 200, (status, page[:300])
        for t in ("2026-01-05 10:00:00", "2026-02-05 10:00:00", new[0]):
            assert f">{t}</td>" in page, t
        assert f"the server is at {here} now" in page, page[page.find("muted mb-sm"):][:400]
        status, text = asyncio.run(_get(app, "/settings/audit.csv", cookie))
        rows = list(csv.reader(io.StringIO(text)))
        assert status == 200 and rows[0] == ["time", "ip", "user", "action", "detail"], rows[:1]
        assert [r[0] for r in rows[1:4]] == [new[0], "2026-02-05 10:00:00",
                                            "2026-01-05 10:00:00"], rows
        assert [r[4] for r in rows[1:4]] == ["v2.9 record", "v2.8 record", "v2.7 record"], rows
        print("audit viewer and CSV: v2.7, v2.8 and new records with their times as written; "
              "the page names the server's zone  OK")

        status, page = asyncio.run(_get(app, "/api/mappings/plc1/log", cookie))
        assert status == 200 and STAMP.search(page) and f"(now {here})" in page, page[:600]
        status, page = asyncio.run(_get(app, "/api/mappings/plc1/traces", cookie))
        assert status == 200 and f"local time (now {here})" in page, page[:900]
        assert re.search(r'<td class="mono nowrap">' + STAMP.pattern + "</td>", page), page[:1500]
        print("log and trace panels name the server's zone; the trace list's times carry "
              "the offset  OK")

        status, page = asyncio.run(_get(app, "/settings", cookie))
        assert status == 200 and "&#34;hex_zone&#34;" in page, "the hex view's zone text"
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)
    js = _read(os.path.join(os.path.dirname(__file__), "..", "app", "web", "static", "app.js"))
    assert "resolvedOptions().timeZone" in js and 'tr("hex_zone"' in js
    print("hex view: its header says the times are in the browser's zone  OK")


def main():
    test_stamp()
    test_files()
    test_trim_and_sign_ins()
    test_viewers()
    print("\nPASS: log and audit times carry their UTC offset; old and new lines read alike")


if __name__ == "__main__":
    main()
