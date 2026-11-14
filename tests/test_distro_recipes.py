"""The source package recipes in packaging/ for the distributions' own repositories
(docs/PACKAGING.md, "Recipes for distributions"; CI builds them in their distributions'
containers: .github/workflows/distro-packages.yml):

  * every recipe's version is ser2netweb/__init__.py's (Debian's: the upstream part of
    <version>+ds-<revision>), and its newest changelog entry is that version's;
  * the dependencies are pyproject.toml's, under each distribution's names, with the floors
    where its format takes a version (Debian's Depends / Build-Depends, openSUSE's Requires /
    BuildRequires, Fedora's Recommends; Fedora's Requires are generated from pyproject.toml
    itself, from which its %prep takes pyserial-asyncio-fast out), the extras as Recommends
    or optdepends. pyserial-asyncio-fast, which none of these distributions packages, is
    left out everywhere (ser2netweb runs on its built-in copy of the transport), the
    Python 3.10 backports too (no distribution here has a Python that old);
  * no recipe names the classic C ser2net's package (Provides, Conflicts, Replaces, Breaks,
    Obsoletes): Arch's only conflict is the AUR's ser2netweb-bin, which installs the same
    files;
  * each systemd unit is the .deb / .rpm's (tools/build_packages.unit()), directive by
    directive (Arch's with uucp for dialout), the sysusers.d / tmpfiles.d entries are
    packaging/sysusers.conf's / tmpfiles.conf's, and Alpine's OpenRC service runs the same
    command, as the same account in the group of the serial ports, on the same data
    directory, 0700;
  * the check steps run the same tests, each one of run_all.py's portable ones and none
    that starts a server;
  * each recipe writes ser2netweb/distro.json before its build (ser2netweb/layout.py,
    DISTRO): JSON that layout takes, naming the distribution, the data directory of the
    package's service and, for every extra's package, the distribution's command with its
    name of it (Debian removes the file again in its clean step);
  * tools/bump_recipes.py --check finds nothing to change: the Arch and Alpine checksums of
    the files beside the recipes, the .SRCINFO, the browser libraries' versions;
  * debian/copyright: DEP-5 with the repacking's Files-Excluded, LICENSE's text as the
    AGPL-3.0-only licence, a paragraph for every browser library and copied module;
    packaging/debian/missing_sources.py fetches the sources of every static file;
  * distro-packages.yml runs for a pull request that changes a recipe, by hand (a tag) and
    weekly, builds every target, reads only and gates nothing (ci.yml doesn't run it).
Run: python3 tests/test_distro_recipes.py   (stdlib only; binds nothing)
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "packaging", "debian"))
sys.path.insert(0, ROOT)

import _distro_files
import build_packages
import bump_recipes as br
import missing_sources
import run_all
from _workflows import jobs, permissions, read as read_workflow, steps
from test_systemd_units import parse

from ser2netweb import layout, optional

try:
    import tomllib
except ImportError:  # Python 3.10: pip's copy
    from pip._vendor import tomli as tomllib

PKG = os.path.join(ROOT, "packaging")
DEBIAN = os.path.join(PKG, "debian", "debian")
UNITS = {"debian": "debian/debian/ser2netweb.service", "fedora": "fedora/ser2netweb.service",
         "opensuse": "opensuse/ser2netweb.service", "arch": "arch/ser2netweb/ser2netweb.service"}
ACCOUNT_FILES = {"debian": "debian/debian", "fedora": "fedora", "opensuse": "opensuse",
                 "arch": "arch/ser2netweb"}
# the one dependency no distribution here packages: the built-in transport stands in
LEFT_OUT = "pyserial-asyncio-fast"
# PyPI name -> the distribution's package (runtime dependencies and the extras' packages)
NAMES = {
    "debian": {"pyserial": "python3-serial", "starlette": "python3-starlette",
               "uvicorn": "python3-uvicorn", "h11": "python3-h11",
               "websockets": "python3-websockets",
               "python-multipart": "python3-python-multipart",  # (python3-multipart: another)
               "jinja2": "python3-jinja2", "markupsafe": "python3-markupsafe",
               "psutil": "python3-psutil", "paho-mqtt": "python3-paho-mqtt",
               "ldap3": "python3-ldap3", "authlib": "python3-authlib",
               "cryptography": "python3-cryptography"},
    "opensuse": {"pyserial": "python3-pyserial", "starlette": "python3-starlette",
                 "uvicorn": "python3-uvicorn", "h11": "python3-h11",
                 "websockets": "python3-websockets",
                 "python-multipart": "python3-python-multipart", "jinja2": "python3-Jinja2",
                 "markupsafe": "python3-MarkupSafe", "psutil": "python3-psutil",
                 "paho-mqtt": "python3-paho-mqtt", "ldap3": "python3-ldap3",
                 "authlib": "python3-Authlib", "cryptography": "python3-cryptography"},
    "arch": {"pyserial": "python-pyserial", "starlette": "python-starlette",
             "uvicorn": "uvicorn", "h11": "python-h11", "websockets": "python-websockets",
             "python-multipart": "python-python-multipart", "jinja2": "python-jinja",
             "markupsafe": "python-markupsafe", "psutil": "python-psutil",
             "paho-mqtt": "python-paho-mqtt", "ldap3": "python-ldap3",
             "authlib": "python-authlib", "cryptography": "python-cryptography"},
    "alpine": {"pyserial": "py3-pyserial", "starlette": "py3-starlette", "uvicorn": "uvicorn",
               "h11": "py3-h11", "websockets": "py3-websockets",
               "python-multipart": "py3-python-multipart", "jinja2": "py3-jinja2",
               "markupsafe": "py3-markupsafe", "psutil": "py3-psutil"},
}
# what a check step may not run: a test that starts ser2netweb or listens on a port
SERVER = re.compile(r"\bfree_port\(|\bServer\(|start_server\(|create_server\(|ser2netweb\.py\b")
NOT_SER2NET = re.compile(r"(?<![\w-])ser2net(?![\w-])")


def read(*parts):
    with open(os.path.join(PKG, *parts), encoding="utf-8") as fh:
        return fh.read()


def _requirement(text):
    m = re.fullmatch(r"\s*([A-Za-z0-9._-]+)\s*>=\s*([0-9][\w.]*)\s*(?:;\s*(.+))?", text)
    assert m, f"pyproject.toml: {text!r} is not name>=floor[; marker]"
    return m.group(1).lower(), m.group(2), m.group(3)


def pyproject():
    """({name: floor} of the runtime dependencies a distribution package has, {name: floor}
    of the extras' packages)."""
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        project = tomllib.load(fh)["project"]
    runtime = {}
    for text in project["dependencies"]:
        name, floor, marker = _requirement(text)
        if marker is None and name != LEFT_OUT:
            runtime[name] = floor
        else:  # only for Python 3.10 (or the one left out)
            assert name == LEFT_OUT or marker == 'python_version < "3.11"', text
    extras = {}
    for extra, reqs in project["optional-dependencies"].items():
        if extra != "all":
            for text in reqs:
                name, floor, _m = _requirement(text)
                extras[name] = floor
    return runtime, extras


def debian_paragraphs():
    text = read("debian", "debian", "control")
    out = []
    for block in text.strip().split("\n\n"):
        fields, key = {}, None
        for line in block.splitlines():
            if line.startswith((" ", "\t")):
                fields[key] += "\n" + line.strip()
            else:
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        out.append(fields)
    return out


def debian_relations(value):
    """{package: floor or None} of a Depends-style field (substitutions left out)."""
    out = {}
    for item in value.replace("\n", " ").split(","):
        item = re.sub(r"<[^>]*>", "", item).strip()
        if not item or item.startswith("${"):
            continue
        m = re.fullmatch(r"([a-z0-9.+-]+)(?:\s*\((>=|=)\s*([^)\s]+)\))?", item)
        assert m, f"debian/control: {item!r}"
        out[m.group(1)] = m.group(3) if m.group(2) != "=" else "=" + m.group(3)
    return out


def spec_tags(text, tag):
    return [m.group(1).strip() for m in re.finditer(rf"(?m)^{tag}:\s*(.+)$", text)]


def test_versions():
    # the recipes name one release, at or before the checkout's version: a release commit
    # raises the version before its tag (and the tarball the recipes hash) exists, and
    # tools/bump_recipes.py moves them once it is out
    version = br.debian_version(read("debian", "debian", "changelog"))
    assert tuple(map(int, version.split("."))) <= tuple(map(int, br.current_version().split("."))), \
        version
    for name in ("fedora", "opensuse"):
        assert br.spec_field(read(name, "ser2netweb.spec"), "Version") == version, name
    fedora = read("fedora", "ser2netweb.spec")
    newest = fedora.split("%changelog\n", 1)[1].splitlines()[0]
    assert newest.endswith(f" - {version}-1") or newest.endswith(f" - {version}-1%{{?dist}}"), \
        newest
    changes = read("opensuse", "ser2netweb.changes")
    assert re.search(rf"^- (?:Update to version|Initial package of ser2netweb) "
                     rf"{re.escape(version)}:?$", changes.split("\n\n", 2)[1], re.M), changes
    assert br.shell_var(read("arch", "ser2netweb", "PKGBUILD"), "pkgver") == version
    assert f"\tpkgver = {version}\n" in read("arch", "ser2netweb", ".SRCINFO")
    assert br.shell_var(read("alpine", "ser2netweb", "APKBUILD"), "pkgver") == version
    # the release's tarball (Arch and Alpine give its checksum; the others get it by URL)
    url = br.TARBALL_URL.format(version="$pkgver")
    assert f"$pkgname-$pkgver.tar.gz::{url.replace('$pkgver', '$pkgver')}".replace(
        "https://github.com/haliskilic/ser2netweb", "$url") in read("arch", "ser2netweb",
                                                                     "PKGBUILD")
    assert f"$pkgname-$pkgver.tar.gz::{url}" in read("alpine", "ser2netweb", "APKBUILD")
    assert "Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz" in fedora
    print(f"every recipe is at {version} (Debian {version}+ds), its changelog too  OK")


def test_dependencies():
    runtime, extras = pyproject()
    assert LEFT_OUT not in runtime and runtime, runtime
    # Debian: Depends and the tests' Build-Depends with the floors; extras recommended
    source, binary = debian_paragraphs()
    want = {NAMES["debian"][n]: f for n, f in runtime.items()}
    assert debian_relations(binary["Depends"]) == want, debian_relations(binary["Depends"])
    assert "${python3:Depends}" in binary["Depends"] and "${misc:Depends}" in binary["Depends"]
    assert debian_relations(source["Build-Depends-Indep"]) == {**want, "tzdata": None}, \
        "the tests' dependencies (tzdata: the time stamp tests)"
    assert br.shell_var(read("alpine", "ser2netweb", "APKBUILD"), "checkdepends") == "tzdata"
    assert debian_relations(binary["Recommends"]) == {NAMES["debian"][n]: f
                                                      for n, f in extras.items()}
    build = debian_relations(source["Build-Depends"])
    for tool in ("dh-sequence-python3", "pybuild-plugin-pyproject", "python3-all"):
        assert tool in build, tool
    assert build["python3-setuptools"] == "77", "pyproject.toml's build-system needs 77"
    assert source["Rules-Requires-Root"] == "no"
    # Fedora: Requires and BuildRequires come from pyproject.toml, without the one left out
    fedora = read("fedora", "ser2netweb.spec")
    assert "%pyproject_buildrequires" in fedora and "%pyproject_save_files -l ser2netweb" \
        in fedora and "%pyproject_wheel" in fedora and "%pyproject_install" in fedora
    sed = re.search(r"(?m)^sed -i '/(.+)/d' pyproject\.toml$", fedora)
    assert sed, "Fedora's %prep takes pyserial-asyncio-fast out of pyproject.toml"
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        hit = [ln for ln in fh.read().splitlines() if re.search(sed.group(1), ln)]
    assert len(hit) == 1 and LEFT_OUT in hit[0] and hit[0].strip().startswith('"'), hit
    assert not spec_tags(fedora, "Requires"), "Fedora's Requires are generated"
    recommends = {}
    for tag in spec_tags(fedora, "Recommends"):
        m = re.fullmatch(r"python3dist\(([\w.-]+)\) >= (\S+)", tag)
        assert m, tag
        recommends[m.group(1)] = m.group(2)
    assert recommends == extras, recommends
    # openSUSE: Requires and the tests' BuildRequires with the floors
    suse = read("opensuse", "ser2netweb.spec")
    requires = dict(re.fullmatch(r"(\S+) >= (\S+)", t).groups()
                    for t in spec_tags(suse, "Requires") if not t.startswith("group("))
    assert requires == {NAMES["opensuse"][n]: f for n, f in runtime.items()}, requires
    assert "group(dialout)" in spec_tags(suse, "Requires")
    modules = {}
    for tag in spec_tags(suse, "BuildRequires"):
        m = re.fullmatch(r"%\{python_module (\S+)(?: >= (\S+))?\}", tag)
        if m:
            modules[m.group(1)] = m.group(2)
    for name, floor in runtime.items():
        assert modules[NAMES["opensuse"][name][len("python3-"):]] == floor, name
    assert modules["setuptools"] == "77"
    assert dict(re.fullmatch(r"(\S+) >= (\S+)", t).groups()
                for t in spec_tags(suse, "Recommends")) == \
        {NAMES["opensuse"][n]: f for n, f in extras.items()}
    # Arch, Alpine: names only (their formats take no floors that pacman / apk would check
    # against a distribution's newest releases)
    pkgbuild = read("arch", "ser2netweb", "PKGBUILD")
    assert sorted(br.shell_array(pkgbuild, "depends")) == \
        sorted(["python", *(NAMES["arch"][n] for n in runtime)])
    assert sorted(br.shell_array(pkgbuild, "makedepends")) == \
        ["python-build", "python-installer", "python-setuptools", "python-wheel"]
    assert sorted(w.split(":")[0] for w in br.shell_array(pkgbuild, "optdepends")) == \
        sorted(NAMES["arch"][n] for n in extras)
    apkbuild = read("alpine", "ser2netweb", "APKBUILD")
    assert sorted(br.apk_list(apkbuild, "depends")) == \
        sorted(["python3", *(NAMES["alpine"][n] for n in runtime)])
    for text in (pkgbuild, apkbuild, fedora, suse, read("debian", "debian", "rules")):
        assert re.search(rf"(?m)^#.*{LEFT_OUT}", text), "says why it is left out"
    print(f"the {len(runtime)} runtime dependencies and {len(extras)} extras of "
          "pyproject.toml in every recipe (with the floors in Debian's, Fedora's and "
          "openSUSE's), pyserial-asyncio-fast left out with its reason  OK")


def test_no_ser2net():
    for fields in debian_paragraphs():
        for key in ("Provides", "Conflicts", "Replaces", "Breaks"):
            assert key not in fields, f"debian/control: {key}"
    for name in ("fedora", "opensuse"):
        text = read(name, "ser2netweb.spec")
        for tag in ("Provides", "Conflicts", "Obsoletes", "Supplements", "Enhances"):
            for value in spec_tags(text, tag):
                assert value.startswith("bundled("), f"{name}: {tag}: {value}"
    pkgbuild = read("arch", "ser2netweb", "PKGBUILD")
    assert br.shell_array(pkgbuild, "conflicts") == ["ser2netweb-bin"]
    for key in ("provides", "replaces"):
        assert not re.search(rf"(?m)^{key}=", pkgbuild), key
    apkbuild = read("alpine", "ser2netweb", "APKBUILD")
    for key in ("provides", "replaces", "install_if"):
        assert not re.search(rf"(?m)^{key}=", apkbuild), key
    # nor does any other relation (a dependency on it, say)
    relations = []
    for fields in debian_paragraphs():
        relations += [v for k, v in fields.items() if "Depends" in k or k in ("Recommends",
                                                                             "Suggests")]
    for name in ("fedora", "opensuse"):
        text = read(name, "ser2netweb.spec")
        relations += [m.group(0) for m in re.finditer(
            r"(?m)^(?:Build)?(?:Requires|Recommends|Suggests)(?:\([\w,]+\))?:.*$", text)]
    for key in ("depends", "makedepends", "checkdepends", "optdepends", "conflicts"):
        if re.search(rf"(?m)^{key}=", pkgbuild):
            relations += br.shell_array(pkgbuild, key)
    relations += [ln for ln in read("arch", "ser2netweb", ".SRCINFO").splitlines()
                  if "depends = " in ln or "conflicts = " in ln or "provides = " in ln]
    for key in ("depends", "makedepends", "checkdepends"):
        if re.search(rf"(?m)^{key}=", apkbuild):
            relations += br.apk_list(apkbuild, key)
    assert relations and not [r for r in relations if NOT_SER2NET.search(r)], relations
    print("no recipe provides, conflicts with, replaces or obsoletes ser2net (Arch: only "
          "ser2netweb-bin)  OK")


def test_units():
    packaged = parse_text(build_packages.unit())
    for name, rel in UNITS.items():
        unit = parse(os.path.join(PKG, rel))
        want = {s: list(v) for s, v in packaged.items()}
        if name == "arch":  # Arch has no dialout group; its serial ports are uucp's
            want["Service"] = [(k, "uucp" if (k, v) == ("SupplementaryGroups", "dialout") else v)
                               for k, v in want["Service"]]
        assert unit == want, f"{rel} is not the .deb / .rpm's unit: {unit} != {want}"
        assert ("ExecStart", "/usr/bin/ser2netweb --data-dir /var/lib/ser2netweb") in \
            unit["Service"], rel
        text = read(rel)
        assert text.startswith("[Unit]\n# The ser2netweb package's service ("), rel
        assert "sudo systemctl enable --now ser2netweb" in text, rel
    for name, folder in ACCOUNT_FILES.items():
        for mine, theirs in (("ser2netweb.sysusers", "sysusers.conf"),
                             ("ser2netweb.tmpfiles", "tmpfiles.conf")):
            assert _entries(read(folder, mine)) == _entries(read(theirs)), (name, mine)
    # the recipes install them as the .deb / .rpm does
    for text in (read("fedora", "ser2netweb.spec"), read("opensuse", "ser2netweb.spec")):
        for where in ("%{_unitdir}/ser2netweb.service", "%{_sysusersdir}/ser2netweb.conf",
                      "%{_tmpfilesdir}/ser2netweb.conf"):
            assert re.search(rf"(?m)^install -Dpm 0644 %{{SOURCE\d}} %{{buildroot}}"
                             rf"{re.escape(where)}$", text), where
        assert "%attr(0700,ser2netweb,ser2netweb) %dir %{_sharedstatedir}/ser2netweb" in text
    pkgbuild = read("arch", "ser2netweb", "PKGBUILD")
    for line in ('install -Dm644 ../ser2netweb.service "$pkgdir/usr/lib/systemd/system/'
                 'ser2netweb.service"',
                 'install -Dm644 ../ser2netweb.sysusers "$pkgdir/usr/lib/sysusers.d/'
                 'ser2netweb.conf"',
                 'install -Dm644 ../ser2netweb.tmpfiles "$pkgdir/usr/lib/tmpfiles.d/'
                 'ser2netweb.conf"'):
        assert line in pkgbuild, line
    rules = read("debian", "debian", "rules")
    assert "dh_installsystemd --no-enable" in rules, "installed, not enabled (as the .deb)"
    assert "dh-sequence-installsysusers" in read("debian", "debian", "control")
    # Alpine (OpenRC): the same command, account, groups and data directory
    initd = read("alpine", "ser2netweb", "ser2netweb.initd")
    assert initd.startswith("#!/sbin/openrc-run\n")
    for needle in (': "${SER2NETWEB_DATA_DIR:=/var/lib/ser2netweb}"',
                   'command="/usr/bin/ser2netweb"',
                   'command_args="--data-dir $SER2NETWEB_DATA_DIR $SER2NETWEB_ARGS"',
                   'command_user="ser2netweb:ser2netweb"', 'supervisor="supervise-daemon"',
                   'checkpath -d -m 0700 -o ser2netweb:ser2netweb "$SER2NETWEB_DATA_DIR"',
                   'supervise-daemon "$RC_SVCNAME" --signal HUP'):
        assert needle in initd, needle
    pre = read("alpine", "ser2netweb", "ser2netweb.pre-install")
    assert "adduser -S -D -H -h /var/lib/ser2netweb -s /sbin/nologin -G ser2netweb" in pre
    assert "addgroup ser2netweb dialout" in pre, "the group of the serial ports (initgroups)"
    assert 'rc_ulimit="-n 65536"' in read("alpine", "ser2netweb", "ser2netweb.confd"), \
        "LimitNOFILE=65536"
    print("the units are the .deb / .rpm's (Arch: uucp), the sysusers.d / tmpfiles.d entries "
          "packaging/'s, Alpine's OpenRC service the same command, account and folder  OK")


def parse_text(text):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "ser2netweb.service")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return parse(path)


