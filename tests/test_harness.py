"""The test harness itself: tests/run_all.py and the helpers the tests share.

Checked:
  * every tests/test_*.py is in run_all's PORTABLE or SOCAT list, once (a file in neither
    never runs), and every listed file exists; a listed file that is gone fails the run;
  * a test that runs past the timeout is killed with everything it started (servers,
    socat): run_all kills its process group (POSIX) or its process tree (Windows
    taskkill /T), so nothing is left holding ports, devices or the output pipes;
  * _server.Server cleans up (the process, its temp folder) when its start fails, and
    show_log_on_failure prints the server's log only while a test fails;
  * no test starts the server with its output on a pipe it never reads;
  * no test listens on a fixed port in 1024-65535 (free_port() / free_udp_port() / port 0
    instead), unless a "# fixed port: <why>" comment says why (fixed_ports below).

Run: python3 tests/test_harness.py
"""
import ast
import contextlib
import io
import os
import re
import tempfile
import time

import run_all
from _server import Server, show_log_on_failure

HERE = os.path.dirname(os.path.abspath(__file__))


def test_every_test_is_registered():
    listed = run_all.PORTABLE + run_all.SOCAT
    twice = sorted({n for n in listed if listed.count(n) > 1})
    assert not twice, f"listed more than once in tests/run_all.py: {twice}"
    files = sorted(n for n in os.listdir(HERE) if n.startswith("test_") and n.endswith(".py"))
    missing = [n for n in files if n not in listed]
    assert not missing, f"never run (add them to PORTABLE or SOCAT in tests/run_all.py): {missing}"
    gone = [n for n in listed if n not in files]
    assert not gone, f"listed in tests/run_all.py but not there: {gone}"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert run_all.run_one("test_this_file_is_not_there.py") is False
    assert out.getvalue().startswith("FAIL  test_this_file_is_not_there.py  (missing"), out.getvalue()
    print(f"run_all: all {len(files)} test files listed once; a listed file that is gone fails  OK")


# a "test" that starts a helper process (as a test starts a server) and then hangs; the
# helper touches its heartbeat file until it is killed
_HANGS = """
import os, subprocess, sys, time
beat = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beat")
subprocess.Popen([sys.executable, "-c",
    "import sys, time\\nwhile True:\\n    open(sys.argv[1], 'a').close(); "
    "__import__('os').utime(sys.argv[1]); time.sleep(0.1)", beat])
print("started the helper", flush=True)
time.sleep(600)
"""


def test_timeout_kills_what_the_test_started():
    d = tempfile.mkdtemp(prefix="s2n_harness_")
    try:
        script, beat = os.path.join(d, "hangs.py"), os.path.join(d, "beat")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(_HANGS)
        t0 = time.monotonic()
        code, out, _err = run_all.run_test(script, timeout=5)
        assert code is None and "started the helper" in out, (code, out)
        assert time.monotonic() - t0 < 60, "collecting the output waited for the helper"
        assert os.path.exists(beat), "the helper never ran"
        time.sleep(1.0)
        before = os.path.getmtime(beat)
        time.sleep(1.5)
        assert os.path.getmtime(beat) == before, "the helper the test started still runs"
    finally:
        for _ in range(20):  # (on Windows the helper's file may take a moment to let go)
            try:
                for name in os.listdir(d):
                    os.remove(os.path.join(d, name))
                os.rmdir(d)
                break
            except OSError:
                time.sleep(0.2)
    print("run_all: a test past its timeout is killed with what it started, its output kept  OK")


class _FailsAfterStart(Server):
    def start(self):
        super().start()
        raise AssertionError("a start that fails after the process is up")


def test_server_cleans_up_a_failed_start():
    srv = _FailsAfterStart({})
    try:
        with srv:
            raise RuntimeError("the with block must not run")
    except AssertionError:
        pass
    assert srv.proc is None, "the server process was left running"
    assert not os.path.exists(srv.tmp), "the server's temp folder was left behind"

    d = tempfile.mkdtemp(prefix="s2n_harness_")
    with open(os.path.join(d, "server.log"), "w", encoding="utf-8") as fh:
        fh.write("the server's last words\n")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        show_log_on_failure(d)  # nothing fails here
    assert out.getvalue() == "", out.getvalue()
    with contextlib.redirect_stdout(out), contextlib.suppress(AssertionError):
        try:
            raise AssertionError("a failing test")
        finally:
            show_log_on_failure(d)
    assert "the server's last words" in out.getvalue(), out.getvalue()
    os.remove(os.path.join(d, "server.log"))
    os.rmdir(d)
    print("_server: a failed start stops the server and removes its folder; the log is "
          "shown when a test fails  OK")


