# Contributing to ser2netweb

[English](#english) · [Türkçe](#türkçe)

## English

Thanks for helping! Bug reports and feature requests go to
[Issues](https://github.com/haliskilic/ser2netweb/issues) (security problems: see
[SECURITY.md](SECURITY.md)). For code, open an issue first for anything bigger than a
small fix, then a pull request that links it (`Fixes #123`). Your first pull request asks
you to sign the one-time [Contributor License Agreement](https://gist.github.com/haliskilic/7a7fdcd355f54d1db6943959d7b76823)
(you keep the copyright; it lets the project stay dual-licensed — see `NOTICE`).

### Development setup
```bash
git clone https://github.com/haliskilic/ser2netweb && cd ser2netweb
python3 -m venv .venv && . .venv/bin/activate      # Python 3.10 – 3.14
pip install -r requirements.txt cryptography        # + requirements-optional.txt for MQTT/LDAP/OIDC
python ser2netweb.py --no-bootstrap --data-dir /tmp/s2n-dev
```

### Checks (all run in CI on every pull request)
| What | Command |
|---|---|
| Lint | `ruff check .` |
| Types | `pip install mypy types-pyserial -r requirements-optional.txt`, then `mypy` and `mypy --platform win32` (settings in `mypy.ini`) |
| Tests (any OS) | `python tests/run_all.py -v` |
| + serial data path (Linux, needs `socat`) | `python tests/run_all.py --socat -v` |
| Coverage (CI fails below the floor in `ci.yml`, job `coverage`) | `pip install coverage && python tests/coverage_run.py && coverage report` |
| Browser layout | `pip install playwright && python -m playwright install chromium`, then `SER2NETWEB_UI_TESTS=1 python tests/test_ui_layout.py` |
| Offline wheels | `python tests/test_vendor_wheels.py`; against PyPI (network): `python tools/refresh_wheels.py --verify` |
| Offline first start (a fresh checkout without `lib/`; CI: every test leg, plus Linux arm64, macOS, armv7 under QEMU) | `tests/offline_first_start.sh python3` |
| Known vulnerabilities | `pip install pip-audit && pip-audit -r requirements.txt` |
| Workflow lint | `pip install zizmor && zizmor --offline .` (settings in `.github/zizmor.yml`) |

The `ci-ok` job passes only when every other CI job passed (the not-required newest-Python
leg aside); it is the check a merge waits for, so a new job in `ci.yml` goes into its
`needs` (`tests/test_ci_gate.py` checks that). Between pull requests,
`.github/workflows/scheduled.yml` re-runs the dependency audit daily, and weekly the Docker
image build and a soak of the serial data path (`tests/stress_24.py`: 24 bridges, every
device unplugged and plugged back 20 times, open files and memory checked; the CI test legs
run it small), and `.github/workflows/image-refresh.yml` scans the published image weekly and
rebuilds the latest release on a moved base image (`docs/DOCKER.md`). Every action in the workflows is pinned to a full commit SHA with its version
in a comment (`uses: actions/checkout@<sha>  # v7.0.1`); Dependabot updates both, and zizmor
and `tests/test_release_pipeline.py` fail on a tag reference.

`tests/test_dead_peer.py` blackholes a loopback flow with iptables; run it with
`SER2NETWEB_BLACKHOLE_TESTS=1` only in a throwaway network namespace or VM, never on a
machine whose firewall matters. The standalone executable checks need a build
(`SER2NETWEB_STANDALONE_BIN=dist/ser2netweb python tests/test_standalone.py`).
`tests/systemd_smoke.sh` (CI jobs `systemd unit` and `systemd unit (binary)`) installs the
systemd units as the README says and runs them on the runner's systemd: it adds a `ser2netweb`
user, `/opt/ser2netweb` and the service, so run it only on a throwaway VM. A change to a unit in
`systemd/` goes into every unit there (`tests/test_systemd_units.py` checks them all).
`systemd/ser2netweb-install.sh` is tested staged under a scratch folder (`--root`,
`tests/test_install_script.py`) and for real by `tests/install_smoke.sh <binary>` (CI job
`install script (binary)`), which installs the binary system-wide and remounts `/tmp`: a
throwaway VM only, too. The `.deb` / `.rpm` packages (`packaging/`, built by
`tools/build_packages.py` with the nfpm release it pins) are checked by
`tests/test_packages.py` (with `SER2NETWEB_NFPM=<nfpm>` it also builds them from a stand-in
binary; `python3 tools/build_packages.py fetch-nfpm DIR` downloads that nfpm), installed in
clean Debian, Ubuntu, Fedora and AlmaLinux containers by `tests/package_smoke.sh IMAGE ARCH`
and on the runner's systemd by `tests/package_smoke.sh --host` (CI jobs `package on …`; the
latter a throwaway VM only). The PyPI package:
`pip install --require-hashes --only-binary=:all: -r requirements-build.txt`,
`python -m build --no-isolation --outdir pypi-dist`, then
`python tests/pypi_smoke.py pypi-dist` (CI jobs `pypi package …`), which installs the wheel
in a temporary venv and starts a server on a free port.
The APT / DNF repositories on GitHub Pages (`tools/package_repo.py`, `docs/PACKAGES.md`)
are checked by `tests/test_package_repo.py`; a pull request that changes them runs
`package-repo.yml`, which builds them from the latest releases, signs them with a throwaway
key and tests them in clean containers (`tests/package_repo_smoke.sh SITE IMAGE VERSION`,
Docker), without deploying.

A test that starts a server uses `tests/_server.py` (`Server`, or `server_log` for the
output: a file, never an unread pipe) and listens on `free_port()` / `free_udp_port()` or
port 0, never on a fixed port: in the ephemeral range an earlier test's closed connection
can still hold it. `tests/test_harness.py` fails on a fixed listening port unless a
`# fixed port: <why>` comment explains it, and on a test file `tests/run_all.py` doesn't list.

### Rules that keep upgrades painless
- **Old `config.json` files must keep working.** Every new setting has a default; never
  add a validation rule that rejects a configuration an earlier release accepted —
  normalise old values on load instead. `tests/test_upgrade_from_*.py` boot real older
  configs; extend them when you touch the config model.
- The REST API (`/api/v1`) and the CLI only grow: no removed fields, routes or flags.
- Logs, the audit log and REST API messages stay in English (they are searched and
  parsed).

### Code layout
`ser2netweb.py` checks the Python version and runs `ser2netweb/cli.py` (`ser2net.py`, its name up
to 2.9.x, does the same), which parses the command line and starts `ser2netweb/runtime.py`, which
owns the event loop and runs the web server and the engine side by side; each module's
docstring says more.

| Where | What |
|---|---|
| `ser2netweb/config.py` | the configuration model (dataclasses), reading `config.json` typed, validation, atomic saves and `backups/` |
| `ser2netweb/state.py` | the shared state: config, supervisor, port watcher, `all.log` / audit log; `ser2netweb/reload.py` (SIGHUP), `ser2netweb/tlsreload.py`, `ser2netweb/storage.py` (free space, retention), `ser2netweb/watchdog.py`, `ser2netweb/datalock.py` |
| `ser2netweb/engine/supervisor.py` | one runner per mapping, applying config changes; `bridge.py` (serial ↔ TCP/UDP, telnet / RFC 2217), `serial_link.py` / `serial_io.py` / `serial_compat.py` (the serial port), `modbus_gateway.py` + `modbus.py` (Modbus RTU ↔ TCP, polling), `mqtt_pub.py`, `sparkplug_node.py` + `sparkplug.py`, `tls.py`, `access.py` / `netguard.py` (who may connect, limits), `portlist.py` / `portdiag.py` (ports and what's wrong with them), `cluster.py`, `netinfo.py`, `bindhint.py`, `sockopts.py` |
| `ser2netweb/web/server.py` | the Starlette app and its middleware; `auth.py` (sessions, CSRF, sign-in limits), `ldap_auth.py`, `oidc_auth.py`, `totp.py`, `proxy.py` (reverse proxy), `httpguard.py`, `api.py` (REST API `/api/v1`), `certinfo.py`, `selfsigned.py` |
| `ser2netweb/web/routes/` | one module per UI area (`login`, `totp`, `settings`, `mappings`, `console`, `cluster`, `system`), shared helpers in `common`, `forms` and `ops` |
| `ser2netweb/web/templates/`, `ser2netweb/web/static/` | Jinja templates; `app.js`, `points.js`, `app.css` and the vendored htmx / xterm.js |
| `ser2netweb/notify.py` | webhook / e-mail notifications |
| `ser2netweb/importer.py`, `ser2netweb/ser2net_import.py` | mapping imports and the classic ser2net reader |
| `ser2netweb/accounts.py` | `--set-password`, `--sign-out-all`, the first admin |
| `ser2netweb/winservice.py`, `ser2netweb/standalone.py`, `ser2netweb/sdnotify.py` | the Windows service, the PyInstaller executables, systemd's notify |
| `ser2netweb/i18n.py`, `ser2netweb/locale/tr.json` | translations |
| `ser2netweb/clock.py`, `ser2netweb/explain.py`, `ser2netweb/logtext.py`, `ser2netweb/logsetup.py`, `ser2netweb/textfold.py`, `ser2netweb/optional.py`, `ser2netweb/signins.py`, `ser2netweb/healthcheck.py`, `ser2netweb/console.py` | times, error texts, log lines, text comparison, optional packages, last sign-ins, `--healthcheck`, the first-run console picker |
| `tests/` | one file per feature, collected by `tests/run_all.py` |

Every web route and WebSocket has an access class (who may use it, how CSRF is checked) in
`tests/test_route_access.py`; a new route fails that test until it is classified there.

### Translations
The web UI is English with a Turkish translation. Wrap every user-facing string:
`{{ _("Save changes") }}` in templates (values as `%(name)s`:
`{{ _("Port %(port)s", port=p) }}`), `_("Port {0} is in use.").format(port)` in Python.
A text with a count has both English forms, never "record(s)":
`{{ ngettext("%(num)s record", "%(num)s records", n) }}`,
`ngettext("Imported {0} mapping.", "Imported {0} mappings.", n).format(n)`, and for app.js
a `[_("..."), _("...")]` pair in `base.html`; each form is an entry of its own (Turkish:
the same text twice). Add the Turkish text to `ser2netweb/locale/tr.json` (new entries at the end);
`tests/test_i18n.py` fails on a missing or unused entry, on a translation that loses a
placeholder or an HTML tag, and on a "(s)" count. The languages are listed once, in
`ser2netweb/i18n.py` (`LANGUAGES`). Logs, the audit log and REST API messages are not translated.

### Dependencies
Runtime pins live in `requirements.txt` (with the Python 3.10 line kept by markers),
the optional feature packages bundled into the binaries and the Docker image in
`requirements-optional.txt`. After changing `requirements.txt`, run
`python tools/refresh_wheels.py` so `vendor/wheels/` (the offline install set) matches.
On Dependabot's pull requests `.github/workflows/refresh-pins.yml` does that: it pushes the
refreshed wheels onto the branch and starts CI on the new commit (on another branch, run it
by hand: Actions → Refresh pinned files → Run workflow). Until the wheels match, only the job
"offline wheels match requirements.txt" fails — the test legs leave that check to it — and
it attaches the refreshed files as a patch (artifact `refreshed-pins`). The Docker image and the standalone
executables install from hash-locked files instead (`pip install --require-hashes`):
`requirements-lock.txt` (the same pins for Python 3.12, with the sha256 of each release's
files) and `requirements-build.txt` (PyInstaller, and build + setuptools for the PyPI
package, with their dependencies); Dependabot updates them with `requirements.txt`, and
`python tools/refresh_locks.py` regenerates them after a hand-made change
(`tests/test_build_lock.py` checks that they match). The PyPI package (`pyproject.toml`)
lists the packages ser2netweb imports as ranges from each pin to below its next major (next
minor for 0.x): a pin bumped out of its range fails `tests/test_pyproject.py` in the same
job, so widen the range in the same pull request. The browser libraries
in `ser2netweb/web/static/` (htmx, xterm.js) are pinned in `vendor/npm/package.json`; change a
version there and run `python tools/refresh_assets.py`, which copies the files out of the npm
release and records their sha256 (Dependabot proposes minor updates, `refresh-pins.yml`
copies them in; test the console by hand before a major one). GitHub's dependency graph (and so the
Dependabot alerts) reads `requirements.txt` for its newest Python only, without the pins
only Python 3.10 installs; `.github/workflows/dependency-graph.yml`
(`tools/dependency_snapshot.py`) submits the whole file from `main` instead, so those get
alerts too.

### Python versions
CI tests Python 3.10–3.14 on Linux and Windows; the newest release (3.15) runs too, without
blocking a merge, until its wheels have settled. Python 3.10 is past its end of life but
stays supported while distributions people run ser2netweb on ship it (Ubuntu 22.04): it keeps
the last dependency releases that support it (the `python_version < "3.11"` pins). It is
dropped once one of those frozen pins has a known vulnerability that no release supporting
3.10 fixes (the py3.10 dependency audit reports it), announced in the release notes one
minor release ahead.

### Third-party code
Runtime dependencies are pinned exactly. When one has a bug its maintainers have not
fixed in a release (pyserial and pyserial-asyncio-fast rarely release), the fix goes into
ser2netweb, not into a fork or a process-wide patch:
- One module per library (`ser2netweb/engine/serial_compat.py` for pyserial and
  pyserial-asyncio-fast), with a subclass ser2netweb itself uses that overrides as little as
  possible. Its docstring names the upstream issue, pull request or commit it mirrors;
  where a report exists, comment there instead of filing another.
- It applies whenever the upstream members it builds on exist (feature detection), not
  behind a version check, which would bring the bug back with a newer release that still
  has it; the start-up log says once when an installed version is not the one checked.
- It has a test that needs no hardware, on Windows too where it applies.
  `tests/test_serial_compat.py` pins the upstream code each override replaces by a hash
  of its source, so a bump that changes that code fails until the override is reviewed
  and the hash updated; a check that the upstream bug is still there says when an
  override can go.
- It is removed once a release has the fix (then bump the pin and refresh
  `vendor/wheels/`).
- BSD-licensed code (pyserial, pyserial-asyncio-fast) may be subclassed or copied with its
  notice; LGPL code (ldap3) is used through its public API only.

Dependabot opens a pull request of its own for pyserial, pyserial-asyncio-fast, ldap3 and
pyasn1 (everything else comes grouped): read their changelogs, and for ldap3 / pyasn1 check
the CI legs that install `requirements-optional.txt`.

### Commits and pull requests
Short imperative subjects with a prefix: `feat:`, `fix:`, `test:`, `docs:`, `ci:`,
`deps:`, `refactor:` (optionally a scope, e.g. `fix(modbus): …`). Keep a pull request to
one topic and describe in it what changes for users. A pull request that changes the UI
shows it (before / after screenshots) and keeps `tr.json` complete; one that changes
documented behaviour updates both READMEs and the guide in `docs/` it touches
(`tests/test_docs.py` checks the CLI options and the links). Releases are cut by the
maintainer (below).

### Releases
The version goes into `ser2netweb/__init__.py` and a dated `## [X.Y.Z] — YYYY-MM-DD` section into
`CHANGELOG.md`; once that is merged, an annotated `vX.Y.Z` tag (`vX.Y.Z-rc.N` for a
pre-release) on the merge commit starts `.github/workflows/release.yml`:
1. the tag is checked: its form, equal to `"v"` + `__version__`, the CHANGELOG section
   (`python tools/release_guard.py tag vX.Y.Z` checks these three locally), and the commit
   is on `main`;
2. the whole CI runs on the tagged commit (`ci.yml`, the checks `ci-ok` gates merges on);
   its standalone build is what gets released, with the `.deb` / `.rpm` packages it makes of
   the Linux binaries and the PyPI sdist and wheel;
3. the image is built for amd64, arm64 and arm/v7, pushed to GHCR by digest only (no tag)
   as `ghcr.io/haliskilic/ser2netweb` and, for the 3.x line, as `ghcr.io/haliskilic/ser2net`
   (its name up to 2.9.x), and that digest is smoke-tested on each (amd64 and arm64
   natively, arm/v7 under QEMU);
4. only then are the files attested and the GitHub release created with them (by `gh`; the
   jobs that can write releases or attestations run GitHub's own actions only; the files are
   the `ser2netweb-*` ones and this version's four packages, as
   `tools/release_guard.py assets` lists them), and the tested image gets its tags `X.Y.Z`,
   `X.Y` and `latest` under both names. A pre-release gets only its own image tag, is
   marked as a pre-release and never becomes "Latest";
5. last, the job `publish to PyPI` uploads the sdist and wheel CI built (environment `pypi`,
   trusted publishing: no token). A pre-release goes up as one too (`X.Y.ZrcN`, which pip
   installs only with `--pre`). Nothing waits for this job: when it fails the release and
   the image are out all the same, and *Re-run failed jobs* publishes once the cause is
   fixed.
6. not for a pre-release, the job `start the release channels` starts
   `package-repo.yml` on `main`: it adds the release's packages to the signed APT / DNF
   repositories on GitHub Pages (the newest three releases, `docs/PACKAGES.md`) once they
   passed its container tests. The environment `github-pages` must allow `main` and the
   `v*` tags; without the secret `PACKAGES_GPG_PRIVATE_KEY` it deploys nothing.

**Once, before the first release that goes to PyPI** (the maintainer, on pypi.org): *Your
account → Publishing → Add a new pending publisher*, GitHub, with PyPI project name
`ser2netweb`, owner `haliskilic`, repository name `ser2netweb`, workflow name
`release.yml` and environment name `pypi`. The first upload then creates the project. In
the repository's *Settings → Environments*, `pypi` (created by the first run otherwise) can
get the same protection as `release` (deployment branches and tags: `v*`; required
reviewers if wanted). Until the publisher exists the job fails with an `invalid-publisher`
error from PyPI, after everything else is out.

One release runs at a time. A job that failed for a passing reason is re-run (*Re-run
failed jobs* keeps the built files and the pushed image digest); a release that needs a
code change gets a new patch version instead of a moved tag. **Actions → Release → Run workflow** is a dry run of the same
pipeline on any branch: it attests, releases, tags and uploads nothing (it leaves one untagged image
version on GHCR).

The release commit also updates the version lines of `ROADMAP.md` (its header and the
Shipped entry) and the image tag examples in both READMEs (`:X.Y`, `:X.Y.Z`); when the UI
changed, the screenshots are taken again (`tools/_capture_screenshots.py`, both languages).

After a release is tagged, freeze what it writes: add the settings it introduced to
`FEATURES` in `tests/make_config_fixture.py`, run `python tests/make_config_fixture.py
vX.Y.Z` and commit `tests/fixtures/config-X.Y.Z.json` with an upgrade test like
`tests/test_upgrade_from_2_8.py` (its data folder layout too; `tests/docker_smoke.sh` boots
the 2.7.1 one), and run `python tests/make_form_fixtures.py vX.Y.Z` for the mixed-version
fleet edits (`tests/fixtures/forms-X.Y.Z.json`, `tests/test_mixed_version_edit.py`), before
the next release changes the config model or the mapping form. A field the mapping form
gains goes into a `FORM_BLOCKS_SINCE` block (`ser2netweb/web/routes/forms.py`) of the version that
reads it.

## Türkçe

Katkınız için teşekkürler! Hata bildirimleri ve özellik istekleri
[Issues](https://github.com/haliskilic/ser2netweb/issues) sayfasına (güvenlik sorunları için
[SECURITY.md](SECURITY.md)). Küçük bir düzeltmeden büyük her şey için önce bir issue açın,
sonra onu bağlayan (`Fixes #123`) bir pull request gönderin. İlk pull request'inizde tek
seferlik [Katkıcı Lisans Sözleşmesi](https://gist.github.com/haliskilic/7a7fdcd355f54d1db6943959d7b76823)
imzalamanız istenir (telif hakkı sizde kalır; projenin çift lisanslı kalabilmesini sağlar —
bkz. `NOTICE`).

### Geliştirme ortamı
```bash
git clone https://github.com/haliskilic/ser2netweb && cd ser2netweb
python3 -m venv .venv && . .venv/bin/activate      # Python 3.10 – 3.14
pip install -r requirements.txt cryptography        # MQTT/LDAP/OIDC için + requirements-optional.txt
python ser2netweb.py --no-bootstrap --data-dir /tmp/s2n-dev
```

### Kontroller
Yukarıdaki tablodaki komutların hepsi her pull request'te CI'da çalışır: `ruff check .`,
`mypy` (ayrıca `--platform win32` ile), `python tests/run_all.py -v` (Linux'ta `socat` ile
`--socat`), coverage, tarayıcı yerleşim testi, çevrimdışı wheel kontrolü, `pip-audit` ve
workflow denetimi (`zizmor`). `tests/offline_first_start.sh python3`, `lib/`'i olmayan temiz
bir checkout'ta çevrimdışı ilk açılışı dener (CI'da her test leg'inde, ayrıca Linux arm64,
macOS ve QEMU altında armv7).

`ci-ok` işi ancak diğer bütün CI işleri geçince geçer (zorunlu olmayan en yeni Python leg'i
hariç); birleştirme onu bekler, bu yüzden `ci.yml`'ye eklenen her iş onun `needs` listesine
de girer (`tests/test_ci_gate.py` bunu denetler). Pull request'ler arasında
`.github/workflows/scheduled.yml` bağımlılık denetimini her gün; Docker imajını ve seri veri
yolunun uzun yük testini (`tests/stress_24.py`: 24 köprü, her cihaz 20 kez çıkarılıp geri
takılır, açık dosyalar ve bellek denetlenir; CI test leg'leri küçük boyutta çalıştırır) her
hafta yeniden çalıştırır; `.github/workflows/image-refresh.yml` yayımlanan imajı her hafta tarar ve
taban imaj değiştiyse son sürümü yeniden derler (`docs/DOCKER.md`). Workflow'lardaki her action, sürümü bir yorumda olacak şekilde tam bir
commit SHA'sına sabitlenir (`uses: actions/checkout@<sha>  # v7.0.1`); ikisini de
Dependabot günceller, tag ile verilen bir referansta zizmor ve
`tests/test_release_pipeline.py` başarısız olur.

`tests/test_dead_peer.py` iptables ile loopback trafiği kestiği için
`SER2NETWEB_BLACKHOLE_TESTS=1` ile yalnızca silinebilir bir ağ ad alanında (network namespace)
ya da sanal makinede çalıştırın. Standalone binary denetimleri bir derleme ister
(`SER2NETWEB_STANDALONE_BIN=dist/ser2netweb python tests/test_standalone.py`). `tests/systemd_smoke.sh` (CI'daki `systemd unit` ve
`systemd unit (binary)` işleri) systemd unit'lerini README'deki gibi kurar ve runner'ın
systemd'sinde çalıştırır: bir `ser2netweb` kullanıcısı, `/opt/ser2netweb` ve servisi ekler; bu
yüzden yalnızca silinebilir bir sanal makinede çalıştırın. `systemd/` altındaki bir unit'te
yapılan değişiklik oradaki her unit'e uygulanır (`tests/test_systemd_units.py` hepsini denetler).
`systemd/ser2netweb-install.sh` geçici bir klasör altında hazırlanarak (`--root`,
`tests/test_install_script.py`) ve gerçekten `tests/install_smoke.sh <binary>` ile (CI'daki
`install script (binary)` işi) test edilir; o, binary'yi sisteme kurar ve `/tmp`'yi yeniden
bağlar: onu da yalnızca silinebilir bir sanal makinede çalıştırın. `.deb` / `.rpm` paketlerini
(`packaging/`; sabitlediği nfpm sürümüyle `tools/build_packages.py` derler)
`tests/test_packages.py` denetler (`SER2NETWEB_NFPM=<nfpm>` ile bir yer tutucu binary'den
derler de; o nfpm'i `python3 tools/build_packages.py fetch-nfpm DIR` indirir);
`tests/package_smoke.sh IMAGE ARCH` onları temiz Debian, Ubuntu, Fedora ve AlmaLinux
konteynerlerine, `tests/package_smoke.sh --host` da runner'ın systemd'sine kurar (CI'daki
`package on …` işleri; ikincisi yalnızca silinebilir bir sanal makinede). PyPI paketi:
`pip install --require-hashes --only-binary=:all: -r requirements-build.txt`,
`python -m build --no-isolation --outdir pypi-dist`, ardından
`python tests/pypi_smoke.py pypi-dist` (CI'daki `pypi package …` işleri); wheel'i geçici bir
venv'e kurar ve boş bir portta bir sunucu başlatır.
GitHub Pages'teki APT / DNF depolarını (`tools/package_repo.py`, `docs/PACKAGES.md`)
`tests/test_package_repo.py` denetler; onları değiştiren bir pull request `package-repo.yml`'yi
çalıştırır: depoları son sürümlerden derler, iş içinde üretilen geçici bir anahtarla imzalar
ve temiz konteynerlerde dener (`tests/package_repo_smoke.sh SITE IMAGE VERSION`, Docker);
yayımlamaz.

Sunucu başlatan bir test `tests/_server.py`'yi kullanır (`Server` ya da çıktı için
`server_log`: okunmayan bir pipe değil, bir dosya) ve sabit bir portu değil,
`free_port()` / `free_udp_port()` ya da 0 portunu dinler: geçici port aralığındaki bir port,
önceki bir testin kapanmış bağlantısında hâlâ tutuluyor olabilir. `tests/test_harness.py`,
`# fixed port: <neden>` yorumuyla açıklanmayan sabit bir dinleme portunda ve
`tests/run_all.py`'nin listelemediği bir test dosyasında başarısız olur.

### Yükseltmeleri sorunsuz tutan kurallar
- **Eski `config.json` dosyaları çalışmaya devam etmeli.** Her yeni ayarın varsayılanı
  olur; önceki bir sürümün kabul ettiği bir yapılandırmayı reddeden doğrulama kuralı
  eklenmez — eski değerler yüklenirken düzeltilir. `tests/test_upgrade_from_*.py` gerçek
  eski config'lerle açılış yapar; config modeline dokunduğunuzda bu testleri genişletin.
- REST API (`/api/v1`) ve komut satırı yalnızca büyür: alan, rota ya da seçenek kaldırılmaz.
- Log'lar, audit log ve REST API mesajları İngilizce kalır (aranır ve işlenirler).

### Çeviriler
Web arayüzü İngilizce, Türkçe çevirisiyle. Kullanıcıya görünen her metni sarın: şablonlarda
`{{ _("Save changes") }}`, Python'da `_("Port {0} is in use.").format(port)`. Sayı içeren
bir metin iki İngilizce biçimle yazılır, asla "record(s)" diye değil: şablonlarda
`{{ ngettext("%(num)s record", "%(num)s records", n) }}`, Python'da
`ngettext("Imported {0} mapping.", "Imported {0} mappings.", n).format(n)`, app.js için
`base.html`'de bir `[_("..."), _("...")]` çifti; her biçim ayrı bir girdidir (Türkçede aynı
metin iki kez). Türkçesini `ser2netweb/locale/tr.json`'a ekleyin (yeni girdiler sona);
`tests/test_i18n.py` eksik ya da kullanılmayan girdide, yer tutucusunu ya da HTML etiketini
kaybeden çeviride ve "(s)" ile yazılmış bir sayıda başarısız olur. Diller tek bir yerde,
`ser2netweb/i18n.py`'deki `LANGUAGES` içinde listelenir. Şablonlarda değerler `%(ad)s` olarak
yazılır: `{{ _("Port %(port)s", port=p) }}`. Log'lar, denetim kaydı ve REST API mesajları
çevrilmez.

### Kod düzeni
`ser2netweb.py` Python sürümünü denetleyip `ser2netweb/cli.py`'yi çalıştırır (2.9.x'e kadarki adı
`ser2net.py` de aynısını yapar); o da komut satırını okur ve olay döngüsüne sahip olan, web
sunucusuyla motoru yan yana çalıştıran `ser2netweb/runtime.py`'yi başlatır; her modülün
docstring'i ayrıntısını anlatır.

| Nerede | Ne |
|---|---|
| `ser2netweb/config.py` | yapılandırma modeli (dataclass'lar), `config.json`'u tipiyle okuma, doğrulama, atomik kayıt ve `backups/` |
| `ser2netweb/state.py` | paylaşılan durum: config, supervisor, port izleyici, `all.log` / denetim kaydı; `ser2netweb/reload.py` (SIGHUP), `ser2netweb/tlsreload.py`, `ser2netweb/storage.py` (boş alan, saklama süresi), `ser2netweb/watchdog.py`, `ser2netweb/datalock.py` |
| `ser2netweb/engine/supervisor.py` | eşleme başına bir runner, config değişikliklerini uygular; `bridge.py` (seri ↔ TCP/UDP, telnet / RFC 2217), `serial_link.py` / `serial_io.py` / `serial_compat.py` (seri port), `modbus_gateway.py` + `modbus.py` (Modbus RTU ↔ TCP, okuma), `mqtt_pub.py`, `sparkplug_node.py` + `sparkplug.py`, `tls.py`, `access.py` / `netguard.py` (kim bağlanabilir, sınırlar), `portlist.py` / `portdiag.py` (portlar ve sorunları), `cluster.py`, `netinfo.py`, `bindhint.py`, `sockopts.py` |
| `ser2netweb/web/server.py` | Starlette uygulaması ve middleware'i; `auth.py` (oturum, CSRF, giriş sınırları), `ldap_auth.py`, `oidc_auth.py`, `totp.py`, `proxy.py` (reverse proxy), `httpguard.py`, `api.py` (REST API `/api/v1`), `certinfo.py`, `selfsigned.py` |
| `ser2netweb/web/routes/` | arayüzün her bölümü için bir modül (`login`, `totp`, `settings`, `mappings`, `console`, `cluster`, `system`); ortak yardımcılar `common`, `forms` ve `ops` içinde |
| `ser2netweb/web/templates/`, `ser2netweb/web/static/` | Jinja şablonları; `app.js`, `points.js`, `app.css` ve gömülü htmx / xterm.js |
| `ser2netweb/notify.py` | webhook / e-posta bildirimleri |
| `ser2netweb/importer.py`, `ser2netweb/ser2net_import.py` | eşleme içe aktarma ve klasik ser2net okuyucusu |
| `ser2netweb/accounts.py` | `--set-password`, `--sign-out-all`, ilk admin |
| `ser2netweb/winservice.py`, `ser2netweb/standalone.py`, `ser2netweb/sdnotify.py` | Windows servisi, PyInstaller binary'leri, systemd notify |
| `ser2netweb/i18n.py`, `ser2netweb/locale/tr.json` | çeviriler |
| `ser2netweb/clock.py`, `ser2netweb/explain.py`, `ser2netweb/logtext.py`, `ser2netweb/logsetup.py`, `ser2netweb/textfold.py`, `ser2netweb/optional.py`, `ser2netweb/signins.py`, `ser2netweb/healthcheck.py`, `ser2netweb/console.py` | zamanlar, hata metinleri, log satırları, metin karşılaştırma, isteğe bağlı paketler, son girişler, `--healthcheck`, ilk açılıştaki konsol seçicisi |
| `tests/` | her özellik için bir dosya, `tests/run_all.py` toplar |

Her web route'unun ve WebSocket'in bir erişim sınıfı vardır (kim kullanabilir, CSRF nasıl
denetlenir): `tests/test_route_access.py`. Yeni bir route orada sınıflandırılana kadar bu
test başarısız olur.

### Bağımlılıklar
Çalışma zamanı sürümleri `requirements.txt`'te (Python 3.10 hattı işaretlerle korunur),
binary'lere ve Docker imajına giren isteğe bağlı paketler `requirements-optional.txt`'te.
`requirements.txt`'i değiştirdikten sonra `python tools/refresh_wheels.py` çalıştırın;
`vendor/wheels/` (çevrimdışı kurulum seti) eşleşsin. Dependabot'un PR'larında bunu
`.github/workflows/refresh-pins.yml` yapar: yenilenen wheel'leri dala push eder ve yeni
commit'te CI'ı başlatır (başka bir dalda elle çalıştırın: Actions → Refresh pinned files →
Run workflow). Wheel'ler eşleşene kadar yalnızca "offline wheels match requirements.txt"
işi başarısız olur (test işleri bu kontrolü ona bırakır) ve yenilenmiş dosyaları bir patch
olarak ekler (`refreshed-pins` artifact'i). Docker imajı ve standalone binary'ler bunun yerine hash kilitli dosyalardan
kurulur (`pip install --require-hashes`): `requirements-lock.txt` (Python 3.12 için aynı
pin'ler, her sürümün dosyalarının sha256'larıyla) ve `requirements-build.txt` (PyInstaller ile
PyPI paketi için build + setuptools ve bağımlılıkları). Dependabot bunları `requirements.txt`
ile birlikte günceller; elle yapılan bir değişiklikten sonra `python tools/refresh_locks.py`
yeniden üretir (`tests/test_build_lock.py` eşleştiklerini denetler). PyPI paketi
(`pyproject.toml`) ser2netweb'in import ettiği paketleri her pin'den bir sonraki major
sürümün altına kadar (0.x için bir sonraki minor) aralıklar olarak listeler: aralığının
dışına çıkan bir pin aynı işte `tests/test_pyproject.py`'yi düşürür, aralığı aynı PR'da
genişletin. `ser2netweb/web/static/` içindeki tarayıcı
kütüphaneleri (htmx, xterm.js) `vendor/npm/package.json`'da sabitlenir; oradaki sürümü
değiştirip `python tools/refresh_assets.py` çalıştırın: dosyaları npm sürümünden kopyalar ve
sha256'larını kaydeder (Dependabot minor güncellemeleri önerir, `refresh-pins.yml` onları
kopyalar; major bir güncellemeden önce konsolu elle deneyin). GitHub'ın bağımlılık grafiği (dolayısıyla Dependabot uyarıları) `requirements.txt`'i
yalnızca en yeni Python için okur, yalnızca Python 3.10'un kurduğu pin'ler eksik kalır;
`.github/workflows/dependency-graph.yml` (`tools/dependency_snapshot.py`) bunun yerine dosyanın
tamamını `main`'den grafiğe gönderir, böylece onlar için de uyarı çıkar.

### Python sürümleri
CI, Python 3.10–3.14'ü Linux ve Windows'ta test eder; en yeni sürüm (3.15) de çalışır, ama
wheel'ları oturana kadar birleştirmeyi engellemez. Python 3.10'un ömrü doldu, ama ser2netweb'in
kullanıldığı dağıtımlar onu getirdiği sürece (Ubuntu 22.04) desteklenir: onu destekleyen
son bağımlılık sürümlerinde kalır (`python_version < "3.11"` pin'leri). Bu donmuş
pin'lerden birinde, 3.10'u destekleyen hiçbir sürümün düzeltmediği bilinen bir güvenlik
açığı çıkınca (py3.10 bağımlılık denetimi bunu bildirir) bırakılır; bu, bir minor sürüm
önceden sürüm notlarında duyurulur.

### Üçüncü taraf kodu
Çalışma zamanı bağımlılıkları tam sürümle sabitlenir. Birindeki bir hata yayımlanmış bir
sürümde düzeltilmediyse (pyserial ve pyserial-asyncio-fast nadiren sürüm çıkarır), düzeltme
bir fork'a ya da tüm süreci değiştiren bir yamaya değil, ser2netweb'e girer:
- Kütüphane başına tek bir modül (pyserial ve pyserial-asyncio-fast için
  `ser2netweb/engine/serial_compat.py`): ser2netweb'in kendisinin kullandığı ve olabildiğince az şeyi
  değiştiren bir alt sınıf. Docstring'i taklit ettiği upstream issue'yu, PR'ı ya da commit'i
  belirtir; bir rapor zaten varsa yenisini açmak yerine oraya yorum yazılır.
- Dayandığı upstream üyeler var olduğu sürece uygulanır (özellik tespiti); sürüm
  kontrolünün arkasına konmaz, çünkü bu, hatayı hâlâ taşıyan daha yeni bir sürümde hatayı
  geri getirir. Kurulu sürüm denetlenen sürüm değilse açılış log'u bunu bir kez yazar.
- Donanım gerektirmeyen bir testi olur, geçerli olduğu yerde Windows'ta da.
  `tests/test_serial_compat.py` her override'ın yerini aldığı upstream kodu kaynağının
  hash'iyle sabitler: o kodu değiştiren bir sürüm yükseltmesi, override gözden geçirilip hash
  güncellenene kadar başarısız olur; upstream hatasının hâlâ durduğunu denetleyen bir test de
  override'ın ne zaman kaldırılabileceğini söyler.
- Düzeltmeyi içeren bir sürüm çıkınca kaldırılır (ardından sabitlenen sürüm yükseltilir ve
  `vendor/wheels/` yenilenir).
- BSD lisanslı kod (pyserial, pyserial-asyncio-fast) alt sınıflanabilir ya da lisans notuyla
  kopyalanabilir; LGPL kod (ldap3) yalnızca açık API'si üzerinden kullanılır.

Dependabot pyserial, pyserial-asyncio-fast, ldap3 ve pyasn1 için ayrı birer pull request
açar (diğerleri gruplu gelir): değişiklik günlüklerini okuyun; ldap3 / pyasn1 için
`requirements-optional.txt`'i kuran CI ayaklarına bakın.

### Commit ve pull request
Kısa, emir kipinde ve önekli başlıklar: `feat:`, `fix:`, `test:`, `docs:`, `ci:`,
`deps:`, `refactor:` (isteğe bağlı bir kapsamla, ör. `fix(modbus): …`). Bir pull request tek
konu içersin ve kullanıcılar için neyin değiştiğini anlatsın. Arayüzü değiştiren bir pull
request bunu gösterir (önce / sonra ekran görüntüleri) ve `tr.json`'u eksiksiz tutar;
belgelenmiş bir davranışı değiştiren her iki README'yi ve dokunduğu `docs/` rehberini
günceller (`tests/test_docs.py` komut satırı seçeneklerini ve bağlantıları denetler). Sürümleri proje sahibi çıkarır (aşağıda).

### Sürümler
Sürüm `ser2netweb/__init__.py`'ye, tarihli bir `## [X.Y.Z] — YYYY-MM-DD` bölümü `CHANGELOG.md`'ye
yazılır; bu birleştirilince merge commit'ine atılan açıklamalı bir `vX.Y.Z` tag'i
(ön sürüm için `vX.Y.Z-rc.N`) `.github/workflows/release.yml`'yi başlatır:
1. tag denetlenir: biçimi, `"v"` + `__version__`'a eşitliği, CHANGELOG bölümü (bu
   üçünü yerelde `python tools/release_guard.py tag vX.Y.Z` denetler) ve commit'in
   `main` üzerinde olması;
2. tag'lenen commit'te CI'ın tamamı çalışır (`ci.yml`, birleştirmede `ci-ok`'un beklediği
   kontroller); yayımlanan, onun standalone derlemesi, Linux binary'lerinden yaptığı
   `.deb` / `.rpm` paketleri ve PyPI sdist'i ile wheel'idir;
3. imaj amd64, arm64 ve arm/v7 için derlenir, GHCR'a yalnızca digest ile (tag'siz)
   `ghcr.io/haliskilic/ser2netweb` ve 3.x serisi için 2.9.x'e kadarki adıyla
   `ghcr.io/haliskilic/ser2net` olarak gönderilir ve bu digest her mimaride smoke test'ten
   geçer (amd64 ve arm64 emülasyonsuz, arm/v7 QEMU altında);
