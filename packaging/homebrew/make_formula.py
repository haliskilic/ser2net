#!/usr/bin/env python3
"""The Homebrew formula of a ser2netweb release, for the tap haliskilic/homebrew-tap
(Formula/ser2netweb.rb; brew install haliskilic/tap/ser2netweb):

    python3 packaging/homebrew/make_formula.py v3.0.0 > Formula/ser2netweb.rb
    python3 packaging/homebrew/make_formula.py v3.0.0 --output Formula/ser2netweb.rb

A Python virtualenv formula: the release tag's source tarball from GitHub (the formula's url,
with its sha256), python@PYTHON, and a resource block for every package ser2netweb needs on
macOS and Linux with that Python: the pins of the tag's requirements.txt and
requirements-optional.txt whose markers hold there (MQTT, LDAP, OIDC and certificates work as
in the standalone executables), and what those need in turn by their PyPI metadata, each at
the version the tag's requirements-lock.txt pins. A resource is the package's source release
(sdist) as PyPI's JSON API lists it, url and sha256; that sha256 must be one the lock file
records for the package. The packages a Homebrew formula provides (PROVIDED: cryptography,
which needs Rust to build, with cffi and pycparser, which it brings) become a depends_on of
that formula instead.

The tap's workflow "bump" runs this file from the new release's tag when one comes out (it
needs nothing but the standard library and the network: github.com and pypi.org) and opens a
pull request with the result. Change the formula here, not in the tap.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
import urllib.error
import urllib.request
from typing import Callable, Optional

REPO = "haliskilic/ser2netweb"
HOMEPAGE = f"https://github.com/{REPO}"
TARBALL_URL = HOMEPAGE + "/archive/refs/tags/{tag}.tar.gz"
PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
# The formula's Python: the newest one every CI test leg runs ser2netweb on (ci.yml), and one
# Homebrew's cryptography formula is built for.
PYTHON = "3.14"
# PyPI package -> the Homebrew formula that provides it (a depends_on, not a resource)
PROVIDED = {"cryptography": "cryptography", "cffi": "cryptography", "pycparser": "cryptography"}
# Where the formula is used: macOS and Linux (Intel and ARM), CPython PYTHON. A package any of
# them needs is a resource (pip installs every resource on each).
ENVIRONMENTS = [
    {"os_name": "posix", "sys_platform": sys_platform, "platform_system": system,
     "platform_machine": machine, "platform_python_implementation": "CPython",
     "implementation_name": "cpython", "python_version": PYTHON,
     "python_full_version": PYTHON + ".0", "implementation_version": PYTHON + ".0",
     "platform_release": "", "platform_version": ""}
    for sys_platform, system, machines in (("darwin", "Darwin", ("arm64", "x86_64")),
                                           ("linux", "Linux", ("x86_64", "aarch64")))
    for machine in machines
]
TAG = re.compile(r"v(\d+\.\d+\.\d+)")  # a release, not a pre-release


class Error(Exception):
    """Why no formula can be made (a message for the person running this)."""


def normalize(name: str) -> str:
    """A package name as PEP 503 compares it."""
    return re.sub(r"[-_.]+", "-", name).lower()


# --- PEP 508 environment markers (the subset package metadata uses) -----------------------
_TOKEN = re.compile(r"""\s*(?:(?P<op>===|==|!=|<=|>=|~=|<|>|\(|\)|not\s+in\b|in\b|and\b|or\b)
                        |'(?P<s1>[^']*)'|"(?P<s2>[^"]*)"|(?P<var>[A-Za-z_][A-Za-z0-9_.]*))""",
                    re.X)
_VERSION_VARS = {"python_version", "python_full_version", "implementation_version"}


def _tokens(marker: str) -> list[tuple[str, str]]:
    out, pos, marker = [], 0, marker.strip()
    while pos < len(marker):
        m = _TOKEN.match(marker, pos)
        if not m or m.end() == pos:
            raise Error(f"can't read the marker {marker!r} at {marker[pos:]!r}")
        pos = m.end()
        if m.group("op"):
            out.append(("op", " ".join(m.group("op").split())))
        elif m.group("var"):
            out.append(("var", m.group("var")))
        else:
            out.append(("str", m.group("s1") if m.group("s1") is not None else m.group("s2")))
    return out


