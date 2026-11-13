"""Smoke test of a distribution's package of ser2netweb, built from a recipe in packaging/
(CI: .github/workflows/distro-packages.yml), run as root in that distribution's container
after its package manager installed the package:

    python3 -I tests/distro_smoke.py --version 3.0.0 [--group uucp] [--init openrc] [--service]

  * ser2netweb --version says the version; --about lists pyserial-asyncio-fast as
    installed or as its built-in transport (most distributions don't package it);
  * the package's files: systemd's unit (ExecStart=/usr/bin/ser2netweb --data-dir
    /var/lib/ser2netweb, User=ser2netweb, SupplementaryGroups= the group of the serial
    ports), the sysusers.d and tmpfiles.d entries; OpenRC's /etc/init.d/ser2netweb and
    /etc/conf.d/ser2netweb;
  * the system account ser2netweb (a system uid, home /var/lib/ser2netweb, no login shell,
    OpenRC: in the group of the serial ports) and /var/lib/ser2netweb, 0700 and the
    account's, when the package made it (systemd's tmpfiles do where they run);
  * the package names no package ser2net (Provides, Conflicts, Replaces, Breaks,
    Obsoletes): the classic C daemon is unrelated and installs beside it;
  * as the account, in the group of the serial ports: --set-password admin into its data
    directory, a start on a free port of 127.0.0.1 that answers /healthz and
    --healthcheck, a clean stop on SIGTERM;
  * --service (OpenRC): rc-service ser2netweb start answers /healthz on 127.0.0.1:8080,
    rc-service ser2netweb stop ends it.
It starts a server: run it in a throwaway container, not on a machine of your own.
Standard library only (the distribution's python3).
"""
from __future__ import annotations

import argparse
import grp
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.request

EXE = "/usr/bin/ser2netweb"
DATA = "/var/lib/ser2netweb"
ACCOUNT = "ser2netweb"
UNIT = "/usr/lib/systemd/system/ser2netweb.service"


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    raise SystemExit(1)


def run(cmd: list[str], **kw) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)
    if p.returncode != 0:
        fail(f"{' '.join(cmd)}: exit {p.returncode}\n{p.stdout}{p.stderr}")
    return p.stdout


def check_commands(version: str) -> None:
    got = run([EXE, "--version"]).strip()
    if got != f"ser2netweb {version}":
        fail(f"--version says {got!r}")
    about = run([EXE, "--about"])
    print(about.rstrip())
    lines = [ln for ln in about.splitlines() if ln.split()[:1] == ["pyserial-asyncio-fast"]]
    if not lines:
        fail("--about doesn't list pyserial-asyncio-fast")
    line = lines[0]
    if "not installed" in line and "built in" not in line:
        fail(f"--about: {line.strip()}")
    print(f"{got}; --about: {line.strip()}  OK")


def check_systemd_files(group: str) -> None:
    with open(UNIT, encoding="utf-8") as fh:
        unit = fh.read()
    want = {"ExecStart": "/usr/bin/ser2netweb --data-dir /var/lib/ser2netweb",
            "User": ACCOUNT, "SupplementaryGroups": group, "StateDirectory": "ser2netweb"}
    for key, value in want.items():
        if not re.search(rf"(?m)^{key}={re.escape(value)}$", unit):
            fail(f"{UNIT} has no {key}={value}")
    for path, line in (("/usr/lib/sysusers.d/ser2netweb.conf",
                        f'u {ACCOUNT} - "ser2netweb serial-to-network bridge" {DATA}'),
                       ("/usr/lib/tmpfiles.d/ser2netweb.conf",
                        f"d {DATA} 0700 {ACCOUNT} {ACCOUNT} -")):
        with open(path, encoding="utf-8") as fh:
            if line not in fh.read().splitlines():
                fail(f"{path} has no line {line!r}")
    analyze = shutil.which("systemd-analyze")
    if analyze:
        p = subprocess.run([analyze, "verify", UNIT], capture_output=True, text=True,
                           timeout=120)
        # (in a container without systemd as PID 1 it may only complain about that)
        print(f"systemd-analyze verify: exit {p.returncode} {p.stderr.strip()[:500]}")
    print(f"{UNIT} (User={ACCOUNT}, SupplementaryGroups={group}), sysusers.d, tmpfiles.d  OK")


