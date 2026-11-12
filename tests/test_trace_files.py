"""Trace files over a mapping's life: deleting them, removed mappings, start-up pruning.

Checks:
  * operators delete one trace file or all of a mapping's from the trace panel (viewers
    403, CSRF header required, only names of the mapping's own list, audited as
    trace_delete); the file being written is closed by the trace writer, not left open
    deleted, and tracing goes on; a mapping without tracing whose files are all gone no
    longer offers Trace;
  * a replace import and a backup restore that remove a mapping close what the server
    kept open for it at once (its runner with the trace writer, its log file; before,
    the log file stayed open until a restart) and keep its files, so restoring a backup
    that has the mapping again finds its traces;
  * the next start deletes the traces (and logs) of mappings that no longer exist and
    logs how many.
The open files are read from /proc (Linux only; skipped elsewhere).

Run: python3 tests/test_trace_files.py
"""
import contextlib
import json
import os
import sys
import time

from _server import Browser, Server, free_port

from app.config import ConfigStore
from app.web.auth import hash_password

PW = "trace-pass-1"
A, B = "aaaaaaaaaaa1", "aaaaaaaaaaa2"


def mapping(mid, name, trace):
    return {"id": mid, "name": name, "enabled": True, "kind": "net",
            "serial": {"port": f"/dev/ser2net-trace-{mid}"},
            "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                        "port": free_port()},
            "options": {"trace_both": trace}}


def open_files(srv) -> set:
    """What the server process has open (None where /proc can't tell)."""
    if not sys.platform.startswith("linux"):
        return None
    out = set()
    fd_dir = f"/proc/{srv.proc.pid}/fd"
    for fd in os.listdir(fd_dir):
        with contextlib.suppress(OSError):
            out.add(os.readlink(os.path.join(fd_dir, fd)))
    return out


def wait(cond, what, timeout=10.0):
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, f"timed out: {what}"
        time.sleep(0.1)


def replace_import(b, mappings):
    export = json.dumps({"version": 1, "mappings": mappings}).encode()
    st, _, page = b.post("/settings/config/import", {"step": "preview", "mode": "replace"},
                         files={"file": ("m.json", export)})
    assert st == 200 and "Import preview" in page, page[:300]
    token = page.split('name="token" value="', 1)[1].split('"', 1)[0]
    st, _, page = b.post("/settings/config/import/apply", {"token": token, "mode": "replace"})
    assert "replaced the existing ones" in page, page[page.find("banner"):][:300]


def restore(b, srv, count):
    """Restore the newest backup with `count` mappings."""
    name = next(x["name"] for x in ConfigStore(srv.config_path).backups()
                if x["mappings"] == count)
    st, _, page = b.post("/settings/backups/restore", {"name": name})
    assert st == 200 and "Restored" in page, page[page.find("banner"):][:300]


USERS = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
          "pwd_version": 1},
         {"username": "op", "password_hash": hash_password(PW), "role": "operator",
          "pwd_version": 1},
         {"username": "view", "password_hash": hash_password(PW), "role": "viewer",
          "pwd_version": 1}]