def _is_pipe(node) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "PIPE"


def test_no_unread_server_pipes():
    """A server's output on a pipe nobody reads blocks the server once the pipe is full."""
    found = []
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(HERE, name), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and getattr(call.func, "attr", getattr(
                    call.func, "id", "")) in ("Popen", "create_subprocess_exec")):
                continue
            runs_server = any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                              and n.value.endswith("ser2net.py") for n in ast.walk(call))
            piped = [k.arg for k in call.keywords if k.arg in ("stdout", "stderr")
                     and _is_pipe(k.value)]
            if runs_server and piped:
                found.append(f"{name}:{call.lineno} ({', '.join(piped)}=PIPE)")
    assert not found, ("a server started with its output on a pipe (use _server.Server or "
                       f"_server.server_log): {found}")
    print("no test starts the server with its output on an unread pipe  OK")


# What makes a test function one that listens: it starts mappings, a server or a process
# (which may listen) itself.
_STARTS = re.compile(r"\b(apply_mapping|Server|Popen|create_subprocess_exec|start_server|"
                     r"create_datagram_endpoint|start_all)\(|\.start\(|ser2net\.py")
# where a port number is given: a mapping's network / the admin UI / the cluster's discovery,
# a function parameter or keyword named like a port, a variable named PORT / UI / ..._PORT
_PORT_KEYS = {"port", "network_port", "discovery_port", "SER2NET_PORT"}
_NETWORK_KEYS = {"mode", "protocol", "bind_ip", "max_connections", "tls", "kick_old_user",
                 "udp_peer_host", "require_login"}
_PORT_NAME = re.compile(r"(?i)(^|_)(port|ui)(_|$)|port$|^ui")
_LISTEN = re.compile(r"(?:TCP|UDP)\d?-LISTEN:(\d+)")


def _number(node):
    """The port number a literal (or str(literal), or literal + i) gives, if in 1024-65535."""
    if isinstance(node, ast.Constant) and not isinstance(node.value, bool):
        v = node.value
        n = v if isinstance(v, int) else int(v) if isinstance(v, str) and v.isdigit() else None
        return n if n is not None and 1024 <= n <= 65535 else None
    if isinstance(node, ast.BinOp):
        return _number(node.left) or _number(node.right)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "str" and node.args):
        return _number(node.args[0])
    return None


