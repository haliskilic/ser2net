"""How text compares where letter case or accents don't count, and how names sort.

* fold(): one name, whatever its letter case (LDAP / OIDC group and user names, account
  names) — Turkish İ, I, ı and i are one letter, accents count (Çorlu is not Corlu);
* search_key(): what a search compares (the dashboard filter, the audit log search) —
  also without accents and compatibility forms, so 'istanbul' finds 'İstanbul Depo' and
  'corlu' finds 'Çorlu'. app.js folds what is typed into the dashboard filter the same
  way (searchKey); the rows' text is folded here, on the server;
* natural_key(): a sort key where digit runs count as numbers (COM2 before COM10,
  'Hat 2' before 'Hat 10').

str.lower() is no use for either: it turns 'İ' into 'i' + U+0307 and leaves 'ı' as it
is, so KALİTE and Kalite, BAKIM and Bakım, or 'istanbul' and 'İstanbul' would differ.
"""
from __future__ import annotations

import re
import unicodedata


def fold(text) -> str:
    """How names compare where letter case doesn't count (LDAP and OIDC group names, LDAP
    user names, an LDAP/OIDC sign-in name or a new account's name against the existing
    ones): NFC, every run of white space (no-break spaces too) as one space, and letter
    case folded so that Turkish İ, I, ı and i are one letter — str.lower() turns 'İ'
    into 'i' + U+0307 and leaves 'ı' as it is, so KALİTE and Kalite, or BAKIM and Bakım,
    would differ."""
    s = " ".join(unicodedata.normalize("NFC", str(text)).split())
    return s.replace("\u0130", "i").replace("\u0131", "i").casefold().replace("i\u0307", "i")


def search_key(text) -> str:
    """`text` as a search compares it: compatibility forms spelled out and accents left
    off (NFKD without its combining marks: Ç is C, Ş is S, ﬁ is fi, a no-break space a
    space), then fold(). A search for search_key(typed) in search_key(text) finds
    'İstanbul Depo' by 'istanbul', 'IŞIK KONTROL' by 'ışık' or 'isik', 'KAZAN DAİRESİ'
    by 'dairesi'. app.js does the same to what is typed (searchKey)."""
    s = unicodedata.normalize("NFKD", str(text))
    return fold("".join(c for c in s if not unicodedata.category(c).startswith("M")))


_DIGITS = re.compile(r"([0-9]+)")


def natural_key(text) -> tuple:
    """A sort key for names with numbers in them: digit runs compare as numbers (COM1,
    COM2, COM10; /dev/ttyS2 before /dev/ttyS10), the rest as search_key() spells it
    (letter case and accents don't count), and the text itself last, so equal keys
    still sort one way."""
    parts = _DIGITS.split(search_key(text))   # text, digits, text, ... (text first)
    return (tuple(int(p) if i % 2 else p for i, p in enumerate(parts)), str(text))