def _entries(text):
    return [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]


def check_tests():
    """Each recipe's list of the tests its check step runs."""
    rules = read("debian", "debian", "rules")
    block = re.search(r"(?ms)^CHECK_TESTS = \\\n(.*?)\n\n", rules).group(1)
    out = {"debian": [w.strip(" \t\\") for w in block.splitlines()]}
    for name in ("fedora", "opensuse"):
        text = read(name, "ser2netweb.spec")
        block = re.search(r"(?ms)^for t in \\\n(.*?)^    ; do$", text).group(1)
        out[name] = [w.strip(" \\") for w in block.splitlines()]
    out["arch"] = br.shell_array(read("arch", "ser2netweb", "PKGBUILD"), "_check_tests")
    out["alpine"] = br.apk_list(read("alpine", "ser2netweb", "APKBUILD"), "_check_tests")
    return out


def test_check_steps():
    lists = check_tests()
    first = lists["debian"]
    assert first == sorted(first) and len(first) == len(set(first)) and len(first) >= 20, first
    for name, tests in lists.items():
        assert tests == first, f"{name}'s check step runs other tests"
    for test in first:
        assert test in run_all.PORTABLE, f"{test}: not a portable test of run_all.py"
        with open(os.path.join(HERE, test), encoding="utf-8") as fh:
            assert not SERVER.search(fh.read()), f"{test} starts a server"
    # how each runs them
    assert "PYTHONDONTWRITEBYTECODE=1 $$py tests/$$t" in read("debian", "debian", "rules")
    assert "PYTHONDONTWRITEBYTECODE=1 %{python3} tests/$t" in read("fedora", "ser2netweb.spec")
    assert "%pyproject_check_import" in read("fedora", "ser2netweb.spec")
    assert "PYTHONDONTWRITEBYTECODE=1 python3 tests/$t" in read("opensuse", "ser2netweb.spec")
    assert 'PYTHONDONTWRITEBYTECODE=1 python "tests/$t"' in read("arch", "ser2netweb", "PKGBUILD")
    assert "PYTHONDONTWRITEBYTECODE=1 python3 tests/$t" in read("alpine", "ser2netweb",
                                                                   "APKBUILD")
    print(f"every check step runs the same {len(first)} tests (portable, no server)  OK")


