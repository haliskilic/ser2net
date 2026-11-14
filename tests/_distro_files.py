"""ser2netweb/distro.json as each recipe in packaging/ writes it before its build
(ser2netweb/layout.py, DISTRO; tests/test_distro_recipes.py and test_distro_recipes_2.py
check them): the JSON text found in the recipe, with the recipe's variables and macros
filled in as its build has them.
"""
from __future__ import annotations

import json
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PKG = os.path.join(ROOT, "packaging")

# recipe -> (its file, the pattern of the text it writes, what its build puts for each
# variable or macro in it)
RECIPES = {
    "debian": ("debian/debian/rules",
               r"^\tprintf '%s\\n' '(\{.*\})' >ser2netweb/distro\.json$",
               {"$(DEB_VENDOR)": "Debian"}),
    "fedora": ("fedora/ser2netweb.spec",
               r"^cat >ser2netweb/distro\.json <<'EOF'\n(\{.*\})\nEOF$",
               {"%{?fedora:Fedora}%{?rhel:EPEL}": "Fedora", "%{_sharedstatedir}": "/var/lib"}),
    "opensuse": ("opensuse/ser2netweb.spec",
                 r"^cat >ser2netweb/distro\.json <<'EOF'\n(\{.*\})\nEOF$",
                 {"%{_sharedstatedir}": "/var/lib"}),
    "arch": ("arch/ser2netweb/PKGBUILD",
             r"^  printf '%s\\n' '(\{.*\})' >ser2netweb/distro\.json$", {}),
    "alpine": ("alpine/ser2netweb/APKBUILD",
               r"^\tprintf '%s\\n' '(\{.*\})' >ser2netweb/distro\.json$", {}),
    "freebsd": ("freebsd/net/ser2netweb/Makefile",
                r"^\t@\$\{PRINTF\} '%s\\n' '(\{.*\})' \\\n\t\t> \$\{WRKSRC\}/ser2netweb/distro\.json$",
                {"${PORTNAME}": "ser2netweb", "${PYTHON_PKGNAMEPREFIX}": "py311-"}),
    "macports": ("macports/net/ser2netweb/Portfile",
                 r'^    puts \$fd "(\{.*\})"$',
                 {'\\"': '"', "${datadir}": "/opt/local/var/db/ser2netweb",
                  "${name}": "ser2netweb"}),
    "void": ("void/srcpkgs/ser2netweb/template",
             r"^\tprintf '%s\\n' '(\{.*\})' \\\n\t\t>ser2netweb/distro\.json$", {}),
    "gentoo": ("gentoo/net-misc/ser2netweb/ser2netweb-*.ebuild",
               r"^\tprintf '%s\\n' '(\{.*\})' \\\n\t\t> ser2netweb/distro\.json \|\| die$", {}),
    "nix": ("nix/package.nix",
            r"^    echo '(\{.*\})' > ser2netweb/distro\.json$", {}),
}


def recipe_path(name: str) -> str:
    rel = RECIPES[name][0]
    if "*" in rel:
        folder, pattern = os.path.split(os.path.join(PKG, *rel.split("/")))
        (found,) = [n for n in os.listdir(folder) if re.fullmatch(
            re.escape(pattern).replace(r"\*", ".+"), n)]
        return os.path.join(folder, found)
    return os.path.join(PKG, *rel.split("/"))


def distro_file(name: str) -> tuple[str, dict]:
    """(the text as the recipe has it, the JSON object its build writes)."""
    _rel, pattern, values = RECIPES[name]
    with open(recipe_path(name), encoding="utf-8") as fh:
        found = re.findall(pattern, fh.read(), flags=re.M)
    assert len(found) == 1, f"{name}: writes ser2netweb/distro.json {len(found)} times"
    text = found[0]
    for var, value in values.items():
        assert var in text, f"{name}: {var} isn't in what it writes"
        text = text.replace(var, value)
    assert not re.search(r"\$[({]|%\{", text), f"{name}: a variable left in {text}"
    try:
        info = json.loads(text)
    except ValueError as e:
        raise AssertionError(f"{name}: ser2netweb/distro.json isn't JSON: {e}: {text}") from e
    assert isinstance(info, dict), name
    return found[0], info
