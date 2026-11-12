"""A source install's ./lib follows the pins after an upgrade (bootstrap.py, ser2net.py).

lib/ is not in git, so `git pull` or new source unpacked over the old keeps the packages
an older release installed there; before 2.9 the bootstrap only checked that they import,
so an upgrade kept running the old versions (2.7's starlette and anyio included). Now an
install records what it was made for (lib/.ser2net-deps.json: the requirement lines, the
Python, the platform), and a server start refreshes a lib/ made for anything else, in
place and offline. Checked here, with the real pip and vendor/wheels in a scratch folder:
- a lib/ as 2.8 left it (no record, an old version's metadata and code) is refreshed:
  the pinned code is back, the old metadata gone, packages added by hand kept, and the
  record written; the next start does nothing;
- a refresh that fails (no wheels) keeps the old lib/, with a warning, and doesn't stop
  the start; a changed bundled wheel, a lib/ this account can't change and a lib/ another
  running process has loaded are not refreshed (warnings);
- one-off commands and --no-bootstrap only warn; no lib/ (a virtualenv) or an empty one
  is left alone; a comment-only change to requirements.txt refreshes nothing;
- `python3 bootstrap.py` reinstalls whatever is there, and refuses while lib/ is in use;
- ser2net.py: a server start refreshes, --about doesn't, the warnings reach the start-up
  log, an old lib/ that can't run this version stops the start with one line (2.8.1's
  code doesn't import with 2.7's starlette), and the Windows service loads its
  dependencies only after connecting to the Service Control Manager (a stop asked
  meanwhile is kept).

Run: python3 tests/test_lib_refresh.py
"""
import glob
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import _server  # noqa: F401  (puts the checkout on sys.path)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
_count = 0


