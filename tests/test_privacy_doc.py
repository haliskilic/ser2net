"""docs/PRIVACY.md says what the code does.

The page lists how long each store keeps data and the settings that keep less, in English
and in Turkish. This checks its numbers against the constants they come from, that the
settings it names exist under those names in the UI (both languages), and that what it calls
off by default is. Change the code and this fails until the page says the same.

Run: python3 tests/test_privacy_doc.py
"""
import json
import os
import re

from _server import ROOT

from ser2netweb import state
from ser2netweb.config import (
    BACKUPS_KEPT, SESSION_TIMEOUT_DEFAULT_S, AppConfig, MappingOptions, MqttSettings,
)
from ser2netweb.state import LOG_STDOUT_ENV

DOC = os.path.join(ROOT, "docs", "PRIVACY.md")
MB = 1024 * 1024


def _text() -> tuple[str, str]:
    with open(DOC, encoding="utf-8") as fh:
        doc = " ".join(fh.read().split())
    en, tr = doc.split("## Türkçe", 1)
    return en, tr


def test_numbers():
    en, tr = _text()
    assert state.ALL_LOG_MAX_BYTES == 10 * MB and state.ALL_LOG_BACKUPS == 5
    assert "rotated at 10 MB, 5 old files kept" in en and "10 MB'ta döner, 5 eski dosya" in tr
    assert state.AUDIT_LOG_MAX_BYTES == 5 * MB
    assert "rotated at 5 MB, 1 old file kept" in en and "5 MB'ta döner, 1 eski dosya" in tr
    assert f"the newest {BACKUPS_KEPT}" in en and f"en yeni {BACKUPS_KEPT}" in tr
    assert state.LOG_MAX_AGE_DAYS == 15 and state.LOG_MAX_BYTES == 100 * MB
    assert "15 days or 100 MB" in en and "15 gün ya da 100 MB" in tr
    opts = MappingOptions()
    assert (opts.trace_max_mb, opts.trace_keep) == (10.0, 3) and not opts.trace_both
    assert "10 MB × 3 old files" in en and "10 MB × 3 eski dosya" in tr
    assert SESSION_TIMEOUT_DEFAULT_S == 8 * 3600
    assert "8 hours by default" in en and "varsayılan 8 saat" in tr
    assert (state.FAILED_LOGIN_RECORDS, state.FAILED_LOGIN_WINDOW_S) == (20, 600)
    assert "(20 failed sign-ins in 10 minutes)" in en and "(10 dakikada 20 başarısız giriş)" in tr
    with open(os.path.join(ROOT, "ser2netweb", "web", "routes", "system.py"), encoding="utf-8") as fh:
        assert "max_age=365 * 86400" in fh.read(), "the language cookie's lifetime"
    assert "the language (1 year)" in en and "dil (1 yıl)" in tr
    from ser2netweb.config import BACKUPS_MIN_KEPT
    assert state.AUDIT_FILES_KEPT == 5 and "up to 5 old files" in en and "en fazla 5 eski dosya" in tr
    assert BACKUPS_MIN_KEPT == 5 and "never the newest 5" in en and "en yeni 5 hiçbir zaman" in tr
    assert (state.LOG_VIEW_LINES, state.LOG_TRIM_TO) == (1000, 0.9)
    assert "the last 1000 lines stay, a file over the size is cut to 90 MB" in en
    assert "son 1000 satır kalır, boyutu aşan dosya 90 MB'a kısaltılır" in tr
    with open(os.path.join(ROOT, "ser2netweb", "web", "routes", "login.py"), encoding="utf-8") as fh:
        assert "max_age=600" in fh.read(), "the SSO state cookie's lifetime"
    assert "its state (10 minutes)" in en and "durumu (10 dakika)" in tr
    print("PRIVACY.md: rotation sizes, backups kept, log and trace limits, cookie lifetimes "
          "and the failed sign-in limit match the code (EN + TR)  OK")


def test_settings_named():
    en, tr = _text()
    with open(os.path.join(ROOT, "ser2netweb", "web", "templates", "settings.html"), encoding="utf-8") as fh:
        page = fh.read()
    with open(os.path.join(ROOT, "ser2netweb", "locale", "tr.json"), encoding="utf-8") as fh:
        catalog = json.load(fh)
    labels = ["Web UI requests in all.log", "Remove it from the backups", "Sign-in notice",
              "Remove LDAP / OIDC accounts not signed in for (days)", "Retention"]
    for label in labels:
        assert f'_("{label}")' in page, label
        assert f"*{label}*" in en, label
        assert f"*{catalog[label].replace(' (gün)', '')}" in tr, (label, catalog[label])
    starts = {"Leave out personal data": "Leave out personal data: IP addresses only",
              "Send each recipient a message of their own": "Send each recipient a message of"}
    for short, full in starts.items():
        key = next(k for k in catalog if k.startswith(full))
        assert f"*{short}*" in en and f"*{catalog[key].split(' (')[0].split(':')[0]}*" in tr, short
    assert f"`{LOG_STDOUT_ENV}=off`" in en and f"`{LOG_STDOUT_ENV}=off`" in tr
    for event in ("Client connected", "Failed sign-in to this UI",
                  "Serial device lost", "Mapping failed to start"):
        assert event in en, event
    print("PRIVACY.md: the settings it names exist under those names (EN + TR)  OK")


def test_off_by_default():
    cfg = AppConfig()
    assert not cfg.notifications.enabled and not cfg.cluster.enabled
    assert not MqttSettings().enabled
    assert not cfg.notifications.mask_personal_data and not cfg.notifications.email_separately
    assert cfg.admin_ui.request_log == "all" and not cfg.admin_ui.login_notice
    assert cfg.external_users_expire_days == 0
    en, tr = _text()
    assert re.search(r"Notifications\*\* \(Settings → \*Notifications\*, off by default\)", en)
    assert "LAN cluster** (off by default)" in en and "LAN cluster** (varsayılan kapalı)" in tr
    print("PRIVACY.md: notifications, the cluster, MQTT and the new settings are off by "
          "default, as it says  OK")


def main():
    test_numbers()
    test_settings_named()
    test_off_by_default()
    print("\nPASS: docs/PRIVACY.md matches the code")


if __name__ == "__main__":
    main()
