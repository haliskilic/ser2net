"""How this copy of ser2netweb is installed, and where it keeps its data by default.

  source      a source checkout (ser2netweb.py, bootstrap.py and ./lib beside the package),
              also the Docker image's /app: data/ in the checkout
  standalone  a standalone executable (PyInstaller): data/ next to the executable
  package     that executable as the .deb / .rpm package installs it, /usr/bin/ser2netweb:
              /var/lib/ser2netweb, the data directory of the package's service
  pip         installed by pip or pipx (the wheel from PyPI: the package in a site-packages
              folder, or wherever --target put it next to its .dist-info): the platform's
              folder for a user's application data, never next to the package, which pip
              owns (and may replace or delete on the next upgrade)
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

--data-dir and --config choose another folder in every case. A pip install gets its
dependencies from pip, so the offline bootstrap into ./lib (bootstrap.py) is not used there.
Standard library only (the command line reads this before the dependencies load).
"""
from __future__ import annotations

import functools
import ntpath
import os
import posixpath
import sys
from collections.abc import Mapping
from typing import Optional

NAME = "ser2netweb"
SOURCE, STANDALONE, PACKAGE, PIP = "source", "standalone", "package", "pip"
WINGET, SCOOP, CHOCOLATEY = "winget", "scoop", "chocolatey"
# --about's "Build:" line
LABELS = {SOURCE: "source", STANDALONE: "standalone executable",
          PACKAGE: "standalone executable, .deb / .rpm package",
          PIP: "Python package (pip / pipx)",
          WINGET: "standalone executable, winget package",
          SCOOP: "standalone executable, Scoop package",
          CHOCOLATEY: "standalone executable, Chocolatey package"}
# the package's PackageIdentifier in winget (packaging/winget), which names the folder winget
# puts a portable package in: <root>\<PackageIdentifier>_<source>
WINGET_ID = "haliskilic.ser2netweb"
# where the .deb / .rpm package puts the executable, and its service's data directory
# (packaging/nfpm.yaml; ser2netweb-install.sh and the standalone unit use /usr/local/bin)
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


def detect(frozen: bool, executable: str, package_dir: str,
           platform: str = sys.platform) -> str:
    """The kind of installation (SOURCE, STANDALONE, PACKAGE or PIP) of a copy whose package
    is `package_dir`, run by `executable`."""
    if frozen:
        # (a Linux path, compared as one whatever OS this runs on; a frozen executable's
        # sys.executable is absolute)
        if platform.startswith("linux") and posixpath.normpath(executable) == PACKAGE_EXECUTABLE:
            return PACKAGE
        if platform == "win32":
            return windows_package_manager(executable) or STANDALONE
        return STANDALONE
    parent = os.path.dirname(os.path.abspath(package_dir))
    if os.path.basename(parent).lower() in _SITE_FOLDERS or _dist_infos(parent):
        return PIP
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


def default_data_dir(app_dir: str, kind_: Optional[str] = None) -> str:
    """The data directory without --data-dir / --config: data/ in `app_dir` (a source
    checkout, or the folder of a standalone executable, also Scoop's), the package's
    service's data directory, or the user data folder (a pip install; winget's and
    Chocolatey's executable, unless `app_dir` has a data/ with a config.json already)."""
    k = kind() if kind_ is None else kind_
    if k == PIP:
        return user_data_dir()
    if k == PACKAGE:
        return PACKAGE_DATA_DIR
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
