# ser2net — Kapsamlı İnceleme Raporu

> Multi-agent kod incelemesi + adversaryal doğrulama + piyasa araştırması.
> Tarih: 2026-06-09 · İncelenen commit: `9ba09bd` (main).
> Yöntem: 10 boyutta paralel kod-tarama ajanı + 4 piyasa-araştırma ajanı.
> Tüm bulgular kaynak kodu (`file:line`) okunarak elle doğrulanmıştır.

---

## 1. Yönetici Özeti

ser2net olgun, iyi tasarlanmış bir proje: temiz katmanlı mimari (engine / web / config / state),
asyncio üzerine kurulu tek-event-loop modeli, atomik config yazımı, per-client backpressure
izolasyonu, scrypt+CSRF+signed-cookie ile gerçek bir güvenlik modeli ve çok platformlu (Windows +
Linux) hedef. Kod kalitesi yüksek; docstring'ler ve yorumlar tasarım kararlarını açıklıyor.

Buna rağmen **production'a çıkmadan önce kapatılması gereken birkaç gerçek hata ve güvenlik açığı
var.** En kritikleri:

| # | Önem | Başlık | Dosya |
|---|------|--------|-------|
| H1 | 🔴 Yüksek | UDP modu `allowed_client_ips`'i yok sayıyor; tek datagram ile akış kaçırma | `app/engine/bridge.py:738` |
| H2 | 🔴 Yüksek | `read_only` RFC2217 kontrol komutlarıyla baypas ediliyor (baud/DTR/BREAK) | `app/engine/bridge.py:247` + `protocols/rfc2217.py:85` |
| H3 | 🔴 Yüksek | `stop()` istemcileri kick etmeden önce `wait_closed()` bekliyor → Python 3.12+ kilitlenme | `app/engine/bridge.py:386` |
| H4 | 🔴 Yüksek | `start()` başarısız olursa serial supervisor task'ı sızıyor; cihaz sonsuza dek tutulu kalıyor | `app/engine/bridge.py:365` + `supervisor.py:74` |
| H5 | 🟠 Orta-Yüksek | Windows'ta `config.json` (secret_key + parola hash) dosya izniyle korunmuyor | `app/config.py:397` |
| H6 | 🟠 Orta-Yüksek | HTMX 400 yanıtını swap etmiyor → form doğrulama hataları kullanıcıya hiç görünmüyor | `app/web/routes.py:404` |

**Stratejik uyarı:** Ürün adı "ser2net", Corey Minyard'ın GPL lisanslı, 25 yıllık, hâlâ aktif (son
sürüm Şubat 2026), her Linux dağıtımında paketli açık kaynak projesiyle **birebir aynı.** Bu, SEO/arama
sonuçlarında rekabet, alıcı kafa karışıklığı ("neden ücretsiz olana para vereyim?") ve marka/itibar riski
yaratıyor. Ticari bir ürün için **farklı bir isim** ciddi olarak değerlendirilmeli (bkz. §6).

---

## 2. Hatalar ve Güvenlik Bulguları

