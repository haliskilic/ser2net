<!-- ser2net — README (Türkçe / varsayılan). English: README.en.md -->

**🌐 [Türkçe](README.md) · [English](README.en.md)**

# ser2net

**Seri portları (COM / ttyUSB / ttyACM / ttyS) ağ üzerinden erişilebilir kılan,
web arayüzünden yönetilen, çapraz-platform (Windows + Linux) saf-Python köprü.**

> C dünyasındaki `ser2net`'in modern, web yönetimli karşılığı. Tek ekrandan onlarca
> seri portu IP:port'a eşleyin; raw / telnet / RFC2217; çift yönlü; düşük gecikme.

---

## 📸 Ekran Görüntüleri

**Pano — onlarca eşleme, canlı durum, algılanan portlar:**

![Pano](docs/screenshots/02-dashboard.png)

| Eşleme ekleme (tüm seri/ağ seçenekleri) | Tarayıcı-içi seri konsol (xterm.js) |
|---|---|
| ![Eşleme formu](docs/screenshots/03-add-mapping.png) | ![Konsol](docs/screenshots/05-console.png) |

| Ayarlar (parola · TLS · yedek) | Giriş |
|---|---|
| ![Ayarlar](docs/screenshots/04-settings.png) | ![Giriş](docs/screenshots/01-login.png) |

---

## 📌 Amaç

Endüstriyel cihazlar, PLC'ler, ölçüm aletleri, GPS/modem, mikrodenetleyiciler ve
konsol portları çoğunlukla **seri (RS-232/485/USB-serial)** haberleşir. Bu cihazlara
ağdaki herhangi bir bilgisayardan erişmek için her seri portu bir **TCP/UDP uç
noktasına** köprülemek gerekir. `ser2net` bunu yapar ve tüm yönetimi **parola
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

- **Python 3.10+** (sistemde kurulu). Başka hiçbir şey gerekmez — bağımlılıklar
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
- `config.json` — admin IP, parola hash'i, tüm eşlemeler (atomik yazım; sahibe özel
  izinler: POSIX'te 0600, Windows'ta `icacls` ile sahip/SYSTEM/Administrators).
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

Yol haritası: [`ROADMAP.md`](ROADMAP.md) · English: [`README.en.md`](README.en.md)