def check_openrc_files() -> None:
    with open("/etc/init.d/ser2netweb", encoding="utf-8") as fh:
        script = fh.read()
    if not os.access("/etc/init.d/ser2netweb", os.X_OK) or \
            not script.startswith("#!/sbin/openrc-run\n"):
        fail("/etc/init.d/ser2netweb is not an executable openrc-run script")
    for needle in ('command="/usr/bin/ser2netweb"', 'command_user="ser2netweb:ser2netweb"',
                   "SER2NETWEB_DATA_DIR:=/var/lib/ser2netweb"):
        if needle not in script:
            fail(f"/etc/init.d/ser2netweb has no {needle}")
    if not os.path.isfile("/etc/conf.d/ser2netweb"):
        fail("no /etc/conf.d/ser2netweb")
    print("/etc/init.d/ser2netweb (openrc-run, as ser2netweb), /etc/conf.d/ser2netweb  OK")


def check_account(group: str, openrc: bool) -> None:
    try:
        pw = pwd.getpwnam(ACCOUNT)
    except KeyError:
        fail(f"no account {ACCOUNT}")
    if pw.pw_uid == 0 or pw.pw_uid >= 1000:
        fail(f"{ACCOUNT} has uid {pw.pw_uid}, not a system account's")
    if pw.pw_dir != DATA:
        fail(f"{ACCOUNT}'s home is {pw.pw_dir}, not {DATA}")
    if not pw.pw_shell.endswith(("/nologin", "/false")):
        fail(f"{ACCOUNT}'s shell is {pw.pw_shell}")
    try:
        members = grp.getgrnam(group).gr_mem
    except KeyError:
        fail(f"no group {group} (the serial ports')")
    if openrc and ACCOUNT not in members:
        fail(f"{ACCOUNT} is not in the group {group} (OpenRC starts it with the account's groups)")
    print(f"account {ACCOUNT}: uid {pw.pw_uid}, home {pw.pw_dir}, shell {pw.pw_shell}; "
          f"group {group} exists  OK")
    if os.path.isdir(DATA):
        st = os.stat(DATA)
        if stat.S_IMODE(st.st_mode) != 0o700 or st.st_uid != pw.pw_uid:
            fail(f"{DATA} is {oct(stat.S_IMODE(st.st_mode))}, uid {st.st_uid}")
        print(f"{DATA}: 0700, the account's  OK")
    else:
        print(f"note: no {DATA} after the install (made at the service's start)")


