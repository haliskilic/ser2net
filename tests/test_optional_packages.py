"""Optional packages (ldap3, authlib, paho-mqtt) that are missing or fail to import are
reported truthfully (ser2netweb/optional.py):

  * "not installed" only for a ModuleNotFoundError of the package itself; a missing
    dependency, an incompatible version (ImportError, TypeError) or a warning turned
    into an error is "installed but failed to import: <the error>"; the hint is
    `pip install -r requirements-optional.txt` (+ restart), the SER2NETWEB_LIB_OVERRIDE
    folder when that is set, and no pip for a standalone executable;
  * LDAP / OIDC sign-in and MQTT publishing with such a package log that line and fail
    cleanly — a sign-in gets 401, never a 500;
  * Settings shows it for LDAP / OIDC (a real import, not find_spec), and
    `ser2netweb --about` lists pyserial-asyncio-fast and pyasn1 and says "failed to import"
    with the error instead of "not installed".

The fake packages come in through sys.path / SER2NETWEB_LIB_OVERRIDE, so this runs without
the real ones. Run: python3 tests/test_optional_packages.py
"""
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile

from _server import ROOT, Browser, Server

from ser2netweb import optional
from ser2netweb.config import LdapSettings, MqttSettings, OidcSettings
from ser2netweb.engine.mqtt_pub import MqttPublisher
from ser2netweb.web import ldap_auth, oidc_auth
from ser2netweb.web.auth import hash_password

PW = "optional-pkg-pass-1"
PIP = "pip install -r requirements-optional.txt"

# fake packages: {name: {relative file: source}}
BROKEN = {
    "ldap3": {"__init__.py": "raise TypeError('this ldap3 is incompatible')\n"},
    # an authlib without authlib.jose (as authlib 2.0 will be)
    "authlib": {"__init__.py": "__version__ = '2.0.0-test'\n"},
    "paho": {"__init__.py": "", "mqtt/__init__.py": "import s2n_test_missing_dependency\n"},
}


