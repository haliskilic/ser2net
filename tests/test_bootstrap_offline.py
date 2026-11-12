"""The offline first run works on every machine Python 3.10+ runs on (bootstrap.py).

psutil is the one pinned package that isn't pure Python, and vendor/wheels has it for
Linux x86_64 and Windows x64 only. It used to be required, so `pip install --no-index`
failed as a whole on ARM Linux (32-bit Raspberry Pi OS included), macOS, musl and ARM or
32-bit Windows Python, and ser2net never started there without the network. Checked here,
with the real pip and vendor/wheels in a scratch folder:
- without a psutil wheel for this machine the set installs without it, in one pip run,
  with one line saying so; everything required imports and the next start does nothing;
- when the whole set fails although a psutil wheel seemed to fit, it is tried once more
  without psutil; a set that fails without psutil too still fails, with the old message;
- whether a bundled wheel fits this Python follows pip's own tag rules;
- a Python without pip gets the command that installs it (not "re-run with --online");
- ser2net.py and bootstrap.py parse on old Pythons and stop with "needs Python 3.10"
  (RHEL 9's 3.9, RHEL 8's 3.6 failed with a TypeError / SyntaxError);
- start.sh takes python3 when it is 3.10+, else the newest python3.1x, else says so;
- --about lists psutil.

Run: python3 tests/test_bootstrap_offline.py
"""
import ast
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile

import _server  # noqa: F401  (puts the checkout on sys.path)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
WHEELS = os.path.join(ROOT, "vendor", "wheels")
_count = 0


