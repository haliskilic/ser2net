<!--
  pyser2net — README
  Türkçe (varsayılan) önce, İngilizce aşağıda. / Turkish first, English below.
-->

# pyser2net

**Seri portları (COM / ttyUSB / ttyACM / ttyS) ağ üzerinden erişilebilir kılan,
web arayüzünden yönetilen, çapraz-platform (Windows + Linux) saf-Python köprü.**

> C dünyasındaki `ser2net`'in modern, web yönetimli karşılığı. Tek ekrandan onlarca
> seri portu IP:port'a eşleyin; raw / telnet / RFC2217; çift yönlü; düşük gecikme.

---

## 📌 Amaç

Endüstriyel cihazlar, PLC'ler, ölçüm aletleri, GPS/modem, mikrodenetleyiciler ve
konsol portları çoğunlukla **seri (RS-232/485/USB-serial)** haberleşir. Bu cihazlara
ağdaki herhangi bir bilgisayardan erişmek için her seri portu bir **TCP/UDP uç
noktasına** köprülemek gerekir. `pyser2net` bunu yapar ve tüm yönetimi **parola
korumalı bir web arayüzünden** sunar — komut satırı veya elle config dosyası
düzenlemeye gerek yok.

**Tipik kullanım senaryoları:**
- Bir sunucuya bağlı 10+ USB-serial cihazı ağdaki uygulamalara açmak
- SCADA / Modbus-RTU cihazlarını uzak istemcilere ulaştırmak (raw veya RFC2217)
- Cihaz konsollarına (switch, router, gömülü kart) ağdan erişim
- Uzak baud/parity değişimi gereken cihazlar için RFC2217
- İki seri portu birbirine köprüleme (serial↔serial)

---

## ✨ Özellikler

- **Taşıma modları:** TCP **sunucu** (dinleme), TCP **istemci** (dışarı bağlanma),
  **UDP**, ve **serial↔serial** köprüleme. TCP köprüleri için isteğe bağlı **TLS**.
- **Protokoller:** `raw`, `telnet` (8-bit temiz), `rfc2217` (uzaktan canlı
  baud/parity/databit/stopbit/akış kontrolü değişimi).
- **Tam seri yapılandırma:** baud (custom dahil), data bit, parity, stop bit, akış
  kontrolü (none/RTS-CTS/XON-XOFF/DSR-DTR), açılışta RTS/DTR, exclusive open, RS-485.
- **Canlı port listesi:** ayrıcalık gerektirmeyen polling + isteğe bağlı olay-tabanlı
  hotplug (Linux pyudev / Windows WM_DEVICECHANGE), yoksa polling'e düşer.
- **IP seçici:** makineye atanmış IP'ler (localhost / LAN / 0.0.0.0) veya custom.
- **Onlarca eşleme:** tek ekrandan ekle/düzenle/sil/başlat/durdur, canlı durum.
- **Erişim kontrolü (eşleme başına):** izinli IP/CIDR listesi, **yüksek-öncelikli**
  istemci IP'leri (doluyken eski istemciyi atar), max bağlantı, eski-kullanıcıyı-at,
  salt-okunur, idle timeout, banner, open/close string, `closeon`.
- **Gözlemlenebilirlik:** per-mapping trafik trace (hex/timestamp), Prometheus
  `/metrics`, config-değişiklik audit log, canlı log görüntüleyici, ve **tarayıcı-içi
  seri konsol** (xterm.js, WebSocket — trafiği izle veya cihaza yaz).
- **Güvenlik:** parola (ilk erişimde belirlenir, scrypt), CSRF, imzalı-çerez oturum,
  login oran sınırı, sıkı güvenlik başlıkları, parola değişince oturum iptali.
- **Tamamen offline:** tüm bağımlılıklar wheel olarak birlikte gelir; internet gerekmez.

---

## 🧰 Gereklilikler

- **Python 3.11+** (sistemde kurulu). Başka hiçbir şey gerekmez — bağımlılıklar
  `vendor/wheels/` içinde gelir ve ilk çalıştırmada `./lib`'e kurulur (offline).
- Linux'ta seri portu **açmak** için kullanıcının `dialout` grubunda olması gerekir
  (port **listelemek** için ayrıcalık gerekmez):
  ```bash
  sudo usermod -aG dialout "$USER"   # ardından oturumu kapatıp aç
  ```
- Windows'ta COM portları için ek yetki gerekmez.
- (Opsiyonel) TLS self-signed üretimi için `openssl`; olay-tabanlı hotplug için
  `pyudev` (Linux) / `pywin32` (Windows) — yoksa polling kullanılır.