def delete_traces():
    srv = Server({"users": USERS, "mappings": [mapping(A, "A", "a.log"), mapping(B, "B", "")]})
    traces = os.path.join(srv.data, "traces")
    os.makedirs(traces)
    for name in (f"{A}-a.log.1", f"{A}-old.log", f"{B}-b.log", f"{B}-b.log.1"):
        with open(os.path.join(traces, name), "w", encoding="utf-8") as fh:
            fh.write(f"{name}\n")
    active = os.path.join(traces, f"{A}-a.log")
    with srv:
        wait(lambda: os.path.isfile(active), "A's trace opened")
        view, op, admin = Browser(srv.base), Browser(srv.base), Browser(srv.base)
        view.login("view", PW)
        op.login("op", PW)
        admin.login("admin", PW)
        st, _, body = view.post(f"/api/mappings/{A}/traces/{A}-old.log", method="DELETE", htmx=True)
        assert st == 403 and "requires the operator role" in body, (st, body[:120])
        st, _, body = view.post(f"/api/mappings/{B}/traces", method="DELETE", htmx=True)
        assert st == 403 and len(os.listdir(traces)) == 5, os.listdir(traces)

        st, _, page = op.post(f"/api/mappings/{A}/traces/{A}-old.log", method="DELETE", htmx=True)
        assert st == 200 and "Deleted 1 trace file(s)." in page, page[:600]
        assert f"{A}-old.log" not in page and f"{A}-a.log.1" in page
        assert not os.path.exists(os.path.join(traces, f"{A}-old.log"))
        for bad in (f"{B}-b.log", "..%2Fconfig.json", "%2E%2E%2F%2E%2E%2Fall.log", "no-such"):
            st, _, page = op.post(f"/api/mappings/{A}/traces/{bad}", method="DELETE", htmx=True)
            assert st in (400, 404) and "Deleted" not in page, (bad, st, page[:300])
        assert os.path.isfile(os.path.join(traces, f"{B}-b.log")) and os.path.isfile(srv.config_path)
        assert os.path.isfile(os.path.join(srv.data, "all.log"))
        print("delete one: an operator deletes a file of the mapping's own list; other "
              "mappings' files and path tricks are refused, viewers 403  OK")

        st, _, page = op.post(f"/api/mappings/{A}/traces/{A}-a.log", method="DELETE", htmx=True)
        assert st == 200 and "Tracing is still on" in page, page[:600]
        assert not os.path.exists(active)
        fds = open_files(srv)
        if fds is not None:
            assert f"{active} (deleted)" not in fds, "the deleted trace is still open"
        st, _, page = op.get(f"/api/mappings/{A}/traces")
        assert "Recording to" in page and f"{A}-a.log.1" in page
        print("delete the file being written: closed by the trace writer, tracing goes on  OK")

        st, _, body = op.get("/api/status")
        assert f'hx-get="/api/mappings/{B}/traces"' in body, "B has files: Trace offered"
        st, _, page = op.post(f"/api/mappings/{B}/traces", method="DELETE", htmx=True)
        assert st == 200 and "Deleted 2 trace file(s)." in page and "No trace files yet." in page
        assert not [n for n in os.listdir(traces) if n.startswith(f"{B}-")], os.listdir(traces)
        st, _, body = op.get("/api/status")
        assert f'hx-get="/api/mappings/{B}/traces"' not in body, "no files, no tracing: no Trace"
        st, _, page = op.post(f"/api/mappings/{B}/traces", method="DELETE", htmx=True)
        assert st == 200 and "No trace files to delete." in page
        assert op.post("/api/mappings/nosuchmapping/traces", method="DELETE", htmx=True)[0] == 404
        st, _, page = admin.get("/settings/audit?action=trace_delete")
        assert "3 record(s)" in page, page[page.find("record(s)") - 40:][:200]
        with open(os.path.join(srv.data, "audit.log"), encoding="utf-8") as fh:
            records = [ln.split("\t") for ln in fh.read().splitlines() if "\ttrace_delete\t" in ln]
        details = [r[3] for r in records]
        assert details[:2] == [f"A/{A}-old.log", f"A/{A}-a.log"], records
        assert details[2].startswith("B: ") and \
            sorted(details[2][3:].split(", ")) == [f"{B}-b.log", f"{B}-b.log.1"], records
        assert {r[4] for r in records} == {"op"}, records
        print("delete all: every file of the mapping, audited; then no Trace button for a "
              "mapping that doesn't trace  OK")


def removed_mappings():
    a = mapping(A, "A", "a.log")
    srv = Server({"users": USERS, "mappings": [a, mapping(B, "B", "b.log")]})
    traces, logs = os.path.join(srv.data, "traces"), os.path.join(srv.data, "logs")
    a_trace, b_trace = os.path.join(traces, f"{A}-a.log"), os.path.join(traces, f"{B}-b.log")
    b_log = os.path.join(logs, f"{B}.log")
    with srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        wait(lambda: os.path.isfile(a_trace) and os.path.isfile(b_trace), "traces opened")
        fds = open_files(srv)
        if fds is not None:
            assert b_trace in fds and b_log in fds, sorted(fds)

        replace_import(b, [a])  # B is gone
        assert [m["id"] for m in srv.saved_config()["mappings"]] == [A]
        fds = open_files(srv)
        if fds is not None:
            assert b_trace not in fds and b_log not in fds, sorted(fds)
            assert a_trace in fds, "A is running again"
        assert os.path.isfile(b_trace) and os.path.isfile(b_log), "files stay until the next start"
        print("replace import: the removed mapping's trace and log are closed, kept on disk  OK")

        restore(b, srv, 2)  # back with B
        st, _, page = b.get(f"/api/mappings/{B}/traces")
        assert st == 200 and f"{B}-b.log" in page, page[:400]
        restore(b, srv, 1)  # and without it again
        assert [m["id"] for m in srv.saved_config()["mappings"]] == [A]
        fds = open_files(srv)
        if fds is not None:
            assert b_trace not in fds and b_log not in fds, sorted(fds)
        print("restore: a backup with the mapping finds its traces again; one without it "
              "closes them  OK")

        srv.stop()
        srv.start()
        b.login("admin", PW)
        wait(lambda: "removed 1 trace file(s)" in srv.output(), "start-up prune logged")
        assert not os.path.exists(b_trace) and not os.path.exists(b_log)
        assert os.path.isfile(a_trace)
        print("start-up: the traces and logs of the removed mapping are pruned"
              + ("" if fds is not None else " (open files not checked: no /proc)") + "  OK")


def main():
    delete_traces()
    removed_mappings()
    print("\nPASS: trace files deleted from the panel; those of removed mappings closed at "
          "once and pruned at the next start")


if __name__ == "__main__":
    main()
