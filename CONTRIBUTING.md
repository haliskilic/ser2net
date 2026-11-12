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
| Offline wheels | `python tests/test_vendor_wheels.py`; against PyPI (network): `python tools/refresh_wheels.py --verify` |
| Known vulnerabilities | `pip install pip-audit && pip-audit -r requirements.txt` |
| Workflow lint | `pip install zizmor && zizmor --offline .` (settings in `.github/zizmor.yml`) |

The `ci-ok` job passes only when every other CI job passed (the not-required newest-Python
leg aside); it is the check a merge waits for, so a new job in `ci.yml` goes into its
`needs` (`tests/test_ci_gate.py` checks that). Between pull requests,
`.github/workflows/scheduled.yml` re-runs the dependency audit daily and the Docker image
build weekly. Every action in the workflows is pinned to a full commit SHA with its version
in a comment (`uses: actions/checkout@<sha>  # v7.0.1`); Dependabot updates both, and zizmor
and `tests/test_release_pipeline.py` fail on a tag reference.

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
this also applies to Dependabot's pull requests. GitHub's dependency graph (and so the
Dependabot alerts) reads `requirements.txt` for its newest Python only; the pins only
Python 3.10 installs are submitted to it from `main` by
`.github/workflows/dependency-graph.yml` (`tools/dependency_snapshot.py`), so they get
alerts too.

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
maintainer (below).

### Releases
The version goes into `app/__init__.py` and a dated `## [X.Y.Z] — YYYY-MM-DD` section into
`CHANGELOG.md`; once that is merged, an annotated `vX.Y.Z` tag (`vX.Y.Z-rc.N` for a
pre-release) on the merge commit starts `.github/workflows/release.yml`:
1. the tag is checked: its form, equal to `"v"` + `__version__`, the CHANGELOG section
   (`python tools/release_guard.py tag vX.Y.Z` checks these three locally), and the commit
   is on `main`;
2. the whole CI runs on the tagged commit (`ci.yml`, the checks `ci-ok` gates merges on);
   its standalone build is what gets released;
3. the image is built for amd64 + arm64, pushed to GHCR by digest only (no tag) and that
   digest is smoke-tested natively on both architectures;
4. only then are the files attested and the GitHub release created with them (by `gh`; the
   jobs that can write releases or attestations run GitHub's own actions only), and the
   tested image gets its tags `X.Y.Z`, `X.Y` and `latest`. A pre-release gets only its own
   image tag, is marked as a pre-release and never becomes "Latest".

One release runs at a time. A job that failed for a passing reason is re-run (*Re-run
failed jobs* keeps the built files and the pushed image digest); a release that needs a
code change gets a new patch version instead of a moved tag. **Actions → Release → Run workflow** is a dry run of the same
pipeline on any branch: it attests, releases and tags nothing (it leaves one untagged image
version on GHCR).

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
`mypy` (ayrıca `--platform win32` ile), `python tests/run_all.py -v` (Linux'ta `socat` ile
`--socat`), coverage, tarayıcı yerleşim testi, çevrimdışı wheel kontrolü, `pip-audit` ve
workflow denetimi (`zizmor`).

`ci-ok` işi ancak diğer bütün CI işleri geçince geçer (zorunlu olmayan en yeni Python leg'i
hariç); birleştirme onu bekler, bu yüzden `ci.yml`'ye eklenen her iş onun `needs` listesine
de girer (`tests/test_ci_gate.py` bunu denetler). Pull request'ler arasında
`.github/workflows/scheduled.yml` bağımlılık denetimini her gün, Docker imajını her hafta
yeniden çalıştırır. Workflow'lardaki her action, sürümü bir yorumda olacak şekilde tam bir
commit SHA'sına sabitlenir (`uses: actions/checkout@<sha>  # v7.0.1`); ikisini de
Dependabot günceller, tag ile verilen bir referansta zizmor ve
`tests/test_release_pipeline.py` başarısız olur.

`tests/test_dead_peer.py` iptables ile loopback trafiği kestiği için
`SER2NET_BLACKHOLE_TESTS=1` ile yalnızca silinebilir bir ağ ad alanında (network namespace)
ya da sanal makinede çalıştırın.

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
geçerli. GitHub'ın bağımlılık grafiği (dolayısıyla Dependabot uyarıları) `requirements.txt`'i
yalnızca en yeni Python için okur; yalnızca Python 3.10'un kurduğu pin'leri
`.github/workflows/dependency-graph.yml` (`tools/dependency_snapshot.py`) `main`'den grafiğe
gönderir, böylece onlar için de uyarı çıkar.

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
değiştiğini anlatsın. Sürümleri proje sahibi çıkarır (aşağıda).

### Sürümler
Sürüm `app/__init__.py`'ye, tarihli bir `## [X.Y.Z] — YYYY-MM-DD` bölümü `CHANGELOG.md`'ye
yazılır; bu birleştirilince merge commit'ine atılan açıklamalı bir `vX.Y.Z` tag'i
(ön sürüm için `vX.Y.Z-rc.N`) `.github/workflows/release.yml`'yi başlatır:
1. tag denetlenir: biçimi, `"v"` + `__version__`'a eşitliği, CHANGELOG bölümü (bu
   üçünü yerelde `python tools/release_guard.py tag vX.Y.Z` denetler) ve commit'in
   `main` üzerinde olması;
2. tag'lenen commit'te CI'ın tamamı çalışır (`ci.yml`, birleştirmede `ci-ok`'un beklediği
   kontroller); yayımlanan, onun standalone derlemesidir;
3. imaj amd64 + arm64 için derlenir, GHCR'a yalnızca digest ile (tag'siz) gönderilir ve
   bu digest iki mimaride de emülasyonsuz smoke test'ten geçer;
4. ancak bundan sonra dosyalar attest edilir ve GitHub release'i onlarla oluşturulur
   (`gh` ile; release ya da attestation yazabilen işlerde yalnızca GitHub'ın kendi
   action'ları çalışır), test edilen imaj da `X.Y.Z`, `X.Y` ve `latest` tag'lerini alır.
   Ön sürüm yalnızca kendi imaj tag'ini alır, ön sürüm olarak işaretlenir ve hiçbir zaman
   "Latest" olmaz.

Aynı anda tek release çalışır. Geçici bir nedenle başarısız olan iş yeniden çalıştırılır
(*Re-run failed jobs* derlenmiş dosyaları ve gönderilmiş imaj digest'ini korur); kod
değişikliği gereken bir release tag taşınarak değil, yeni bir yama sürümüyle düzeltilir.
**Actions → Release → Run workflow** aynı akışı herhangi bir dalda
deneme olarak çalıştırır: hiçbir şeyi attest etmez, yayımlamaz ya da tag'lemez (GHCR'da
tag'siz bir imaj sürümü bırakır).