### 🔴 H1 — UDP modu erişim listesini (ACL) tamamen yok sayıyor — `app/engine/bridge.py:738`
`_UdpBridge.datagram_received` hiçbir yerde `_client_allowed()` çağırmıyor (ACL yalnızca TCP yolu
`_on_client`'ta uygulanıyor, satır 564). Ayrıca tek izlenen peer, **en son datagram'ı gönderen adrese**
yeniden bağlanıyor. Tek bir (spoof edilebilir) UDP paketi gönderen herhangi bir host "peer" oluyor ve
o andan itibaren tüm serial→net trafiğini alıyor; `read_only` değilse seri porta da yazabiliyor.
Peer yeniden bağlama, `read_only` kontrolünden **önce** yapıldığı için akış kaçırma read-only
haritalamalarda bile çalışıyor.
- **Etki:** `allowed_client_ips` UDP'de sessizce göz ardı edilir → operatöre **sahte güvenlik hissi**.
- **Çözüm:** `datagram_received` başında `if not self.runner._client_allowed(addr[0]): return`. Peer
  adresini ilk gönderene (veya yapılandırılmış `remote_host`'a) kilitle, her farklı gönderende yeniden
  bağlama. `read_only` kontrolünü peer-rebind'den **önce** yap.

### 🔴 H2 — `read_only` RFC2217 kontrol komutlarıyla baypas ediliyor — `bridge.py:247` + `rfc2217.py:85`
`_pump_net_to_serial`, `read_only` ayarlıysa yalnızca **veri** payload'unu bastırıyor; ama bytes'ları
önce `self._session.from_network(data)`'dan geçiriyor. RFC2217 oturumunda bu, pyserial'in
`PortManager.filter()`'ını çağırır ve `SET-BAUDRATE / DATASIZE / PARITY / STOPSIZE / SET-CONTROL`
(RTS/DTR/BREAK/purge) komutlarını **doğrudan canlı `serial.Serial` nesnesine** uygular.
- **Etki:** "read-only" bir RFC2217 istemcisi baud/parity değiştirebilir, DTR/RTS'yi toggle'layabilir
  (bazı cihazları resetler), BREAK gönderebilir (bazı seri konsolları durdurur), buffer purge edebilir.
  Reklamı yapılan bir güvenlik kontrolünün tam baypası.
- **Çözüm:** `read_only` haritalamalarda RFC2217 oturumunu, baudrate/parity/rts/dtr/break setattr'ını
  reddeden bir proxy ile kur (`_ModemSafeSerial` gibi), **veya** `rfc2217 + read_only` kombinasyonunu
  config doğrulamada reddet — tıpkı `rfc2217 + max_connections>1`'in reddedildiği gibi (`config.py:193`).

### 🔴 H3 — `stop()` Python 3.12+ üzerinde kilitlenebilir — `app/engine/bridge.py:386`
`stop()` sırası: `self._server.close()` → **`await self._server.wait_closed()`** → … → istemcileri
`c.kick()`. Python 3.12'de `asyncio.Server.wait_closed()` davranışı değişti: artık sunucunun açtığı
**tüm aktif bağlantılar kapanana kadar bekleyebiliyor.** İstemciler `wait_closed()`'dan *sonra*
kick edildiği için, aktif istemci varken bir haritalamayı durdurma/yeniden başlatma deadlock'a girebilir.
- **Etki:** Python 3.12/3.13 (README'nin desteklediği 3.10+ aralığı) üzerinde aktif istemcili bir
  mapping'i durdurmak/restart etmek/silmek event loop'u süresiz bloklayabilir.
- **Çözüm:** Önce istemcileri kick et, sonra `wait_closed()` çağır. Sıralamayı düzelt:
  `server.close()` → istemcileri kick et → `await server.wait_closed()`. Ek olarak `wait_closed()`'a
  `asyncio.wait_for(..., timeout)` koy. (3.10/3.11'de fark etmez, ama 3.12+ için kritik.)

### 🔴 H4 — `start()` başarısızlığında serial task sızıntısı; cihaz sonsuza dek tutulu — `bridge.py:365` + `supervisor.py:74`
`MappingRunner.start()` önce `_serial_task`'ı oluşturuyor (satır 365), **sonra** `asyncio.start_server`'ı
await ediyor (satır 369). `start_server` hata verirse (örn. "address in use"), istisna
`Supervisor.apply_mapping`'e gider; orada yakalanıp `state="error"` yapılıyor ama **`runner.stop()`
asla çağrılmıyor** (supervisor.py:77-81). Sonuç: yörüngede kalan `_serial_supervisor` görevi seri portu
açık tutmaya/yeniden bağlanmaya devam eder.
- **Etki:** Port çakışması yaşayan bir mapping, seri cihazı kalıcı olarak kilitler; başka hiçbir mapping
  o portu açamaz. "Dozens of mappings" senaryosunda sinsi bir kaynak sızıntısı.
- **Çözüm:** `start()` içine `try/except` ekleyip hata anında oluşturulmuş task'ları iptal et; veya
  `apply_mapping`'in except dalında `await runner.stop()` çağır.

### 🟠 H5 — Windows'ta config sırları dosya izniyle korunmuyor — `app/config.py:397`
`ConfigStore.__init__` ve `AppState` yalnızca `os.name == "posix"` iken `chmod(0o700/0o600)` yapıyor.
Windows'ta `config.json` (içinde `secret_key` ve `password_hash`) ve `data/logs/` varsayılan ACL'leri
miras alır — paylaşımlı bir Windows makinesinde diğer yerel kullanıcılar okuyabilir. README "atomic write,
0600" diyor; bu iddia Windows'ta **yanlış.**
- **Etki:** `secret_key`'i okuyan biri geçerli session cookie üretebilir (auth baypası). Bu, ürünün
  birincil hedef platformlarından biri (Windows) için gerçek bir güvenlik açığı.
- **Çözüm:** Windows'ta `icacls` ile dosyayı sadece sahibe/SYSTEM'e kısıtla (ctypes veya
  `subprocess` ile), veya en azından README'deki "0600" iddiasını platformla netleştir ve kullanıcıyı uyar.

### 🟠 H6 — Form doğrulama hataları kullanıcıya hiç görünmüyor (HTMX 400) — `routes.py:404`, `server.py`
Mapping formu `hx-post=... hx-swap="innerHTML"` ile gönderiliyor. HTMX 2.x **varsayılan olarak 4xx
yanıtlarını swap etmez** (responseHandling hata kabul eder, DOM'a dokunmaz). `mapping_save` doğrulama
hatasında `_form_error(...)`'ı `status_code=400` ile döndürüyor → sunucu hata bannerlı formu render
ediyor ama tarayıcı onu **atıyor.** Operatör geçersiz bir mapping kaydetmeye çalıştığında **hiçbir şey
olmuyormuş gibi** görünüyor.
- **Etki:** Sessiz başarısızlık; ciddi UX hatası. Boş/yanlış alanlarda kullanıcı neyin yanlış olduğunu
  asla göremez.
- **Çözüm:** `htmx.config.responseHandling`'i 400/422'yi swap edecek şekilde ayarla, veya hata
  fragmentini `200` ile döndür (sunucu tarafı doğrulama hatası "başarısız HTTP" değil, normal akış).

### 🟠 Orta — Diğer doğrulanmış bulgular

| Kod | Başlık | Konum | Not |
|-----|--------|-------|-----|
| M1 | UI'da mapping düzenlemek `match`(VID/PID), RS-485, `openstr`/`closestr` ayarlarını sessizce siliyor | `routes.py:533,550` (`_serial_dict`/`_build_mapping_dict` bu alanları hiç toplamıyor) | Veri kaybı. Import JSON ile set edilen alanlar her UI kaydında uçuyor. |
| M2 | Gizlenen form blokları `disabled` yapıldığı için submit edilmiyor → mod değişiminde ACL kaybı | `app/web/static/app.js:40` (`applyShowWhen`) | M1 ile birleşince düzenleme tehlikeli. |
| M3 | TCP ve UDP **aynı portu** paylaşamıyor — çakışma kontrolü protokolü ayırmıyor | `app/config.py:371` | Geçerli bir config (TCP:4001 + UDP:4001) yanlışlıkla reddediliyor. |
| M4 | Maplar arası **seri port çakışması** kontrol edilmiyor | `app/config.py:352` | İki mapping aynı COM portunu açmaya çalışınca ikincisi açılışta patlar. |
| M5 | scrypt parola doğrulama event loop'ta **senkron** çalışıyor (~50-100ms) → tüm köprüler stall | `routes.py:70,133` | `asyncio.to_thread` kullan. |
| M6 | `read_mapping_log` 100 MB'a kadar dosyayı senkron okuyor (event loop bloke) | `app/state.py:182` | İş parçacığına taşı + tail oku. |
| M7 | `config.save()` çift `fsync` ile event loop'u bloke ediyor | `app/config.py:413`, `routes.py:392` | `asyncio.to_thread`. |
| M8 | `all.log` ve `audit.log` **sınırsız büyüyor** (log bakımı sadece per-mapping logları kırpıyor) | `app/state.py:211` | Global loglara da rotasyon ekle. |
| M9 | Connect-out (TCP client) TLS **sertifika doğrulamasını tamamen kapatıyor** (`CERT_NONE`) | `app/engine/bridge.py:425` | MITM mümkün. En azından opt-in `verify` seçeneği sun. |
| M10 | `logout` session'ı iptal edemiyor — yakalanmış cookie 8 saat geçerli kalır | `routes.py:80` | Stateless token; logout'ta `pwd_version` benzeri bir nonce veya kısa TTL+refresh düşün. |
| M11 | RFC2217 oturumu seri instance'ı bir kez bağlıyor; reconnect sonrası **ölü porta** komut gönderir | `bridge.py:197` | Reconnect'te oturumları tazele veya canlı referans tut. |
| M12 | `/metrics` session-cookie auth arkasında → Prometheus scrape edemiyor | `routes.py:307` | Bearer token / IP allowlist ile ayrı auth. |
| M13 | Telnet modu IAC BREAK (243) ve ECHO negotiation'ı desteklemiyor | `protocols/telnet.py` | Klasik konsol sunucularına göre eksik; interaktif konsol için ECHO+SGA gerek. |
| M14 | Korumsuz config.json startup'ta çökmeye yol açar (no `.bak`, no try/except) | `app/config.py:406` | Bozuk JSON'da net hata + güvenli mod. |
| M15 | Tracer her seri chunk'ta **senkron `write+flush`** yapıyor (hot path) | `bridge.py:80` | Trace açıkken throughput düşer; buffered/thread yaz. |

### 🟢 Düşük öncelik / cilalama

- **start.bat** `py` launcher'ı asla bulamıyor: `%errorlevel%` parantezli `else` bloğunda parse anında
  genişler (`start.bat:13`). Sadece `py` kurulu (python.org default) Windows makinelerinde "Python not
  found" der. `setlocal enabledelayedexpansion` + `!errorlevel!` veya `where py && set ... || ...` kullan.
- **systemd** birim ilk kurulumu bozuyor: `ProtectSystem=strict` + `ReadWritePaths=/opt/ser2net/data`,
  bootstrap'ın `./lib`'e wheel yazmasını engeller (`systemd/ser2net.service:24`). `lib`'i de
  ReadWritePaths'e ekle, ya da `--no-bootstrap` ile çalıştırıp lib'i önceden doldur. (pyudev için
  `AF_NETLINK` da kısıtlı ama bu sadece polling'e düşürür — zararsız.)
- **Windows'ta graceful shutdown yok:** `loop.add_signal_handler` Windows'ta hep `NotImplementedError`
  verir ve suppress edilir (`runtime.py:86`). Bir Windows servis yöneticisinden (Shawl) gelen SIGTERM
  düzgün kapanışı tetiklemez. Windows için ayrı bir kapanış mekanizması (Ctrl+C event / named-pipe) gerek.
- **Forced SelectorEventLoop** Windows'ta servisi ~512 socket'le (`FD_SETSIZE`) sınırlar
  (`runtime.py:41`) — "dozens of mappings × çok istemci" iddiasıyla çelişebilir. Bilinçli bir tasarım
  tercihi (pyserial-asyncio-fast uyumu); en azından dokümante et / izle.
- **psutil** bootstrap'ta zorunlu listede ama yalnızca x86_64 Linux + win_amd64 wheel'leri bundle'lı →
  ARM Linux / macOS'ta offline kurulum patlar (`bootstrap.py:34`). Runtime'da zaten opsiyonel
  (try/except), bootstrap'ta da opsiyonel yap veya ek wheel ekle.
- RS-485 / RTS-DTR ayar hataları **sessizce yutuluyor**, kod yorumu "surfaced to the caller" dese de
  (`serial_io.py:137`).
- `max_connections` aşımı: yeni istemci, kick edilen kurban gerçekten ayrılmadan eklenir; ani bağlantı
  patlamasında aynı ölü kurban tekrar tekrar kick edilir (`bridge.py:571`).
- Login rate-limiter durumu hiç temizlenmiyor (sınırsız büyüyen dict) ve proxy arkasında tüm IP'ler
  tek IP'ye çöker (`auth.py:120`).
- WebSocket Origin kontrolü Origin header yoksa atlanır (`routes.py:253`) — tarayıcı saldırganı için
  düşük risk.
- `session_timeout_s` config alanı ölü: kod sabit `SESSION_TTL = 8*3600` kullanıyor (`routes.py:29`).
- Yinelenen mapping adlarına izin var (log/metrics etiketleri karışır).
- xterm konsolunun reconnect yolu yok: mapping restart edilince terminal kalıcı ölür (`app.js:120`).
- `console.choose_admin_bind` docstring'i kodla çelişiyor (headless default 0.0.0.0 diyor, kod 127.0.0.1).
- scrypt maliyeti `N=2^14`, güncel OWASP rehberinin (2^17) altında (`auth.py:22`).
- Trace dosyaları varsayılan (world-readable olabilen) izinlerle açılır, ham seri payload içerir.

---

## 3. Test ve CI Eksiklikleri

Mevcut testler iyi (24-bridge full-duplex stres dahil) ama **kritik boşluklar** var ve hepsi
Linux/socat'a bağlı:

- **Sıfır kapsama:** per-mapping TLS köprüleri, UDP modu, priority eviction, backup/restore, auth yaşam
  döngüsü (logout/expiry/rate-limit recovery), `closeon` cross-chunk, `read_only`, RFC2217 hata durumları.
- **Yanıltıcı testler:** `test_v2_console.py` auth tamamen kırık olsa bile geçebilir.
- **Windows yolu hiç test edilmiyor** (socat Linux-only) — oysa Windows birinci sınıf hedef.
- **CI yok** (`.github/workflows` yok), birleşik test runner yok, lint/type-check (ruff/mypy) config yok.
- Sleep tabanlı senkronizasyon + sabit port numaraları → yavaş ve flaky.
- Protokol parser'ları ve config doğrulama için negatif/birim testi yok (malformed telnet/RFC2217).

**Öneri:** GitHub Actions matrisi (Linux + Windows × Python 3.10–3.13), ruff + mypy, ve yukarıdaki
güvenlik bulguları için regresyon testleri (özellikle H1/H2/H3).

---

## 4. Piyasa Analizi — Rakipler

### 4a. Açık kaynak namesake: ser2net (Corey Minyard)
GPL-2.0, C, gensio tabanlı, **son sürüm 4.6.7 (Şub 2026).** YAML config; raw TCP/UDP/SCTP, telnet
RFC2217, TLS + certauth (sertifika/parola, opsiyonel 2FA), PAM, mDNS, rotators, connback (veri-tetiklemeli
geri arama), IPMI SoL, trace dosyaları, banner/openstr/closestr/closeon, max-connections + kickolduser,
remaddr. **Web UI yok** (kullanıcılar LuCI app'leri ekliyor); 3.x→4.x YAML geçişi config'leri bozmuştu.
4.6.x'ten beri **Windows binary** de var. → Bizim avantajımız: gerçek web UI, Prometheus, öncelikli IP
allowlist, serial↔serial, yönetilen TCP-client modu. Onların derinliği: protokol genişliği (SCTP, certauth,
connback, mDNS, rotators, IPMI).

### 4b. Donanım device server'lar (yer değiştirdiğimiz pazar)
| Ürün | Fiyat (yaklaşık) | Öne çıkan |
|------|------------------|-----------|
| Moxa NPort 5100/5600 | $135–275 (1 port) / rack | Real COM/TTY sürücü, RFC2217, SNMP, AES (5600) |
| Digi PortServer/ConnectPort TS | $527–615 (4 port) | RealPort COM redirector (SSL+AES'li), Remote Manager |
| Lantronix UDS/EDS/SLC 8000 | $91 (kullanılmış) → $1530+ (SLC) | AES-256, SSH/SSL, FIPS 140-2, OOB konsol |
| Perle IOLAN SDS | $405–468 (1 port) | TruePort redirector, RADIUS/TACACS+/LDAP/Kerberos |
| Advantech EKI-1500 | $745–1330 (4 port) | Çift LAN redundancy, 255 sanal COM |
| USR-IOT / PUSR | **$25–41** | **Modbus RTU↔TCP + MQTT/JSON bulut dahil** |

**En güçlü sinyal:** Her donanım satıcısı **client-side sanal COM port sürücüsü** (RealPort/Real
COM/TruePort/VCOM) ile pazarlanıyor — kategorinin en çok pazarlanan özelliği. Ayrıca Modbus TCP↔RTU
gateway, premium satıcılarda **ayrı ve pahalı bir SKU** (Moxa MGate ~$415–590/port) iken ucuz USR-IOT
$41'e bundle ediyor — yani gateway saf yazılım, yüksek ödeme isteği var.

### 4c. Yazılım rakipleri
| Ürün | Fiyat | Öne çıkan / eksik |
|------|-------|-------------------|
| Eltima Serial to Ethernet Connector | $259.95/seat (kalıcı) | En tam paket: sanal COM, RFC2217, UDP, şifreleme, Modbus gateway, Win+Linux. Web UI yok. |
| TALtech TCP-Com | $199/PC | 256 port, TCP+UDP, Windows servisi. TLS yok, Windows-only. |
| FabulaTech Network Serial Port Kit | per-seat (gizli) | Server+client sürücü, auto-reconnect, servis modu. Windows-only. |
| HW VSP3 | free (1 port) / paid | Sanal COM, RFC2217. **Discontinued** (son 2015). |
| VirtualHere | **$49/server (kalıcı)** | USB-over-IP; solo geliştirici, binlerce kurulum. Serial-aware değil. |
| socat / ser2sock / com0com+com2tcp+hub4com / pyserial örnekleri | free | Güçlü ama CLI, UI yok, çoğu bakımsız. com0com son sürüm 2018. |

### 4d. Pazar trendleri (2025–2026)
- **Modbus TCP↔RTU gateway:** ~$2.8B pazar (2030, ~%11.3 CAGR). En net "donanım bütçesini lisansa
  çevirme" fırsatı.
- **MQTT/Sparkplug B köprüleme:** IIoT'nin baskın northbound deseni; hafif "seri port → MQTT topic"
  özelliği boş bir orta segmenti dolduruyor.
- **Container deployment:** Docker'a USB passthrough güvenilmez; kimse resmi/desteklenen image sunmuyor.
- **Uyumluluk (IEC 62443 / NERC CIP):** per-kullanıcı RBAC, MFA, oturum zaman aşımı/limiti, kurcalanamaz
  audit log, syslog export, oturum kaydı → sub-$300 hiçbir yazılım rakibinde yok.
- **Lisanslama:** Niş, kalıcı (perpetual) per-host lisansı tercih ediyor ($49–260). Saf SaaS yok.
  Tekrarlayan gelir için "kalıcı çekirdek + opsiyonel filo aboneliği" modeli (Opengear Lighthouse gibi).

---

## 5. Önerilen Özellikler (önceliklendirilmiş)

### 🔴 Must (rekabette masaya oturmak için)
1. **Client-side sanal COM port hikâyesi.** Tüm ciddi rakiplerin lider özelliği. Seçenekler: kendi imzalı
   sürücün, **com0com+com2tcp için desteklenen resmi reçete**, veya RFC2217 ile Eltima/HW VSP3/Serial-IP
   istemcileriyle interop. Bu olmadan donanım kutularını yer değiştiremezsin.
2. **Modbus RTU↔TCP gateway modu** (web UI'da per-port toggle, slave-ID routing, çoklu TCP master).
   En yüksek ödeme isteği olan, saf yazılım, hiçbir yazılım rakibinde yok.
3. **Çok-kullanıcı / RBAC + per-port izin + audit + MFA** (uyumluluk paketi). 62443/NERC CIP kapısı;
   Pro tier'ı haklı çıkarır.
4. **Resmi Docker image + docker-compose + host-side USB/RFC2217 reçetesi.** İlk hareket eden Home
   Assistant / zwave-js / IIoT-edge kitlesini ve ücretsiz funnel'ı kazanır.
5. **Adı netleştir/değiştir** (bkz. §6).

### 🟠 Should
6. **Tam REST API (OpenAPI)** — web UI'nın yaptığı her şey + config-as-code import/export. Python web
   app'inde ucuz; MSP/otomasyon alıcılarını açar.
7. **mTLS / client-certificate auth + per-mapping kullanıcı auth.** ser2net certauth paritesi.
8. **LDAP / Active Directory auth (Pro tier)**, sonra OIDC/SAML SSO.
9. **MQTT publisher** (raw satır veya Modbus register → MQTT topic, TLS, birth/death mesajları).
10. **Per-port oturum kaydı/replay + canlı izleme** (Teleport-vari). Zaten her byte'ı görüyorsun; ucuz.
11. **Connect-back** (veri-tetiklemeli giden bağlantı) — SCADA/alarm cihazları için.
12. **Zaman-damgalı/hex trace geliştirmeleri + web'den indirme** (var olanı avantaja çevir).
13. **i18n (TR/EN)** — zaten roadmap'te; tüm template string'leri hardcoded.

### 🟢 Could
14. mDNS/zero-conf keşif · 15. Rotators (port havuzu konsol sunucusu) · 16. Sparkplug B edge node ·
17. Çoklu-host filo dashboard'u (abonelik ürünü) · 18. SNMP agent (MIB-II + trap) · 19. Çok-NIC
binding + failover · 20. **Açık kaynak ser2net YAML config import aracı** (en büyük kurulu tabandan
kullanıcı çevirir — düşük efor, yüksek funnel).

---

## 6. İsim / Konumlandırma Notu (stratejik)

Üç bağımsız araştırma ajanı da aynı sonuca vardı: **proprietary bir ürünü, aktif geliştirilen GPL açık
kaynak "ser2net" ile birebir aynı adla satmak risklidir.** "ser2net" araması, paket yöneticileri ve
geliştirici zihninde upstream'e ait; ücretsiz alternatif TLS/RFC2217/UDP/mDNS sunuyor. Öneri: belirgin
bir ürün adı + "ser2net'in modern, web-yönetilen hâli / ser2net YAML import" konumlandırması (rakip
değil, yükseltme olarak). Bu, hem SEO hem marka riskini yönetir.

---

## 7. Önerilen Roadmap (revize)

**v2.1.1 — Düzeltmeler (önce bunlar, sürümden önce):** H1, H2, H3, H4, H5, H6 + M1–M4 (veri kaybı /
config doğrulama) + start.bat & systemd düzeltmeleri. Her biri için regresyon testi.

**v2.2 — Güvenilirlik & dağıtım:** event-loop bloklamalarını kaldır (M5–M7), global log rotasyonu (M8),
Windows graceful shutdown, CI matrisi (Linux+Windows × 3.10–3.13) + ruff/mypy, **resmi Docker image**,
PyInstaller paketleri + Windows servis installer.

**v2.3 — Rekabet paritesi:** **Client-side sanal COM reçetesi/interop** (Must #1), **REST API** (#6),
mTLS + per-mapping auth (#7).

**v2.4 — Endüstriyel derinlik & monetizasyon:** **Modbus gateway** (#2), **MQTT publisher** (#9),
**RBAC + audit + uyumluluk paketi** (#3), LDAP/AD (#8), per-port oturum kaydı (#10). Lisans modeli:
ücretsiz 1-port tier + kalıcı per-host ($79–149) + filo aboneliği.

**v2.5+:** connback, rotators, mDNS, Sparkplug B, çoklu-host filo dashboard, ser2net YAML import.

---

*Not: Bu rapordaki tüm `file:line` referansları, incelenen working copy (`9ba09bd`) üzerinde elle
doğrulanmıştır. H1 ve H2, çok-ajanlı adversaryal doğrulamadan da "confirmed" (high confidence) olarak
geçmiştir; kalan bulgular oturum limiti nedeniyle ajan-doğrulamasını tamamlayamamış, bunun yerine bu
incelemede kaynak kod doğrudan okunarak teyit edilmiştir.*