def load_bootstrap(root: str, wheels: str):
    """A fresh copy of bootstrap.py working on <root>/lib with the checkout's pins."""
    global _count
    _count += 1
    spec = importlib.util.spec_from_file_location(f"_bootstrap_offline_{_count}",
                                                  os.path.join(ROOT, "bootstrap.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROOT = root
    mod.LIB_DIR = os.path.join(root, "lib")
    mod.WHEELS_DIR = wheels
    mod.REQ_FILE = os.path.join(ROOT, "requirements.txt")
    return mod


def wheels_without(root: str, *projects: str) -> str:
    """vendor/wheels as links in <root>/wheels, without the wheels of `projects` (as on a
    machine with no wheel of theirs: the bundled ones are for other platforms)."""
    out = os.path.join(root, "wheels")
    os.makedirs(out)
    for name in os.listdir(WHEELS):
        if name.split("-", 1)[0].lower() in projects:
            continue
        src = os.path.join(WHEELS, name)
        try:
            os.symlink(src, os.path.join(out, name))
        except OSError:  # Windows without the symlink privilege
            shutil.copy(src, out)
    return out


class Output:
    """stdout and stderr of the bootstrap (pip's own output goes to the real ones)."""

    def __enter__(self):
        self.out, self.err = io.StringIO(), io.StringIO()
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(contextlib.redirect_stdout(self.out))
        self._stack.enter_context(contextlib.redirect_stderr(self.err))
        return self

    def __exit__(self, *exc):
        self._stack.close()
        sys.stdout.write(self.out.getvalue())
        sys.stdout.write(self.err.getvalue())
        return False

    @property
    def text(self) -> str:
        return self.out.getvalue() + self.err.getvalue()


def pip_runs(bs) -> list:
    """Count the pip runs of `bs` (they still run)."""
    calls: list = []
    real = bs.subprocess.run

    def run(cmd, *a, **kw):
        if "pip" in cmd:
            calls.append(cmd)
        return real(cmd, *a, **kw)

    bs.subprocess = type("sp", (), {"run": staticmethod(run)})
    return calls


def lib_has(lib: str, project: str) -> bool:
    return any(n.lower().startswith(project + "-") and n.endswith(".dist-info")
               for n in os.listdir(lib)) or os.path.isdir(os.path.join(lib, project))


def missing_in_lib(bs) -> list[str]:
    """The required modules lib/ itself doesn't have (whatever else this Python has)."""
    return [module for module, _pip in bs.REQUIRED
            if importlib.machinery.PathFinder.find_spec(module, [bs.LIB_DIR]) is None]


def test_installs_without_psutil():
    root = tempfile.mkdtemp(prefix="s2n_boot_")
    try:
        bs = load_bootstrap(root, wheels_without(root, "psutil"))
        assert not bs._wheel_at_hand("psutil")
        calls = pip_runs(bs)
        with Output() as out:
            bs.install()
        assert len(calls) == 1, f"one pip run, not a failing one first: {calls}"
        assert not any("psutil" in " ".join(c) for c in calls)
        assert "installed without psutil (optional" in out.text, out.text
        assert "network interfaces are listed with Python's standard library" in out.text
        assert "ERROR" not in out.text and "failed" not in out.text, out.text
        lib = os.path.join(root, "lib")
        assert not lib_has(lib, "psutil") and lib_has(lib, "starlette")
        assert missing_in_lib(bs) == [], missing_in_lib(bs)
        assert importlib.machinery.PathFinder.find_spec("psutil", [lib]) is None
        assert not bs.lib_is_stale() and bs.read_stamp() == bs.wanted_stamp()
        # the next start: lib/ is up to date and has what is required (psutil is not
        # asked for again), so nothing is installed
        again = load_bootstrap(root, os.path.join(root, "wheels"))
        calls = pip_runs(again)
        again.ensure(refresh_lib=True)
        assert calls == [], calls
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("no psutil wheel for this machine: installed without it in one run, one line "
          "says so, the next start does nothing  OK")


def test_retried_without_psutil():
    """A psutil wheel that seemed to fit, but the set doesn't install with it: once more
    without it. A set that doesn't install without it either fails as before."""
    root = tempfile.mkdtemp(prefix="s2n_boot_")
    try:
        wheels = wheels_without(root, "psutil")
        bs = load_bootstrap(root, wheels)
        bs._wheel_at_hand = lambda project: True
        calls = pip_runs(bs)
        with Output() as out:
            bs.install()
        assert len(calls) == 2, calls
        assert "trying again without psutil (optional)" in out.text, out.text
        assert "installed without psutil" in out.text, out.text
        assert missing_in_lib(bs) == [], missing_in_lib(bs)

        for name in os.listdir(wheels):
            if name.startswith("starlette-"):
                os.remove(os.path.join(wheels, name))
        shutil.rmtree(os.path.join(root, "lib"))
        bs = load_bootstrap(root, wheels)
        calls = pip_runs(bs)
        with Output() as out:
            try:
                bs.install()
            except SystemExit as e:
                assert e.code == 1, e.code
            else:
                raise AssertionError("an install without starlette's wheel succeeded")
        assert len(calls) == 1, calls  # psutil was left out from the start: no retry
        assert "offline install failed" in out.text, out.text
        assert "installed without" not in out.text, out.text
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("a set that fails with psutil is tried once more without it; one that fails "
          "without it too says so as before  OK")


def test_wheel_at_hand():
    root = tempfile.mkdtemp(prefix="s2n_boot_")
    try:
        bs = load_bootstrap(root, WHEELS)
        from pip._vendor.packaging.tags import sys_tags

        tags = {str(t) for t in sys_tags()}
        bundled = any("-abi3-" in t and t.endswith(("manylinux_2_28_x86_64", "win_amd64"))
                      for t in tags)
        assert bs._wheel_at_hand("psutil") == bundled, (bundled, sorted(tags)[:3])
        assert bs._wheel_at_hand("starlette"), "a py3-none-any wheel fits every Python"
        other = os.path.join(root, "other")
        os.makedirs(other)
        for name in ("psutil-7.2.2-cp36-abi3-macosx_99_0_riscv64.whl",
                     "psutil-7.2.2-cp36-abi3-win_unknownarch.whl", "not-a-wheel.whl"):
            open(os.path.join(other, name), "w").close()
        bs.WHEELS_DIR = other
        assert not bs._wheel_at_hand("psutil"), "another platform's wheel doesn't fit"
        bs.WHEELS_DIR = os.path.join(root, "missing")
        assert bs._wheel_at_hand("psutil"), "can't tell: leave it to pip"
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"bundled psutil wheel fits this Python: {bundled} (pip's tag rules)  OK")


def test_requirements_without():
    root = tempfile.mkdtemp(prefix="s2n_boot_")
    try:
        bs = load_bootstrap(root, WHEELS)
        path = bs._requirements_without(["psutil"])
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        finally:
            os.remove(path)
        assert not any(line.lower().startswith("psutil") for line in lines), lines
        assert 'starlette==1.7.0 ; python_version < "3.11"' in lines, lines
        assert "typing_extensions==4.16.0" in lines and "markupsafe==3.0.4" in lines
        assert all(line and not line.startswith("#") for line in lines), lines
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("requirements.txt minus psutil keeps every other pin and its marker  OK")


def test_no_pip():
    root = tempfile.mkdtemp(prefix="s2n_boot_")
    real = importlib.util.find_spec
    importlib.util.find_spec = lambda name, *a: None if name == "pip" else real(name, *a)
    try:
        bs = load_bootstrap(root, WHEELS)
        calls = pip_runs(bs)
        with Output() as out:
            try:
                bs.install()
            except SystemExit as e:
                assert e.code == 1, e.code
            else:
                raise AssertionError("install() went on without pip")
        assert calls == [], calls
        assert "has no pip" in out.text and "sudo apt install python3-pip" in out.text, out.text
        assert "--online" not in out.text and "offline install failed" not in out.text
    finally:
        importlib.util.find_spec = real
        shutil.rmtree(root, ignore_errors=True)
    print("no pip: the command that installs it, not 're-run with --online'  OK")


def _guard_runs_first(name: str) -> None:
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src, feature_version=(3, 7))  # nothing newer than that up to the check
    assert not any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
                   for n in tree.body), f"{name}: annotations would be postponed (3.7+ only)"
    body = [n for n in tree.body if not (isinstance(n, ast.Expr)
                                         and isinstance(n.value, ast.Constant))]
    first = next(i for i, n in enumerate(body)
                 if isinstance(n, ast.If) and "version_info" in ast.unparse(n.test))
    # before it: the stdlib imports and constants only (no app/, no definitions)
    for n in body[:first]:
        assert (isinstance(n, ast.Import) or isinstance(n, ast.ImportFrom) and n.level == 0
                and not (n.module or "").startswith("app")
                or isinstance(n, ast.Assign) and isinstance(n.value, (ast.Tuple, ast.Constant))), \
            f"{name}:{n.lineno}: runs before the version check"
    assert not any(isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AnnAssign))
                   for n in body[:first]), name
    # an older Python stops with the message (sys.version_info patched, the file run)
    code = ("import sys; sys.version_info = (3, 9, 18, 'final', 0); "
            f"exec(compile(open({os.path.join(ROOT, name)!r}).read(), {name!r}, 'exec'), "
            f"{{'__name__': '__main__', '__file__': {os.path.join(ROOT, name)!r}}})")
    p = subprocess.run([sys.executable, "-c", code, "--version"], capture_output=True,
                       text=True, timeout=60, cwd=ROOT)
    assert p.returncode == 1 and "ser2net needs Python 3.10 or newer" in p.stderr, \
        (p.returncode, p.stdout, p.stderr)
    assert "sudo dnf install python3.11" in p.stderr and "Traceback" not in p.stderr, p.stderr


