"""The console picker of the admin UI address (first start, --reconfigure,
--install-service) checks that the port is free.

A second instance on one machine was offered 8080 again and took it without a check; the
one that lost the port then failed to start its UI. Checks:
  * port_in_use: a listener on the address (or, on Linux, on 0.0.0.0) makes the port taken,
    as uvicorn's bind would find it; a closed one or another port doesn't; IPv6 too; ""
    (every interface, IPv4 and IPv6) is taken by a listener of either family, and named
    "*" in what the picker says;
  * with a terminal: a taken port is reported and the next free one offered as the
    default (Enter takes it); a taken port typed in is still taken, with a note (and a
    warning for the start-up log); a free port is offered as it is, without a word;
  * without a terminal (a service, a container's first start): the port stays, with a
    warning on the console and in the start-up log (prepare_config's notes).

Run: python3 tests/test_console_port_picker.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile

import _server  # noqa: F401  (puts the repo root and lib/ on sys.path)

from app import console, runtime
from app.config import AdminUI
from app.engine import netinfo


def listener(host: str = "127.0.0.1") -> socket.socket:
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    s.bind((host, 0))
    s.listen()
    return s


class Terminal:
    """sys.stdin that is a terminal, and input() answering from a list (prompts kept)."""

    def __init__(self, answers: list[str]) -> None:
        self.answers, self.prompts = list(answers), []

    def isatty(self) -> bool:
        return True

    def input(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0)


class NoTerminal:
    def isatty(self) -> bool:
        return False


@contextlib.contextmanager
def console_with(stdin, answers_from=None):
    import builtins

    out = io.StringIO()
    real = (sys.stdin, builtins.input, netinfo.list_ip_candidates)
    sys.stdin = stdin
    if answers_from is not None:
        builtins.input = answers_from.input
    netinfo.list_ip_candidates = lambda: [{"label": "127.0.0.1 (this machine only)",
                                           "value": "127.0.0.1"}]
    try:
        with contextlib.redirect_stdout(out):
            yield out
    finally:
        sys.stdin, builtins.input, netinfo.list_ip_candidates = real


def test_port_in_use() -> None:
    with listener() as held:
        port = held.getsockname()[1]
        assert console.port_in_use("127.0.0.1", port)
        free = console.next_free_port("127.0.0.1", port)
        assert free is not None and free > port and not console.port_in_use("127.0.0.1", free)
    assert not console.port_in_use("127.0.0.1", port), "closed: free again"
    if sys.platform.startswith("linux"):   # Windows lets a specific address share it
        with listener("0.0.0.0") as anywhere:
            assert console.port_in_use("127.0.0.1", anywhere.getsockname()[1])
    try:
        held6 = listener("::1")
    except OSError:
        print("SKIP: no IPv6 loopback here: the IPv6 check was not run")
    else:
        with held6:
            assert console.port_in_use("::1", held6.getsockname()[1])
    assert not console.port_in_use("192.0.2.1", port), "not this machine's: not this check's"
    # "" (every interface of both families: the console's "IPv4 + IPv6" choice)
    with listener("0.0.0.0") as anywhere:
        assert console.port_in_use("", anywhere.getsockname()[1])
    assert not console.port_in_use("", port)
    try:   # IPv6 only: '' is taken by the :: half alone
        held6 = socket.socket(socket.AF_INET6)
        held6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        held6.bind(("::", 0))
        held6.listen()
    except OSError:
        print("SKIP: no IPv6 here: '' was checked against IPv4 only")
    else:
        with held6:
            assert not console.port_in_use("0.0.0.0", held6.getsockname()[1])
            assert console.port_in_use("", held6.getsockname()[1])
    assert console.in_use_warning("", port).startswith(f"WARNING: port {port} on * is in use")
    print("port_in_use: a listener on the address (and 0.0.0.0 on Linux) takes it, a closed "
          "one doesn't; IPv6; '' by either family; next_free_port  OK")


def test_interactive() -> None:
    with listener() as held:
        port = held.getsockname()[1]
        current = AdminUI(bind_ip="127.0.0.1", port=port)
        # Enter: the next free port
        term, notes = Terminal(["1", ""]), []
        with console_with(term, term) as out:
            ip, chosen = console.choose_admin_bind(current, notes)
        assert ip == "127.0.0.1" and chosen != port and not console.port_in_use(ip, chosen)
        assert f"Port {port} on 127.0.0.1 is in use" in out.getvalue(), out.getvalue()
        assert f"{chosen} is free" in out.getvalue() and term.prompts[-1] == \
            f"Web UI TCP port [{chosen}]: ", term.prompts
        assert notes == [], notes
        # the taken port typed in: kept, with a note and a warning for the log
        term, notes = Terminal(["1", str(port)]), []
        with console_with(term, term) as out:
            assert console.choose_admin_bind(current, notes) == ("127.0.0.1", port)
        assert f"NOTE: port {port} on 127.0.0.1 is in use right now" in out.getvalue()
        assert notes == [console.in_use_warning("127.0.0.1", port)], notes
    # a free port: offered as it is, nothing said
    term, notes = Terminal(["1", ""]), []
    with console_with(term, term) as out:
        assert console.choose_admin_bind(AdminUI(bind_ip="127.0.0.1", port=port), notes) == \
            ("127.0.0.1", port)
    assert "in use" not in out.getvalue() and notes == [] and term.prompts[-1] == \
        f"Web UI TCP port [{port}]: "
    print("with a terminal: a taken port reported, the next free one offered; a taken one "
          "typed in kept with a note; a free one as it is  OK")


def test_without_terminal() -> None:
    with listener() as held:
        port = held.getsockname()[1]
        notes: list[str] = []
        with console_with(NoTerminal()) as out:
            assert console.choose_admin_bind(AdminUI(port=port), notes) == ("127.0.0.1", port)
        warning = console.in_use_warning("127.0.0.1", port)
        assert notes == [warning] and warning in out.getvalue(), (notes, out.getvalue())
        assert warning.startswith(f"WARNING: port {port} on 127.0.0.1 is in use") and \
            "SER2NET_PORT" in warning
        # what a start without a terminal logs (--reconfigure here; a first run alike)
        tmp = tempfile.mkdtemp(prefix="s2n-picker-")
        saved = {k: os.environ.pop(k) for k in ("SER2NET_BIND_IP", "SER2NET_PORT")
                 if k in os.environ}
        try:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": port}}, fh)
            with console_with(NoTerminal()):
                _store, config = runtime.prepare_config(path, reconfigure=True)
            assert warning in config.load_notes and config.admin_ui.port == port, \
                config.load_notes
        finally:
            os.environ.update(saved)
            shutil.rmtree(tmp, ignore_errors=True)
    with console_with(NoTerminal()) as out:
        notes = []
        console.choose_admin_bind(AdminUI(port=port), notes)
    assert notes == [] and "in use" not in out.getvalue()
    print("without a terminal: the port stays, with a warning on the console and in the "
          "start-up log  OK")


def main() -> None:
    test_port_in_use()
    test_interactive()
    test_without_terminal()
    print("\nPASS: admin UI port picker")


if __name__ == "__main__":
    main()
