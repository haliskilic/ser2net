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
# --about's "Build:" line
LABELS = {SOURCE: "source", STANDALONE: "standalone executable",
          PACKAGE: "standalone executable, .deb / .rpm package",
          PIP: "Python package (pip / pipx)"}
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
        if platform.startswith("linux") and os.path.abspath(executable) == PACKAGE_EXECUTABLE:
            return PACKAGE
        return STANDALONE
    parent = os.path.dirname(os.path.abspath(package_dir))
    if os.path.basename(parent).lower() in _SITE_FOLDERS or _dist_infos(parent):
        return PIP
    # a source checkout (an editable install `pip install -e .` too: the package stays in
    # the checkout, so its data does as well)
    return SOURCE


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
    checkout, or the folder of a standalone executable), the package's service's data
    directory, or a pip install's user data folder."""
    k = kind() if kind_ is None else kind_
    if k == PIP:
        return user_data_dir()
    if k == PACKAGE:
        return PACKAGE_DATA_DIR
    return os.path.join(app_dir, "data")


def licence_dir(package_dir: str = PACKAGE_DIR) -> Optional[str]:
    """A pip install's licence texts (LICENSE, NOTICE, ...: the wheel's license-files,
    which an installer puts in <name>.dist-info/licenses next to the package), else None."""
    parent = os.path.dirname(os.path.abspath(package_dir))
    for name in _dist_infos(parent):
        folder = os.path.join(parent, name, "licenses")
        if os.path.isdir(folder):
            return folder
    return None
