"""The optional packages (requirements-optional.txt: paho-mqtt, ldap3, authlib,
cryptography): ser2netweb runs without them, and a feature that needs one says why it
can't work.

It says so truthfully: "not installed" only when Python finds no such package at all.
A package that is there but fails to import (a missing or incompatible dependency, an
incompatible version, a warning turned into an error) is reported with that error, so
nobody is told to install what is already installed.
"""
from __future__ import annotations

import importlib
import sys
import types
from dataclasses import dataclass
from typing import Optional

from . import envvars, layout

REQUIREMENTS = "requirements-optional.txt"
LIB_OVERRIDE_ENV = "SER2NETWEB_LIB_OVERRIDE"
# a pip install's extras that bring each package (pyproject.toml [project.optional-dependencies])
EXTRAS = {"paho-mqtt": "mqtt", "ldap3": "ldap", "authlib": "oidc", "cryptography": "tls"}


@dataclass
class Problem:
    """Why an optional package can't be used."""
    dist: str            # its pip name: ldap3, authlib, paho-mqtt, cryptography
    missing: bool        # not installed; else installed, but importing it failed
    error: str           # the import error: "Type: message"
    frozen: bool = False  # a standalone executable, which bundles every optional package
    pip: bool = False    # a pip / pipx install: its extras bring them (layout.PIP)
    homebrew: bool = False  # the Homebrew formula, which installs every one of them
    # a distribution's package: its recipe's layout.DISTRO_FILE (the distribution's command)
    distro: Optional[dict] = None
    override: str = ""   # SER2NETWEB_LIB_OVERRIDE (a folder imported before the bundled copy)
    override_env: str = LIB_OVERRIDE_ENV  # the name it is set under (or SER2NET_LIB_OVERRIDE)

    @property
    def hint(self) -> str:
        """What to do about it."""
        if self.override and not self.missing:
            return f"{self.override_env} puts {self.override} first: check the {self.dist} there"
        if self.frozen:
            return f"this executable should contain {self.dist}; please report it"
        if self.homebrew:
            return (f"the Homebrew formula installs {self.dist}: brew reinstall ser2netweb"
                    + ("" if self.missing else ", then restart ser2netweb"))
        if self.distro is not None:
            return self._distro_hint() + ("" if self.missing else ", then restart ser2netweb")
        if self.pip:
            extra = EXTRAS.get(self.dist, "all")
            return (f'pip install "ser2netweb[{extra}]", or pipx inject ser2netweb {self.dist}'
                    + ("" if self.missing else ", then restart ser2netweb"))
        return f"pip install -r {REQUIREMENTS}" + ("" if self.missing else ", then restart ser2netweb")

    def _distro_hint(self) -> str:
        """The recipe's command for this package ({dist}: its PyPI name, {extra}: the extra
        that brings it); a recipe that lists the commands per package and not this one says
        the distribution has none; else the distribution's package by name."""
        info = self.distro or {}
        name = str(info.get("distribution", "")).strip()
        how = info.get("optional")
        if isinstance(how, dict):
            how = how.get(self.dist)
            if not isinstance(how, str) or not how.strip():
                return f"{name} has no package of it"
        if isinstance(how, str) and how.strip():
            return how.strip().replace("{dist}", self.dist).replace(
                "{extra}", EXTRAS.get(self.dist, "all"))
        return f"install the {name} package of {self.dist}"

    def __str__(self) -> str:
        what = (f"{self.dist} is not installed" if self.missing else
                f"{self.dist} is installed but failed to import: {self.error}")
        return f"{what} ({self.hint})"


def problem(exc: BaseException, dist: str, root: str = "") -> Problem:
    """Describe what importing `dist` raised. `root`: its top package or packages (paho.mqtt
    for paho-mqtt; default: dist) — only a ModuleNotFoundError for one of these means
    "not installed"; one for anything else is a dependency that is missing."""
    root = root or dist
    parts = root.split(".")
    missing = (isinstance(exc, ModuleNotFoundError)
               and exc.name in {".".join(parts[:i]) for i in range(1, len(parts) + 1)})
    return Problem(dist, missing, f"{type(exc).__name__}: {exc}",
                   frozen=bool(getattr(sys, "frozen", False)), pip=layout.kind() == layout.PIP,
                   homebrew=layout.kind() == layout.HOMEBREW,
                   distro=(layout.distro_info() or {}) if layout.kind() == layout.DISTRO
                   else None,
                   override=envvars.text(LIB_OVERRIDE_ENV),
                   override_env=envvars.source(LIB_OVERRIDE_ENV))


def load(module: str, dist: str, root: str = "") -> tuple[types.ModuleType | None, Problem | None]:
    """Import `module` of the package `dist`: (the module, None), or (None, Problem).
    `root` as for problem() (default: the top package of `module`). Never raises (an
    import can raise anything, not only ImportError)."""
    try:
        return importlib.import_module(module), None
    except Exception as e:
        return None, problem(e, dist, root or module.split(".")[0])
