"""Where each kind of installation keeps its data by default (ser2netweb/layout.py).

  * a source checkout (this one) and the Docker image: data/ in the checkout; a standalone
    executable: data/ next to it; the .deb / .rpm (and AUR) package's /usr/bin/ser2netweb:
    /var/lib/ser2netweb (its service's); the executable in the snap: $SNAP_COMMON/data
    for root (the snap's service), $SNAP_USER_COMMON/data for another user;
  * a pip / pipx install (the package in a site-packages or dist-packages folder, or next to
    its .dist-info after pip install --target) never writes next to the package: its data
    is in the user's data folder, $XDG_DATA_HOME/ser2netweb (a relative one is ignored)
    or ~/.local/share/ser2netweb, ~/Library/Application Support/ser2netweb on macOS,
    %LOCALAPPDATA%\\ser2netweb on Windows; an editable install stays a source checkout;
  * the Homebrew formula's virtual environment (pip's layout in
    <prefix>/Cellar/ser2netweb/<version>/libexec, also reached through <prefix>/opt):
    <prefix>/var/ser2netweb, its service's (brew services), with pip's behaviour otherwise
    (no bootstrap, the .dist-info's licence texts, the ser2netweb command in hints) and its
    own --about line and hint for a missing optional package;
  * --data-dir and --config still choose the folder;
  * a pip install skips the bootstrap into ./lib (pip installed the dependencies), says
    "Python package (pip / pipx)" in --about, reads its licence texts from its .dist-info,
    names the ser2netweb command in hints and refuses --install-service (Windows);
  * end to end, a copy laid out as pip installs the wheel (the package in a site-packages
    folder with its .dist-info): --version, --about and --set-password without
    --data-dir, which writes config.json into the user data folder and nothing next to
    the package; laid out as the Homebrew formula installs it: --about and --set-password
    into <prefix>/var/ser2netweb.

Run: python3 tests/test_install_layout.py   (binds nothing)
"""
import os
import shutil
import subprocess
import sys
import tempfile

from _server import ROOT

from ser2netweb import __version__, cli, console, layout, licences, optional

ROOT = os.path.abspath(ROOT)
PW = "layout-pass-1"


