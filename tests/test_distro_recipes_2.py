"""The nixpkgs, Void Linux, Gentoo (GURU), FreeBSD ports and MacPorts recipes of ser2netweb
(packaging/nix, packaging/void, packaging/gentoo, packaging/freebsd, packaging/macports),
tools/bump_recipes_2.py and .github/workflows/distro-packages-2.yml, which builds them.

  * every recipe builds the same release from its git tag tarball: ser2netweb/__init__.py's
    version, or one before it (a release pull request raises the version before its tag,
    and with it the tarball the checksums are of, exists; the weekly run then warns);
  * the dependencies each names are pyproject.toml's, by the distribution's names, and the
    extras' packages are offered (an option, a variant, an optional feature) or named as
    missing in that distribution; a dependency the distribution lacks has its recipe here;
  * none provides, replaces or conflicts with ser2net, Corey Minyard's unrelated program:
    the command, the service and the account are ser2netweb's, and "ser2net" on its own
    appears only in the sentences that say so;
  * the checksums agree: the SHA-256 of Void, FreeBSD and MacPorts, the size of FreeBSD,
    MacPorts and Gentoo; given these checksums, tools/bump_recipes_2.py writes the recipes
    unchanged, and for another release it moves each one (version, checksums, revision);
  * the workflow: pull requests touching the recipes, by hand with a tag, weekly; a
    read-only token; one job per target after the checksum job; images, trees and
    downloads pinned; not run by any other workflow.

Run: python3 tests/test_distro_recipes_2.py   (stdlib only; binds nothing)
"""
from __future__ import annotations

import base64
import io
import os
import re
import shutil
import sys
import tarfile
import tempfile

from _workflows import jobs, names, read, steps

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import bump_recipes_2 as bump

try:
    import tomllib
except ImportError:  # Python 3.10: pip's copy
    from pip._vendor import tomli as tomllib

PKG = os.path.join(ROOT, "packaging")
NIX = os.path.join(PKG, "nix")
VOID = os.path.join(PKG, "void", "srcpkgs")
GENTOO = os.path.join(PKG, "gentoo")
FREEBSD = os.path.join(PKG, "freebsd")
MACPORTS = os.path.join(PKG, "macports")
TAG_TARBALL = "https://github.com/haliskilic/ser2netweb/archive/refs/tags/v"

# the extras' packages a distribution doesn't have (docs/PACKAGING.md says so too)
MISSING_EXTRAS = {
    "void": {"authlib"},
    "macports": {"authlib", "paho-mqtt"},  # (no py-authlib; py-paho-mqtt stops at 3.13)
}