def fixed_ports(path: str) -> list[str]:
    """Fixed port numbers a test listens on, as "file:line: what": in a function that starts
    something (or one whose result such a function uses, a config builder), or at module
    level in a file that starts something. Exempt: a "# fixed port: <why>" comment on the
    line, on the statement it is in, or in the comment lines right above that statement."""
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    if not _STARTS.search(src):
        return []
    tree, lines = ast.parse(src), src.splitlines()
    parent = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    defs = {n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    params = {name: [a.arg for a in f.args.posonlyargs + f.args.args] for name, f in defs.items()}
    direct = {name for name, f in defs.items() if _STARTS.search(ast.get_source_segment(src, f))}
    starting = set(direct)
    while True:  # ... and what calls those
        more = {name for name, f in defs.items() if name not in starting and any(
            isinstance(c, ast.Call) and getattr(c.func, "id", None) in starting
            for c in ast.walk(f))}
        if not more:
            break
        starting |= more
    feeding = {c.func.id for name in direct for c in ast.walk(defs[name])
               if isinstance(c, ast.Call) and getattr(c.func, "id", None) in defs
               and not isinstance(parent.get(c), ast.Expr)}
    constants = {n.targets[0].id: n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                 and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                 and _number(n.value)}
    hits = []

    def port_given(value, what):
        if _number(value):
            hits.append((value, f"{what} {_number(value)}"))
        for n in ast.walk(value):  # a constant used as the port: report its assignment
            if isinstance(n, ast.Name) and n.id in constants:
                hits.append((constants[n.id], f"{n.id} = {_number(constants[n.id].value)}"))

    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and _PORT_NAME.search(t.id) and _number(n.value):
                    hits.append((n, f"{t.id} = {_number(n.value)}"))
        elif isinstance(n, ast.Dict):
            keys = {k.value for k in n.keys if isinstance(k, ast.Constant)}
            outer = parent.get(n)
            network = bool(keys & _NETWORK_KEYS) or (isinstance(outer, ast.Dict) and any(
                v is n and isinstance(k, ast.Constant) and k.value in ("network", "admin_ui")
                for k, v in zip(outer.keys, outer.values)))
            for k, v in zip(n.keys, n.values):
                if (isinstance(k, ast.Constant) and k.value in _PORT_KEYS
                        and (network or k.value != "port")):
                    port_given(v, f"{k.value!r}:")
        elif isinstance(n, ast.Call):
            for kw in n.keywords:
                if kw.arg in _PORT_KEYS:
                    port_given(kw.value, f"{kw.arg}=")
            names = params.get(getattr(n.func, "id", None), [])
            for name, arg in zip(names, n.args):
                if "port" in name.lower():
                    port_given(arg, f"{n.func.id}({name}=)")
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = n.args.posonlyargs + n.args.args
            for a, d in zip(args[len(args) - len(n.args.defaults):], n.args.defaults):
                if "port" in a.arg.lower() and _number(d):
                    hits.append((d, f"def {n.name}({a.arg}={_number(d)})"))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            for m in _LISTEN.finditer(n.value):
                if 1024 <= int(m.group(1)) <= 65535:
                    hits.append((n, m.group(0)))

    def listens(node):
        func = None
        while node in parent:
            node = parent[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = node  # the outermost one
        return func is None or func.name in starting or func.name in feeding

    def explained(node):
        stmt = node
        while not isinstance(stmt, ast.stmt) and stmt in parent:
            stmt = parent[stmt]
        first = getattr(stmt, "lineno", node.lineno)
        span = lines[first - 1:node.lineno]
        i = first - 2
        while i >= 0 and lines[i].lstrip().startswith("#"):
            span.append(lines[i])
            i -= 1
        return any("# fixed port:" in ln for ln in span)

    name = os.path.basename(path)
    return sorted({f"{name}:{node.lineno}: {what}" for node, what in hits
                   if listens(node) and not explained(node)})


def test_no_fixed_listening_ports():
    """A fixed port in the ephemeral range (Linux 32768-60999, Windows 49152-65535) can still
    be held by an earlier test's closed client socket (TIME_WAIT): the bind then fails, even
    with SO_REUSEADDR; below it, another program may hold it (a database on 8086, say)."""
    found = [hit for name in sorted(os.listdir(HERE)) if name.endswith(".py")
             for hit in fixed_ports(os.path.join(HERE, name))]
    assert not found, ("a test listens on a fixed port; use _server.free_port() / "
                       "free_udp_port() or port 0, or explain it with a '# fixed port: <why>' "
                       "comment:\n  " + "\n  ".join(found))
    # the check finds what it is meant to find
    d = tempfile.mkdtemp(prefix="s2n_harness_")
    sample = os.path.join(d, "test_sample.py")
    with open(sample, "w", encoding="utf-8") as fh:
        fh.write('PORT = 47101\n'
                 'UI = 8085  # fixed port: the stub below only prints it\n'
                 'def start(dev, port):\n'
                 '    return sup.apply_mapping({"network": {"bind_ip": "127.0.0.1", "port": port}})\n'
                 'def config(n):\n'
                 '    return {"network": {"mode": "server", "port": 4000 + n}}\n'
                 'def test_a():\n'
                 '    start("/dev/x", 47303)\n'
                 '    start("/dev/y", PORT)\n'
                 '    Server(config(1))\n'
                 '    # fixed port: a stub peer reports it, nothing listens there\n'
                 '    peer = {"network": {"mode": "server",\n'
                 '                        "port": 4555}}\n'
                 'def test_b():\n'
                 '    return {"network": {"mode": "server", "port": 5000}}  # only validated\n')
    got = fixed_ports(sample)
    os.remove(sample)
    os.rmdir(d)
    assert got == ["test_sample.py:1: PORT = 47101", "test_sample.py:6: 'port': 4000",
                   "test_sample.py:8: start(port=) 47303"], got
    print("no test listens on a fixed port without saying why (free_port / port 0)  OK")


if __name__ == "__main__":
    test_every_test_is_registered()
    test_timeout_kills_what_the_test_started()
    test_server_cleans_up_a_failed_start()
    test_no_unread_server_pipes()
    test_no_fixed_listening_ports()
    print("\nPASS: the test harness (run_all, shared helpers)")