def test_distro_file():
    # (Fedora's and Alpine's names of the extras' packages: python3-<name>, py3-<name>)
    names = dict(NAMES, fedora={d: f"python3-{d}" for d in optional.EXTRAS},
                 alpine={d: f"py3-{d}" for d in optional.EXTRAS})
    for name in ("debian", "fedora", "opensuse", "arch", "alpine"):
        _text, info = _distro_files.distro_file(name)
        assert set(info) == {"distribution", "data_dir", "optional"}, (name, info)
        assert info["data_dir"] == "/var/lib/ser2netweb", (name, info)
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, layout.DISTRO_FILE), "w", encoding="utf-8") as fh:
                json.dump(info, fh)
            assert layout.distro_info(tmp) == info, name
        for dist in optional.EXTRAS:
            hint = optional.Problem(dist, True, "", distro=info).hint
            assert hint.startswith(("sudo ", "doas ")) and hint.split()[-1] == \
                names[name][dist], (name, dist, hint)
    raw, _info = _distro_files.distro_file("fedora")
    assert '"%{?fedora:Fedora}%{?rhel:EPEL}"' in raw, "EPEL's build names EPEL"
    assert "ser2netweb/distro.json" in _entries(read("debian", "debian", "clean"))
    print("every recipe writes ser2netweb/distro.json: its distribution, /var/lib/ser2netweb, "
          "the command for each extra's package  OK")


