"""Turkish and other Unicode text: how names compare, are searched and sort.

Checks (one table of test vectors, used for every side):
  * fold(): letter case doesn't count and Turkish İ, I, ı and i are one letter (BİLGİ
    İŞLEM is bilgi işlem, IŞIK is ışık), but accents do (Çorlu is not Corlu); decomposed
    input (NFD) and no-break spaces fold as the composed text and a space;
  * search_key(): also without accents, so a lowercase or accent-less search finds
    'İstanbul Depo', 'KAZAN DAİRESİ', 'IŞIK KONTROL', 'Çorlu';
  * natural_key(): COM1, COM2, COM10 and Hat 2 before Hat 10; the UI's port lists
    (portlist.visible) come in that order, the REST API keeps the device order;
  * app.js folds what is typed into the dashboard filter the same way (searchKey, run
    with node when it is installed; the Chromium test types into the real page).

Run: python3 tests/test_text_fold.py
"""
import json
import os
import re
import shutil
import subprocess

from _server import ROOT

from app.config import fold as config_fold
from app.engine import portlist
from app.textfold import fold, natural_key, search_key

# (text, fold(text), search_key(text))
VECTORS = [
    ("BİLGİ İŞLEM", "bilgi işlem", "bilgi islem"),
    ("bilgi işlem", "bilgi işlem", "bilgi islem"),
    ("IŞIK KONTROL", "işik kontrol", "isik kontrol"),
    ("ışık", "işik", "isik"),
    ("ŞİŞLİ", "şişli", "sisli"),
    ("İstanbul Depo", "istanbul depo", "istanbul depo"),
    ("KAZAN DAİRESİ", "kazan dairesi", "kazan dairesi"),
    ("İzmir", "izmir", "izmir"),
    ("Çorlu", "çorlu", "corlu"),
    ("C\u0327orlu", "çorlu", "corlu"),                 # decomposed (NFD)
    ("Hat\u00a02", "hat 2", "hat 2"),                   # no-break space
    ("  Hat \u202f 10 ", "hat 10", "hat 10"),           # narrow no-break space, padding
    ("ÖZEL ĞÜÇ", "özel ğüç", "ozel guc"),
    ("Straße", "strasse", "strasse"),
    ("\ufb01BER", "fiber", "fiber"),                    # the ﬁ ligature
]
SEARCHES = [   # (typed, row text): found
    ("istanbul", "İstanbul Depo"), ("İSTANBUL", "istanbul depo"), ("izmir", "İzmir"),
    ("dairesi", "KAZAN DAİRESİ"), ("ışık", "IŞIK KONTROL"), ("isik", "IŞIK KONTROL"),
    ("IŞIK", "ışık kontrol"), ("corlu", "Çorlu"), ("ÇORLU", "corlu"), ("şişli", "ŞİŞLİ"),
    ("hat 2", "Hat\u00a02"),
]


def test_fold_and_search_key():
    assert config_fold is fold, "app.config.fold is the same function (old import path)"
    bad = [(t, fold(t), want) for t, want, _k in VECTORS if fold(t) != want]
    bad += [(t, search_key(t), key) for t, _f, key in VECTORS if search_key(t) != key]
    assert not bad, bad
    assert fold("Çorlu") != fold("Corlu"), "a name keeps its accents"
    for typed, text in SEARCHES:
        assert search_key(typed) in search_key(text), (typed, text)
    assert search_key("istasyon") not in search_key("İSTANBUL"), "no false match"
    # what str.lower() got wrong
    assert "istanbul" not in "İstanbul Depo".lower() and "ışık" not in "IŞIK KONTROL".lower()
    print(f"fold / search_key: {len(VECTORS)} vectors, {len(SEARCHES)} searches (İ/ı/I/i, "
          "accents, NFD, no-break spaces)  OK")


def test_natural_order():
    assert sorted(["COM10", "COM2", "COM1"], key=natural_key) == ["COM1", "COM2", "COM10"]
    assert sorted(["Hat 10", "hat 2", "Hat 1"], key=natural_key) == ["Hat 1", "hat 2", "Hat 10"]
    assert sorted(["/dev/ttyS10", "/dev/ttyUSB0", "/dev/ttyS2"], key=natural_key) == \
        ["/dev/ttyS2", "/dev/ttyS10", "/dev/ttyUSB0"]
    assert sorted(["COM01", "COM1"], key=natural_key) == ["COM01", "COM1"], "equal keys: by text"
    ports = [{"device": d, "hardware": None} for d in ("COM1", "COM10", "COM2", "COM3")]
    ports.append({"device": "/dev/ttyS5", "hardware": False})
    assert [p["device"] for p in portlist.visible(ports)] == ["COM1", "COM2", "COM3", "COM10"]
    assert [p["device"] for p in portlist.visible(ports, show_all=True)][-1] == "COM10"
    assert [p["device"] for p in ports][:3] == ["COM1", "COM10", "COM2"], "the list itself unchanged"
    print("natural_key: COM1 < COM2 < COM10, Hat 2 < Hat 10; the UI's port lists in that order  OK")


def test_app_js_fold():
    """app.js's searchKey gives search_key's results (its code, run with node)."""
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed: app.js's searchKey is checked in Chromium only "
              "(test_ui_layout)")
        return
    src = open(os.path.join(ROOT, "app", "web", "static", "app.js"), encoding="utf-8").read()
    code = re.search(r"\n  var MARKS = .*?\n  function searchKey\(s\) \{.*?\n  \}\n", src, re.S)
    assert code, "searchKey not found in app.js"
    texts = [t for t, _f, _k in VECTORS] + [t for pair in SEARCHES for t in pair]
    script = code.group(0) + (
        "var texts = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        "process.stdout.write(JSON.stringify(texts.map(searchKey)));")
    out = subprocess.run([node, "-e", script], input=json.dumps(texts), capture_output=True,
                         text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    bad = [(t, g, search_key(t)) for t, g in zip(texts, got, strict=True) if g != search_key(t)]
    assert not bad, bad
    print(f"app.js searchKey = search_key for all {len(texts)} texts (node)  OK")


def main():
    test_fold_and_search_key()
    test_natural_order()
    test_app_js_fold()
    print("\nPASS: Turkish / Unicode folding, search and natural order")


if __name__ == "__main__":
    main()
