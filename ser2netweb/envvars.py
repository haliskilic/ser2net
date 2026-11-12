"""The environment variables ser2netweb reads: SER2NETWEB_<X>, and SER2NET_<X>, their names
up to 2.9.x, which keep working.

get("SER2NETWEB_BIND_IP") is SER2NETWEB_BIND_IP when it is in the environment, else
SER2NET_BIND_IP, else the default: the new name wins when both are set, also when it is
set to "" (which so turns an old name's value off). Standard library only: the start
scripts and --healthcheck read the variables before the dependencies load.

Defaults that ser2netweb's own files set in the environment (the Docker image's ENV, the
systemd template unit's node name) keep the old names, the ones that lose: a setting of
the user's under either name wins over them.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Optional

PREFIX = "SER2NETWEB_"
OLD_PREFIX = "SER2NET_"


def old_name(name: str) -> str:
    """The 2.9.x name of a variable: SER2NETWEB_X -> SER2NET_X."""
    if not name.startswith(PREFIX):
        raise ValueError(f"not a {PREFIX}* name: {name!r}")
    return OLD_PREFIX + name[len(PREFIX):]


def lookup(name: str, environ: Optional[Mapping[str, str]] = None) -> tuple[str, str] | None:
    """(the name that is set, its value) for SER2NETWEB_X `name`, or None when neither
    SER2NETWEB_X nor SER2NET_X is in the environment."""
    env = os.environ if environ is None else environ
    for candidate in (name, old_name(name)):
        value = env.get(candidate)
        if value is not None:
            return candidate, value
    return None


def get(name: str, default: Optional[str] = None,
        environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The value of SER2NETWEB_X `name` (or of SER2NET_X), else `default`."""
    found = lookup(name, environ)
    return default if found is None else found[1]


def text(name: str, environ: Optional[Mapping[str, str]] = None) -> str:
    """The value without surrounding blanks ('' when unset)."""
    return (get(name, "", environ) or "").strip()


def source(name: str, environ: Optional[Mapping[str, str]] = None) -> str:
    """The name the value comes from, for a message that names it: SER2NETWEB_X `name`,
    or SER2NET_X when only that is set."""
    found = lookup(name, environ)
    return name if found is None else found[0]
