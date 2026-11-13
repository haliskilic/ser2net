"""Where each kind of installation keeps its data by default (ser2netweb/layout.py).

  * a source checkout (this one) and the Docker image: data/ in the checkout; a standalone
    executable: data/ next to it; the .deb / .rpm package's /usr/bin/ser2netweb:
    /var/lib/ser2netweb (its service's);
  * a pip / pipx install (the package in a site-packages or dist-packages folder, or next to
    its .dist-info after pip install --target) never writes next to the package: its data
    is in the user's data folder, $XDG_DATA_HOME/ser2netweb (a relative one is ignored)
    or ~/.local/share/ser2netweb, ~/Library/Application Support/ser2netweb on macOS,
    %LOCALAPPDATA%\\ser2netweb on Windows; an editable install stays a source checkout;
  * the Windows executable where winget, Scoop or Chocolatey puts it (winget's portable
    package folder haliskilic.ser2netweb_<source>, Scoop's apps\\<app>\\<version> with its
    manifest.json and install.json, Chocolatey's lib\\<id>\\tools next to <id>.nupkg) says
    so in --about; winget's and Chocolatey's keep the data in the user's data folder (a
    data\\config.json next to the executable, as 3.0.0 made it there, stays in use), Scoop's
    in data\\ next to it (the folder its manifest persists);
  * --data-dir and --config still choose the folder;
  * a pip install skips the bootstrap into ./lib (pip installed the dependencies), says
    "Python package (pip / pipx)" in --about, reads its licence texts from its .dist-info,
    names the ser2netweb command in hints and refuses --install-service (Windows);
  * end to end, a copy laid out as pip installs the wheel (the package in a site-packages
    folder with its .dist-info): --version, --about and --set-password without
    --data-dir, which writes config.json into the user data folder and nothing next to
    the package.

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
    print("source checkout, standalone executable, the package's /usr/bin/ser2netweb, "
          "pip in site-packages / dist-packages / --target: told apart  OK")


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
                                  layout.WINGET, layout.SCOOP, layout.CHOCOLATEY}
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
    print("user data folder: XDG_DATA_HOME (absolute) or ~/.local/share, Application Support, "
          "%LOCALAPPDATA%; the default per kind of installation  OK")


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
          "win, no bootstrap for pip, the hints name the ser2netweb command and the extras  OK")


def _pip_copy(tmp: str) -> str:
    """This checkout's package laid out as pip installs the wheel: <tmp>/site-packages with
    ser2netweb/ and ser2netweb-<version>.dist-info (licence files under licenses/, as
    pyproject.toml lists them). Returns the site-packages folder."""
    site = os.path.join(tmp, "venv", "lib", "site-packages")
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


if __name__ == "__main__":
    test_detect()
    test_windows_package_managers()
    test_user_data_dir()
    test_command_line()
    test_pip_install_end_to_end()
    print("\nPASS: install layout")