def text(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def one(pattern: str, body: str, what: str) -> str:
    found = re.findall(pattern, body, flags=re.M)
    assert len(found) == 1, f"{what}: expected one match of {pattern!r}, found {found}"
    return found[0]


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def ser2netweb_version() -> str:
    return one(r'^__version__ = "(.*)"$', text(ROOT, "ser2netweb", "__init__.py"), "__init__")


def pyproject() -> tuple[set[str], set[str]]:
    """(the dependencies every Python 3.11+ needs, the packages of the extras), normalized."""
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        project = tomllib.load(fh)["project"]

    def name(req: str) -> str:
        return norm(re.match(r"[A-Za-z0-9._-]+", req).group(0))

    required = {name(r) for r in project["dependencies"] if 'python_version < "3.11"' not in r}
    extras = {name(r) for extra, reqs in project["optional-dependencies"].items()
              if extra != "all" for r in reqs}
    return required, extras


# --- each recipe: (version, required deps, extras' packages) -------------------------------

def nix() -> tuple[str, set[str], set[str]]:
    body = text(NIX, "package.nix")
    version = one(r'^  version = "(.*)";$', body, "package.nix")
    m = re.search(r"(?ms)^  dependencies = with python3Packages; \[\n(.*?)^  \];", body)
    assert m, "package.nix: dependencies = with python3Packages; [ ... ];"
    block = m.group(1)
    required = {norm(n) for n in block.split()}
    m = re.search(r"(?ms)^  optional-dependencies = with python3Packages; \{\n(.*?)^  \};", body)
    assert m, "package.nix: optional-dependencies"
    extras = {norm(n) for n in re.findall(r"= \[ ([\w-]+) \];", m.group(1))}
    return version, required, extras


def _void_name(pkg: str) -> str:
    assert pkg.startswith("python3-"), pkg
    return norm(pkg[len("python3-"):])


def void() -> tuple[str, set[str], set[str]]:
    body = text(VOID, "ser2netweb", "template")
    version = one(r"^version=(.*)$", body, "template")
    depends = one(r'(?s)^depends="(.*?)"$', body, "template depends")
    required = {_void_name(p) for p in depends.split()}
    msg = text(VOID, "ser2netweb", "INSTALL.msg")
    extras = {_void_name(p) for p in re.findall(r"python3-[\w.-]*\w", msg)}
    return version, required, extras


def gentoo() -> tuple[str, set[str], set[str]]:
    folder = os.path.join(GENTOO, "net-misc", "ser2netweb")
    ebuilds = [n for n in os.listdir(folder) if n.endswith(".ebuild")]
    assert len(ebuilds) == 1, ebuilds
    version = one(r"^ser2netweb-(\d+\.\d+\.\d+)\.ebuild$", ebuilds[0], "ebuild name")
    body = text(folder, ebuilds[0])
    rdepend = one(r'(?s)^RDEPEND="(.*?)"$', body, "RDEPEND")
    required = {norm(n) for n in re.findall(r"dev-python/([\w.-]+)\[\$\{PYTHON_USEDEP\}\]",
                                             rdepend)}
    extras = {norm(n) for n in re.findall(r'^\toptfeature ".*" dev-python/([\w.-]+)$', body,
                                          flags=re.M)}
    return version, required, extras


def freebsd() -> tuple[str, set[str], set[str]]:
    body = text(FREEBSD, "net", "ser2netweb", "Makefile")
    version = one(r"^DISTVERSION=\t(\S+)$", body, "Makefile")
    run = one(r"(?ms)^RUN_DEPENDS=\t(.*?[^\\])$", body, "RUN_DEPENDS")
    required = {norm(n) for n in re.findall(r"\$\{PYTHON_PKGNAMEPREFIX\}([\w.-]+?)>=", run)}
    extras = {norm(n) for n in re.findall(
        r"^[A-Z]+_RUN_DEPENDS=\t\$\{PYTHON_PKGNAMEPREFIX\}([\w.-]+?)>=", body, flags=re.M)}
    return version, required, extras


_MACPORTS_NAMES = {"serial": "pyserial"}  # (py-serial is pyserial)


def macports() -> tuple[str, set[str], set[str]]:
    body = text(MACPORTS, "net", "ser2netweb", "Portfile")
    version = one(r"^github\.setup\s+haliskilic ser2netweb (\S+) v$", body, "Portfile")
    main, _, variants = body.partition("\nvariant ")
    ports = r"port:py\$\{python\.version\}-([\w.-]+)"
    required = {norm(_MACPORTS_NAMES.get(n, n)) for n in re.findall(ports, main)}
    extras = {norm(_MACPORTS_NAMES.get(n, n)) for n in re.findall(ports, variants)}
    return version, required, extras


RECIPES = {"nix": nix, "void": void, "gentoo": gentoo, "freebsd": freebsd,
           "macports": macports}


def test_one_release_from_its_tag():
    versions = {target: read_recipe()[0] for target, read_recipe in RECIPES.items()}
    assert len(set(versions.values())) == 1, f"the recipes name different releases: {versions}"
    version = versions["nix"]
    current = ser2netweb_version()
    assert version_tuple(version) <= version_tuple(current), (
        f"the recipes build {version}, newer than ser2netweb/__init__.py's {current}")
    # the tag's tarball, by each distribution's way of naming it
    package = text(NIX, "package.nix")
    assert 'owner = "haliskilic";\n    repo = "ser2netweb";\n    tag = "v${finalAttrs.version}";' \
        in package, "nix: fetchFromGitHub of the tag"
    template = text(VOID, "ser2netweb", "template")
    assert f'distfiles="{TAG_TARBALL}${{version}}.tar.gz"' in template, "void: distfiles"
    ebuild = text(GENTOO, "net-misc", "ser2netweb", f"ser2netweb-{version}.ebuild")
    assert ('SRC_URI="https://github.com/haliskilic/${PN}/archive/refs/tags/v${PV}.tar.gz -> '
            '${P}.gh.tar.gz"') in ebuild, "gentoo: SRC_URI"
    makefile = text(FREEBSD, "net", "ser2netweb", "Makefile")
    for line in ("DISTVERSIONPREFIX=\tv", "USE_GITHUB=\tyes", "GH_ACCOUNT=\thaliskilic"):
        assert f"\n{line}\n" in makefile, f"freebsd: {line}"
    assert "GH_TAGNAME" not in makefile, "freebsd: the tag's tarball (GH_TAGNAME = vX.Y.Z)"
    portfile = text(MACPORTS, "net", "ser2netweb", "Portfile")
    assert re.search(r"(?m)^github\.tarball_from archive$", portfile), "macports: archive"
    # the PyPI name, the command and the licence
    assert 'pname = "ser2netweb";' in package and 'mainProgram = "ser2netweb";' in package
    assert "license = lib.licenses.agpl3Only;" in package and "maintainers = [ ];" in package
    assert "pkgname=ser2netweb\n" in template and "PORTNAME=\tser2netweb\n" in makefile
    assert 'LICENSE="AGPL-3 0BSD MIT"' in ebuild
    note = "" if version == current else (f" (ser2netweb/__init__.py is at {current}: "
                                          f"tools/bump_recipes_2.py {current} once its tag is out)")
    print(f"the five recipes build v{version} from its tag's tarball{note}  OK")


def test_dependencies_are_pyprojects():
    required, extras = pyproject()
    for target, read_recipe in RECIPES.items():
        _version, needs, offers = read_recipe()
        assert needs == required, (
            f"{target}: dependencies {sorted(needs)} are not pyproject.toml's {sorted(required)}"
            f" (missing {sorted(required - needs)}, extra {sorted(needs - required)})")
        missing = MISSING_EXTRAS.get(target, set())
        assert offers | missing == extras and not offers & missing, (
            f"{target}: the extras' packages {sorted(offers)} + missing {sorted(missing)} are "
            f"not pyproject.toml's {sorted(extras)}")
    # pyserial-asyncio-fast, where the distribution has none, comes with the recipes
    assert os.path.isfile(os.path.join(VOID, "python3-pyserial-asyncio-fast", "template"))
    gentoo_dep = os.path.join(GENTOO, "dev-python", "pyserial-asyncio-fast")
    assert [n for n in os.listdir(gentoo_dep) if n.endswith(".ebuild")]
    assert os.path.isfile(os.path.join(gentoo_dep, "Manifest"))
    assert os.path.isfile(os.path.join(MACPORTS, "python", "py-pyserial-asyncio-fast",
                                       "Portfile"))
    # Void's python-multipart (0.0.9) is below every floor: the update comes with them
    multipart = text(VOID, "python3-python-multipart", "template")
    assert version_tuple(one(r"^version=(.*)$", multipart, "multipart")) >= (0, 0, 20)
    print(f"each recipe needs pyproject.toml's {len(required)} dependencies and offers its "
          f"extras' {len(extras)} packages (or names the missing ones)  OK")


def _descriptive(path: str, body: str) -> str:
    """The recipe without the sentences that tell ser2netweb and ser2net apart."""
    for start, end in (("  longDescription = ''", "  '';"),          # package.nix
                       ("long_description", "\n\n"),                   # Portfile
                       ("\t<longdescription", "</longdescription>")):  # metadata.xml
        while start in body:
            head, _, rest = body.partition(start)
            body = head + rest.partition(end)[2]
    if os.path.basename(path) == "pkg-descr":
        body = ""
    return body


def test_nothing_of_ser2net():
    files = []
    for folder in (NIX, os.path.join(VOID, "ser2netweb"), GENTOO, FREEBSD,
                   os.path.join(MACPORTS, "net")):
        for base, _dirs, found in os.walk(folder):
            files += [os.path.join(base, n) for n in found]
    assert len(files) > 20, files
    for path in files:
        body = text(path)
        rel = os.path.relpath(path, ROOT)
        for pattern in (r"(?m)^(conflicts|provides|replaces)=.*",   # Void
                        r"![!]?[\w-]+/ser2net\b",                     # Gentoo blockers
                        r"(?m)^CONFLICTS\w*=.*",                      # FreeBSD
                        r"(?m)^\s*conflicts\s.*"):                    # MacPorts
            for hit in re.findall(pattern, body):
                assert "ser2net" not in hit, f"{rel}: {hit}"
        alone = re.findall(r".*\bser2net\b(?!web).*", _descriptive(path, body))
        assert not alone, f"{rel}: ser2net (not ser2netweb) outside its description: {alone}"
    # the command, the service and the account
    run = text(VOID, "ser2netweb", "files", "ser2netweb", "run")
    assert "exec chpst -u _ser2netweb:_ser2netweb:dialout ser2netweb --data-dir " \
           "/var/lib/ser2netweb ${OPTS}" in run, run
    template = text(VOID, "ser2netweb", "template")
    assert 'system_accounts="_ser2netweb"' in template and "vsv ser2netweb" in template
    initd = text(GENTOO, "net-misc", "ser2netweb", "files", "ser2netweb.initd")
    assert 'command="/usr/bin/ser2netweb"' in initd and 'command_user="ser2netweb:ser2netweb"' \
        in initd
    unit = text(GENTOO, "net-misc", "ser2netweb", "files", "ser2netweb.service")
    assert "User=ser2netweb\n" in unit and \
        "ExecStart=/usr/bin/ser2netweb --data-dir /var/lib/ser2netweb\n" in unit
    acct = text(GENTOO, "acct-user", "ser2netweb", "ser2netweb-0.ebuild")
    assert "ACCT_USER_GROUPS=( ser2netweb dialout uucp )" in acct
    module = text(NIX, "module.nix")
    assert "systemd.services.ser2netweb = {" in module and "users.users.ser2netweb = {" in module
    portfile = text(MACPORTS, "net", "ser2netweb", "Portfile")
    assert "startupitem.user        _${name}" in portfile
    print(f"{len(files)} recipe files: no Provides/Conflicts on ser2net; its own command, "
          f"service and account  OK")


def test_freebsd_user_and_group():
    makefile = text(FREEBSD, "net", "ser2netweb", "Makefile")
    assert "\nUSERS=\t\tser2netweb\n" in makefile and "\nGROUPS=\t\tser2netweb dialer\n" in makefile
    user = one(r"^(ser2netweb:.*)$", text(FREEBSD, "UIDs"), "UIDs").split(":")
    group = one(r"^(ser2netweb:.*)$", text(FREEBSD, "GIDs"), "GIDs").split(":")
    uid, gid = int(user[2]), int(group[2])
    assert uid == gid == int(user[3]), (user, group)
    assert 100 <= uid < 1000, "ports' ids are below 1000 (UIDs: DO NOT USE 1000-65533)"
    assert user[8] == "/var/db/ser2netweb" and user[9] == "/usr/sbin/nologin", user
    dialer = one(r"^(dialer:\*:68:.*)$", text(FREEBSD, "GIDs"), "GIDs dialer")
    assert "ser2netweb" in dialer.split(":")[3].split(","), dialer
    rc = text(FREEBSD, "net", "ser2netweb", "files", "ser2netweb.in")
    assert ': ${ser2netweb_dir:="/var/db/ser2netweb"}' in rc
    assert ': ${ser2netweb_user:="ser2netweb"}' in rc and "%%PREFIX%%/bin/ser2netweb" in rc
    print(f"FreeBSD: user and group ser2netweb ({uid}), a member of dialer  OK")


def recorded_checksums() -> dict[str, object]:
    """The checksums as the recipes record them, each in one place."""
    version = nix()[0]
    void_sum = one(r"^checksum=([0-9a-f]{64})$", text(VOID, "ser2netweb", "template"), "void")
    distinfo = text(FREEBSD, "net", "ser2netweb", "distinfo")
    name = bump.distfiles(version)["freebsd"]
    fbsd_sum = one(rf"^SHA256 \({re.escape(name)}\) = ([0-9a-f]{{64}})$", distinfo, "distinfo")
    fbsd_size = int(one(rf"^SIZE \({re.escape(name)}\) = (\d+)$", distinfo, "distinfo"))
    portfile = text(MACPORTS, "net", "ser2netweb", "Portfile")
    mp_rmd = one(r"^checksums\s+rmd160  ([0-9a-f]{40}) \\$", portfile, "Portfile rmd160")
    mp_sum = one(r"^\s+sha256  ([0-9a-f]{64}) \\$", portfile, "Portfile sha256")
    mp_size = int(one(r"^\s+size    (\d+)$", portfile, "Portfile size"))
    manifest = text(GENTOO, "net-misc", "ser2netweb", "Manifest")
    dist = bump.distfiles(version)["gentoo"]
    g_size, g_blake, g_sha512 = one(
        rf"^DIST {re.escape(dist)} (\d+) BLAKE2B ([0-9a-f]{{128}}) SHA512 ([0-9a-f]{{128}})$",
        manifest, "Manifest")
    nar = one(r'^    hash = "(sha256-[A-Za-z0-9+/]{43}=)";$', text(NIX, "package.nix"), "nix")
    assert len(base64.b64decode(nar[len("sha256-"):])) == 32, nar
    assert void_sum == fbsd_sum == mp_sum, ("SHA-256", void_sum, fbsd_sum, mp_sum)
    assert fbsd_size == mp_size == int(g_size), ("size", fbsd_size, mp_size, g_size)
    return {"size": fbsd_size, "sha256": void_sum, "sha512": g_sha512, "blake2b": g_blake,
            "rmd160": mp_rmd, "nar": nar}


def test_checksums_agree():
    sums = recorded_checksums()
    version = nix()[0]
    files, (old, new) = bump.plan(version, sums, 0)
    for path, body in files.items():
        assert bump._untimed(body) == bump._untimed(text(path)), (
            f"tools/bump_recipes_2.py would change {os.path.relpath(path, ROOT)} for the "
            f"checksums the recipes record")
    assert old == new, (old, new)
    print(f"one SHA-256 and size in every recipe ({sums['sha256'][:12]}..., {sums['size']} "
          f"bytes); bump_recipes_2.py {version} with them changes nothing  OK")


def test_bump_moves_every_recipe():
    tmp = tempfile.mkdtemp(prefix="recipes2-")
    saved = {n: getattr(bump, n) for n in ("NIX", "VOID", "GENTOO", "FREEBSD", "MACPORTS")}
    try:
        for target in ("nix", "void", "gentoo", "freebsd", "macports"):
            shutil.copytree(os.path.join(PKG, target), os.path.join(tmp, target))
        for n, path in saved.items():
            setattr(bump, n, os.path.join(tmp, os.path.relpath(path, PKG)))
        # a revision of the release to start from, as after a packaging fix
        for path, line, revised in ((bump.VOID, "revision=1", "revision=3"),
                                    (bump.MACPORTS, "revision            0",
                                     "revision            2")):
            body = text(path)
            assert line in body, (path, line)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body.replace(line, revised))
        makefile = os.path.join(bump.FREEBSD, "Makefile")
        body = text(makefile)
        with open(makefile, "w", encoding="utf-8") as fh:
            fh.write(body.replace("DISTVERSION=\t", "PORTREVISION=\t1\nDISTVERSION=\t", 1)
                     if "PORTREVISION" not in body else body)
        sums = {"size": 123, "sha256": "a" * 64, "sha512": "b" * 128, "blake2b": "c" * 128,
                "rmd160": "d" * 40, "nar": "sha256-" + base64.b64encode(b"e" * 32).decode()}
        files, (old, new) = bump.plan("9.8.7", sums, 1700000000)
        out = {os.path.relpath(p, tmp).replace(os.sep, "/"): t for p, t in files.items()}
        assert '  version = "9.8.7";' in out["nix/package.nix"]
        assert f'    hash = "{sums["nar"]}";' in out["nix/package.nix"]
        void_t = out["void/srcpkgs/ser2netweb/template"]
        assert "version=9.8.7\nrevision=1\n" in void_t and f"checksum={'a' * 64}" in void_t
        assert out["gentoo/net-misc/ser2netweb/Manifest"] == (
            f"DIST ser2netweb-9.8.7.gh.tar.gz 123 BLAKE2B {'c' * 128} SHA512 {'b' * 128}\n")
        assert new.endswith(os.path.join("net-misc", "ser2netweb", "ser2netweb-9.8.7.ebuild"))
        assert old != new
        mk = out["freebsd/net/ser2netweb/Makefile"]
        assert "DISTVERSION=\t9.8.7\n" in mk and "PORTREVISION" not in mk
        assert out["freebsd/net/ser2netweb/distinfo"] == (
            "TIMESTAMP = 1700000000\n"
            f"SHA256 (haliskilic-ser2netweb-v9.8.7_GH0.tar.gz) = {'a' * 64}\n"
            "SIZE (haliskilic-ser2netweb-v9.8.7_GH0.tar.gz) = 123\n")
        port = out["macports/net/ser2netweb/Portfile"]
        assert "github.setup        haliskilic ser2netweb 9.8.7 v\n" in port
        assert "revision            0\n" in port
        assert (f"checksums           rmd160  {'d' * 40} \\\n"
                f"                    sha256  {'a' * 64} \\\n"
                f"                    size    123\n") in port
        before = nix()[0]
        for rel, body in out.items():
            assert before not in body, f"{rel} still names {before}"
    finally:
        for n, path in saved.items():
            setattr(bump, n, path)
        shutil.rmtree(tmp, ignore_errors=True)
    print("bump_recipes_2.py moves the version, the checksums and the revision of each "
          "recipe, and renames the ebuild  OK")