def check_names() -> None:
    """No relation named to the classic ser2net's package."""
    if shutil.which("dpkg-query"):
        text = run(["dpkg-query", "-W", "-f=${Provides}\n${Conflicts}\n${Replaces}\n${Breaks}\n",
                    "ser2netweb"])
        names = re.findall(r"[\w.+-]+", re.sub(r"\([^)]*\)", "", text))
    elif shutil.which("rpm"):
        text = run(["rpm", "-q", "--provides", "--conflicts", "--obsoletes", "ser2netweb"])
        names = [ln.split()[0] for ln in text.splitlines() if ln.strip()]
    elif shutil.which("pacman"):
        text = run(["pacman", "-Qi", "ser2netweb"])
        names = [w for ln in text.splitlines()
                 if ln.split(":")[0].strip() in ("Provides", "Conflicts With", "Replaces")
                 for w in ln.split(":", 1)[1].split() if w != "None"]
    elif shutil.which("apk"):
        text = run(["apk", "info", "--provides", "--replaces", "ser2netweb"])
        names = [re.split(r"[=<>~]", w)[0] for ln in text.splitlines()
                 if ln.strip() and " " not in ln.strip() for w in ln.split()]
    else:
        fail("no package manager found")
    if "ser2net" in names:
        fail(f"the package names ser2net: {text}")
    print(f"names no package ser2net ({', '.join(sorted(set(names))) or 'nothing'})  OK")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def healthz(port: int, proc=None, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            fail(f"ser2netweb exited ({proc.returncode})")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
                if r.read().decode().strip() == "ok":
                    return
        except OSError:
            pass
        time.sleep(0.5)
    fail(f"no answer on 127.0.0.1:{port}/healthz in {timeout:.0f} s")


def check_start(group: str) -> None:
    pw = pwd.getpwnam(ACCOUNT)
    if not os.path.isdir(DATA):  # as the unit's StateDirectory= / start_pre would
        os.mkdir(DATA, 0o700)
        os.chown(DATA, pw.pw_uid, pw.pw_gid)
    env = {"PATH": "/usr/bin:/bin", "HOME": DATA, "LANG": "C.UTF-8"}
    as_account: dict = {"user": ACCOUNT, "group": grp.getgrgid(pw.pw_gid).gr_name,
                        "extra_groups": [group], "cwd": DATA}
    run([EXE, "--data-dir", DATA, "--set-password", "admin"], input="distro-smoke-pass-1\n",
        env=env, **as_account)
    if not os.path.isfile(os.path.join(DATA, "config.json")):
        fail(f"--set-password wrote no {DATA}/config.json")
    port = free_port()
    server_env = dict(env, SER2NETWEB_BIND_IP="127.0.0.1", SER2NETWEB_PORT=str(port))
    log_path = os.path.join("/tmp", "ser2netweb-smoke.log")
    with open(log_path, "w+", encoding="utf-8") as log:
        proc = subprocess.Popen([EXE, "--data-dir", DATA], stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, env=server_env,
                                **as_account)
        try:
            healthz(port, proc)
            run([EXE, "--data-dir", DATA, "--healthcheck"], env=env, **as_account)
            proc.terminate()
            code = proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)
            log.seek(0)
            text = log.read()
    print(text[-3000:].rstrip())
    if code not in (0, -15):
        fail(f"exit {code} after SIGTERM")
    st = os.stat(os.path.join(DATA, "config.json"))
    if st.st_uid != pw.pw_uid:
        fail("config.json doesn't belong to the account")
    print(f"as {ACCOUNT} (+{group}): --set-password, a start on 127.0.0.1:{port} answering "
          f"/healthz and --healthcheck, stopped by SIGTERM (exit {code})  OK")


def check_openrc_service() -> None:
    os.makedirs("/run/openrc", exist_ok=True)
    open("/run/openrc/softlevel", "a").close()  # (a container booted no runlevel)
    try:
        run(["rc-service", "ser2netweb", "start"])
        healthz(8080, timeout=60)
        p = subprocess.run(["rc-service", "ser2netweb", "status"], capture_output=True,
                           text=True, timeout=60)
        print(p.stdout.strip())
    finally:
        subprocess.run(["rc-service", "ser2netweb", "stop"], timeout=120)
    print("rc-service ser2netweb start: /healthz on 127.0.0.1:8080; stopped  OK")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", required=True, help="the version the package has")
    ap.add_argument("--group", default="dialout", help="the group of the serial ports")
    ap.add_argument("--init", choices=("systemd", "openrc"), default="systemd")
    ap.add_argument("--service", action="store_true", help="start it with rc-service too")
    args = ap.parse_args(argv)
    if os.geteuid() != 0:
        fail("run it as root, in a throwaway container")
    check_commands(args.version)
    if args.init == "systemd":
        check_systemd_files(args.group)
    else:
        check_openrc_files()
    check_account(args.group, args.init == "openrc")
    check_names()
    check_start(args.group)
    if args.service:
        check_openrc_service()
    print("\nPASS: the installed package")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
