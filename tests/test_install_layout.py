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
  * the Windows executable where winget, Scoop or Chocolatey puts it (winget's portable
    package folder haliskilic.ser2netweb_<source>, Scoop's apps\\<app>\\<version> with its
    manifest.json and install.json, Chocolatey's lib\\<id>\\tools next to <id>.nupkg) says
    so in --about; winget's and Chocolatey's keep the data in the user's data folder (a
    data\\config.json next to the executable, as 3.0.0 made it there, stays in use), Scoop's
    in data\\ next to it (the folder its manifest persists);
  * a distribution's package built from a recipe in packaging/ (pip's layout with the file
    the recipe wrote into the package, layout.DISTRO_FILE): its service's data directory
    as the file says, the distribution named in --about, the distribution's command in the
    hint for a missing optional package; a file that isn't a recipe's (or a build tree
    with one) changes nothing;
  * --data-dir and --config still choose the folder;
  * a pip install skips the bootstrap into ./lib (pip installed the dependencies), says
    "Python package (pip / pipx)" in --about, reads its licence texts from its .dist-info,
    names the ser2netweb command in hints and refuses --install-service (Windows);
  * end to end, a copy laid out as pip installs the wheel (the package in a site-packages
    folder with its .dist-info): --version, --about and --set-password without
    --data-dir, which writes config.json into the user data folder and nothing next to
    the package; laid out as the Homebrew formula installs it: --about and --set-password
    into <prefix>/var/ser2netweb; laid out as a distribution's recipe installs it: --about
    and --set-password into the data directory its file names.

