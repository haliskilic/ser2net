"""Tests of features that need an optional package (requirements-optional.txt: ldap3,
authlib, paho-mqtt, cryptography).

    from _optional import need
    if not need("ldap3", what="LDAP bind and search"):
        return                  # printed: SKIP LDAP bind and search: ldap3 is not installed

A missing package skips that part with a SKIP line, which tests/run_all.py repeats under
the test's PASS line, so a skipped part never passes unnoticed. With
SER2NET_OPTIONAL_TESTS=1 (set on the CI legs that install requirements-optional.txt) a
missing package fails the test instead. A package that is installed but fails to import
fails the test either way (the import error is raised).
"""
from __future__ import annotations

import importlib
import os

REQUIRED = os.environ.get("SER2NET_OPTIONAL_TESTS") == "1"


def _installed(module: str) -> bool:
    try:
        importlib.import_module(module)
    except ModuleNotFoundError as e:
        if e.name and module.split(".")[0] == e.name.split(".")[0]:
            return False
        raise   # installed, but one of its dependencies is missing: not a skip
    return True


def need(*modules: str, what: str) -> bool:
    """True when every module imports; else SKIP `what` (or fail, see above)."""
    missing = [m for m in modules if not _installed(m)]
    if not missing:
        return True
    why = " and ".join(missing) + (" is" if len(missing) == 1 else " are") + " not installed"
    if REQUIRED:
        raise AssertionError(f"SER2NET_OPTIONAL_TESTS=1, but {why} (needed for: {what})")
    print(f"SKIP {what}: {why} (pip install -r requirements-optional.txt)")
    return False
