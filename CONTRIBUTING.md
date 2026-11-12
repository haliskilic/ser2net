# Contributing to ser2net

[English](#english) · [Türkçe](#türkçe)

## English

Thanks for helping! Bug reports and feature requests go to
[Issues](https://github.com/haliskilic/ser2net/issues) (security problems: see
[SECURITY.md](SECURITY.md)). For code, open an issue first for anything bigger than a
small fix, then a pull request that links it (`Fixes #123`). Your first pull request asks
you to sign the one-time [Contributor License Agreement](https://gist.github.com/haliskilic/7a7fdcd355f54d1db6943959d7b76823)
(you keep the copyright; it lets the project stay dual-licensed — see `NOTICE`).

### Development setup
```bash
git clone https://github.com/haliskilic/ser2net && cd ser2net
python3 -m venv .venv && . .venv/bin/activate      # Python 3.10 – 3.14
pip install -r requirements.txt cryptography        # + requirements-optional.txt for MQTT/LDAP/OIDC
python ser2net.py --no-bootstrap --data-dir /tmp/s2n-dev
```

### Checks (all run in CI on every pull request)
| What | Command |
|---|---|
| Lint | `ruff check .` |
| Types | `pip install mypy types-pyserial -r requirements-optional.txt`, then `mypy` and `mypy --platform win32` (settings in `mypy.ini`) |
| Tests (any OS) | `python tests/run_all.py -v` |
| + serial data path (Linux, needs `socat`) | `python tests/run_all.py --socat -v` |
| Coverage (CI fails below the floor in `ci.yml`, job `coverage`) | `pip install coverage && python tests/coverage_run.py && coverage report` |
| Browser layout | `pip install playwright && python -m playwright install chromium`, then `SER2NET_UI_TESTS=1 python tests/test_ui_layout.py` |
| Offline wheels | `python tests/test_vendor_wheels.py` |
| Known vulnerabilities | `pip install pip-audit && pip-audit -r requirements.txt` |

`tests/test_dead_peer.py` blackholes a loopback flow with iptables; run it with
`SER2NET_BLACKHOLE_TESTS=1` only in a throwaway network namespace or VM, never on a
machine whose firewall matters. The standalone executable checks need a build
(`SER2NET_STANDALONE_BIN=dist/ser2net python tests/test_standalone.py`).

### Rules that keep upgrades painless
- **Old `config.json` files must keep working.** Every new setting has a default; never
  add a validation rule that rejects a configuration an earlier release accepted —
  normalise old values on load instead. `tests/test_upgrade_from_*.py` boot real older
  configs; extend them when you touch the config model.
- The REST API (`/api/v1`) and the CLI only grow: no removed fields, routes or flags.
- Logs, the audit log and REST API messages stay in English (they are searched and
  parsed).

### Code layout
`app/engine/` the bridges (serial, TCP/UDP, protocols, Modbus, MQTT, cluster) ·
`app/web/routes/` one module per UI area (`login`, `settings`, `mappings`, `console`,
`cluster`, `system`) with shared helpers in `common`, `forms` and `ops` ·
`app/web/api.py` the REST API · `app/config.py` the config model ·
`tests/` one file per feature, collected by `tests/run_all.py`. Every web route and
WebSocket has an access class (who may use it, how CSRF is checked) in
`tests/test_route_access.py`; a new route fails that test until it is classified there.

### Translations
The web UI is English with a Turkish translation. Wrap every user-facing string:
`{{ _("Save changes") }}` in templates (values as `%(name)s`:
`{{ _("Port %(port)s", port=p) }}`), `_("Port {0} is in use.").format(port)` in Python.
Add the Turkish text to `app/locale/tr.json`; `tests/test_i18n.py` fails on a missing or
unused entry and on a translation that loses a placeholder or an HTML tag. Logs, the audit log
and REST API messages are not translated.

### Dependencies
Runtime pins live in `requirements.txt` (with the Python 3.10 line kept by markers),
the optional feature packages bundled into the binaries and the Docker image in
`requirements-optional.txt`. After changing `requirements.txt`, run
`python tools/refresh_wheels.py` so `vendor/wheels/` (the offline install set) matches —
this also applies to Dependabot's pull requests.

### Python versions
CI tests Python 3.10–3.14 on Linux and Windows; the newest release (3.15) runs too, without
blocking a merge, until its wheels have settled. Python 3.10 is past its end of life but
stays supported while distributions people run ser2net on ship it (Ubuntu 22.04): it keeps
the last dependency releases that support it (the `python_version < "3.11"` pins). It is
dropped once one of those frozen pins has a known vulnerability that no release supporting
3.10 fixes (the py3.10 dependency audit reports it), announced in the release notes one
minor release ahead.

### Commits and pull requests
Short imperative subjects with a prefix: `feat:`, `fix:`, `test:`, `docs:`, `ci:`,
`deps:`, `refactor:` (optionally a scope, e.g. `fix(modbus): …`). Keep a pull request to
one topic and describe in it what changes for users. Releases are cut by the
maintainer: version in `app/__init__.py`, `CHANGELOG.md`, an annotated `vX.Y.Z` tag;
`.github/workflows/release.yml` builds, tests and publishes the executables.

## Türkçe

Katkınız için teşekkürler! Hata bildirimleri ve özellik istekleri
[Issues](https://github.com/haliskilic/ser2net/issues) sayfasına (güvenlik sorunları için
[SECURITY.md](SECURITY.md)). Küçük bir düzeltmeden büyük her şey için önce bir issue açın,
sonra onu bağlayan (`Fixes #123`) bir pull request gönderin. İlk pull request'inizde tek
seferlik [Katkıcı Lisans Sözleşmesi](https://gist.github.com/haliskilic/7a7fdcd355f54d1db6943959d7b76823)
imzalamanız istenir (telif hakkı sizde kalır; projenin çift lisanslı kalabilmesini sağlar —
bkz. `NOTICE`).

### Geliştirme ortamı
```bash
git clone https://github.com/haliskilic/ser2net && cd ser2net
python3 -m venv .venv && . .venv/bin/activate      # Python 3.10 – 3.14
pip install -r requirements.txt cryptography        # MQTT/LDAP/OIDC için + requirements-optional.txt
python ser2net.py --no-bootstrap --data-dir /tmp/s2n-dev
```

### Kontroller
Yukarıdaki tablodaki komutların hepsi her pull request'te CI'da çalışır: `ruff check .`,
`mypy` (ayrıca `--platform win32` ile), `python tests/run_all.py -v` (Linux'ta `socat` ile `--socat`), coverage, tarayıcı
yerleşim testi, çevrimdışı wheel kontrolü ve `pip-audit`. `tests/test_dead_peer.py`
iptables ile loopback trafiği kestiği için `SER2NET_BLACKHOLE_TESTS=1` ile yalnızca
silinebilir bir ağ ad alanında (network namespace) ya da sanal makinede çalıştırın.

### Yükseltmeleri sorunsuz tutan kurallar
- **Eski `config.json` dosyaları çalışmaya devam etmeli.** Her yeni ayarın varsayılanı
  olur; önceki bir sürümün kabul ettiği bir yapılandırmayı reddeden doğrulama kuralı
  eklenmez — eski değerler yüklenirken düzeltilir. `tests/test_upgrade_from_*.py` gerçek
  eski config'lerle açılış yapar; config modeline dokunduğunuzda bu testleri genişletin.
- REST API (`/api/v1`) ve komut satırı yalnızca büyür: alan, rota ya da seçenek kaldırılmaz.
- Log'lar, audit log ve REST API mesajları İngilizce kalır (aranır ve işlenirler).

### Çeviriler
Web arayüzü İngilizce, Türkçe çevirisiyle. Kullanıcıya görünen her metni sarın: şablonlarda
`{{ _("Save changes") }}`, Python'da `_("Port {0} is in use.").format(port)`. Türkçesini
`app/locale/tr.json`'a ekleyin; `tests/test_i18n.py` eksik ya da kullanılmayan girdide ve yer
tutucusunu ya da HTML etiketini kaybeden çeviride başarısız olur.

### Route'lar
Her web route'unun ve WebSocket'in bir erişim sınıfı vardır (kim kullanabilir, CSRF nasıl
denetlenir): `tests/test_route_access.py`. Yeni bir route orada sınıflandırılana kadar bu
test başarısız olur.

### Bağımlılıklar
Çalışma zamanı sürümleri `requirements.txt`'te (Python 3.10 hattı işaretlerle korunur),
binary'lere ve Docker imajına giren isteğe bağlı paketler `requirements-optional.txt`'te.
`requirements.txt`'i değiştirdikten sonra `python tools/refresh_wheels.py` çalıştırın;
`vendor/wheels/` (çevrimdışı kurulum seti) eşleşsin. Bu Dependabot'un PR'ları için de
geçerli.

### Python sürümleri
CI, Python 3.10–3.14'ü Linux ve Windows'ta test eder; en yeni sürüm (3.15) de çalışır, ama
wheel'ları oturana kadar birleştirmeyi engellemez. Python 3.10'un ömrü doldu, ama ser2net'in
kullanıldığı dağıtımlar onu getirdiği sürece (Ubuntu 22.04) desteklenir: onu destekleyen
son bağımlılık sürümlerinde kalır (`python_version < "3.11"` pin'leri). Bu donmuş
pin'lerden birinde, 3.10'u destekleyen hiçbir sürümün düzeltmediği bilinen bir güvenlik
açığı çıkınca (py3.10 bağımlılık denetimi bunu bildirir) bırakılır; bu, bir minor sürüm
önceden sürüm notlarında duyurulur.

### Commit ve pull request
Kısa, emir kipinde ve önekli başlıklar: `feat:`, `fix:`, `test:`, `docs:`, `ci:`,
`deps:`, `refactor:`. Bir pull request tek konu içersin ve kullanıcılar için neyin
değiştiğini anlatsın. Sürümleri proje sahibi çıkarır.