def write_packages(folder, packages):
    for name, files in packages.items():
        for rel, src in files.items():
            path = os.path.join(folder, name, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(src)


@contextlib.contextmanager
def fake_packages(packages):
    """These packages, in this process, instead of any installed ones."""
    folder = tempfile.mkdtemp(prefix="s2n_fakepkg_")
    write_packages(folder, packages)
    tops = set(packages)
    saved = {k: v for k, v in sys.modules.items() if k.split(".")[0] in tops}
    for k in saved:
        del sys.modules[k]
    sys.path.insert(0, folder)
    try:
        yield folder
    finally:
        sys.path.remove(folder)
        for k in [k for k in sys.modules if k.split(".")[0] in tops]:
            del sys.modules[k]
        sys.modules.update(saved)
        shutil.rmtree(folder, ignore_errors=True)


@contextlib.contextmanager
def environment(frozen=False, override=""):
    saved = os.environ.pop("SER2NETWEB_LIB_OVERRIDE", None)
    old = os.environ.pop("SER2NET_LIB_OVERRIDE", None)
    if override:
        os.environ["SER2NETWEB_LIB_OVERRIDE"] = override
    had = hasattr(sys, "frozen")
    if frozen:
        sys.frozen = True
    try:
        yield
    finally:
        os.environ.pop("SER2NETWEB_LIB_OVERRIDE", None)
        if saved is not None:
            os.environ["SER2NETWEB_LIB_OVERRIDE"] = saved
        if old is not None:
            os.environ["SER2NET_LIB_OVERRIDE"] = old
        if frozen and not had:
            del sys.frozen


def test_classification():
    def missing_module(name):
        return ModuleNotFoundError(f"No module named {name!r}", name=name)

    with environment():
        p = optional.problem(missing_module("ldap3"), "ldap3")
        assert p.missing and str(p) == f"ldap3 is not installed ({PIP})", p
        for exc in (missing_module("pyasn1"),                           # a dependency
                    ImportError("cannot import name 'tagMap' from 'pyasn1.codec.ber.encoder'"),
                    TypeError("an incompatible version"),
                    DeprecationWarning("tagMap is deprecated")):        # with -W error
            p = optional.problem(exc, "ldap3")
            assert not p.missing, exc
            assert str(p) == (f"ldap3 is installed but failed to import: {type(exc).__name__}: "
                              f"{exc} ({PIP}, then restart ser2netweb)"), p
        # the package names: paho-mqtt is paho.mqtt; authlib.jose missing is not "no authlib"
        assert optional.problem(missing_module("paho"), "paho-mqtt", "paho.mqtt").missing
        assert optional.problem(missing_module("paho.mqtt"), "paho-mqtt", "paho.mqtt").missing
        assert not optional.problem(missing_module("authlib.jose"), "authlib").missing
        assert not optional.problem(missing_module("ldap3x"), "ldap3").missing
    with environment(override="/opt/lgpl"):
        p = optional.problem(TypeError("x"), "ldap3")
        assert p.hint == "SER2NETWEB_LIB_OVERRIDE puts /opt/lgpl first: check the ldap3 there", p
        assert optional.problem(missing_module("ldap3"), "ldap3").hint == PIP
    with environment(frozen=True):
        for exc in (missing_module("ldap3"), TypeError("x")):
            p = optional.problem(exc, "ldap3")
            assert p.frozen and "pip" not in p.hint, p
            assert p.hint == "this executable should contain ldap3; please report it", p
    print("not installed only for the package itself; else the import error, with the hint  OK")


def test_load():
    with fake_packages({"s2nfake_broken": {"__init__.py": "raise TypeError('boom')\n"},
                        "s2nfake_dep": {"__init__.py": "import s2nfake_absent_dependency\n"},
                        "s2nfake_ok": {"__init__.py": "__version__ = '1.0'\n"}}), environment():
        mod, p = optional.load("s2nfake_ok", "s2nfake-ok")
        assert mod is not None and p is None and mod.__version__ == "1.0"
        mod, p = optional.load("s2nfake_absent", "s2nfake-absent")
        assert mod is None and p.missing, p
        mod, p = optional.load("s2nfake_broken", "s2nfake-broken", "s2nfake_broken")
        assert mod is None and not p.missing and p.error == "TypeError: boom", p
        mod, p = optional.load("s2nfake_dep", "s2nfake-dep", "s2nfake_dep")
        assert mod is None and not p.missing, p
        assert p.error == "ModuleNotFoundError: No module named 's2nfake_absent_dependency'", p
    print("load(): a real import; never raises, whatever the import raises  OK")


def test_features_with_broken_packages():
    logs = []
    with fake_packages(BROKEN), environment():
        s = LdapSettings(enabled=True, server_uri="ldap://127.0.0.1:1",
                         user_dn_template="uid={username},dc=ex")
        assert ldap_auth.authenticate(s, "alice", "pw", logs.append) is None
        assert logs[-1] == ("LDAP login disabled: ldap3 is installed but failed to import: "
                            f"TypeError: this ldap3 is incompatible ({PIP}, then restart "
                            "ser2netweb)"), logs
        o = OidcSettings(enabled=True, issuer="https://idp.invalid", client_id="c")
        assert oidc_auth.complete(o, "https://h/cb", "CODE", "N", logs.append) is None
        assert logs[-1] == ("OIDC sign-in disabled: authlib is installed but failed to import: "
                            "ModuleNotFoundError: No module named 'authlib.jose' "
                            f"({PIP}, then restart ser2netweb)"), logs
        pub = MqttPublisher(MqttSettings(enabled=True, host="127.0.0.1"), logs.append)
        assert pub.connect() is False
        assert logs[-1] == ("MQTT publishing disabled: paho-mqtt is installed but failed to "
                            "import: ModuleNotFoundError: No module named "
                            f"'s2n_test_missing_dependency' ({PIP}, then restart ser2netweb)"), logs
        # the mapping status says so too (dashboard: "MQTT off: ...")
        assert pub.state["state"] == "off" and "failed to import" in pub.state["error"], \
            pub.state
    print("LDAP / OIDC sign-in and MQTT: a broken package is logged as such, nothing raises  OK")


def test_about():
    folder = tempfile.mkdtemp(prefix="s2n_override_")
    try:
        write_packages(folder, BROKEN)
        env = dict(os.environ, SER2NETWEB_LIB_OVERRIDE=folder)
        out = subprocess.run([sys.executable, "ser2netweb.py", "--no-bootstrap", "--about"], cwd=ROOT,
                             env=env, capture_output=True, text=True, encoding="utf-8",
                             timeout=60).stdout
        lines = {ln.split()[0]: ln for ln in out.splitlines() if ln.startswith("  ")}
        assert lines["ldap3"].split(None, 1)[1] == \
            "failed to import: TypeError: this ldap3 is incompatible", out
        assert "2.0.0-test" in lines["authlib"], out
        for name in ("pyserial", "pyserial-asyncio-fast", "starlette", "uvicorn", "pyasn1"):
            assert name in lines, out
        assert "0.16" in lines["pyserial-asyncio-fast"], out
        assert lines["paho-mqtt"].split(None, 1)[1] == ("failed to import: ModuleNotFoundError: "
                                                        "No module named "
                                                        "'s2n_test_missing_dependency'"), out
        # the override's folder is written as <override> (the account name in a path stays
        # out of a bug report), and the last line says what the labels stand for
        assert lines["authlib"].endswith(f"(<override>{os.sep}authlib)"), out
        assert folder not in out, out
        assert "<override> = SER2NETWEB_LIB_OVERRIDE" in out.splitlines()[-1], out
        home = os.path.expanduser("~")
        if os.path.dirname(home) != home:
            assert home not in out, out
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("--about: pyserial-asyncio-fast and pyasn1 listed; a broken package says why; "
          "folders as labels  OK")


def test_about_paths():
    """--about writes a package's folder under a known root as that root's label, the most
    specific root first: which copy is loaded stays visible, the account name doesn't."""
    sys.path.insert(0, ROOT)
    from ser2netweb import cli as entry

    def p(*parts):
        return os.path.abspath(os.path.join(os.sep, *parts))

    roots = [("<override>", p("srv", "override")), ("<bundle>", p("tmp", "_MEI1234")),
             ("<ser2net>", p("home", "ayse", "ser2net")),
             ("<python>", p("home", "ayse", "ser2net", ".venv")), ("~", p("home", "ayse"))]
    cases = {
        p("home", "ayse", "ser2net", ".venv", "lib", "site-packages", "serial"):
            os.path.join("<python>", "lib", "site-packages", "serial"),
        p("home", "ayse", "ser2net", "lib", "ldap3"): os.path.join("<ser2net>", "lib", "ldap3"),
        p("home", "ayse", ".local", "lib", "uvicorn"): os.path.join("~", ".local", "lib", "uvicorn"),
        p("srv", "override", "ldap3"): os.path.join("<override>", "ldap3"),
        p("tmp", "_MEI1234", "starlette"): os.path.join("<bundle>", "starlette"),
        p("home", "ayse"): "~",
        p("home", "ayse2", "lib"): p("home", "ayse2", "lib"),   # only a common prefix
        p("usr", "lib", "python3", "dist-packages", "serial"):
            p("usr", "lib", "python3", "dist-packages", "serial"),
    }
    for path, want in cases.items():
        got = entry._where(path, roots)
        assert got == want, (path, got, want)
    if os.name == "nt":  # letter case aside, as Windows compares paths
        assert entry._where(r"C:\USERS\Ayse\AppData\x", [("~", r"c:\users\ayse")]) == \
            r"~\AppData\x"
        assert entry._where(r"D:\x", [("~", r"C:\Users\ayse")]) == r"D:\x"   # another drive
    labels = [label for label, _folder in entry._known_roots({"SER2NETWEB_LIB_OVERRIDE": " o "})]
    assert labels[0] == "<override>" and "<ser2netweb>" in labels and "<python>" in labels, labels
    assert "<override>" not in [lb for lb, _f in entry._known_roots({})]
    assert all(os.path.dirname(f) != f for _lb, f in entry._known_roots({})), "a whole drive"
    print("--about: folders under the override, the bundle, ser2netweb, Python or the home "
          "folder are written as labels; others as they are  OK")


def test_settings_and_sign_in():
    folder = tempfile.mkdtemp(prefix="s2n_override_")
    write_packages(folder, BROKEN)
    config = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                         "pwd_version": 1}],
              "ldap": {"enabled": True, "server_uri": "ldap://127.0.0.1:1",
                       "user_dn_template": "uid={username},dc=ex"},
              "oidc": {"enabled": True, "issuer": "https://idp.invalid", "client_id": "ser2net",
                       "client_secret": "s3cret"}}
    try:
        with Server(config, env={"SER2NETWEB_LIB_OVERRIDE": folder}) as srv:
            st, _, page = Browser(srv.base).post("/login", {"username": "alice", "password": "x"})
            assert st == 401 and "Invalid username or password." in page, st
            with open(os.path.join(srv.data, "all.log"), encoding="utf-8") as fh:
                log = fh.read()
            assert ("LDAP login disabled: ldap3 is installed but failed to import: TypeError: "
                    f"this ldap3 is incompatible (SER2NETWEB_LIB_OVERRIDE puts {folder} first: "
                    "check the ldap3 there)") in log, log[-1500:]
            admin = Browser(srv.base)
            admin.login("admin", PW)
            _, _, page = admin.get("/settings")
            assert ("LDAP sign-in is on, but <span class=\"mono\">ldap3</span> is installed and "
                    "failed to import") in page, "no LDAP banner"
            assert "TypeError: this ldap3 is incompatible" in page
            assert ("Single sign-on (OIDC) is on, but <span class=\"mono\">authlib</span> is "
                    "installed and failed to import") in page, "no OIDC banner"
            assert "No module named &#39;authlib.jose&#39;" in page
            assert f"SER2NETWEB_LIB_OVERRIDE loads modules from <span class=\"mono\">{folder}" in page
            assert "pip install" not in page
            print("server: LDAP sign-in with a broken ldap3 is a 401; Settings says why  OK")
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    # not installed at all (what Python raises then), LDAP only
    folder = tempfile.mkdtemp(prefix="s2n_override_")
    write_packages(folder, {"ldap3": {"__init__.py": "raise ModuleNotFoundError(\"No module named "
                                                     "'ldap3'\", name='ldap3')\n"}})
    del config["oidc"]
    try:
        with Server(config, env={"SER2NETWEB_LIB_OVERRIDE": folder}) as srv:
            admin = Browser(srv.base)
            admin.login("admin", PW)
            _, _, page = admin.get("/settings")
            assert ("LDAP sign-in is on, but <span class=\"mono\">ldap3</span> is not installed, "
                    "so it won’t work until you install it: <span class=\"mono\">pip install -r "
                    "requirements-optional.txt</span>") in page, "no not-installed banner"
            assert "failed to import" not in page
            print("server: a missing ldap3 is 'not installed' with the pip command  OK")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def main():
    test_classification()
    test_load()
    test_features_with_broken_packages()
    test_about()
    test_about_paths()
    test_settings_and_sign_in()
    print("\nPASS: optional packages that are missing or fail to import are reported truthfully")


if __name__ == "__main__":
    main()