def test_bump_check():
    now = dt.datetime(2026, 11, 13, tzinfo=dt.timezone.utc)
    recipes = br.debian_version(read("debian", "debian", "changelog"))  # (the recipes' release)
    stale = [rel for rel, text in br.planned(recipes, None, now).items()
             if text != br.read(rel)]
    assert not stale, f"python3 tools/bump_recipes.py --check: out of date: {stale}"
    # and a release moves every recipe (on copies: nothing is written)
    moved = br.planned("9.8.7", b"tarball", now)
    assert br.debian_version(moved[br.DEBIAN_CHANGELOG]) == "9.8.7"
    assert moved[br.DEBIAN_CHANGELOG].startswith("ser2netweb (9.8.7+ds-1) unstable;")
    assert br.spec_field(moved[br.FEDORA_SPEC], "Version") == "9.8.7"
    assert br.spec_field(moved[br.FEDORA_SPEC], "Release") == "1%{?dist}"
    assert "- 9.8.7-1\n- Update to 9.8.7\n" in moved[br.FEDORA_SPEC]
    assert br.spec_field(moved[br.SUSE_SPEC], "Version") == "9.8.7"
    assert "- Update to version 9.8.7:" in moved[br.SUSE_CHANGES]
    import hashlib
    tar256, tar512 = (hashlib.sha256(b"tarball").hexdigest(),
                      hashlib.sha512(b"tarball").hexdigest())
    assert br.shell_var(moved[br.PKGBUILD], "pkgver") == "9.8.7"
    assert br.shell_var(moved[br.PKGBUILD], "pkgrel") == "1"
    assert br.shell_array(moved[br.PKGBUILD], "sha256sums")[0] == tar256
    assert f"\tsha256sums = {tar256}\n" in moved[br.SRCINFO]
    assert br.shell_var(moved[br.APKBUILD], "pkgrel") == "0"
    assert f"{tar512}  ser2netweb-9.8.7.tar.gz\n" in moved[br.APKBUILD]
    print("tools/bump_recipes.py --check: nothing to change; a release moves every recipe  OK")