def _tarball(entries) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, mode, data in entries:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o775
            elif isinstance(data, str):
                info.type, info.linkname = tarfile.SYMTYPE, data
            else:
                info.mode, info.size = mode, len(data)
            tar.addfile(info, io.BytesIO(data) if isinstance(data, bytes) else None)
    return buf.getvalue()


def test_nar_hash():
    """fetchFromGitHub's hash of a tarball: the top folder stripped, the executable bit kept,
    the entries in byte order; a member outside the top folder is refused."""
    tree = [("p-1/", 0, None), ("p-1/b.txt", 0o664, b"two"), ("p-1/a/", 0, None),
            ("p-1/a/run.sh", 0o775, b"#!/bin/sh\n"), ("p-1/link", 0, "b.txt")]
    first = bump.nar_sri(_tarball(tree))
    assert re.fullmatch(r"sha256-[A-Za-z0-9+/]{43}=", first), first
    # the folder name and the order of the members don't count
    renamed = [(n.replace("p-1", "other-2"), m, d) for n, m, d in reversed(tree)]
    assert bump.nar_sri(_tarball(renamed)) == first
    # the executable bit, a file's contents and a link's target do
    for changed in ([(n, 0o664 if n.endswith(".sh") else m, d) for n, m, d in tree],
                    [(n, m, b"2" if n.endswith(".txt") else d) for n, m, d in tree],
                    [(n, m, "a" if n.endswith("link") else d) for n, m, d in tree]):
        assert bump.nar_sri(_tarball(changed)) != first
    for bad in ([("p-1/", 0, None), ("q-1/x", 0o644, b"")],
                [("p-1/../x", 0o644, b"")]):
        try:
            bump.nar_sri(_tarball(bad))
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    print("the NAR hash strips the top folder, keeps the executable bit and refuses paths "
          "outside it  OK")


