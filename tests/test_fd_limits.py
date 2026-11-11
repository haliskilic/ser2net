"""The process's open-file limit: raised at start, counted, reported (app/engine/netguard.py).

Every serial port, socket and file holds a descriptor, and a process may have at most its
soft RLIMIT_NOFILE open (1024 under systemd unless the unit raises it). ser2net raises the
soft limit toward the hard one at start (at most 65536, never lowering it) and logs the
result; warns at start when the configured mappings need close to the limit; and /metrics
reports ser2net_open_fds and ser2net_fd_limit. On Windows (no such limit for sockets and
serial ports) none of this applies: the functions say so and the server still boots.

Checked: raise_fd_limit in child processes started with a low, a high and a hard-capped
limit; open_fds counts; the start-up estimate and its warning; a real server boot logs
the limit line and serves both gauges; asyncio's error reports (e.g. a listener out of
descriptors) reach all.log.

Run: python3 tests/test_fd_limits.py
"""
import os
import subprocess
import sys

from _server import Browser, Server

from app.config import AppConfig
from app.engine import netguard
from app.web.auth import hash_password

PW = "fd-limits-pass-1"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _raise_in_child(soft: int, hard: int, target: int) -> list[str]:
    """Run raise_fd_limit(target) in a child whose limits start at (soft, hard); returns
    [its log line, the soft limit after, the hard limit after]."""
    code = (
        "import resource, sys\n"
        f"resource.setrlimit(resource.RLIMIT_NOFILE, ({soft}, {hard}))\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "from app.engine import netguard\n"
        f"print(netguard.raise_fd_limit({target}))\n"
        "print(*resource.getrlimit(resource.RLIMIT_NOFILE), sep='\\n')\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=30, check=True).stdout
    return out.strip().splitlines()


def test_raise_fd_limit():
    import resource

    _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard < 4096:
        print(f"SKIP raise_fd_limit: the hard limit here is {hard}")
        return
    line, soft_after, hard_after = _raise_in_child(512, 4096, 65536)
    assert (soft_after, hard_after) == ("4096", "4096"), (soft_after, hard_after)
    assert line == "open files: limit raised from 512 to 4096 (hard limit 4096)", line

    line, soft_after, _ = _raise_in_child(512, 4096, 2048)
    assert soft_after == "2048" and "raised from 512 to 2048" in line, (line, soft_after)

    line, soft_after, _ = _raise_in_child(3000, 4096, 2048)
    assert soft_after == "3000" and line == "open files: limit 3000", "never lowered"
    print("raise_fd_limit: up to the hard limit or the target, never lower, logged  OK")


def test_open_fds_and_limit():
    import resource

    n = netguard.open_fds()
    assert n is not None and n >= 3, n  # at least stdin, stdout, stderr
    fd = os.open(os.devnull, os.O_RDONLY)
    try:
        assert netguard.open_fds() == n + 1
    finally:
        os.close(fd)
    soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    assert netguard.fd_limit() == (None if soft == resource.RLIM_INFINITY else soft)
    print("open_fds / fd_limit: the process's open files and its soft limit  OK")


def _mapping(i: int, **extra) -> dict:
    return {"id": f"m{i}", "name": f"m{i}", "serial": {"port": f"/dev/ttyX{i}"},
            "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 4000 + i,
                        "max_connections": 2}, **extra}


def test_estimate_and_warning():
    cfg = AppConfig.from_dict({"mappings": [
        _mapping(1), _mapping(2, enabled=False), _mapping(3, mqtt={"enabled": True}),
        {"id": "b", "name": "b", "kind": "serialbridge", "serial": {"port": "/dev/a"},
         "serial_b": {"port": "/dev/b"}}]})
    # m1: log + port + listener + 2 clients; m2: log; m3: as m1 + MQTT 3; b: log + 2 ports
    assert netguard.fd_estimate(cfg) == netguard.FD_BASE + 5 + 1 + 8 + 3, netguard.fd_estimate(cfg)
    assert netguard.fd_budget_warning(cfg, limit=1024) == ""
    big = AppConfig.from_dict({"mappings": [_mapping(i) for i in range(200)]})
    warning = netguard.fd_budget_warning(big, limit=1024)
    assert warning.startswith("WARNING: the 200 mappings need about 1040 open files"), warning
    assert "LimitNOFILE" in warning
    print("fd_estimate / fd_budget_warning: per-mapping need, a warning near the limit  OK")


def test_server_reports_limit():
    cfg = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                      "pwd_version": 1}], "mappings": []}
    with Server(cfg) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        st, _, body = b.get("/metrics")
        assert st == 200, st
        gauges = dict(ln.split(" ", 1) for ln in body.splitlines()
                      if ln.startswith(("ser2net_open_fds ", "ser2net_fd_limit ")))
        assert set(gauges) == {"ser2net_open_fds", "ser2net_fd_limit"}, body
        assert 0 < int(gauges["ser2net_open_fds"]) < int(gauges["ser2net_fd_limit"]), gauges
        log = srv.output()
        assert "open files: limit" in log, log[-2000:]
    print("server: start-up log has the open-file limit, /metrics has both gauges  OK")


def test_loop_errors_reach_the_log():
    import asyncio

    from app import runtime

    lines = []

    async def main():
        loop = asyncio.get_running_loop()
        runtime._log_loop_errors(loop, lines.append)
        loop.call_exception_handler({"message": "socket.accept() out of system resource",
                                     "exception": OSError(24, "Too many open files")})
        loop.call_exception_handler({"message": "Exception in callback x()"})

    asyncio.run(main())
    assert lines == ["event loop: socket.accept() out of system resource (OSError: [Errno 24] "
                     "Too many open files)"], lines
    print("event loop errors go to all.log too (at most once a minute)  OK")


def test_windows():
    assert netguard.fd_limit() is None and netguard.open_fds() is None
    assert netguard.raise_fd_limit() == ""
    assert netguard.fd_budget_warning(AppConfig.from_dict({"mappings": []})) == ""
    cfg = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                      "pwd_version": 1}], "mappings": []}
    with Server(cfg) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        st, _, body = b.get("/metrics")
        assert st == 200 and "ser2net_up 1" in body and "ser2net_open_fds" not in body, body
    print("Windows: no open-file limit to raise or report; the server boots  OK")


if __name__ == "__main__":
    if os.name == "posix":
        test_raise_fd_limit()
        test_open_fds_and_limit()
        test_estimate_and_warning()
        test_loop_errors_reach_the_log()
        test_server_reports_limit()
    else:
        test_estimate_and_warning()
        test_windows()
    print("\nPASS: open-file limit raised, counted and reported")