4. ancak bundan sonra dosyalar attest edilir ve GitHub release'i onlarla oluşturulur
   (`gh` ile; release ya da attestation yazabilen işlerde yalnızca GitHub'ın kendi
   action'ları çalışır; dosyalar `tools/release_guard.py assets`'in listelediği
   `ser2netweb-*` dosyaları ve bu sürümün dört paketidir), test edilen imaj da iki adı
   altında `X.Y.Z`, `X.Y` ve `latest` tag'lerini alır.
   Ön sürüm yalnızca kendi imaj tag'ini alır, ön sürüm olarak işaretlenir ve hiçbir zaman
   "Latest" olmaz;
5. en son `publish to PyPI` işi CI'ın derlediği sdist ve wheel'i yükler (`pypi` ortamı,
   trusted publishing: token yok). Ön sürüm de ön sürüm olarak gider (`X.Y.ZrcN`; pip onu
   yalnızca `--pre` ile kurar). Bu işi bekleyen bir şey yoktur: başarısız olursa da release
   ve imaj yayımlanmış olur; neden giderilince *Re-run failed jobs* yayımlar.
6. ön sürüm değilse `start the release channels` işi `package-repo.yml`'yi `main` üzerinde
   başlatır: sürümün paketleri konteyner testlerinden geçince GitHub Pages'teki imzalı
   APT / DNF depolarına eklenir (en yeni üç sürüm, `docs/PACKAGES.md`). `github-pages`
   ortamı `main`'e ve `v*` tag'lerine izin vermelidir; `PACKAGES_GPG_PRIVATE_KEY` sırrı
   yoksa hiçbir şey yayımlanmaz.