def _version(text: str) -> tuple[int, ...]:
    """The release numbers of a version (3.14.0 -> (3, 14, 0)); a suffix (rc1) is ignored."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", text)
    if not m:
        raise Error(f"not a version: {text!r}")
    return tuple(int(n) for n in m.group(1).split("."))


def _compare_versions(left: str, op: str, right: str) -> bool:
    if op in ("==", "!=") and right.endswith(".*"):
        prefix = _version(right[:-2])
        equal = _version(left)[:len(prefix)] == prefix
        return equal if op == "==" else not equal
    a, b = _version(left), _version(right)
    width = max(len(a), len(b))
    a, b = a + (0,) * (width - len(a)), b + (0,) * (width - len(b))
    if op == "~=":
        n = len(_version(right))
        if n < 2:
            raise Error(f"~={right} needs two release numbers")
        return a >= b and a[:n - 1] == b[:n - 1]
    return {"==": a == b, "===": a == b, "!=": a != b, "<": a < b, "<=": a <= b,
            ">": a > b, ">=": a >= b}[op]


def marker_holds(marker: str, env: dict[str, str]) -> bool:
    """Whether a PEP 508 marker holds in `env` (an "extra" not given is ""). Raises Error for
    a marker it can't read."""
    tokens, pos = _tokens(marker), 0

    def peek() -> tuple[str, str]:
        return tokens[pos] if pos < len(tokens) else ("end", "")

    def take() -> tuple[str, str]:
        nonlocal pos
        token = peek()
        pos += 1
        return token

    def value() -> tuple[str, Optional[str]]:  # (its text, the variable's name or None)
        kind, text = take()
        if kind == "str":
            return text, None
        if kind == "var":
            return env.get(text, ""), text
        raise Error(f"marker {marker!r}: a value expected, not {text!r}")

    def comparison() -> bool:
        if peek() == ("op", "("):
            take()
            result = disjunction()
            if take() != ("op", ")"):
                raise Error(f"marker {marker!r}: ')' missing")
            return result
        left, lvar = value()
        kind, op = take()
        if kind != "op" or op in ("(", ")", "and", "or"):
            raise Error(f"marker {marker!r}: an operator expected, not {op!r}")
        right, rvar = value()
        if op in ("in", "not in"):
            inside = left in right
            return inside if op == "in" else not inside
        if "extra" in (lvar, rvar):
            left, right = normalize(left), normalize(right)
        if (lvar or rvar) in _VERSION_VARS:
            return _compare_versions(left, op, right)
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        raise Error(f"marker {marker!r}: {op} compares versions only")

    def conjunction() -> bool:
        result = comparison()
        while peek() == ("op", "and"):
            take()
            result = comparison() and result
        return result

    def disjunction() -> bool:
        result = conjunction()
        while peek() == ("op", "or"):
            take()
            result = conjunction() or result
        return result

    result = disjunction()
    if pos != len(tokens):
        raise Error(f"marker {marker!r}: unexpected {tokens[pos][1]!r}")
    return result


# --- requirements ----------------------------------------------------------------------------
_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*"
                          r"\(?\s*([^;()]*?)\s*\)?\s*(?:;\s*(.*?))?\s*$")


def parse_requirement(text: str) -> tuple[str, str, str]:
    """(normalized name, version specifiers, marker) of a requirement ("anyio<5,>=4.0.0",
    "pyasn1 (>=0.4.6)", 'x==1 ; python_version < "3.11"')."""
    m = _REQUIREMENT.match(text)
    if not m:
        raise Error(f"can't read the requirement {text!r}")
    return normalize(m.group(1)), m.group(3).strip(), (m.group(4) or "").strip()


def satisfies(version: str, specifiers: str) -> bool:
    """Whether `version` meets every specifier of a comma-separated list (">=4,<5")."""
    for spec in filter(None, (s.strip() for s in specifiers.split(","))):
        m = re.match(r"(===|==|!=|<=|>=|~=|<|>)\s*(\S+)$", spec)
        if not m:
            raise Error(f"can't read the version specifier {spec!r}")
        if not _compare_versions(version, m.group(1), m.group(2)):
            return False
    return True


def _logical_lines(text: str) -> list[str]:
    """A requirements file's lines, continuations joined, comments and options dropped."""
    out, current = [], ""
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        current += line
        current = re.sub(r"\s--hash=\S+", "", current).strip()
        if current and not current.startswith("-"):
            out.append(current)
        current = ""
    return out


def pins(text: str) -> list[tuple[str, str, str]]:
    """The name==version pins of a requirements file: (name, version, marker) each."""
    out = []
    for line in _logical_lines(text):
        name, spec, marker = parse_requirement(line)
        m = re.fullmatch(r"==\s*(\S+)", spec)
        if not m:
            raise Error(f"{line!r} is not pinned (name==version)")
        out.append((name, m.group(1), marker))
    return out


def lock_hashes(text: str) -> dict[str, set[str]]:
    """name -> the sha256 values requirements-lock.txt records for its pin."""
    out: dict[str, set[str]] = {}
    name = ""
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==", line)
        if m:
            name = normalize(m.group(1))
            out.setdefault(name, set())
        for digest in re.findall(r"--hash=sha256:([0-9a-f]{64})", line):
            if name:
                out[name].add(digest)
    return out