def test_detect():
    pkg = os.path.join(ROOT, "ser2netweb")
    assert layout.detect(False, sys.executable, pkg) == layout.SOURCE
    assert layout.kind() == layout.SOURCE, "this checkout"
    # (Linux paths: the same answer on every OS the test runs on)
    for exe in ("/usr/bin/ser2netweb", "/usr/bin/../bin/ser2netweb"):
        assert layout.detect(True, exe, "/tmp/_MEI1/ser2netweb", platform="linux") == \
            layout.PACKAGE, exe
    for exe, plat in (("/usr/local/bin/ser2netweb", "linux"), ("/opt/x/ser2netweb", "linux"),
                      ("/usr/bin/ser2netweb", "darwin"),
                      (r"C:\Program Files\ser2netweb\ser2netweb.exe", "win32")):
        assert layout.detect(True, exe, "/tmp/_MEI1/ser2netweb", platform=plat) == \
            layout.STANDALONE, exe
    for site in (("venv", "lib", "python3.12", "site-packages"),
                 ("usr", "lib", "python3", "dist-packages"), ("Python312", "Lib", "Site-Packages")):
        folder = os.path.join(os.sep, *site, "ser2netweb")
        assert layout.detect(False, sys.executable, folder) == layout.PIP, folder
    snap = {"SNAP_NAME": "ser2netweb", "SNAP": "/snap/ser2netweb/42"}
    exe = "/snap/ser2netweb/42/bin/ser2netweb"
    assert layout.detect(True, exe, "/tmp/_MEI1/ser2netweb", "linux", snap) == layout.SNAP
    for exe_, env, plat in ((exe, {**snap, "SNAP_NAME": "other"}, "linux"),
                            (exe, {"SNAP": "/snap/ser2netweb/42"}, "linux"),
                            ("/opt/x/ser2netweb", snap, "linux"),
                            ("/snap/ser2netweb/420/bin/ser2netweb", snap, "linux"),
                            (exe, snap, "darwin")):
        assert layout.detect(True, exe_, "/tmp/_MEI1/ser2netweb", plat, env) == \
            layout.STANDALONE, (exe_, env, plat)
    assert layout.detect(True, "/usr/bin/ser2netweb", "/tmp/_MEI1/ser2netweb", "linux",
                         snap) == layout.PACKAGE
    with tempfile.TemporaryDirectory() as tmp:
        # the Homebrew formula's keg: its virtual environment in libexec, run through the
        # keg or through <prefix>/opt/ser2netweb (a link to it)
        prefix = os.path.join(os.path.realpath(tmp), "brew")
        keg = os.path.join(prefix, "Cellar", "ser2netweb", "3.0.0")
        site = os.path.join("libexec", "lib", "python3.14", "site-packages")
        os.makedirs(os.path.join(keg, site, "ser2netweb"))
        os.makedirs(os.path.join(prefix, "opt"))
        paths = [os.path.join(keg, site, "ser2netweb")]
        try:
            os.symlink(keg, os.path.join(prefix, "opt", "ser2netweb"), target_is_directory=True)
            paths.append(os.path.join(prefix, "opt", "ser2netweb", site, "ser2netweb"))
        except OSError:  # (Windows without the symlink privilege; Homebrew isn't there)
            pass
        for pkg in paths:
            assert layout.detect(False, sys.executable, pkg) == layout.HOMEBREW, pkg
            assert layout.homebrew_prefix(pkg) == prefix, pkg
        # pip's layout anywhere else, also another formula's keg
        other = os.path.join(prefix, "Cellar", "other", "1.0", site, "ser2netweb")
        os.makedirs(other)
        for pkg in (other, os.path.join(os.sep, "venv", "lib", "python3.12", "site-packages",
                                         "ser2netweb")):
            assert layout.detect(False, sys.executable, pkg) == layout.PIP, pkg
            assert layout.homebrew_prefix(pkg) is None, pkg
    assert layout.homebrew_prefix() is None, "this checkout"
    with tempfile.TemporaryDirectory() as tmp:
        pkg = os.path.join(tmp, "ser2netweb")
        os.mkdir(pkg)
        assert layout.detect(False, sys.executable, pkg) == layout.SOURCE
        assert layout.licence_dir(pkg) is None
        # pip install --target DIR: no site-packages in the path, but the installer's record
        os.makedirs(os.path.join(tmp, "ser2netweb-3.0.0.dist-info", "licenses"))
        assert layout.detect(False, sys.executable, pkg) == layout.PIP
        assert layout.licence_dir(pkg) == os.path.join(tmp, "ser2netweb-3.0.0.dist-info",
                                                       "licenses")
    print("source checkout, standalone executable, the package's /usr/bin/ser2netweb, the "
          "snap's, pip in site-packages / dist-packages / --target, the Homebrew formula's "
          "keg: told apart  OK")