Run: python3 tests/test_install_layout.py   (binds nothing)
"""
import json
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


def _touch(*parts: str) -> str:
    path = os.path.join(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{}")
    return path


def test_windows_package_managers():
    winget = (r"C:\Users\ayse\AppData\Local\Microsoft\WinGet\Packages"
              r"\haliskilic.ser2netweb_Microsoft.Winget.Source_8wekyb3d8bbwe")
    machine = (r"C:\Program Files\WinGet\Packages"  # (winget install --scope machine)
               r"\HALISKILIC.SER2NETWEB_Microsoft.Winget.Source_8wekyb3d8bbwe")
    for folder in (winget, machine):
        # (the name the release gives the file, or the command's: winget versions differ)
        for exe in ("ser2netweb-windows-x64.exe", "ser2netweb.exe"):
            path = folder + "\\" + exe
            assert layout.windows_package_manager(path) == layout.WINGET, path
            assert layout.detect(True, path, "/x/_MEI1/ser2netweb", platform="win32") == \
                layout.WINGET, path
            # (only a frozen Windows build is one)
            assert layout.detect(True, path, "/x/_MEI1/ser2netweb", platform="linux") == \
                layout.STANDALONE, path
    for path in (r"C:\Users\ayse\WinGet\Packages\other.ser2netweb_x\ser2netweb.exe",
                 r"C:\Users\ayse\haliskilic.ser2netweb\ser2netweb.exe",
                 r"C:\tools\ser2netweb.exe", r"ser2netweb.exe", r"C:\ser2netweb.exe"):
        assert layout.windows_package_manager(path) is None, path
    with tempfile.TemporaryDirectory() as tmp:
        # Scoop: <root>\apps\<app>\<version> (and the junction current), with the files it writes
        for version in ("3.0.0", "current"):
            folder = os.path.join(tmp, "scoop", "apps", "ser2netweb", version)
            exe = os.path.join(folder, "ser2netweb.exe")
            _touch(folder, "manifest.json")
            assert layout.windows_package_manager(exe) is None, "install.json missing"
            _touch(folder, "install.json")
            assert layout.windows_package_manager(exe) == layout.SCOOP, exe
            assert layout.detect(True, exe, "/x/_MEI1/ser2netweb", platform="win32") == \
                layout.SCOOP
        elsewhere = os.path.join(tmp, "dist", "ser2netweb", "3.0.0")
        _touch(elsewhere, "manifest.json")
        _touch(elsewhere, "install.json")
        assert layout.windows_package_manager(os.path.join(elsewhere, "ser2netweb.exe")) is None
        # Chocolatey: <root>\lib\<id>\tools, next to <id>.nupkg
        tools = os.path.join(tmp, "chocolatey", "lib", "ser2netweb", "tools")
        exe = os.path.join(tools, "ser2netweb.exe")
        os.makedirs(tools)
        assert layout.windows_package_manager(exe) is None, "no .nupkg"
        _touch(tmp, "chocolatey", "lib", "ser2netweb", "other.nupkg")
        assert layout.windows_package_manager(exe) is None, "another package's .nupkg"
        _touch(tmp, "chocolatey", "lib", "ser2netweb", "ser2netweb.nupkg")
        assert layout.windows_package_manager(exe) == layout.CHOCOLATEY
        assert layout.detect(True, exe, "/x/_MEI1/ser2netweb", platform="win32") == \
            layout.CHOCOLATEY
        assert layout.detect(False, sys.executable, os.path.join(ROOT, "ser2netweb"),
                             platform="win32") == layout.SOURCE
        # where the data goes without --data-dir
        for k in (layout.WINGET, layout.CHOCOLATEY):
            assert layout.default_data_dir(tools, k) == layout.user_data_dir(), k
        assert layout.default_data_dir(folder, layout.SCOOP) == os.path.join(folder, "data")
        _touch(tools, "data", "config.json")  # 3.0.0 put it there: kept in use
        for k in (layout.WINGET, layout.CHOCOLATEY):
            assert layout.default_data_dir(tools, k) == os.path.join(tools, "data"), k
    assert set(layout.LABELS) == {layout.SOURCE, layout.STANDALONE, layout.PACKAGE, layout.PIP,
                                  layout.HOMEBREW, layout.SNAP, layout.WINGET, layout.SCOOP,
                                  layout.CHOCOLATEY, layout.DISTRO}
    # no kind shadows another: Linux's (the package's path, the snap) and Windows' (the
    # package managers' folders) only on their own platform
    snap = {"SNAP_NAME": "ser2netweb", "SNAP": "/snap/ser2netweb/42"}
    for exe, plat in (("/snap/ser2netweb/42/bin/ser2netweb", "win32"),
                      ("/usr/bin/ser2netweb", "win32"), (winget + "\\ser2netweb.exe", "linux")):
        assert layout.detect(True, exe, "/x/_MEI1/ser2netweb", plat, snap) == \
            layout.STANDALONE, (exe, plat)
    assert layout.detect(True, winget + "\\ser2netweb.exe", "/x/_MEI1/ser2netweb", "win32",
                         snap) == layout.WINGET
    for k, name in ((layout.WINGET, "winget"), (layout.SCOOP, "Scoop"),
                    (layout.CHOCOLATEY, "Chocolatey")):
        assert layout.LABELS[k] == f"standalone executable, {name} package", layout.LABELS[k]
    print("the Windows executable from winget, Scoop and Chocolatey: told apart; winget's and "
          "Chocolatey's data in the user's folder (3.0.0's data\\ next to it kept), Scoop's "
          "in its persisted data\\  OK")


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


def _homebrew_upgrade():
    """3.0.0 took the keg for a pip install: a copy run by hand kept its data in the user
    data folder, which stays the default while <prefix>/var/ser2netweb has no config.json.
    (Called with layout.kind() answering HOMEBREW.)"""
    saved = layout.homebrew_prefix, layout.user_data_dir
    with tempfile.TemporaryDirectory() as tmp:
        prefix, user = os.path.join(tmp, "brew"), os.path.join(tmp, "user", "ser2netweb")
        var = os.path.join(prefix, "var", "ser2netweb")
        layout.homebrew_prefix, layout.user_data_dir = (lambda *_a: prefix), (lambda *_a: user)
        try:
            assert layout.default_data_dir("/x") == var, "neither folder exists"
            _touch(user, "all.log")
            assert layout.default_data_dir("/x") == var, "a user folder without a config.json"
            _touch(user, "config.json")
            assert layout.default_data_dir("/x") == user, "3.0.0's data, run by hand"
            assert cli.parse_args([]).data_dir == user
            os.makedirs(var)  # (the formula makes it)
            assert layout.default_data_dir("/x") == user
            _touch(var, "config.json")  # the service ran, or --data-dir was given
            assert layout.default_data_dir("/x") == var, "the formula's folder wins"
        finally:
            layout.homebrew_prefix, layout.user_data_dir = saved


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
    with _Kind(layout.HOMEBREW):
        _homebrew_upgrade()
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
    for k in (layout.WINGET, layout.CHOCOLATEY):
        with _Kind(k):
            assert cli.parse_args([]).data_dir == layout.user_data_dir(), k
            assert cli.parse_args(["--data-dir", "/srv/s2n"]).data_dir == "/srv/s2n", k
    with _Kind(layout.SCOOP):
        assert cli.parse_args([]).data_dir == os.path.join(ROOT, "data")
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
        # data 3.0.0 left in the user data folder (it took the keg for a pip install) stays
        # the default until <prefix>/var/ser2netweb has a config.json
        user = layout.user_data_dir(sys.platform, env, home)
        old = _touch(user, "config.json")
        p = _run(site, dict(env, COLUMNS="500"), "--help")
        assert user in " ".join(p.stdout.split()), p.stdout
        shutil.rmtree(os.path.join(tmp, "xdg"), ignore_errors=True)
        if os.path.exists(old):  # (macOS, Windows: the folder is under the home directory)
            os.remove(old)
        p = _run(site, env, "--set-password", "admin", stdin=PW + "\n")
        assert p.returncode == 0, p
        assert os.path.isfile(os.path.join(want, "config.json")), os.listdir(prefix)
        assert sorted(os.listdir(site)) == ["ser2netweb", f"ser2netweb-{__version__}.dist-info"]
        assert not os.path.exists(os.path.join(tmp, "xdg")), "not the user data folder"
        p = _run(site, env, "--licenses")
        assert os.path.join(f"ser2netweb-{__version__}.dist-info", "licenses",
                            "THIRD-PARTY-NOTICES.md") in p.stdout, p
    print("a copy laid out as the Homebrew formula installs it: --about, --set-password into "
          "<prefix>/var/ser2netweb (3.0.0's data in the user data folder kept in use), its "
          "licence texts  OK")


DEBIAN = {"distribution": "Debian", "data_dir": "/var/lib/ser2netweb",
          "optional": "sudo apt install python3-{dist}"}


def _distro_file(site: str, info) -> str:
    path = os.path.join(site, "ser2netweb", layout.DISTRO_FILE)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(info if isinstance(info, str) else json.dumps(info))
    return path


def test_distribution_package():
    """A distribution's package built from a recipe in packaging/: pip's layout (the package
    in site-packages or dist-packages with its .dist-info) and the file the recipe wrote
    into the package (layout.DISTRO_FILE), which names the distribution, its service's data
    directory and how its optional packages are installed."""
    with tempfile.TemporaryDirectory() as tmp:
        site = os.path.join(tmp, "usr", "lib", "python3", "dist-packages")
        pkg = os.path.join(site, "ser2netweb")
        os.makedirs(pkg)
        assert layout.detect(False, sys.executable, pkg) == layout.PIP, "no file: pip's"
        _distro_file(site, DEBIAN)
        assert layout.distro_info(pkg) == DEBIAN
        assert layout.detect(False, sys.executable, pkg) == layout.DISTRO
        assert layout.detect(True, "/opt/x/ser2netweb", pkg, "linux") == layout.STANDALONE
        # what isn't a recipe's file is pip's layout as before (and never fails)
        for bad in ("", "not json", "[]", '{"data_dir": "/var/lib/ser2netweb"}',
                    '{"distribution": ""}', '{"distribution": 3}', "\udcff"):
            path = _distro_file(site, bad) if bad != "\udcff" else None
            if path is None:
                with open(os.path.join(pkg, layout.DISTRO_FILE), "wb") as fh:
                    fh.write(b'{"distribution": "\xff"}')
            assert layout.distro_info(pkg) is None, bad
            assert layout.detect(False, sys.executable, pkg) == layout.PIP, bad
        # (a build tree with the file, as a recipe's check step runs the tests: a source
        # checkout still)
        tree = os.path.join(tmp, "build", "ser2netweb-3.1.0")
        os.makedirs(os.path.join(tree, "ser2netweb"))
        _distro_file(tree, DEBIAN)
        assert layout.detect(False, sys.executable, os.path.join(tree, "ser2netweb")) == \
            layout.SOURCE
    assert layout.distro_info() is None, "this checkout has no such file"
    saved = layout.distro_info
    try:
        for info, want in ((DEBIAN, "/var/lib/ser2netweb"),
                           ({"distribution": "FreeBSD", "data_dir": "/var/db/ser2netweb"},
                            "/var/db/ser2netweb"),
                           ({"distribution": "X", "data_dir": "relative/dir"},
                            layout.user_data_dir()),
                           ({"distribution": "X"}, layout.user_data_dir())):
            layout.distro_info = lambda *_a, info=info: info
            assert layout.default_data_dir("/usr/bin", layout.DISTRO) == want, info
            with _Kind(layout.DISTRO):
                assert cli.parse_args([]).data_dir == want, info
                assert cli.parse_args(["--data-dir", "/srv/s2n"]).data_dir == "/srv/s2n"
                assert layout.label() == f"distribution package ({info['distribution']})"
                # pip's layout otherwise: no bootstrap, the ser2netweb command in hints
                saved_path, had = list(sys.path), "bootstrap" in sys.modules
                cli.load_dependencies(cli.parse_args([]), refresh=True)
                assert sys.path == saved_path and ("bootstrap" in sys.modules) == had
                assert console.reconfigure_hint("/d", environ={})[1] == \
                    "ser2netweb --reconfigure --data-dir /d"
                assert dict(cli._known_roots({}))["<ser2netweb>"] == layout.PACKAGE_DIR
        # the hint for a missing optional package: the recipe's command, else the
        # distribution's package by name
        layout.distro_info = lambda *_a: DEBIAN
        with _Kind(layout.DISTRO):
            p = optional.problem(ModuleNotFoundError("x", name="paho"), "paho-mqtt", "paho.mqtt")
            assert not p.pip and not p.homebrew and p.distro == DEBIAN, p
            assert p.hint == "sudo apt install python3-paho-mqtt", p.hint
            p = optional.problem(ImportError("broken"), "ldap3")
            assert p.hint == "sudo apt install python3-ldap3, then restart ser2netweb", p.hint
        for template, want in (("sudo port install ser2netweb +{extra}",
                                "sudo port install ser2netweb +oidc"),
                               ({"authlib": "sudo xbps-install python3-Authlib"},
                                "sudo xbps-install python3-Authlib"),
                               ({"ldap3": "x"}, "Void has no package of it"),
                               ("", "install the Void package of authlib"),
                               ("{nothing} {dist}", "{nothing} authlib")):
            info = {"distribution": "Void", "optional": template}
            assert optional.Problem("authlib", True, "", distro=info).hint == want, template
    finally:
        layout.distro_info = saved
    print("a distribution's package (the recipe's file in pip's layout): its name in --about, "
          "its service's data directory, the distribution's command in hints; anything else "
          "pip's layout as before  OK")


def test_distribution_package_end_to_end():
    """The package as a recipe in packaging/ installs it (pip's layout with the recipe's
    file): --about names the distribution, --set-password without --data-dir writes into
    its service's data directory, nothing next to the package."""
    with tempfile.TemporaryDirectory() as tmp:
        site = _pip_copy(tmp, os.path.join(tmp, "usr", "lib", "python3", "dist-packages"))
        data = os.path.join(tmp, "var", "lib", "ser2netweb")
        _distro_file(site, dict(DEBIAN, data_dir=data))
        home = os.path.join(tmp, "home")
        env = dict(os.environ, HOME=home, USERPROFILE=home, XDG_DATA_HOME=os.path.join(tmp, "xdg"),
                   LOCALAPPDATA=os.path.join(tmp, "local"))
        for k in ("SER2NETWEB_LIB_OVERRIDE", "SER2NET_LIB_OVERRIDE"):
            env.pop(k, None)
        p = _run(site, env, "--about")
        assert p.returncode == 0 and "Build: distribution package (Debian)" in \
            p.stdout.splitlines(), p
        p = _run(site, dict(env, COLUMNS="500"), "--help")
        assert data in " ".join(p.stdout.split()), p.stdout
        p = _run(site, env, "--set-password", "admin", stdin=PW + "\n")
        assert p.returncode == 0, p
        assert os.path.isfile(os.path.join(data, "config.json")), os.listdir(tmp)
        assert not os.path.exists(os.path.join(tmp, "xdg")), "not the user data folder"
        assert sorted(os.listdir(site)) == ["ser2netweb", f"ser2netweb-{__version__}.dist-info"]
        p = _run(site, env, "--licenses")
        assert os.path.join(f"ser2netweb-{__version__}.dist-info", "licenses",
                            "THIRD-PARTY-NOTICES.md") in p.stdout, p
    print("a copy laid out as a distribution's recipe installs it: --about, --set-password "
          "into its service's data directory, its licence texts  OK")


if __name__ == "__main__":
    test_detect()
    test_windows_package_managers()
    test_user_data_dir()
    test_command_line()
    test_pip_install_end_to_end()
    test_homebrew_end_to_end()
    test_distribution_package()
    test_distribution_package_end_to_end()
    print("\nPASS: install layout")
