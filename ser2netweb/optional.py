"""The optional packages (requirements-optional.txt: paho-mqtt, ldap3, authlib,
cryptography): ser2net runs without them, and a feature that needs one says why it
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

from . import envvars

REQUIREMENTS = "requirements-optional.txt"
LIB_OVERRIDE_ENV = "SER2NETWEB_LIB_OVERRIDE"


@dataclass
class Problem:
    """Why an optional package can't be used."""
    dist: str            # its pip name: ldap3, authlib, paho-mqtt, cryptography
    missing: bool        # not installed; else installed, but importing it failed
    error: str           # the import error: "Type: message"
    frozen: bool = False  # a standalone executable, which bundles every optional package
    override: str = ""   # SER2NETWEB_LIB_OVERRIDE (a folder imported before the bundled copy)
    override_env: str = LIB_OVERRIDE_ENV  # the name it is set under (or SER2NET_LIB_OVERRIDE)

    @property
    def hint(self) -> str:
        """What to do about it."""
        if self.override and not self.missing:
            return f"{self.override_env} puts {self.override} first: check the {self.dist} there"
        if self.frozen:
            return f"this executable should contain {self.dist}; please report it"
        return f"pip install -r {REQUIREMENTS}" + ("" if self.missing else ", then restart ser2net")

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
                   frozen=bool(getattr(sys, "frozen", False)),
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