def test_user_data_dir():
    home = "/home/ayse"
    assert layout.user_data_dir("linux", {}, home) == "/home/ayse/.local/share/ser2netweb"
    assert layout.user_data_dir("linux", {"XDG_DATA_HOME": "/srv/xdg"}, home) == \
        "/srv/xdg/ser2netweb"
    for ignored in ("", "relative/share", "~/share"):  # the XDG spec: absolute only
        assert layout.user_data_dir("linux", {"XDG_DATA_HOME": ignored}, home) == \
            "/home/ayse/.local/share/ser2netweb", ignored
    assert layout.user_data_dir("freebsd14", {}, home) == "/home/ayse/.local/share/ser2netweb"
    assert layout.user_data_dir("darwin", {"XDG_DATA_HOME": "/srv/xdg"}, "/Users/ayse") == \
        "/Users/ayse/Library/Application Support/ser2netweb"
    win_home = r"C:\Users\ayse"
    assert layout.user_data_dir("win32", {"LOCALAPPDATA": r"D:\Profiles\ayse\Local"},
                                win_home) == r"D:\Profiles\ayse\Local\ser2netweb"
    assert layout.user_data_dir("win32", {}, win_home) == r"C:\Users\ayse\AppData\Local\ser2netweb"
    assert layout.user_data_dir("win32", {"LOCALAPPDATA": "Local"}, win_home) == \
        r"C:\Users\ayse\AppData\Local\ser2netweb"
    assert layout.default_data_dir("/opt/x", layout.SOURCE) == os.path.join("/opt/x", "data")
    assert layout.default_data_dir("/opt/x", layout.STANDALONE) == os.path.join("/opt/x", "data")
    assert layout.default_data_dir("/usr/bin", layout.PACKAGE) == "/var/lib/ser2netweb"
    assert layout.default_data_dir("/venv/site-packages", layout.PIP) == layout.user_data_dir()
    # (not in a keg: the user data folder)
    assert layout.default_data_dir("/venv/site-packages", layout.HOMEBREW) == \
        layout.user_data_dir()
    snap = {"SNAP_COMMON": "/var/snap/ser2netweb/common",
            "SNAP_USER_COMMON": "/home/ayse/snap/ser2netweb/common"}
    assert layout.snap_data_dir(snap, root=True) == "/var/snap/ser2netweb/common/data"
    assert layout.snap_data_dir(snap, root=False) == "/home/ayse/snap/ser2netweb/common/data"
    for env in ({}, {"SNAP_COMMON": "relative"}):
        assert layout.snap_data_dir(env, root=True) == layout.user_data_dir(), env
    print("user data folder: XDG_DATA_HOME (absolute) or ~/.local/share, Application Support, "
          "%LOCALAPPDATA%; the snap's for root and another user; the default per kind of "
          "installation  OK")


class _Kind:
    """layout.kind() answering `kind` while in use."""

    def __init__(self, kind):
        self.kind, self.saved = kind, layout.kind

    def __enter__(self):
        layout.kind = lambda: self.kind
        return self

    def __exit__(self, *exc):
        layout.kind = self.saved


def test_command_line():
    args = cli.parse_args([])
    assert args.data_dir == os.path.join(ROOT, "data") and not args.data_dir_given, args.data_dir
    with _Kind(layout.PIP):
        args = cli.parse_args([])
        assert args.data_dir == layout.user_data_dir() and not args.data_dir_given
        for given in (["--data-dir", "/srv/s2n"], ["--data-dir=/srv/s2n"]):
            args = cli.parse_args(given)
            assert args.data_dir == "/srv/s2n" and args.data_dir_given, given
        args = cli.parse_args(["--config", "/srv/s2n/config.json"])
        assert args.config == "/srv/s2n/config.json"
        # the bootstrap into ./lib is not used: nothing imported, nothing put on sys.path
        saved_path, had = list(sys.path), "bootstrap" in sys.modules
        cli.load_dependencies(cli.parse_args([]), refresh=True)
        assert sys.path == saved_path and ("bootstrap" in sys.modules) == had
        assert console.reconfigure_hint("/d", environ={})[1] == \
            "ser2netweb --reconfigure --data-dir /d"
    with _Kind(layout.PACKAGE):
        assert cli.parse_args([]).data_dir == "/var/lib/ser2netweb"
    saved = layout.homebrew_prefix
    layout.homebrew_prefix = lambda *_a: "/opt/homebrew"
    try:
        with _Kind(layout.HOMEBREW):
            args = cli.parse_args([])
            assert args.data_dir == os.path.join("/opt/homebrew", "var", "ser2netweb"), \
                args.data_dir
            assert not args.data_dir_given
            assert cli.parse_args(["--data-dir", "/srv/s2n"]).data_dir == "/srv/s2n"
            saved_path, had = list(sys.path), "bootstrap" in sys.modules
            cli.load_dependencies(cli.parse_args([]), refresh=True)
            assert sys.path == saved_path and ("bootstrap" in sys.modules) == had
            assert console.reconfigure_hint("/d", environ={})[1] == \
                "ser2netweb --reconfigure --data-dir /d"
            assert dict(cli._known_roots({}))["<ser2netweb>"] == layout.PACKAGE_DIR
            assert optional.problem(ModuleNotFoundError("x", name="ldap3"), "ldap3").homebrew
    finally:
        layout.homebrew_prefix = saved
    with _Kind(layout.SNAP):
        assert console.reconfigure_hint("/d", environ={})[1] == \
            "ser2netweb --reconfigure --data-dir /d"
    hint = optional.Problem("cryptography", True, "", homebrew=True).hint
    assert hint == "the Homebrew formula installs cryptography: brew reinstall ser2netweb", hint
    assert optional.Problem("authlib", False, "", homebrew=True).hint.endswith(
        ", then restart ser2netweb")
    hint = optional.Problem("paho-mqtt", True, "", pip=True).hint
    assert hint == 'pip install "ser2netweb[mqtt]", or pipx inject ser2netweb paho-mqtt', hint
    assert optional.Problem("ldap3", False, "", pip=True).hint.endswith(", then restart ser2netweb")
    assert optional.Problem("authlib", True, "").hint == "pip install -r requirements-optional.txt"
    print("the command line: the default data folder per installation, --data-dir / --config "
          "win, no bootstrap for pip and Homebrew, the hints name the ser2netweb command, the "
          "extras and the formula  OK")