---

## 🚀 Kurulum & Çalıştırma

```bash
# Linux / macOS
python3 ser2net.py            # veya: ./start.sh

# Windows
start.bat
```

İlk çalıştırmada:
1. **Konsolda** arayüzün hangi IP'den erişileceği sorulur (makine IP'leri veya custom)
   ve port (varsayılan 8080). Başsız/servis ortamında güvenli varsayılan **127.0.0.1**.
2. Tarayıcıda açılan adrese gidin; **ilk ekranda admin parolasını belirleyin**.
3. Panodan **+ Eşleme ekle** ile COM/tty seçip IP:port'a eşleyin.

Bind IP'yi sonradan değiştirmek:
```bash
python3 ser2net.py --reconfigure
```

### Offline kurulum (internetsiz makine)
Bağımlılıklar `vendor/wheels/` içinde bulunur; `ser2net.py` ilk açılışta bunları
`./lib`'e kurar (`pip install --no-index`). İnternet gerektirmez. Farklı Python
sürümü/işletim sistemi için ek wheel gerekirse:
```bash
python3 -m pip download -r requirements.txt -d vendor/wheels \
  --platform win_amd64 --python-version 312 --only-binary=:all:
```

---

## 🖥️ Kullanım

- **Eşleme ekle:** Ad, tür (Serial↔Network / Serial↔Serial), seri port + parametreler,
  ağ modu (server/client/udp), protokol, bind/remote IP:port, erişim kuralları.
- **Başlat/Durdur/Yeniden başlat/Kopyala/Sil:** her satırda.
- **Log:** eşlemenin geçmiş logu (en yeni üstte, restart sonrası kalıcı).
- **Monitor:** tarayıcıda canlı seri terminal (xterm.js); ağ eşlemelerinde cihaza yazılabilir.
- **Ayarlar:** parola değiştir, admin TLS (yol ver veya self-signed üret), mapping
  **yedek al/yükle** (JSON), durum.
- **/metrics:** Prometheus formatında metrikler (kimlik doğrulamalı).

---

## 🔒 Güvenlik

Arayüz **her zaman parola korumalı**. Varsayılan bind **127.0.0.1**; ağa açmak için
açılışta IP seçimi gerekir ve TLS'siz ağ bind'inde uyarı verilir. LAN'a açılan
kurulumlarda TLS (`admin_ui.tls_*`) ve eşleme bazlı `allowed_client_ips` önerilir.
Raw TCP düz-metindir — güvensiz ağlarda dikkat. Allowed/priority listesinde tek başına
`0.0.0.0`/`::` "herkes" demektir.

---

## ⚙️ Servis olarak çalıştırma

- **Linux (systemd):** `systemd/ser2net.service` — ayrı, yetkisiz bir kullanıcı,
  `SupplementaryGroups=dialout`, `Restart=on-failure`, sertleştirme direktifleri.
  **root ile çalıştırmayın.**