def test_python_version_guard():
    for name in ("ser2net.py", "bootstrap.py"):
        _guard_runs_first(name)
    print("ser2net.py / bootstrap.py: parse on old Pythons, stop with 'needs Python 3.10'  OK")


def _fake_python(folder: str, name: str, version_ok: bool) -> None:
    path = os.path.join(folder, name)
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\n"
                 f'if [ "$1" = -c ]; then exit {0 if version_ok else 1}; fi\n'
                 'echo "ran ${0##*/} $*"\n')
    os.chmod(path, 0o755)


def test_start_sh():
    if os.name != "posix":
        print("SKIP start.sh: POSIX only")
        return
    folder = tempfile.mkdtemp(prefix="s2n_start_")
    try:
        for tool in ("dirname", "tr"):
            os.symlink(shutil.which(tool), os.path.join(folder, tool))

        def start(*pythons: tuple[str, bool]) -> subprocess.CompletedProcess:
            for name in os.listdir(folder):
                if name.startswith("python"):
                    os.remove(os.path.join(folder, name))
            for name, ok in pythons:
                _fake_python(folder, name, ok)
            return subprocess.run([shutil.which("bash"), os.path.join(ROOT, "start.sh"),
                                   "--about"], capture_output=True, text=True, timeout=30,
                                  env={"PATH": folder})

        p = start(("python3", True), ("python3.13", True))
        assert p.returncode == 0 and p.stdout == "ran python3 ser2net.py --about\n", p
        p = start(("python3", False), ("python3.11", True), ("python3.12", True))
        assert p.stdout == "ran python3.12 ser2net.py --about\n", p
        p = start(("python3", False), ("python", True))
        assert p.stdout == "ran python ser2net.py --about\n", p
        p = start(("python3", False))
        assert p.returncode == 1 and "Python 3.10+ not found" in p.stderr, p
        assert "sudo dnf install python3.11" in p.stderr, p.stderr
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("start.sh: python3 if 3.10+, else the newest python3.1x, else a hint  OK")


def test_about_lists_psutil():
    p = subprocess.run([sys.executable, "ser2net.py", "--no-bootstrap", "--about"], cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    line = next((ln for ln in p.stdout.splitlines() if ln.strip().startswith("psutil ")), "")
    assert line, p.stdout
    print(f"--about: {line.strip()}  OK")


if __name__ == "__main__":
    test_requirements_without()
    test_wheel_at_hand()
    test_installs_without_psutil()
    test_retried_without_psutil()
    test_no_pip()
    test_python_version_guard()
    test_start_sh()
    test_about_lists_psutil()
    print("\nPASS: offline first run on any platform")