def load_bootstrap(root: str, wheels: str | None = None):
    """A fresh copy of bootstrap.py working on <root>/lib with the checkout's pins."""
    global _count
    _count += 1
    spec = importlib.util.spec_from_file_location(f"_bootstrap_{_count}",
                                                  os.path.join(ROOT, "bootstrap.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROOT = root
    mod.LIB_DIR = os.path.join(root, "lib")
    mod.WHEELS_DIR = wheels or os.path.join(ROOT, "vendor", "wheels")
    mod.REQ_FILE = os.path.join(ROOT, "requirements.txt")
    return mod


def no_pip(*_a, **_k):
    raise AssertionError("pip must not run here")


def dist_infos(lib: str, project: str) -> list[str]:
    """The <project>-<version>.dist-info folders in lib/."""
    return sorted(n for n in os.listdir(lib) if n.endswith(".dist-info") and re.sub(
        r"[-_.]+", "-", n[: -len(".dist-info")].rsplit("-", 1)[0]).lower() == project)


def make_like_2_8(lib: str) -> None:
    """What an older release leaves: no record, an old starlette's metadata next to the
    current one and its code edited to stand for the old version; plus a package and a
    module added by hand."""
    os.remove(os.path.join(lib, ".ser2net-deps.json"))
    old = os.path.join(lib, "starlette-1.2.1.dist-info")
    os.makedirs(old)
    with open(os.path.join(old, "METADATA"), "w") as fh:
        fh.write("Metadata-Version: 2.1\nName: starlette\nVersion: 1.2.1\n")
    with open(os.path.join(lib, "starlette", "__init__.py"), "a") as fh:
        fh.write("\nOLD_RELEASE = True\n")
    os.makedirs(os.path.join(lib, "userpkg"))
    with open(os.path.join(lib, "userpkg", "__init__.py"), "w") as fh:
        fh.write("X = 1\n")
    os.makedirs(os.path.join(lib, "userpkg-1.0.dist-info"))
    with open(os.path.join(lib, "userpkg-1.0.dist-info", "METADATA"), "w") as fh:
        fh.write("Metadata-Version: 2.1\nName: userpkg\nVersion: 1.0\n")
    with open(os.path.join(lib, "usermod.py"), "w") as fh:
        fh.write("Y = 2\n")


def starlette_text(lib: str) -> str:
    with open(os.path.join(lib, "starlette", "__init__.py")) as fh:
        return fh.read()


class Scratch:
    """A scratch root with a lib/ installed from vendor/wheels (shared by the tests)."""
    root = ""

    @classmethod
    def lib(cls) -> str:
        if not cls.root:
            cls.root = tempfile.mkdtemp(prefix="s2n_lib_")
            bs = load_bootstrap(cls.root)
            started = time.monotonic()
            assert bs._pip_install(online=False), "offline install from vendor/wheels"
            print(f"(offline install into a scratch lib/: {time.monotonic() - started:.1f} s)")
            assert not bs.lib_is_stale() and bs.read_stamp() == bs.wanted_stamp()
        return os.path.join(cls.root, "lib")


def test_refresh_after_upgrade():
    lib = Scratch.lib()
    make_like_2_8(lib)
    bs = load_bootstrap(Scratch.root)
    assert bs.lib_is_stale(), "a lib/ without the record (2.8 and older) is refreshed"
    started = time.monotonic()
    bs.ensure(refresh_lib=True)
    took = time.monotonic() - started
    assert bs.notes == [], bs.notes
    assert "OLD_RELEASE" not in starlette_text(lib), "the pinned starlette is back"
    kept = dist_infos(lib, "starlette")
    assert len(kept) == 1 and kept[0] != "starlette-1.2.1.dist-info", kept
    for added in ("userpkg/__init__.py", "userpkg-1.0.dist-info/METADATA", "usermod.py"):
        assert os.path.exists(os.path.join(lib, added)), f"{added} added by hand must stay"
    assert bs.read_stamp() == bs.wanted_stamp() and not bs.lib_is_stale()
    with open(os.path.join(lib, ".ser2net-deps.json")) as fh:
        assert set(json.load(fh)) == {"requirements", "python", "platform"}
    # the next start: nothing to do
    bs = load_bootstrap(Scratch.root)
    bs._pip_install = no_pip
    bs.ensure(refresh_lib=True)
    print(f"a 2.8-era lib/ is refreshed in place ({took:.1f} s), old metadata gone, added "
          "packages kept; the next start does nothing  OK")


def _make_stale(root: str, value: str = "an older release") -> None:
    path = os.path.join(root, "lib", ".ser2net-deps.json")
    with open(path) as fh:
        stamp = json.load(fh)
    stamp["requirements"] = value
    with open(path, "w") as fh:
        json.dump(stamp, fh)


def test_failed_refresh_keeps_old_lib():
    lib = Scratch.lib()
    _make_stale(Scratch.root)
    with open(os.path.join(lib, "starlette", "__init__.py"), "a") as fh:
        fh.write("\nOLD_RELEASE = True\n")
    empty = tempfile.mkdtemp(prefix="s2n_nowheels_")
    try:
        bs = load_bootstrap(Scratch.root, wheels=empty)
        bs.ensure(refresh_lib=True)  # returns: the old lib/ still has every module
        assert any("refreshing ./lib failed" in n for n in bs.notes), bs.notes
        assert "OLD_RELEASE" in starlette_text(lib), "the old lib/ is kept as it was"
        assert bs.read_stamp()["requirements"] == "an older release", "no record written"
    finally:
        shutil.rmtree(empty, ignore_errors=True)
    print("a refresh that fails keeps the old lib/ and the start goes on, with a warning  OK")


def test_changed_wheel_not_installed():
    _make_stale(Scratch.root)
    wheels = tempfile.mkdtemp(prefix="s2n_badwheels_")
    try:
        src = sorted(glob.glob(os.path.join(ROOT, "vendor", "wheels", "*.whl")),
                     key=os.path.getsize)[0]
        shutil.copy(src, wheels)
        with open(os.path.join(wheels, "SHA256SUMS"), "w") as fh:
            fh.write(f"{hashlib.sha256(b'other').hexdigest()}  {os.path.basename(src)}\n")
        bs = load_bootstrap(Scratch.root, wheels=wheels)
        bs._pip_install = no_pip
        assert bs.refresh() is False
        assert any("don't match" in n for n in bs.notes), bs.notes
    finally:
        shutil.rmtree(wheels, ignore_errors=True)
    print("a changed bundled wheel: not installed, the old lib/ kept  OK")


def test_unwritable_lib_not_refreshed():
    if os.name == "nt" or os.geteuid() == 0:
        print("SKIP unwritable lib/: needs a POSIX non-root account")
        return
    _make_stale(Scratch.root)
    folder = os.path.join(Scratch.lib(), "starlette")
    mode = os.stat(folder).st_mode
    os.chmod(folder, 0o555)
    try:
        bs = load_bootstrap(Scratch.root)
        bs._pip_install = no_pip
        assert bs.refresh() is False
        assert any("can't change" in n and "starlette" in n for n in bs.notes), bs.notes
    finally:
        os.chmod(folder, mode)
    print("a lib/ this account can't change: not refreshed (nothing half replaced)  OK")


def test_only_a_server_start_refreshes():
    _make_stale(Scratch.root)
    bs = load_bootstrap(Scratch.root)
    bs._pip_install = no_pip
    bs.ensure(refresh_lib=False)
    assert any("next ser2net start" in n for n in bs.notes), bs.notes
    bs = load_bootstrap(Scratch.root)
    bs.warn_if_stale()
    assert any("--no-bootstrap" in n for n in bs.notes), bs.notes
    print("one-off commands and --no-bootstrap leave an out-of-date lib/ alone, with a warning  OK")


def test_no_or_empty_lib_untouched():
    root = tempfile.mkdtemp(prefix="s2n_nolib_")
    try:
        bs = load_bootstrap(root)
        bs._pip_install = no_pip
        bs.ensure(refresh_lib=True)  # the modules come from this environment
        assert not os.path.exists(os.path.join(root, "lib")), "no lib/ made"
        os.makedirs(os.path.join(root, "lib"))
        assert not bs.lib_is_stale(), "an empty lib/ is not one to refresh"
        bs.ensure(refresh_lib=True)
        assert os.listdir(os.path.join(root, "lib")) == []
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("no lib/ (a virtualenv) or an empty one: nothing installed  OK")


def test_record():
    root = tempfile.mkdtemp(prefix="s2n_req_")
    try:
        bs = load_bootstrap(root)
        with open(bs.REQ_FILE) as fh:
            text = fh.read()
        bs.REQ_FILE = os.path.join(root, "requirements.txt")
        with open(bs.REQ_FILE, "w") as fh:
            fh.write(text)
        base = bs.wanted_stamp()
        with open(bs.REQ_FILE, "w") as fh:
            fh.write("# a new comment\n" + text.replace("\n", "   # c\n", 3) + "\n\n")
        assert bs.wanted_stamp() == base, "comments and blank lines don't count"
        with open(bs.REQ_FILE, "w") as fh:
            fh.write(text.replace("uvicorn==", "uvicorn==9", 1))
        assert bs.wanted_stamp()["requirements"] != base["requirements"], "a pin counts"
        assert base["python"].startswith(sys.implementation.name) and base["platform"]
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"the record: the requirement lines, {base['python']}, {base['platform']}  OK")


def _holder(lib: str):
    """A process with an extension module of lib/ loaded (a running ser2net), or None
    when lib/ has none for this platform."""
    exts = [p for p in glob.glob(os.path.join(lib, "**", "*"), recursive=True)
            if p.endswith((".so", ".pyd"))]
    if not any(os.path.sep + "psutil" + os.path.sep in p for p in exts):
        return None
    p = subprocess.Popen([sys.executable, "-c",
                          "import sys; sys.path.insert(0, sys.argv[1]); import psutil; "
                          "print(psutil.__file__, flush=True); sys.stdin.read()", lib],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    loaded = p.stdout.readline()
    assert loaded.startswith(lib), f"psutil not loaded from the scratch lib/: {loaded!r}"
    return p


def test_lib_in_use_not_refreshed():
    if os.name != "nt" and not sys.platform.startswith("linux"):
        print("SKIP lib/ in use: only Windows and Linux can tell")
        return
    lib = Scratch.lib()
    _make_stale(Scratch.root)
    holder = _holder(lib)
    if holder is None:
        print("SKIP lib/ in use: no compiled psutil in lib/ for this platform")
        return
    try:
        bs = load_bootstrap(Scratch.root)
        bs._pip_install = no_pip
        assert bs.lib_in_use(), "the other process's loaded extension is seen"
        assert bs.refresh() is False
        assert any("not refreshing ./lib" in n and "another ser2net" in n for n in bs.notes)
        assert bs.main([]) == 1, "python3 bootstrap.py refuses while lib/ is in use"
    finally:
        holder.stdin.close()
        holder.wait(30)
    assert load_bootstrap(Scratch.root).lib_in_use() == "", "free again"
    print("a lib/ another process has loaded: not refreshed, bootstrap.py refuses  OK")


def test_bootstrap_py_reinstalls():
    lib = Scratch.lib()
    bs = load_bootstrap(Scratch.root)
    bs.ensure()  # whatever the tests before left: current again
    with open(os.path.join(lib, "starlette", "__init__.py"), "a") as fh:
        fh.write("\nEDITED = True\n")
    assert not bs.lib_is_stale(), "the record matches"
    assert bs.main([]) == 0
    assert "EDITED" not in starlette_text(lib), "python3 bootstrap.py installs again"
    print("python3 bootstrap.py reinstalls the pinned set whatever the record says  OK")


def test_ser2net_entry_points():
    import ser2net as cli
    from ser2netweb import runtime, standalone, winservice

    calls: list = []
    stub = SimpleNamespace(
        ensure=lambda online, refresh_lib: calls.append(("ensure", refresh_lib)),
        warn_if_stale=lambda: calls.append(("warn",)),
        notes=["WARNING: not refreshing ./lib: (test)"])
    saved = (sys.modules.get("bootstrap"), winservice.run, runtime.service_main)
    sys.modules["bootstrap"] = stub  # type: ignore[assignment]
    d = tempfile.mkdtemp(prefix="s2n_cli_")
    try:
        assert cli.main(["--about", "--data-dir", d]) == 0
        assert calls == [("ensure", False)], calls
        calls.clear()
        assert cli.main(["--about", "--no-bootstrap", "--data-dir", d]) == 0
        assert calls == [("warn",)], calls
        assert "WARNING: not refreshing ./lib: (test)" in standalone.notes, "to all.log"

        # --service: connect first (winservice.run), load the dependencies after that,
        # in the service's main; a stop before then is kept. The SCM's "running" callback
        # (on_ready) reaches runtime.when_ready once the runtime is loaded; stderr goes to
        # the data directory's service.log
        def ready():
            pass

        def fake_run(name, main, stop, log_path=None, on_ready=None):
            calls.append(("connected", name, log_path))
            on_ready(ready)
            stop()
            return main()

        def fake_service_main(path, name="ser2net"):
            calls.append(("serve", path, name, runtime._shutdown_requested.is_set(),
                          ready in runtime._ready_hooks))
            return 0

        # dependencies from an old lib/ that couldn't be refreshed and lack what this
        # version imports: one line that says what to do, not a traceback
        import ser2netweb
        stub.lib_is_stale = lambda: True
        saved_attr, saved_mod = ser2netweb.runtime, sys.modules["ser2netweb.runtime"]
        del ser2netweb.runtime
        sys.modules["ser2netweb.runtime"] = None  # type: ignore[assignment]  (import fails)
        try:
            cli._import_runtime()
            raise AssertionError("no SystemExit")
        except SystemExit as e:
            assert "older release" in str(e.code) and "bootstrap.py" in str(e.code), e.code
        finally:
            ser2netweb.runtime, sys.modules["ser2netweb.runtime"] = saved_attr, saved_mod
        stub.lib_is_stale = lambda: False
        try:  # not the old lib/'s fault: the error as it is
            del ser2netweb.runtime
            sys.modules["ser2netweb.runtime"] = None  # type: ignore[assignment]
            cli._import_runtime()
            raise AssertionError("no ImportError")
        except ImportError:
            pass
        finally:
            ser2netweb.runtime, sys.modules["ser2netweb.runtime"] = saved_attr, saved_mod

        calls.clear()
        winservice.run = fake_run
        runtime.service_main = fake_service_main
        assert cli.main(["--service", "--service-name", "plant-2", "--data-dir", d]) == 0
        assert calls == [("connected", "plant-2", os.path.join(d, winservice.SERVICE_LOG)),
                         ("ensure", True),
                         ("serve", os.path.join(d, "config.json"), "plant-2", True, True)], calls
    finally:
        if saved[0] is None:
            sys.modules.pop("bootstrap", None)
        else:
            sys.modules["bootstrap"] = saved[0]
        winservice.run, runtime.service_main = saved[1], saved[2]
        runtime._shutdown_requested.clear()
        runtime._ready_hooks.clear()
        standalone.notes.clear()
        shutil.rmtree(d, ignore_errors=True)
    print("ser2net.py: a start refreshes, --about doesn't; warnings reach the log; an old "
          "lib/ that can't run this version stops with one line; the service connects to "
          "the SCM before loading its dependencies  OK")


if __name__ == "__main__":
    saved_path = list(sys.path)
    try:
        test_record()
        test_no_or_empty_lib_untouched()
        test_refresh_after_upgrade()
        test_failed_refresh_keeps_old_lib()
        test_changed_wheel_not_installed()
        test_unwritable_lib_not_refreshed()
        test_only_a_server_start_refreshes()
        test_lib_in_use_not_refreshed()
        test_bootstrap_py_reinstalls()
        sys.path[:] = saved_path  # (the scratch lib/ out of the way)
        test_ser2net_entry_points()
    finally:
        sys.path[:] = saved_path
        if Scratch.root:
            shutil.rmtree(Scratch.root, ignore_errors=True)
    print("\nPASS: ./lib follows the pins")