def test_debian_copyright():
    text = read("debian", "debian", "copyright")
    assert text.startswith("Format: https://www.debian.org/doc/packaging-manuals/"
                           "copyright-format/1.0/\n")
    header = text.split("\n\n", 1)[0]
    assert re.search(r"(?m)^Files-Excluded:\n vendor/wheels$", header), "the +ds repacking"
    licences = {}
    files = []
    for block in text.split("\n\n"):
        if block.startswith("License: ") and "\nFiles:" not in block:
            name, _, body = block.partition("\n")
            licences[name[len("License: "):]] = body
        elif block.startswith("Files: "):
            files.append(block)
    agpl = "\n".join("" if ln == " ." else ln[1:] for ln in licences["AGPL-3.0-only"].split("\n"))
    with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as fh:
        assert agpl.strip("\n") == "\n".join(ln.rstrip() for ln in fh.read().splitlines()).strip(
            "\n"), "the AGPL-3.0-only text is LICENSE's"
    assert set(licences) == {"AGPL-3.0-only", "BSD-3-clause", "Expat", "0BSD"}, set(licences)
    covered = " ".join(re.sub(r"(?m)^(?!Files:| ).*$", "", b) for b in files).split()
    with open(os.path.join(ROOT, "vendor", "npm", "package.json"), encoding="utf-8") as fh:
        record = json.load(fh)["ser2net"]
    for path in [*record["files"], "ser2netweb/engine/serial_transport.py",
                 "ser2netweb/web/starlette_compat.py"]:
        assert path in covered, f"debian/copyright: no paragraph for {path}"
    for target, _member in missing_sources.WANT.values():
        assert re.search(rf"(?m)^Files: debian/missing-sources/{re.escape(target)}", text), target
    assert set(missing_sources.WANT) == {e["package"] for e in record["files"].values()}, \
        "missing_sources.py fetches the source of every static file's package"
    print("debian/copyright: DEP-5, Files-Excluded vendor/wheels, LICENSE as AGPL-3.0-only, a "
          "paragraph per browser library, copied module and missing source  OK")