def needed(marker: str) -> bool:
    """Whether a requirement with this marker applies in any of ENVIRONMENTS (no extra)."""
    return not marker or any(marker_holds(marker, dict(env, extra="")) for env in ENVIRONMENTS)


# --- the packages ----------------------------------------------------------------------------
Fetch = Callable[[str], bytes]


def fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        raise Error(f"not an https:// address: {url}")
    request = urllib.request.Request(  # noqa: S310 (https:// only)
        url, headers={"User-Agent": "ser2netweb-make-formula"})
    try:
        with urllib.request.urlopen(request, timeout=120) as r:  # noqa: S310 (https:// URLs)
            data: bytes = r.read()
            return data
    except urllib.error.URLError as e:
        raise Error(f"{url}: {e}") from None


def resolve(requirements: str, optional: str, lock: str,
            get: Fetch = fetch) -> tuple[list[dict[str, str]], list[str]]:
    """The resources (name, url, sha256, version: each package's sdist) and the Homebrew
    formulae (depends_on) of the formula."""
    locked = {name: version for name, version, marker in pins(lock) if needed(marker)}
    hashes = lock_hashes(lock)
    todo: list[tuple[str, str, str]] = []  # (name, version, who needs it)
    for source, text in (("requirements.txt", requirements),
                         ("requirements-optional.txt", optional)):
        for name, version, marker in pins(text):
            if needed(marker):
                todo.append((name, version, source))
    formulae: set[str] = set()
    found: dict[str, dict[str, str]] = {}
    while todo:
        name, version, why = todo.pop(0)
        if name in PROVIDED:
            formulae.add(PROVIDED[name])
            continue
        if name in found:
            if found[name]["version"] != version:
                raise Error(f"{name}: {found[name]['version']} and {version} ({why}) wanted")
            continue
        if locked.get(name) != version:
            raise Error(f"{name}=={version} ({why}) is not what requirements-lock.txt pins "
                        f"({locked.get(name, 'nothing')})")
        meta = json.loads(get(PYPI_JSON.format(name=name, version=version)))
        sdists = [u for u in meta.get("urls", []) if u.get("packagetype") == "sdist"]
        if len(sdists) != 1:
            raise Error(f"{name} {version}: {len(sdists)} source releases on PyPI, not one")
        sdist = sdists[0]
        if sdist.get("yanked"):
            raise Error(f"{name} {version}: its source release is yanked on PyPI")
        digest = sdist["digests"]["sha256"]
        if digest not in hashes.get(name, set()):
            raise Error(f"{name} {version}: PyPI's sdist sha256 {digest} is not one "
                        "requirements-lock.txt records")
        filename = sdist["filename"]
        stem = re.sub(r"\.(tar\.gz|zip)$", "", filename)
        if not stem.endswith("-" + version):
            raise Error(f"{name} {version}: unexpected sdist name {filename}")
        # (the name Homebrew's audit wants: the sdist's, '_' and '.' as '-')
        found[name] = {"name": re.sub(r"[_.]", "-", stem[:-len(version) - 1]).lower(),
                       "version": version, "url": sdist["url"], "sha256": digest}
        for requirement in meta.get("info", {}).get("requires_dist") or []:
            dep, spec, marker = parse_requirement(requirement)
            if not needed(marker):
                continue
            if dep in PROVIDED:
                formulae.add(PROVIDED[dep])
                continue
            if dep not in locked:
                raise Error(f"{name} {version} needs {requirement!r}, which "
                            "requirements-lock.txt doesn't pin")
            if not satisfies(locked[dep], spec):
                raise Error(f"{name} {version} needs {requirement!r}; the lock has "
                            f"{dep}=={locked[dep]}")
            todo.append((dep, locked[dep], f"{name} {version}"))
    resources = sorted(found.values(), key=lambda r: r["name"])
    return resources, sorted(formulae)


# --- the formula -----------------------------------------------------------------------------
def tarball_files(data: bytes, names: tuple[str, ...]) -> dict[str, str]:
    """The text of `names` (paths in the repository) in a GitHub source tarball (whose
    files are under one top folder), read without unpacking anything."""
    out: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            top, _sep, rel = member.name.partition("/")
            if rel in names and member.isfile():
                f = tar.extractfile(member)
                if f is not None:
                    out[rel] = f.read().decode("utf-8")
    missing = [n for n in names if n not in out]
    if missing:
        raise Error(f"the source tarball has no {', '.join(missing)}")
    return out


