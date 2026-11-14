"""How this copy of ser2netweb is installed, and where it keeps its data by default.

  source      a source checkout (ser2netweb.py, bootstrap.py and ./lib beside the package),
              also the Docker image's /app: data/ in the checkout
  standalone  a standalone executable (PyInstaller): data/ next to the executable
  package     that executable as the .deb / .rpm package (and the AUR's ser2netweb-bin)
              installs it, /usr/bin/ser2netweb: /var/lib/ser2netweb, the data directory of
              the package's service
  pip         installed by pip or pipx (the wheel from PyPI: the package in a site-packages
              folder, or wherever --target put it next to its .dist-info): the platform's
              folder for a user's application data, never next to the package, which pip
              owns (and may replace or delete on the next upgrade)
  distro      a distribution's package built from a recipe in packaging/ (Debian, Fedora,
              openSUSE, Arch, Alpine, FreeBSD ports, MacPorts, ...): pip's layout, the
              package in the system's site-packages, with the file DISTRO_FILE the recipe
              wrote into it before the build (pyproject.toml takes it into the wheel). It
              names the distribution, the data directory of the package's service
              (/var/lib/ser2netweb, FreeBSD's /var/db/ser2netweb, ...), the default, and how
              the distribution installs an optional package (the hint for a missing one).
              Without that file (a package of someone else's recipe) it is pip's layout
  homebrew    the Homebrew formula (haliskilic/tap/ser2netweb): pip's layout too, in the
              virtual environment of the formula's keg (<prefix>/Cellar/ser2netweb/<version>/
              libexec): <prefix>/var/ser2netweb, the data directory of its service
              (brew services), which outlives the keg; unless that folder has no
              config.json and the user data folder has one (ser2netweb 3.0.0's, which took
              the keg for a pip install: data of a copy run by hand, kept in use)
  snap        the standalone executable in the snap: root (the snap's service, sudo)
              $SNAP_COMMON/data, /var/snap/ser2netweb/common/data; another user
              $SNAP_USER_COMMON/data (the snap's folder in the home directory)
  winget, scoop, chocolatey
              the Windows executable where one of these package managers puts it: winget's
              portable package folder <root>\\haliskilic.ser2netweb_<source>, Scoop's
              <root>\\apps\\<app>\\<version> (or current, with the manifest.json and
              install.json Scoop writes there), Chocolatey's <root>\\lib\\<id>\\tools (next
              to <id>.nupkg). The package manager owns that folder (Chocolatey replaces it on
              an upgrade, a machine-wide one is for administrators only), so winget's and
              Chocolatey's copy keep the data in the user's folder, as pip's, unless a data
              folder with a config.json is already next to the executable (ser2netweb 3.0.0's,
              which knew no package manager); Scoop's manifest persists data\\ next to the
              executable (<root>\\persist\\ser2netweb\\data, kept by updates), so its copy
              keeps the data there, as a standalone executable does

--data-dir and --config choose another folder in every case. A pip install (and the Homebrew
formula's) gets its dependencies from pip, a distribution's package from the distribution, so
the offline bootstrap into ./lib (bootstrap.py) is not used there.
Standard library only (the command line reads this before the dependencies load).
"""
from __future__ import annotations

import functools
import json
import ntpath
import os
import posixpath
import sys
from collections.abc import Mapping
from typing import Optional

NAME = "ser2netweb"
SOURCE, STANDALONE, PACKAGE, PIP = "source", "standalone", "package", "pip"
HOMEBREW, SNAP, DISTRO = "homebrew", "snap", "distro"
WINGET, SCOOP, CHOCOLATEY = "winget", "scoop", "chocolatey"
# --about's "Build:" line
LABELS = {SOURCE: "source", STANDALONE: "standalone executable",
          PACKAGE: "standalone executable, Linux package (.deb / .rpm / AUR)",
          PIP: "Python package (pip / pipx)", HOMEBREW: "Homebrew formula",
          SNAP: "standalone executable, snap",
          WINGET: "standalone executable, winget package",
          SCOOP: "standalone executable, Scoop package",
          CHOCOLATEY: "standalone executable, Chocolatey package",
          DISTRO: "distribution package"}
# the installations whose package an installer put into a site-packages folder (pip, pipx,
# the Homebrew formula's virtual environment, a distribution's package): the dependencies
# are installed beside it, the licence texts are in the .dist-info, and the command is
# ser2netweb
PIP_KINDS = (PIP, HOMEBREW, DISTRO)
# what a recipe in packaging/ writes into the package before the build, as a JSON object:
#   "distribution"  its name ("Debian", "FreeBSD ports"), for --about
#   "data_dir"      the data directory of the package's service, the default (absolute)
#   "optional"      how to install an optional package (paho-mqtt, ldap3, authlib,
#                   cryptography): a command with {dist} (that PyPI name) or {extra} (the
#                   extra that brings it: mqtt, ldap, oidc, tls), or {PyPI name: command}
DISTRO_FILE = "distro.json"
# the package's PackageIdentifier in winget (packaging/winget), which names the folder winget
# puts a portable package in: <root>\<PackageIdentifier>_<source>
WINGET_ID = "haliskilic.ser2netweb"
# where the .deb / .rpm package (and the AUR's) puts the executable, and its service's data
# directory (packaging/nfpm.yaml, packaging/aur; ser2netweb-install.sh and the standalone
# unit use /usr/local/bin)
PACKAGE_EXECUTABLE = "/usr/bin/ser2netweb"
PACKAGE_DATA_DIR = "/var/lib/ser2netweb"
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))  # this package's folder
_SITE_FOLDERS = ("site-packages", "dist-packages")  # (dist-packages: Debian's Python)