**PyPI'ye giden ilk sürümden önce bir kez** (bakımcı, pypi.org'da): *Your account →
Publishing → Add a new pending publisher*, GitHub; PyPI project name `ser2netweb`, owner
`haliskilic`, repository name `ser2netweb`, workflow name `release.yml`, environment name
`pypi`. İlk yükleme projeyi oluşturur. Depodaki *Settings → Environments* altında `pypi`
(yoksa ilk çalıştırma oluşturur) `release` ile aynı korumayı alabilir (deployment branches
and tags: `v*`; istenirse required reviewers). Publisher tanımlanana kadar iş, her şey
yayımlandıktan sonra PyPI'nin `invalid-publisher` hatasıyla başarısız olur.

Aynı anda tek release çalışır. Geçici bir nedenle başarısız olan iş yeniden çalıştırılır
(*Re-run failed jobs* derlenmiş dosyaları ve gönderilmiş imaj digest'ini korur); kod
değişikliği gereken bir release tag taşınarak değil, yeni bir yama sürümüyle düzeltilir.
**Actions → Release → Run workflow** aynı akışı herhangi bir dalda
deneme olarak çalıştırır: hiçbir şeyi attest etmez, yayımlamaz, tag'lemez ya da PyPI'ye yüklemez (GHCR'da
tag'siz bir imaj sürümü bırakır).

Sürüm commit'i `ROADMAP.md`'nin sürüm satırlarını (başlığı ve Shipped girdisini) ve iki
README'deki imaj tag örneklerini (`:X.Y`, `:X.Y.Z`) de günceller; arayüz değiştiyse ekran
görüntüleri yeniden alınır (`tools/_capture_screenshots.py`, iki dilde).

Bir sürüm tag'lendikten sonra yazdıkları dondurulur: getirdiği ayarlar
`tests/make_config_fixture.py` içindeki `FEATURES`'a eklenir,
`python tests/make_config_fixture.py vX.Y.Z` çalıştırılır ve
`tests/fixtures/config-X.Y.Z.json`, `tests/test_upgrade_from_2_8.py` gibi bir yükseltme
testiyle (data klasörü düzeniyle birlikte; `tests/docker_smoke.sh` 2.7.1'inkiyle açılır)
commit'lenir; karışık sürümlü küme düzenlemeleri için de
`python tests/make_form_fixtures.py vX.Y.Z` çalıştırılır (`tests/fixtures/forms-X.Y.Z.json`,
`tests/test_mixed_version_edit.py`). Bunlar bir sonraki sürüm config modelini ya da eşleme
formunu değiştirmeden önce yapılır. Eşleme formuna eklenen bir alan, onu okuyan sürümün
`FORM_BLOCKS_SINCE` bloğuna girer (`ser2netweb/web/routes/forms.py`).
