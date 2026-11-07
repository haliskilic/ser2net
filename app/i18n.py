"""Translations of the web UI (English source strings, Turkish catalog).

Every user-facing string is written in English and wrapped: ``_("Mappings")`` in
templates and Python; strings with values use numbered placeholders —
``_("Port {0} is in use.").format(port)`` — so a translation may put them in another
order. The language of the current request lives in a context variable that
GuardMiddleware sets for the web UI only; everything else (logs, the audit log, the
REST API, start-up messages) runs with the default and stays English.

Catalogs: app/locale/<lang>.json, a flat {"English": "Translation"} object.
tests/test_i18n.py checks that every wrapped string has a Turkish translation with the
same placeholders.
"""
from __future__ import annotations

import contextvars
import json
import os
from functools import lru_cache

LANGUAGES = {"en": "English", "tr": "Türkçe"}
DEFAULT = "en"
COOKIE = "ser2net_lang"
LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")

_lang: contextvars.ContextVar[str] = contextvars.ContextVar("ser2net_lang", default=DEFAULT)


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict:
    if lang == DEFAULT:
        return {}
    try:
        with open(os.path.join(LOCALE_DIR, f"{lang}.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def missing_catalogs() -> list[str]:
    """The offered languages whose catalog can't be loaded — a packaging mistake that
    would otherwise only show as an English UI with the language switch still on
    (runtime logs a warning for each at start-up; this also loads the catalogs)."""
    return [lang for lang in LANGUAGES if lang != DEFAULT and not catalog(lang)]


def current() -> str:
    return _lang.get()


def set_language(lang: str):
    """Use ``lang`` for the rest of this request; returns a token for reset()."""
    return _lang.set(lang if lang in LANGUAGES else DEFAULT)


def reset(token) -> None:
    _lang.reset(token)


def gettext(message: str) -> str:
    return catalog(_lang.get()).get(message, message) if message else message


def ngettext(singular: str, plural: str, n: int) -> str:
    return gettext(singular if n == 1 else plural)


_ = gettext


def negotiate(cookie: str | None, accept_language: str | None, default: str = "auto") -> str:
    """The language for a request: the visitor's own choice (cookie), else the admin's
    default when it is not "auto", else the browser's preference, else English."""
    if cookie in LANGUAGES:
        return cookie
    if default in LANGUAGES:
        return default
    best, best_q = DEFAULT, 0.0
    for part in (accept_language or "").split(","):
        tag, _sep, params = part.strip().partition(";")
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                q = 0.0
        code = tag.strip().lower().split("-", 1)[0]
        if code in LANGUAGES and q > best_q:
            best, best_q = code, q
    return best