def _dist_infos(folder: str) -> list[str]:
    """The ser2netweb-*.dist-info folders an installer left in `folder`."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(n for n in names
                  if n.lower().startswith(NAME + "-") and n.lower().endswith(".dist-info"))


def homebrew_prefix(package_dir: str = PACKAGE_DIR) -> Optional[str]:
    """The Homebrew prefix (/opt/homebrew, /usr/local, /home/linuxbrew/.linuxbrew) of a copy
    the ser2netweb formula installed, else None. Its virtual environment is the keg's
    libexec: <prefix>/Cellar/ser2netweb/<version>/libexec/lib/python3.X/site-packages/
    ser2netweb, also when it runs through <prefix>/opt/ser2netweb (a link to the keg)."""
    parts = os.path.realpath(package_dir).split(os.sep)
    if (len(parts) > 8 and parts[-8] == "Cellar" and parts[-7] == NAME
            and parts[-5:-3] == ["libexec", "lib"] and parts[-3].startswith("python")
            and parts[-2] == "site-packages"):
        return os.sep.join(parts[:-8]) or os.sep
    return None


def _in_snap(executable: str, env: Mapping[str, str]) -> bool:
    """Whether `executable` is the one in ser2netweb's snap ($SNAP: /snap/ser2netweb/<rev>;
    a parallel install's name is ser2netweb_<key>)."""
    name, snap = env.get("SNAP_NAME", ""), env.get("SNAP", "")
    return (name == NAME and posixpath.isabs(snap)
            and posixpath.normpath(executable).startswith(posixpath.normpath(snap) + "/"))


def detect(frozen: bool, executable: str, package_dir: str,
           platform: str = sys.platform, environ: Optional[Mapping[str, str]] = None) -> str:
    """The kind of installation (SOURCE, STANDALONE, PACKAGE, PIP, HOMEBREW, SNAP, WINGET,
    SCOOP, CHOCOLATEY or DISTRO) of a copy whose package is `package_dir`, run by
    `executable`. No two can match: a frozen executable is PACKAGE or SNAP only on Linux
    and a package manager's only on Windows; HOMEBREW, DISTRO and PIP are never frozen,
    HOMEBREW is pip's layout in a keg, DISTRO pip's layout with a recipe's DISTRO_FILE."""
    if frozen:
        # (a Linux path, compared as one whatever OS this runs on; a frozen executable's
        # sys.executable is absolute)
        if platform.startswith("linux"):
            if posixpath.normpath(executable) == PACKAGE_EXECUTABLE:
                return PACKAGE
            if _in_snap(executable, os.environ if environ is None else environ):
                return SNAP
        if platform == "win32":
            return windows_package_manager(executable) or STANDALONE
        return STANDALONE
    parent = os.path.dirname(os.path.abspath(package_dir))
    if os.path.basename(parent).lower() in _SITE_FOLDERS or _dist_infos(parent):
        if homebrew_prefix(package_dir):
            return HOMEBREW
        return DISTRO if distro_info(package_dir) else PIP
    # a source checkout (an editable install `pip install -e .` too: the package stays in
    # the checkout, so its data does as well)
    return SOURCE


def windows_package_manager(executable: str) -> Optional[str]:
    """WINGET, SCOOP or CHOCOLATEY when that package manager put the Windows executable
    `executable` (the absolute path of a frozen build: PyInstaller resolves the symbolic link
    winget puts on PATH, not Scoop's `current` junction) where it is, else None. The path is
    a Windows one, its folder names compared without case; the files the package managers
    write next to it are looked up with os.path (ntpath on Windows)."""
    folder = ntpath.dirname(executable)
    parent = ntpath.dirname(folder)
    name, parent_name, grandparent_name = (ntpath.basename(p).lower()
                                           for p in (folder, parent, ntpath.dirname(parent)))
    if name.startswith(WINGET_ID + "_"):
        return WINGET
    if grandparent_name == "apps" and all(os.path.isfile(os.path.join(folder, f))
                                          for f in ("manifest.json", "install.json")):
        return SCOOP
    if (name == "tools" and grandparent_name == "lib"
            and os.path.isfile(os.path.join(parent, parent_name + ".nupkg"))):
        return CHOCOLATEY
    return None


