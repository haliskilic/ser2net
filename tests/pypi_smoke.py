"""Smoke test of the PyPI package (CI jobs "pypi package (build)" and "pypi package on ..."):
the sdist and the wheel `python -m build` wrote into DIST, then the wheel installed in a
fresh virtual environment.

    python tests/pypi_smoke.py DIST                    # the files, then install and run
    python tests/pypi_smoke.py DIST --contents-only    # the files in them only
    python tests/pypi_smoke.py DIST --pip-arg=--no-index --pip-arg=--find-links=vendor/wheels

The files: one sdist and one wheel of ser2netweb/__init__.py's version (as PEP 440 writes
it: 3.0.0-rc.1 is 3.0.0rc1). The wheel has exactly the package as this tree has it (its
templates, static files and translations too) and the licence files in its .dist-info, the
command ser2netweb = ser2netweb.cli:main, Requires-Python >=3.10 and the licence
AGPL-3.0-only; the sdist has the same package and nothing of the rest of the tree (no
tests, vendor/wheels, start scripts, executables).

Installed (dependencies from PyPI, or as --pip-arg says): --version and --about (a "Python
package (pip / pipx)"); --set-password without --data-dir writes config.json into the user
data folder the platform has ($XDG_DATA_HOME/ser2netweb, ~/Library/Application
Support/ser2netweb, %LOCALAPPDATA%\\ser2netweb; pointed at a temporary folder here) and
nothing into site-packages; a start with a temporary --data-dir on a free port answers
/healthz. It starts a server: run it on a CI runner, or locally inside a network namespace.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LICENCE_FILES = ["LICENSE", "NOTICE", "COMMERCIAL_LICENSE.md", "THIRD-PARTY-NOTICES.md"]
SDIST_EXTRA = {"PKG-INFO", "pyproject.toml", "setup.cfg", "MANIFEST.in",
               "packaging/README.pypi.md", *LICENCE_FILES}


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    raise SystemExit(1)


def raw_version() -> str:
    """ser2netweb/__init__.py's __version__ (what --version says)."""
    with open(os.path.join(ROOT, "ser2netweb", "__init__.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    return next(n.value.value for n in tree.body if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "") == "__version__"
                and isinstance(n.value, ast.Constant))


def version() -> str:
    """That version as PEP 440 normalises it, as the files have it (3.0.0-rc.1 -> 3.0.0rc1)."""
    try:
        from packaging.version import Version
    except ImportError:  # pip's copy
        from pip._vendor.packaging.version import Version
    return str(Version(raw_version()))


def source_files() -> set[str]:
    """The package's files in this tree (no bytecode)."""
    out = set()
    for folder, dirs, files in os.walk(os.path.join(ROOT, "ser2netweb")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if not name.endswith((".pyc", ".pyo")):
                rel = os.path.relpath(os.path.join(folder, name), ROOT)
                out.add(rel.replace(os.sep, "/"))
    return out


def npm_licences() -> list[str]:
    folder = os.path.join(ROOT, "tools", "licenses", "npm")
    return sorted(f"tools/licenses/npm/{n}" for n in os.listdir(folder) if n.endswith(".txt"))


def check_contents(dist: str) -> tuple[str, str]:
    v = version()
    names = sorted(os.listdir(dist))
    wheel_name, sdist_name = f"ser2netweb-{v}-py3-none-any.whl", f"ser2netweb-{v}.tar.gz"
    if names != sorted([wheel_name, sdist_name]):
        fail(f"{dist} holds {names}, not {sdist_name} and {wheel_name}")
    pkg = source_files()
    info = f"ser2netweb-{v}.dist-info"
    with zipfile.ZipFile(os.path.join(dist, wheel_name)) as z:
        files = set(z.namelist())
        metadata = z.read(f"{info}/METADATA").decode("utf-8")
        entry_points = z.read(f"{info}/entry_points.txt").decode("utf-8")
    licences = {f"{info}/licenses/{n}" for n in LICENCE_FILES + npm_licences()}
    own = {f"{info}/{n}" for n in ("METADATA", "WHEEL", "RECORD", "entry_points.txt",
                                   "top_level.txt")}
    if {f for f in files if f.startswith("ser2netweb/")} != pkg:
        fail(f"the wheel's package differs from this tree's: missing "
             f"{sorted(pkg - files)}, extra {sorted(f for f in files - pkg if f.startswith('ser2netweb/'))}")
    rest = files - pkg
    if rest != licences | own:
        fail(f"the wheel's other files: missing {sorted((licences | own) - rest)}, "
             f"extra {sorted(rest - licences - own)}")
    for line in ("Name: ser2netweb", f"Version: {v}", "Requires-Python: >=3.10",
                 "License-Expression: AGPL-3.0-only", "Description-Content-Type: text/markdown"):
        if line not in metadata.splitlines():
            fail(f"the wheel's METADATA has no {line!r}")
    if not re.search(r"(?m)^\[console_scripts\]\nser2netweb = ser2netweb\.cli:main$", entry_points):
        fail(f"no console script ser2netweb = ser2netweb.cli:main: {entry_points!r}")
    top = f"ser2netweb-{v}/"
    with tarfile.open(os.path.join(dist, sdist_name)) as t:
        members = {m.name for m in t.getmembers() if m.isfile()}
    if any(not m.startswith(top) for m in members):
        fail("an sdist file outside its folder")
    members = {m[len(top):] for m in members}
    extra = {m for m in members - pkg - SDIST_EXTRA - set(npm_licences())
             if not m.startswith("ser2netweb.egg-info/")}
    if extra or not pkg <= members:
        fail(f"the sdist: extra {sorted(extra)}, missing {sorted(pkg - members)}")
    print(f"{wheel_name}: the package ({len(pkg)} files: code, templates, static files, "
          f"translations), the licence files, ser2netweb = ser2netweb.cli:main; {sdist_name}: "
          "the same package and nothing else of the tree  OK")
    return os.path.join(dist, wheel_name), v


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(cmd: list[str], env: dict, stdin: str = "", cwd: str | None = None) -> str:
    # (errors="replace": a Windows console writes its own code page, cp1252 for "—")
    p = subprocess.run(cmd, env=env, input=stdin, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300, cwd=cwd)
    if p.returncode != 0:
        fail(f"{' '.join(cmd)} exited {p.returncode}:\n{p.stdout}\n{p.stderr}")
    return p.stdout


def install_and_run(wheel: str, v: str, pip_args: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="s2n_pypi_") as tmp:
        venv = os.path.join(tmp, "venv")
        subprocess.run([sys.executable, "-m", "venv", venv], check=True, timeout=300)
        bindir = os.path.join(venv, "Scripts" if os.name == "nt" else "bin")
        python = os.path.join(bindir, "python.exe" if os.name == "nt" else "python")
        exe = os.path.join(bindir, "ser2netweb.exe" if os.name == "nt" else "ser2netweb")
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        env = {k: v_ for k, v_ in os.environ.items()
               if not k.startswith(("SER2NETWEB_", "SER2NET_", "PYTHON"))}
        env.update(HOME=home, USERPROFILE=home, XDG_DATA_HOME=os.path.join(tmp, "xdg"),
                   LOCALAPPDATA=os.path.join(tmp, "local"))
        run([python, "-m", "pip", "install", "--disable-pip-version-check", *pip_args, wheel],
            env)
        # (from the temporary folder: nothing of this checkout on the path)
        got = run([exe, "--version"], env, cwd=tmp).strip()
        if got != f"ser2netweb {raw_version()}":
            fail(f"--version: {got}")
        about = run([exe, "--about"], env, cwd=tmp)
        if "Build: Python package (pip / pipx)" not in about.splitlines():
            fail(f"--about doesn't say pip:\n{about}")
        print(f"{got}; --about: Build: Python package (pip / pipx)  OK")

        # the default data folder: the platform's user data folder
        if sys.platform == "win32":
            want = os.path.join(env["LOCALAPPDATA"], "ser2netweb")
        elif sys.platform == "darwin":
            want = os.path.join(home, "Library", "Application Support", "ser2netweb")
        else:
            want = os.path.join(env["XDG_DATA_HOME"], "ser2netweb")
        run([exe, "--set-password", "admin"], env, stdin="pypi-smoke-pass-1\n", cwd=tmp)
        if not os.path.isfile(os.path.join(want, "config.json")):
            fail(f"--set-password wrote no {want}/config.json")
        site = run([python, "-I", "-c", "import os, ser2netweb; "
                    "print(os.path.dirname(os.path.dirname(ser2netweb.__file__)))"], env,
                   cwd=tmp).strip()
        if os.path.basename(site).lower() != "site-packages":
            fail(f"the package is in {site}, not a site-packages folder")
        for name in ("data", "lib"):
            if os.path.exists(os.path.join(site, name)):
                fail(f"something wrote {os.path.join(site, name)}")
        print(f"no --data-dir: config.json in {want} (the user data folder), nothing in "
              "site-packages  OK")

        # a real start
        data, port = os.path.join(tmp, "data"), free_port()
        senv = dict(env, SER2NETWEB_BIND_IP="127.0.0.1", SER2NETWEB_PORT=str(port))
        log = open(os.path.join(tmp, "server.log"), "w+", encoding="utf-8")
        proc = subprocess.Popen([exe, "--data-dir", data], env=senv, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, cwd=tmp)
        try:
            body, deadline = "", time.monotonic() + 90
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz",
                                                timeout=5) as r:
                        body = r.read().decode()
                    break
                except OSError:
                    time.sleep(0.5)
            if body.strip() != "ok":
                log.seek(0)
                fail(f"/healthz didn't answer ok (exit {proc.poll()}):\n{log.read()[-4000:]}")
        finally:
            if os.name == "nt":
                # the console-script launcher (ser2netweb.exe) runs python.exe as its child:
                # end the whole tree, or that child keeps data/all.log open
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=60)
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=30)
            log.close()
        if not os.path.isfile(os.path.join(data, "config.json")):
            fail(f"the start wrote no {data}/config.json")
        print(f"a start with --data-dir and port {port}: /healthz answered ok  OK")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dist", help="the folder python -m build wrote")
    ap.add_argument("--contents-only", action="store_true")
    ap.add_argument("--pip-arg", action="append", default=[],
                    help="for the pip install of the wheel (repeatable)")
    args = ap.parse_args(argv)
    wheel, v = check_contents(args.dist)
    if not args.contents_only:
        install_and_run(wheel, v, args.pip_arg)
    print("\nPASS: PyPI package")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