def render(tag: str, sha256: str, resources: list[dict[str, str]], formulae: list[str],
           build_line: str) -> str:
    """The formula's text. `build_line`: what this version's --about says on its Build:
    line in the formula's virtual environment (the test checks it)."""
    version = tag[1:]
    deps = [f'  depends_on "{f}" => :no_linkage\n' for f in formulae]
    deps.append(f'  depends_on "python@{PYTHON}"\n')
    blocks = "".join(f'''
  resource "{r['name']}" do
    url "{r['url']}"
    sha256 "{r['sha256']}"
  end
''' for r in resources)
    return f'''# Made by packaging/homebrew/make_formula.py of {REPO} ({tag}); change it there.
class Ser2netweb < Formula
  include Language::Python::Virtualenv

  desc "Serial-to-network bridge with a web UI"
  homepage "{HOMEPAGE}"
  url "{TARBALL_URL.format(tag=tag)}"
  sha256 "{sha256}"
  license "AGPL-3.0-only"

  livecheck do
    url :stable
    strategy :github_latest
  end

{"".join(deps)}{blocks}
  def install
    virtualenv_install_with_resources
  end

  def post_install
    (var/"ser2netweb").mkpath
  end

  def caveats
    <<~EOS
      The service (brew services start ser2netweb) keeps its data and its log
      (all.log) in #{{var}}/ser2netweb; its web UI listens on 127.0.0.1:8080.
      The first admin password, also while it runs:
        ser2netweb --data-dir #{{var}}/ser2netweb --set-password admin
    EOS
  end

  service do
    run [opt_bin/"ser2netweb", "--data-dir", var/"ser2netweb"]
    keep_alive true
    working_dir var/"ser2netweb"
    # (the log lines go to all.log there, which ser2netweb rotates; this file gets what
    # goes wrong before that)
    environment_variables SER2NETWEB_LOG_STDOUT: "off"
    log_path var/"log/ser2netweb.log"
    error_log_path var/"log/ser2netweb.log"
  end

  test do
    assert_equal "ser2netweb {version}", shell_output("#{{bin}}/ser2netweb --version").strip
    about = shell_output("#{{bin}}/ser2netweb --about")
    assert_match "Build: {build_line}", about
    refute_match(/not installed|failed to import/, about)

    port = free_port
    env = {{ "SER2NETWEB_BIND_IP" => "127.0.0.1", "SER2NETWEB_PORT" => port.to_s }}
    pid = spawn env, bin/"ser2netweb", "--data-dir", testpath/"data"
    begin
      healthy = false
      60.times do
        sleep 1
        healthy = shell_output("curl -s http://127.0.0.1:#{{port}}/healthz || true").include?("ok")
        break if healthy
      end
      assert healthy, "no answer on /healthz"
    ensure
      Process.kill("TERM", pid)
      Process.wait(pid)
    end
  end
end
'''


def build_line(layout_py: str) -> str:
    """What --about's Build: line is in the formula's virtual environment for the version
    whose ser2netweb/layout.py this is (3.0.0's took it for a pip install)."""
    m = re.search(r'HOMEBREW: "([^"]+)"', layout_py)
    return m.group(1) if m else "Python package (pip / pipx)"


def make(tag: str, tarball: Optional[bytes] = None, get: Fetch = fetch) -> str:
    """The formula of release `tag` (vX.Y.Z); `tarball`: its source tarball, else downloaded."""
    m = TAG.fullmatch(tag)
    if not m:
        raise Error(f"{tag!r} is not a release tag (vX.Y.Z)")
    data = tarball if tarball is not None else get(TARBALL_URL.format(tag=tag))
    files = tarball_files(data, ("requirements.txt", "requirements-optional.txt",
                                 "requirements-lock.txt", "ser2netweb/__init__.py",
                                 "ser2netweb/layout.py"))
    found = re.search(r'(?m)^__version__ = "([^"]+)"', files["ser2netweb/__init__.py"])
    if not found or found.group(1) != m.group(1):
        raise Error(f"the tarball of {tag} is version {found.group(1) if found else '?'}")
    resources, formulae = resolve(files["requirements.txt"], files["requirements-optional.txt"],
                                  files["requirements-lock.txt"], get)
    return render(tag, hashlib.sha256(data).hexdigest(), resources, formulae,
                  build_line(files["ser2netweb/layout.py"]))


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("tag", help="the release tag, vX.Y.Z")
    p.add_argument("--output", "-o", help="write the formula here (default: standard output)")
    p.add_argument("--tarball", help="the tag's source tarball, already downloaded")
    args = p.parse_args(argv)
    try:
        tarball = None
        if args.tarball:
            with open(args.tarball, "rb") as fh:
                tarball = fh.read()
        text = make(args.tag, tarball)
    except Error as e:
        print(f"make_formula: {e}", file=sys.stderr)
        return 1
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