def test_debian_layout():
    for rel in ("rules", "tests/smoke"):
        mode = _git_mode(f"packaging/debian/debian/{rel}")
        assert mode in (None, "100755"), f"debian/{rel}: mode {mode}"
    assert read("debian", "debian", "source", "format").strip() == "3.0 (quilt)"
    tests = read("debian", "debian", "tests", "control")
    assert "Tests: smoke\nDepends: @\n" in tests
    watch = read("debian", "debian", "watch")
    assert "version=4" in watch and "repacksuffix=+ds" in watch and \
        "dversionmangle=s/\\+ds\\d*$//" in watch
    assert "Source: ser2netweb\n" in read("debian", "debian", "control")
    assert "\nPackage: ser2netweb\nArchitecture: all\n" in read("debian", "debian", "control")
    print("debian/: rules and tests/smoke executable, 3.0 (quilt), autopkgtest, uscan +ds  OK")


def _git_mode(rel):
    git = shutil.which("git")
    if not git or not os.path.isdir(os.path.join(ROOT, ".git")) and \
            not os.path.isfile(os.path.join(ROOT, ".git")):
        return None
    p = subprocess.run([git, "ls-files", "-s", rel], cwd=ROOT, capture_output=True, text=True)
    return p.stdout.split()[0] if p.returncode == 0 and p.stdout.strip() else None