- **Windows:** [Shawl](https://github.com/mtkennerly/shawl) ile servis sarmalama.

---

## 🗂️ Yapılandırma & durum dosyaları

Tüm durum **veri dizininde** (varsayılan `data/`):
- `config.json` — admin IP, parola hash'i, tüm eşlemeler (atomik yazım, 0600).
- `all.log` — global etkinlik/audit; `audit.log` — config değişiklikleri.
- `logs/<id>.log` — eşleme başına geçmiş (saatlik bakım: >15 gün veya >100MB kırpılır).
- `tls/` — self-signed üretilirse sertifika/anahtar.

`config.json` + `all.log` silinince sistem **sıfırdan** başlar (orphan loglar otomatik temizlenir).

---

## 🧪 Test

socat (Linux) ile sanal seri portlar kullanılır; donanım gerekmez:
```bash
python3 tests/test_bridge_raw.py        # raw çift yönlü
python3 tests/test_rfc2217.py           # RFC2217 + canlı baud
python3 tests/test_v2_transports.py     # TCP-client / UDP / serial-bridge
python3 tests/test_v2_console.py        # WebSocket konsol
python3 tests/stress_24.py 24 512 200   # 24 eşzamanlı köprü stres
```

---

## 📜 Lisans

**Ticari/özel lisans** — bkz. [`LICENSE`](LICENSE). Yazara ait, tüm hakları saklıdır;
geçerli bir ticari lisans olmadan kullanım/dağıtım/satış yapılamaz. Birlikte gelen
üçüncü-taraf bileşenler kendi (izin-verici) lisanslarını korur — bkz.
[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md). İletişim: haliskilic90@gmail.com

Yol haritası: [`ROADMAP.md`](ROADMAP.md).

---
---

# pyser2net (English)

**A cross-platform (Windows + Linux), pure-Python serial-to-network bridge with a
web management UI** — a modern, web-managed take on the classic `ser2net`. Map dozens
of serial ports (COM / ttyUSB / ttyACM / ttyS) to TCP/UDP endpoints, bidirectionally,
with low latency.

## Purpose
Industrial devices, PLCs, instruments, GPS/modems, microcontrollers and console ports
usually speak **serial** (RS-232/485/USB-serial). pyser2net bridges each serial port to
a **TCP/UDP endpoint** so any computer on the network can reach it, and manages
everything from a **password-protected web UI** — no CLI or hand-edited config files.

## Features
- **Transports:** TCP **server**, TCP **client** (connect-out), **UDP**, and
  **serial↔serial** bridging; optional per-mapping **TLS** for TCP bridges.
- **Protocols:** `raw`, `telnet` (8-bit clean), `rfc2217` (remote live baud/parity/etc.).
- **Full serial config:** baud (incl. custom), data/stop bits, parity, flow control,
  RTS/DTR on open, exclusive open, RS-485.
- **Live port list:** privilege-free polling + optional event hotplug (pyudev /
  WM_DEVICECHANGE) with polling fallback. **IP picker** from the machine's addresses or custom.
- **Dozens of mappings:** add/edit/delete/start/stop with live status, from one screen.
- **Per-mapping access control:** allowed + **high-priority** client IPs/CIDRs (a
  priority client evicts the oldest when full), max connections, kick-old, read-only,
  idle timeout, banner, open/close strings, `closeon`.
- **Observability:** traffic trace (hex/timestamp), Prometheus `/metrics`, config audit
  log, live log viewer, and an in-browser **serial console** (xterm.js over WebSocket).
- **Security:** password (set on first access, scrypt), CSRF, signed-cookie sessions,
  login rate-limiting, strict security headers, session revocation on password change.
- **Fully offline:** all dependencies bundled as wheels; no internet required.

## Requirements
- **Python 3.11+** installed. Dependencies ship in `vendor/wheels/` and install to
  `./lib` on first run (offline).
- Linux: membership in the `dialout` group to **open** serial ports
  (`sudo usermod -aG dialout "$USER"`, then re-login). Windows: no extra privileges.
- Optional: `openssl` (self-signed TLS), `pyudev`/`pywin32` (event hotplug).

## Install & run
```bash
python3 ser2net.py        # Linux/macOS (or ./start.sh)
start.bat                 # Windows
```
First run: the **console** asks which local IP the admin UI binds to (defaults to
loopback `127.0.0.1` when non-interactive); then open the URL and **set an admin
password**; then add mappings. Re-pick the bind IP with `python3 ser2net.py --reconfigure`.

### Offline install
Bundled wheels in `vendor/wheels/` install to `./lib` via `pip --no-index` on first
launch. Add wheels for other Python versions/OSes with `pip download -r requirements.txt`.

## Usage
Add mappings (transport mode, serial params, network/remote, access rules); per-row
Start/Stop/Restart/Copy/Delete; **Log** (per-mapping history), **Monitor** (live xterm
console), **Settings** (password, admin TLS, mappings backup/restore), `/metrics`.

## Security
Always password-protected; binds to `127.0.0.1` by default. Exposing to the network
requires an explicit IP choice and warns when bound without TLS. For LAN deployments use
TLS and `allowed_client_ips`. Raw TCP is plaintext. A bare `0.0.0.0`/`::` in an
allow/priority list means "any client".

## Run as a service
Linux: `systemd/ser2net.service` (dedicated unprivileged user + `dialout`, hardened,
never root). Windows: wrap with Shawl.

## State files
In the data dir (`data/`): `config.json` (admin IP, password hash, mappings; atomic,
0600), `all.log` / `audit.log`, `logs/<id>.log` (per-mapping, auto-trimmed >15 days /
>100 MB), `tls/`. Deleting `config.json` + `all.log` fully resets.

## Testing
socat-based virtual serial tests (no hardware) under `tests/`; `stress_24.py` for
24 concurrent bridges.

## License
**Commercial / proprietary** — see [`LICENSE`](LICENSE). All rights reserved; no use,
distribution or resale without a valid commercial license. Bundled third-party
components keep their own (permissive) licenses — see
[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md). Contact: haliskilic90@gmail.com
