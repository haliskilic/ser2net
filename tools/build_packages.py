"""The .deb and .rpm packages of the standalone Linux executables, built with nfpm.

    python3 tools/build_packages.py fetch-nfpm DIR      # the pinned nfpm into DIR, checked
    python3 tools/build_packages.py build --nfpm NFPM --out OUT ARTIFACTS
    python3 tools/build_packages.py unit                # the unit the packages install
    python3 tools/build_packages.py names [--arch A] [--format deb|rpm]
                                                        # the file names of this version
    python3 tools/build_packages.py version             # that version

ARTIFACTS is laid out as the CI workflow downloads the standalone build's artifacts:
ser2netweb-linux-<x64|arm64>/ser2netweb and meta-linux-<x64|arm64>/THIRD-PARTY-LICENSES.txt
(either architecture may be missing; --arch picks some). For each architecture `build`
writes the two packages packaging/nfpm.yaml describes into OUT, named as
tools/release_guard.py lists them for ser2netweb/__init__.py's version:

  /usr/bin/ser2netweb                         the executable, unchanged
  /usr/lib/systemd/system/ser2netweb.service  systemd/ser2netweb-standalone.service with
                                              ExecStart=/usr/bin/ser2netweb (unit())
  /usr/lib/sysusers.d/ser2netweb.conf         the account ser2netweb, home /var/lib/ser2netweb
  /usr/lib/tmpfiles.d/ser2netweb.conf         /var/lib/ser2netweb, 0700, the account's
  /usr/share/doc/ser2netweb/                  LICENSE, NOTICE, COMMERCIAL_LICENSE.md,
                                              THIRD-PARTY-NOTICES.md, the executable's
                                              THIRD-PARTY-LICENSES.txt (+ copyright, .deb)

with the scripts in packaging/ (the account and the folder where systemd-sysusers /
systemd-tmpfiles are missing, daemon-reload, a removal stops the service; nothing enables
or starts it). The packages' files get the time of the commit (SOURCE_DATE_EPOCH, unless
set), so a rebuild of the same commit and executables is the same.

nfpm is a single Go executable: `fetch-nfpm` downloads the release NFPM_VERSION names for
this machine from GitHub and checks it against NFPM_SHA256 before unpacking it (nothing is
installed system-wide). Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release_guard  # tools/ is not a package

ROOT = release_guard.ROOT
CONFIG = os.path.join("packaging", "nfpm.yaml")
STANDALONE_UNIT = os.path.join(ROOT, "systemd", "ser2netweb-standalone.service")
PACKAGE_EXECUTABLE = "/usr/bin/ser2netweb"  # (ser2netweb/layout.py knows it too)
STANDALONE_EXECUTABLE = "/usr/local/bin/ser2netweb"
# the release's executable name -> nfpm's name of its architecture
ARCHES = {"x64": "amd64", "arm64": "arm64"}

NFPM_VERSION = "2.48.0"
# sha256 of nfpm_<version>_Linux_<machine>.tar.gz, from the release's checksums.txt
NFPM_SHA256 = {
    "x86_64": "6d7f9a28b2f397934403950fcbba83f598b08d02edbe79a0815b3b4897341750",
    "arm64": "a9a75213763783b7669719e505474c748c5ad7dc66591a195619deb50f85c2f4",
}
NFPM_URL = ("https://github.com/goreleaser/nfpm/releases/download/v{version}/"
            "nfpm_{version}_Linux_{machine}.tar.gz")

PACKAGE_HEADER = """\
# The ser2netweb package's service (.deb / .rpm): the standalone executable
# /usr/bin/ser2netweb with its data in /var/lib/ser2netweb, as the account ser2netweb the
# package created. Installing the package doesn't start it; to run it now and at boot:
#   sudo systemctl enable --now ser2netweb
# An upgrade of the package replaces this file: put changes into a drop-in
# (sudo systemctl edit ser2netweb), which stays. Made from
# systemd/ser2netweb-standalone.service by tools/build_packages.py.
"""


def unit(text: str | None = None) -> str:
    """The unit the packages install: the standalone unit with its leading comment
    replaced by PACKAGE_HEADER and /usr/local/bin/ser2netweb by /usr/bin/ser2netweb;
    everything else (user, StateDirectory, the sandbox) as it is."""
    if text is None:
        with open(STANDALONE_UNIT, encoding="utf-8") as fh:
            text = fh.read()
    m = re.match(r"\[Unit\]\n((?:#.*\n)+)(?=Description=)", text)
    if not m:
        raise SystemExit(f"{STANDALONE_UNIT}: no comment between [Unit] and Description=")
    out = "[Unit]\n" + PACKAGE_HEADER + text[m.end():]
    if STANDALONE_EXECUTABLE + " " not in out:
        raise SystemExit(f"{STANDALONE_UNIT} doesn't run {STANDALONE_EXECUTABLE}")
    return out.replace(STANDALONE_EXECUTABLE + " ", PACKAGE_EXECUTABLE + " ")


def package_names(version: str) -> dict[str, tuple[str, str]]:
    """{arch: (the .deb's file name, the .rpm's)} (release_guard.release_packages)."""
    names = release_guard.release_packages(version)
    return {arch: (names[2 * i], names[2 * i + 1]) for i, arch in enumerate(ARCHES)}


def fetch_nfpm(folder: str, machine: str | None = None) -> str:
    """nfpm NFPM_VERSION for this machine (x86_64 or aarch64 Linux) in `folder`, checked
    against NFPM_SHA256. Returns its path."""
    machine = machine or platform.machine()
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)
    if machine not in NFPM_SHA256:
        raise SystemExit(f"no pinned nfpm for {machine} (only {', '.join(NFPM_SHA256)})")
    url = NFPM_URL.format(version=NFPM_VERSION, machine=machine)
    with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 (a fixed https:// URL)
        data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != NFPM_SHA256[machine]:
        raise SystemExit(f"{url}: sha256 {got}, not the pinned {NFPM_SHA256[machine]}")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "nfpm")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        member = tar.getmember("nfpm")
        source = tar.extractfile(member) if member.isfile() else None
        if source is None:
            raise SystemExit(f"{url}: no nfpm executable inside")
        with open(path, "wb") as fh:
            fh.write(source.read())
    os.chmod(path, 0o755)  # noqa: S103 (an executable for everyone, as the release has it)
    print(f"nfpm {NFPM_VERSION} ({machine}, sha256 {got[:16]}…) -> {path}")
    return path


def _commit_time() -> str | None:
    try:
        out = subprocess.run(["git", "-C", ROOT, "log", "-1", "--format=%ct"],  # noqa: S607
                             capture_output=True, text=True, timeout=30)
    except OSError:
        return None
    stamp = out.stdout.strip()
    return stamp if out.returncode == 0 and stamp.isdigit() else None


def build(nfpm: str, artifacts: str, out: str, arches: list[str] | None = None) -> list[str]:
    """Both packages of each architecture whose executable is in `artifacts` (or of
    `arches`, each of which must be there), into `out`. Returns their paths."""
    version = release_guard.app_version(ROOT)
    names = package_names(version)
    wanted = arches or [a for a in ARCHES
                        if os.path.isfile(os.path.join(artifacts, f"ser2netweb-linux-{a}",
                                                       "ser2netweb"))]
    if not wanted:
        raise SystemExit(f"{artifacts}: no ser2netweb-linux-<x64|arm64>/ser2netweb in it")
    os.makedirs(out, exist_ok=True)
    made = []
    with tempfile.TemporaryDirectory(prefix="s2n_pkg_") as tmp:
        unit_path = os.path.join(tmp, "ser2netweb.service")
        with open(unit_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(unit())
        for arch in wanted:
            binary = os.path.join(artifacts, f"ser2netweb-linux-{arch}", "ser2netweb")
            licences = os.path.join(artifacts, f"meta-linux-{arch}", "THIRD-PARTY-LICENSES.txt")
            for path in (binary, licences):
                if not os.path.isfile(path):
                    raise SystemExit(f"missing: {path}")
            env = dict(os.environ, SER2NETWEB_ARCH=ARCHES[arch], SER2NETWEB_VERSION=version,
                       SER2NETWEB_BINARY=os.path.abspath(binary), SER2NETWEB_UNIT=unit_path,
                       SER2NETWEB_LICENSES=os.path.abspath(licences))
            if "SOURCE_DATE_EPOCH" not in env and (stamp := _commit_time()):
                env["SOURCE_DATE_EPOCH"] = stamp
            for packager, name in zip(("deb", "rpm"), names[arch], strict=True):
                target = os.path.abspath(os.path.join(out, name))
                subprocess.run([nfpm, "package", "--config", CONFIG, "--packager", packager,
                                "--target", target], cwd=ROOT, env=env, check=True,
                               timeout=600)
                if not os.path.isfile(target):
                    raise SystemExit(f"nfpm wrote no {target}")
                print(f"{arch}: {name} ({os.path.getsize(target) // 1024} KiB)")
                made.append(target)
    return made


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch-nfpm", help="download the pinned nfpm into DIR and check it")
    f.add_argument("dir")
    b = sub.add_parser("build", help="the .deb and .rpm of each executable in ARTIFACTS")
    b.add_argument("artifacts")
    b.add_argument("--nfpm", required=True, help="the nfpm executable (fetch-nfpm)")
    b.add_argument("--out", required=True, help="where the packages go")
    b.add_argument("--arch", action="append", choices=sorted(ARCHES),
                   help="only this architecture (repeatable; default: every one there)")
    sub.add_parser("unit", help="print the unit the packages install")
    n = sub.add_parser("names", help="print the package file names of this version")
    n.add_argument("--arch", choices=sorted(ARCHES))
    n.add_argument("--format", choices=("deb", "rpm"))
    sub.add_parser("version", help="print the version the packages get (as tagged)")
    args = ap.parse_args(argv)
    if args.cmd == "fetch-nfpm":
        fetch_nfpm(args.dir)
    elif args.cmd == "build":
        build(args.nfpm, args.artifacts, args.out, args.arch)
    elif args.cmd == "unit":
        sys.stdout.write(unit())
    elif args.cmd == "version":
        print(release_guard.app_version(ROOT))
    else:
        names = package_names(release_guard.app_version(ROOT))
        for arch in [args.arch] if args.arch else list(ARCHES):
            deb, rpm = names[arch]
            print("\n".join(n for fmt, n in (("deb", deb), ("rpm", rpm))
                            if args.format in (None, fmt)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