def test_workflow():
    wf = read("distro-packages-2.yml")
    on = wf.split("\non:\n", 1)[1].split("\n\npermissions:", 1)[0]
    assert re.findall(r"(?m)^  ([a-z_]+):", on) == ["pull_request", "workflow_dispatch",
                                                     "schedule"], on
    for path in ("packaging/nix/**", "packaging/void/**", "packaging/gentoo/**",
                 "packaging/freebsd/**", "packaging/macports/**", "tools/bump_recipes_2.py",
                 "tests/test_distro_recipes_2.py", ".github/workflows/distro-packages-2.yml"):
        assert f'      - "{path}"\n' in on, path
    assert "    inputs:\n      tag:\n" in on, "workflow_dispatch takes a tag"
    assert "\npermissions:\n  contents: read\n\n" in wf, "a read-only token"
    all_jobs = jobs(wf)
    assert list(all_jobs) == ["recipes", "nixpkgs", "void", "gentoo", "freebsd", "macports"]
    for name, block in all_jobs.items():
        if name != "recipes":
            assert "    needs: recipes\n" in block, name
        assert "    permissions:" not in block, f"{name}: the workflow's read-only token"
        assert "${{ inputs." not in block.replace("TAG: ${{ inputs.tag }}", ""), name
        for step in steps(block):
            if "actions/checkout@" in step:
                assert "persist-credentials: false" in step, name
            for run in re.findall(r"(?ms)^        run: \|\n(.*?)(?=^      - |\Z)", step):
                assert "${{" not in run, f"{name}: an expression inside a run script"
    recipes = all_jobs["recipes"]
    assert 'python3 tools/bump_recipes_2.py "$VERSION" --check' in recipes
    assert "python3 tests/test_distro_recipes_2.py" in recipes
    # pinned: images by digest, trees by commit, downloads by checksum
    for image in re.findall(r"(?m)^\s+(?:image|STAGE3|PORTAGE): (\S+)", wf):
        assert re.search(r"@sha256:[0-9a-f]{64}$", image), image
    assert re.search(r"nixpkgs/archive/[0-9a-f]{40}\.tar\.gz", all_jobs["nixpkgs"])
    assert re.search(r"VOID_PACKAGES: [0-9a-f]{40}", all_jobs["void"])
    assert re.search(r"PORTS_COMMIT: [0-9a-f]{40}", all_jobs["freebsd"])
    assert len(re.findall(r"sum=[0-9a-f]{64} ;;", all_jobs["macports"])) >= 2
    assert 'shasum -a 256 -c -' in all_jobs["macports"]
    for target, call in (("nixpkgs", "callPackage ./packaging/nix/package.nix {}"),
                         ("void", "./xbps-src -Q pkg"), ("gentoo", "emerge --oneshot"),
                         ("gentoo", "pkgcheck scan"), ("freebsd", "portlint -AC"),
                         ("freebsd", "make DEVELOPER=yes BATCH=yes package"),
                         ("macports", "port lint --nitpick"),
                         ("macports", "sudo port -N -v install ser2netweb")):
        assert call in all_jobs[target], (target, call)
    for target in ("nixpkgs", "void", "gentoo", "freebsd", "macports"):
        assert 'grep -Fx "ser2netweb $VERSION"' in all_jobs[target], target
    for name in names():
        if name != "distro-packages-2.yml":
            assert "distro-packages-2.yml" not in read(name), f"{name} runs distro-packages-2"
    print("distro-packages-2.yml: pull requests, by hand with a tag, weekly; read-only; "
          "pinned; a job per target after the checksums  OK")


if __name__ == "__main__":
    test_one_release_from_its_tag()
    test_dependencies_are_pyprojects()
    test_nothing_of_ser2net()
    test_freebsd_user_and_group()
    test_checksums_agree()
    test_bump_moves_every_recipe()
    test_nar_hash()
    test_workflow()
    print("\nPASS: distribution recipes (nixpkgs, Void, Gentoo, FreeBSD, MacPorts)")