def _pip_copy(tmp: str, site: str = "") -> str:
    """This checkout's package laid out as pip installs the wheel: `site` (default
    <tmp>/venv/lib/site-packages) with ser2netweb/ and ser2netweb-<version>.dist-info
    (licence files under licenses/, as pyproject.toml lists them). Returns `site`."""
    site = site or os.path.join(tmp, "venv", "lib", "site-packages")
    shutil.copytree(os.path.join(ROOT, "ser2netweb"), os.path.join(site, "ser2netweb"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    info = os.path.join(site, f"ser2netweb-{__version__}.dist-info")
    lic = os.path.join(info, "licenses")
    for rel in ("LICENSE", "NOTICE", "COMMERCIAL_LICENSE.md", "THIRD-PARTY-NOTICES.md",
                *(os.path.join("tools", "licenses", "npm", n)
                  for n in os.listdir(os.path.join(ROOT, "tools", "licenses", "npm")))):
        os.makedirs(os.path.dirname(os.path.join(lic, rel)), exist_ok=True)
        shutil.copy2(os.path.join(ROOT, rel), os.path.join(lic, rel))
    with open(os.path.join(info, "METADATA"), "w", encoding="utf-8") as fh:
        fh.write(f"Metadata-Version: 2.4\nName: ser2netweb\nVersion: {__version__}\n")
    return site


def _run(site: str, env: dict, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    # -I: neither this checkout (the current directory) nor PYTHONPATH on sys.path, only the
    # copy's folder and the interpreter's own packages (the dependencies)
    code = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
            "from ser2netweb.cli import main; raise SystemExit(main())")
    return subprocess.run([sys.executable, "-I", "-c", code, site, *args], env=env,
                          input=stdin, capture_output=True, text=True, encoding="utf-8",
                          timeout=120, cwd=os.path.dirname(site))


def test_pip_install_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        site = _pip_copy(tmp)
        home = os.path.join(tmp, "home")
        env = dict(os.environ, HOME=home, USERPROFILE=home, XDG_DATA_HOME=os.path.join(tmp, "xdg"),
                   LOCALAPPDATA=os.path.join(tmp, "local"))
        for k in ("SER2NETWEB_LIB_OVERRIDE", "SER2NET_LIB_OVERRIDE"):
            env.pop(k, None)
        want = layout.user_data_dir(sys.platform, env, home)
        assert want.startswith(tmp), want
        p = _run(site, env, "--version")
        assert p.returncode == 0 and p.stdout.strip() == f"ser2netweb {__version__}", p
        p = _run(site, env, "--about")
        assert p.returncode == 0 and "Build: Python package (pip / pipx)" in p.stdout, p
        assert "<ser2netweb>" not in p.stdout, "ser2netweb's folder is not site-packages"
        p = _run(site, dict(env, COLUMNS="500"), "--help")  # (no line breaks in the path)
        assert want in " ".join(p.stdout.split()), p.stdout
        p = _run(site, env, "--set-password", "admin", stdin=PW + "\n")
        assert p.returncode == 0, p
        assert os.path.isfile(os.path.join(want, "config.json")), os.listdir(tmp)
        assert sorted(os.listdir(site)) == ["ser2netweb", f"ser2netweb-{__version__}.dist-info"], \
            os.listdir(site)
        assert not os.path.exists(os.path.join(site, "lib")), "no bootstrap"
        given = os.path.join(tmp, "given")
        p = _run(site, env, "--data-dir", given, "--set-password", "admin", stdin=PW + "\n")
        assert p.returncode == 0 and os.path.isfile(os.path.join(given, "config.json")), p
        p = _run(site, env, "--licenses")
        assert os.path.join(f"ser2netweb-{__version__}.dist-info", "licenses",
                            "THIRD-PARTY-NOTICES.md") in p.stdout, p
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from ser2netweb import licences; "
                "print(' '.join(a for a, _t, _x in licences.texts()))")
        out = subprocess.run([sys.executable, "-I", "-c", code, site], capture_output=True,
                             text=True, timeout=60, env=env).stdout.split()
        assert out[:4] == ["license", "notice", "commercial", "third-party"], out
        assert any(a.startswith("npm-") for a in out), out
        p = _run(site, env, "--install-service")
        assert p.returncode == 1, p
        if os.name == "nt":
            assert "a pip / pipx install can't be a Windows service" in p.stderr, p
    # this checkout's own texts are unchanged
    assert [a for a, _t, _x in licences.texts()][:4] == ["license", "notice", "commercial",
                                                          "third-party"]
    print("a copy laid out as pip installs it: --version, --about, --set-password into the "
          "user data folder (nothing next to the package), --data-dir, its licence texts, no "
          "Windows service  OK")


def test_homebrew_end_to_end():
    """The package as the formula's virtual environment has it (in the keg's libexec): the
    formula's --about line, and --set-password without --data-dir writes into
    <prefix>/var/ser2netweb, the data directory of its service, nothing into the keg."""
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(os.path.realpath(tmp), "brew")
        keg = os.path.join(prefix, "Cellar", "ser2netweb", __version__)
        site = _pip_copy(tmp, os.path.join(keg, "libexec", "lib", "python3.14", "site-packages"))
        home = os.path.join(tmp, "home")
        env = dict(os.environ, HOME=home, USERPROFILE=home, XDG_DATA_HOME=os.path.join(tmp, "xdg"),
                   LOCALAPPDATA=os.path.join(tmp, "local"))
        for k in ("SER2NETWEB_LIB_OVERRIDE", "SER2NET_LIB_OVERRIDE"):
            env.pop(k, None)
        want = os.path.join(prefix, "var", "ser2netweb")
        p = _run(site, env, "--about")
        assert p.returncode == 0 and "Build: Homebrew formula" in p.stdout.splitlines(), p
        p = _run(site, dict(env, COLUMNS="500"), "--help")
        assert want in " ".join(p.stdout.split()), p.stdout
        p = _run(site, env, "--set-password", "admin", stdin=PW + "\n")
        assert p.returncode == 0, p
        assert os.path.isfile(os.path.join(want, "config.json")), os.listdir(prefix)
        assert sorted(os.listdir(site)) == ["ser2netweb", f"ser2netweb-{__version__}.dist-info"]
        assert not os.path.exists(os.path.join(tmp, "xdg")), "not the user data folder"
        p = _run(site, env, "--licenses")
        assert os.path.join(f"ser2netweb-{__version__}.dist-info", "licenses",
                            "THIRD-PARTY-NOTICES.md") in p.stdout, p
    print("a copy laid out as the Homebrew formula installs it: --about, --set-password into "
          "<prefix>/var/ser2netweb, its licence texts  OK")


if __name__ == "__main__":
    test_detect()
    test_user_data_dir()
    test_command_line()
    test_pip_install_end_to_end()
    test_homebrew_end_to_end()
    print("\nPASS: install layout")