@functools.cache
def kind() -> str:
    """The kind of this installation (detect() for the running copy; found once)."""
    return detect(bool(getattr(sys, "frozen", False)), sys.executable, PACKAGE_DIR)


def distro_info(package_dir: str = PACKAGE_DIR) -> Optional[dict]:
    """The DISTRO_FILE a distribution's recipe wrote into the package `package_dir`, when
    it is a JSON object that names the distribution; else None (no such file, or one that
    can't be read: pip's layout then, as without it)."""
    try:
        with open(os.path.join(package_dir, DISTRO_FILE), encoding="utf-8") as fh:
            info = json.load(fh)
    except (OSError, ValueError):  # (UnicodeDecodeError and JSONDecodeError are ValueErrors)
        return None
    if not isinstance(info, dict) or not isinstance(info.get("distribution"), str) \
            or not info["distribution"].strip():
        return None
    return info


def label(kind_: Optional[str] = None) -> str:
    """--about's "Build:" text: LABELS', with the distribution's name for its package."""
    k = kind() if kind_ is None else kind_
    if k == DISTRO:
        info = distro_info()
        if info:
            return f"{LABELS[k]} ({info['distribution'].strip()})"
    return LABELS[k]


def user_data_dir(platform: str = sys.platform, environ: Optional[Mapping[str, str]] = None,
                  home: Optional[str] = None) -> str:
    """A user's folder for ser2netweb's data: %LOCALAPPDATA%\\ser2netweb on Windows,
    ~/Library/Application Support/ser2netweb on macOS, $XDG_DATA_HOME/ser2netweb (or
    ~/.local/share/ser2netweb) elsewhere, as the XDG base directory spec has it: a
    relative XDG_DATA_HOME is ignored."""
    env = os.environ if environ is None else environ
    path = ntpath if platform == "win32" else posixpath
    if home is None:
        home = os.path.expanduser("~")
    if platform == "win32":
        base = env.get("LOCALAPPDATA", "")
        if not path.isabs(base):
            base = path.join(home, "AppData", "Local")
    elif platform == "darwin":
        base = path.join(home, "Library", "Application Support")
    else:
        base = env.get("XDG_DATA_HOME", "")
        if not path.isabs(base):
            base = path.join(home, ".local", "share")
    return path.join(base, NAME)


def snap_data_dir(environ: Optional[Mapping[str, str]] = None,
                  root: Optional[bool] = None) -> str:
    """The snap's data directory: $SNAP_COMMON/data for root (the snap's service runs as
    root on it), $SNAP_USER_COMMON/data for another user; the user data folder without
    them (not in a snap)."""
    env = os.environ if environ is None else environ
    if root is None:
        root = getattr(os, "geteuid", lambda: -1)() == 0
    base = env.get("SNAP_COMMON" if root else "SNAP_USER_COMMON", "")
    return posixpath.join(base, "data") if posixpath.isabs(base) else user_data_dir()


def default_data_dir(app_dir: str, kind_: Optional[str] = None) -> str:
    """The data directory without --data-dir / --config: data/ in `app_dir` (a source
    checkout, or the folder of a standalone executable, also Scoop's), the package's
    service's data directory (also a distribution's package's, as its recipe says), the
    user data folder (a pip install; winget's and
    Chocolatey's executable, unless `app_dir` has a data/ with a config.json already), the
    Homebrew formula's <prefix>/var/ser2netweb (unless only the user data folder has a
    config.json: 3.0.0's default there) or the snap's (snap_data_dir)."""
    k = kind() if kind_ is None else kind_
    if k == PIP:
        return user_data_dir()
    if k == DISTRO:
        folder = (distro_info() or {}).get("data_dir")
        return folder if isinstance(folder, str) and os.path.isabs(folder) else user_data_dir()
    if k == HOMEBREW:
        prefix, user = homebrew_prefix(), user_data_dir()
        if not prefix:
            return user
        var = os.path.join(prefix, "var", NAME)
        if (not os.path.isfile(os.path.join(var, "config.json"))
                and os.path.isfile(os.path.join(user, "config.json"))):
            return user
        return var
    if k == PACKAGE:
        return PACKAGE_DATA_DIR
    if k == SNAP:
        return snap_data_dir()
    here = os.path.join(app_dir, "data")
    if k in (WINGET, CHOCOLATEY) and not os.path.isfile(os.path.join(here, "config.json")):
        return user_data_dir()
    return here


def licence_dir(package_dir: str = PACKAGE_DIR) -> Optional[str]:
    """A pip install's licence texts (LICENSE, NOTICE, ...: the wheel's license-files,
    which an installer puts in <name>.dist-info/licenses next to the package), else None."""
    parent = os.path.dirname(os.path.abspath(package_dir))
    for name in _dist_infos(parent):
        folder = os.path.join(parent, name, "licenses")
        if os.path.isdir(folder):
            return folder
    return None
