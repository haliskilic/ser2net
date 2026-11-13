"""What keeps working under the names of 2.9.x and older, when ser2net became ser2netweb:

  * ser2net.py, the start script's old name: the same command line as ser2netweb.py
    (--version, --about, a server start), so systemd units, Windows services, copies of
    start.sh / start.bat and scripts that name it keep working; a server started with it
    says so once in its start-up log, other commands print nothing extra;
  * the SER2NET_* environment variables: each one the code reads (SER2NETWEB_* now) is
    still read under its old name, the new name wins when both are set (also set to "",
    which turns an old name's value off), and messages name the variable as it is set; a
    server and --healthcheck with only the old SER2NET_BIND_IP / SER2NET_PORT; nothing
    in ser2netweb/ reads one of them past the helper (envvars).

Run: python3 tests/test_old_names.py
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

from _server import ROOT, Server, free_port, wait_served

from ser2netweb import (
    __version__, accounts, cli, console, envvars, healthcheck, licences, optional, runtime,
    standalone, state, watchdog,
)
from ser2netweb.config import AdminUI, node_name
from ser2netweb.web import proxy


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, script, "--no-bootstrap", *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)


def test_old_script_runs_the_same():
    new, old = run("ser2netweb.py", "--version"), run("ser2net.py", "--version")
    assert new.returncode == old.returncode == 0, (new, old)
    assert new.stdout == old.stdout and new.stdout.split()[-1] == __version__, (new, old)
    new, old = run("ser2netweb.py", "--about"), run("ser2net.py", "--about")
    assert new.returncode == old.returncode == 0 and old.stdout == new.stdout, (new, old)
    assert old.stderr == "", old.stderr
    print("ser2net.py: --version / --about as ser2netweb.py, nothing extra printed  OK")


def test_old_script_note():
    note = cli.OLD_SCRIPT_NOTE.format(script="ser2net.py")
    assert "ser2net.py" in note and "ser2netweb.py" in note, note
    with Server({"mappings": []}, script="ser2net.py") as srv:
        out = srv.output()
    assert out.count(note) == 1, out[-3000:]
    with Server({"mappings": []}) as srv:
        out = srv.output()
    assert note not in out, out[-3000:]
    # in-process: the note is for the start-up log only, given by the old script alone
    standalone.notes.clear()
    d = tempfile.mkdtemp(prefix="s2n_old_")
    try:
        cli.main(["--about", "--no-bootstrap", "--data-dir", d], started_as="ser2net.py")
        assert standalone.notes == [note], standalone.notes
    finally:
        standalone.notes.clear()
        shutil.rmtree(d, ignore_errors=True)
    print("ser2net.py: a server it starts logs the old-name note once; ser2netweb.py none  OK")


def test_helper():
    env = {"SER2NET_PORT": " 9000 ", "SER2NETWEB_BIND_IP": "::", "SER2NET_BIND_IP": "0.0.0.0",
           "SER2NETWEB_NODE_NAME": "", "SER2NET_NODE_NAME": "old"}
    assert envvars.old_name("SER2NETWEB_PORT") == "SER2NET_PORT"
    assert envvars.lookup("SER2NETWEB_PORT", env) == ("SER2NET_PORT", " 9000 ")
    assert envvars.text("SER2NETWEB_PORT", env) == "9000"
    assert envvars.source("SER2NETWEB_PORT", env) == "SER2NET_PORT"
    assert envvars.get("SER2NETWEB_BIND_IP", environ=env) == "::", "the new name wins"
    assert envvars.source("SER2NETWEB_BIND_IP", env) == "SER2NETWEB_BIND_IP"
    assert envvars.get("SER2NETWEB_NODE_NAME", environ=env) == "", "set to '': still wins"
    assert envvars.get("SER2NETWEB_LOG_STDOUT", "on", env) == "on"
    assert envvars.lookup("SER2NETWEB_LOG_STDOUT", env) is None
    assert envvars.source("SER2NETWEB_LOG_STDOUT", env) == "SER2NETWEB_LOG_STDOUT"
    try:
        envvars.get("SER2NET_PORT", environ=env)
    except ValueError:
        pass
    else:
        raise AssertionError("an old name was taken for a new one")
    print("envvars: SER2NETWEB_X, else SER2NET_X, else the default; the new name wins  OK")


def test_every_variable_under_its_old_name():
    """Each variable the code reads, under its 2.9.x name, and both names together."""
    old = {"SER2NET_NODE_NAME": "line-1"}
    assert node_name(AdminUI(node_name="stored"), old) == "line-1"
    assert node_name(AdminUI(node_name="stored"), dict(old, SER2NETWEB_NODE_NAME="")) == "stored"
    assert runtime.node_name_notes({"SER2NET_NODE_NAME": "x" * 65})[0].startswith(
        "WARNING: SER2NET_NODE_NAME 'xxx"), "named as it is set"
    mirror = "https://git.example.com/s2n"
    assert licences.source_url({"SER2NET_SOURCE_URL": mirror}) == mirror
    assert licences.source_url({"SER2NET_SOURCE_URL": mirror,
                                "SER2NETWEB_SOURCE_URL": mirror + "-new"}) == mirror + "-new"
    assert "SER2NET_SOURCE_URL is not" in licences.source_url_note({"SER2NET_SOURCE_URL": "x"})
    admin = AdminUI()
    assert proxy.public_url(admin, {"SER2NET_PUBLIC_URL": "https://gw.example.com/"}) == \
        "https://gw.example.com"
    assert proxy.trusted_entries(admin, {"SER2NET_TRUSTED_PROXIES": "10.0.0.1"}) == \
        (["10.0.0.1"], "SER2NET_TRUSTED_PROXIES")
    assert proxy.trusted_entries(admin, {"SER2NET_TRUSTED_PROXIES": "10.0.0.1",
                                         "SER2NETWEB_TRUSTED_PROXIES": "10.0.0.2"}) == \
        (["10.0.0.2"], "SER2NETWEB_TRUSTED_PROXIES")
    assert state.log_stdout({"SER2NET_LOG_STDOUT": "off"}) == (False, "")
    assert state.log_stdout({"SER2NET_LOG_STDOUT": "off", "SER2NETWEB_LOG_STDOUT": "on"}) == \
        (True, "")
    assert watchdog.limit_from_env({"SER2NET_LOOP_WATCHDOG": "0"}) == (0.0, "")
    assert runtime.event_loop_choice("win32", {"SER2NET_WINDOWS_LOOP": "proactor"})[0] == \
        "proactor"
    assert accounts.provision_requested({"SER2NET_ADMIN_PASSWORD_FILE": "/run/secrets/pw"})
    assert console.reconfigure_hint("/data", environ={"SER2NET_BIND_IP": "0.0.0.0"}) == \
        ("env", "")
    # the ones read from os.environ itself
    with mock.patch.dict(os.environ, {"SER2NET_BIND_IP": "0.0.0.0", "SER2NET_PORT": "9001"}):
        for name in ("SER2NETWEB_BIND_IP", "SER2NETWEB_PORT"):
            os.environ.pop(name, None)
        a = AdminUI()
        assert runtime._env_bind_override(a) is True and (a.bind_ip, a.port) == ("0.0.0.0", 9001)
        os.environ["SER2NETWEB_PORT"] = "9002"
        a = AdminUI()
        assert runtime._env_bind_override(a) is True and a.port == 9002, "the new name wins"
        os.environ["SER2NET_BIND_IP"] = "eth0"
        try:
            runtime._env_bind_override(AdminUI())
        except runtime.ConfigError as e:
            assert str(e).startswith("SER2NET_BIND_IP must be an IP address"), e
        else:
            raise AssertionError("eth0 was taken")
    d = tempfile.mkdtemp(prefix="s2n_old_")
    try:
        cfg = os.path.join(d, "config.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": 8123}}, fh)
        with mock.patch.dict(os.environ, {"SER2NET_PORT": "7001"}):
            os.environ.pop("SER2NETWEB_PORT", None)
            assert healthcheck.target(cfg) == "http://127.0.0.1:7001/healthz"
        with mock.patch.dict(os.environ, {"SER2NET_LIB_OVERRIDE": d}):
            os.environ.pop("SER2NETWEB_LIB_OVERRIDE", None)
            problem = optional.problem(ModuleNotFoundError("x", name="ldap3"), "ldap3")
            assert (problem.override, problem.override_env) == (d, "SER2NET_LIB_OVERRIDE")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("every SER2NET_* variable still read; the SER2NETWEB_* name wins; messages name "
          "the one set  OK")


def test_server_with_old_names():
    """A server (the Docker image's and the systemd units' way: the address from the
    environment) and its --healthcheck with only SER2NET_BIND_IP / SER2NET_PORT, then with
    both names (the new one wins)."""
    srv = Server({"mappings": []})
    for name in ("SER2NETWEB_BIND_IP", "SER2NETWEB_PORT"):
        srv.env.pop(name, None)
    srv.env.update(SER2NET_BIND_IP="127.0.0.1", SER2NET_PORT=str(srv.port))
    with srv:
        p = subprocess.run([sys.executable, "ser2netweb.py", "--no-bootstrap", "--data-dir",
                            srv.data, "--healthcheck"], cwd=ROOT, env=srv.env,
                           capture_output=True, text=True, timeout=60)
        assert p.returncode == 0 and p.stdout.startswith("ok:"), p
    srv = Server({"mappings": []})
    srv.env.update(SER2NET_BIND_IP="127.0.0.1", SER2NET_PORT=str(free_port()))
    with srv:   # SER2NETWEB_BIND_IP / _PORT (Server's) win
        assert wait_served(srv.port)
    print("server + --healthcheck with SER2NET_BIND_IP / SER2NET_PORT only; with both, the "
          "SER2NETWEB_* address  OK")


def test_package_reads_through_the_helper():
    found = []
    for path in glob.glob(os.path.join(ROOT, "ser2netweb", "**", "*.py"), recursive=True):
        with open(path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if re.search(r"""(environ|getenv)\b.{0,20}["']SER2NET""", line):
                    found.append(f"{os.path.relpath(path, ROOT)}:{n}")
    assert not found, ("read a SER2NETWEB_* variable with envvars (also reads its old "
                       f"name): {found}")
    print("ser2netweb/ reads its variables through envvars only  OK")


def main():
    test_old_script_runs_the_same()
    test_old_script_note()
    test_helper()
    test_every_variable_under_its_old_name()
    test_server_with_old_names()
    test_package_reads_through_the_helper()
    print("\nPASS: the names of 2.9.x keep working")


if __name__ == "__main__":
    main()