def test_workflow():
    text = read_workflow("distro-packages.yml")
    on = text.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    triggers = re.findall(r"(?m)^  ([a-z_]+):", on)
    assert triggers == ["pull_request", "workflow_dispatch", "schedule"], triggers
    for path in ("packaging/debian/**", "packaging/fedora/**", "packaging/opensuse/**",
                 "packaging/arch/**", "packaging/alpine/**",
                 ".github/workflows/distro-packages.yml", "tests/distro_build.sh",
                 "tests/distro_smoke.py", "tools/bump_recipes.py"):
        assert f"\n      - {path}\n" in on, path
    assert "      tag:\n" in on and re.search(r'(?m)^    - cron: "[^"]+"', on), on
    assert re.search(r"(?m)^permissions:\n  contents: read\n", text)
    all_jobs = jobs(text)
    assert set(all_jobs) == {"debian", "rpm", "arch", "alpine"}, set(all_jobs)
    targets = set()
    for job, block in all_jobs.items():
        assert permissions(block) in ({}, {"contents": "read"}), job
        job_steps = steps(block)
        assert "actions/checkout@" in job_steps[0] and "persist-credentials: false" in \
            job_steps[0], job
        assert any('sh tests/distro_build.sh prepare "$TAG"' in s and "TAG: ${{ inputs.tag }}"
                   in s for s in job_steps), job
        for s in job_steps:
            m = re.search(r'docker run --rm -v "\$PWD:/src" -w /src (\S+) sh tests/'
                          r'distro_build.sh (\S+)', s)
            if m:
                targets.add(m.group(2).strip('"'))
    targets |= set(re.findall(r"(?m)^            target: (\w+)$", text))
    targets.discard("$TARGET")
    assert targets == {"debian", "fedora", "epel", "opensuse", "arch", "alpine"}, targets
    for image in ("debian:unstable", "debian:trixie", "ubuntu:26.04", "fedora:rawhide",
                  "fedora:latest", "almalinux:10", "opensuse/tumbleweed", "archlinux:latest",
                  "alpine:edge"):
        assert image in text, image
    assert "continue-on-error: ${{ matrix.target == 'epel' }}" in all_jobs["rpm"]
    assert "distro-packages" not in read_workflow("ci.yml"), "a merge condition"
    with open(os.path.join(HERE, "distro_build.sh"), encoding="utf-8") as fh:
        script = fh.read()
    for target in targets:
        assert f"\nbuild_{target}() {{\n" in script, target
    print("distro-packages.yml: pull requests on the recipes, by hand with a tag, weekly; "
          f"{len(targets)} targets in containers, read-only, not in ci.yml  OK")


if __name__ == "__main__":
    test_versions()
    test_dependencies()
    test_no_ser2net()
    test_units()
    test_check_steps()
    test_distro_file()
    test_bump_check()
    test_debian_copyright()
    test_debian_layout()
    test_workflow()
    print("\nPASS: distribution recipes")
