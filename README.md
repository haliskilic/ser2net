<!-- ser2net — README (Türkçe / varsayılan). English: README.en.md -->

**🌐 [Türkçe](README.md) · [English](README.en.md)**

# ser2net

[![Lisans: AGPL-3.0](https://img.shields.io/badge/Lisans-AGPL--3.0-blue.svg)](LICENSE) [![Ticari lisans](https://img.shields.io/badge/Ticari-lisans%20mevcut-green.svg)](COMMERCIAL_LICENSE.md) [![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-db61a2?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/haliskilic)

**Seri portları (COM / ttyUSB / ttyACM / ttyS) ağ üzerinden erişilebilir kılan,
web arayüzünden yönetilen, çapraz-platform (Windows + Linux) saf-Python köprü.**

> C dünyasındaki `ser2net`'in modern, web yönetimli karşılığı. Tek ekrandan onlarca
> seri portu IP:port'a eşleyin; raw / telnet / RFC2217; çift yönlü; düşük gecikme.

---

## 📸 Ekran Görüntüleri

**Pano — onlarca eşleme, canlı durum, algılanan portlar ve LAN cluster paneli:**

![Pano](docs/screenshots/02-dashboard.png)

**LAN cluster — tüm düğümlerin eşlemeleri tek tabloda; düğüm sağlığı + uzaktan başlat/durdur:**

![Cluster](docs/screenshots/06-cluster.png)

| Eşleme ekleme (tüm seri/ağ seçenekleri) | Tarayıcı-içi seri konsol (xterm.js) |
|---|---|
| ![Eşleme formu](docs/screenshots/03-add-mapping.png) | ![Konsol](docs/screenshots/05-console.png) |

| Ayarlar (parola · TLS · cluster · yedek) | Giriş |
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
  **UDP**, ve **serial↔serial** köprüleme. TCP köprüleri için isteğe bağlı **TLS**
  (istemci sertifikaları, CA dosyası ya da parmak iziyle sunucu denetimi: bkz. Güvenlik). UDP,
  cihazın çıktısını izinli bir IP'den gelen son datagram'ın göndericisine ya da her zaman
  sabit bir **yanıt adresine** (IP + port) gönderir; **karşı uç zaman aşımı** sessiz kalan
  göndericiyi unutturur. Datagram'lar seri portu en fazla 64 KiB'lık bir kuyrukta bekler:
  hattan hızlı gönderen birinin bunu aşan datagram'ları atılır, sayılır ve loglanır.
- **Protokoller:** `raw`, `telnet` (8-bit temiz), `rfc2217` (uzaktan canlı
  baud/parity/databit/stopbit/akış kontrolü değişimi), `modbus` (RTU↔TCP gateway). Portun
  yerine getiremediği bir RFC 2217 isteği (bozuk ya da ayrılmış bir değer veya seri portun
  reddettiği bir değer) RFC 2217'nin dediği gibi güncel ayarla yanıtlanır ve portu olduğu
  gibi bırakır; istemci bağlı kalır. Kaybolan seri port yeniden açılırken de RFC 2217
  istemcisi bağlı kalır ve yeniden açılan port onun ayarlarını alır; istenirse istemci
  ayrılınca port eşlemenin ayarlarına döner (bkz.
  [`docs/VIRTUAL-COM.md`](docs/VIRTUAL-COM.md)). 1 KiB'tan uzun bir telnet ya da RFC 2217
  alt müzakeresi (gerçekleri birkaç bayttır) o istemcinin oturumunu sonlandırır; nedeni
  log'a yazılır.
- **Modbus ağ geçidi:** Modbus/TCP master'larını seri Modbus/RTU slave'lere köprüler —
  çoklu master, paylaşımlı bus'ta transaction serileştirme + transaction-id koruması,
  RTU yanıtı gelmezse `0x0B` (gateway-timeout) exception. Seri port kaybolursa (USB
  adaptör çıkarıldığında) master'lar bu sürede hemen `0x0A` (gateway yolu yok) alır ve
  port, her eşlemede olduğu gibi yeniden açılır (durum `reconnecting`, cihaz
  kayboldu/geri geldi bildirimleri).
  Master'lar diğer istemciler gibi kabul edilir (izinli IP'ler, max bağlantı, öncelikli
  IP'ler, eski-kullanıcıyı-at); **salt okunur** bir gateway okuma olmayan her isteği
  (FC5/6/15/16/22/23 gibi yazmalar, slave'i yeniden başlatan ya da susturan tanılama
  komutları, bilinmeyen fonksiyon kodları) hatta göndermeden `0x01` exception ile yanıtlar.
  Banner, open/close string ve `closeon` burada kullanılmaz (Modbus çerçevelerini bozar).
  **Edge modu:** coil ve register'ları periyodik okuyup MQTT'ye yayınlar (bool,
  uint/int/float 16/32, ölçek); `"writable": true` noktalar MQTT'den yazılır
  (`<topic>/<nokta>/set`, yalnızca o noktaya tek değer: coil için FC5, holding register için
  kendi bir ya da iki register'ına FC6/FC16; JSON liste ya da başka bir metin
  `.../set/result`'ta `error: …` alır). **Salt okunur** bu yazmaları da reddeder (`error:
  mapping is read-only`; reddedilen DCMD loglanır); MQTT'den yazma alan eşlemeler panoda
  **CMD** etiketiyle gösterilir (bkz. Güvenlik: kimin komut gönderebileceğine broker karar
  verir). İstenirse **Sparkplug B**
  edge node olarak, her Modbus unit'i bir cihaz (`unit1`, …): `bdSeq` ile NBIRTH/NDEATH,
  `seq` ile DBIRTH/DDATA, DCMD ile yazma, NCMD ile rebirth. Bir komut yayınlandığında bir
  kez yazılır: broker'ın yeniden bağlanma, yeniden başlatma ya da config kaydından sonra
  tekrar gönderdiği retained bir `/set`, **Retained /set komutunu her bağlantıdan sonra
  yeniden uygula** işaretli değilse yok sayılır (ve loglanır); retained bir Sparkplug
  NCMD/DCMD her zaman yok sayılır, boş bir mesaj (retained mesajı temizleyen) komut
  değildir ve ser2net bağlı değilken yayınlanan bir komut sonradan iletilmez.
- **Client-side sanal COM:** uzaktaki portu yerel `COMx`/`tty` olarak göstermek için
  reçeteler (pyserial `rfc2217://`, Linux `socat`, Windows com0com+com2tcp / HW VSP3) —
  bkz. [`docs/VIRTUAL-COM.md`](docs/VIRTUAL-COM.md).
- **Tam seri yapılandırma:** baud (custom dahil), data bit, parity, stop bit, akış
  kontrolü (none/RTS-CTS/XON-XOFF/DSR-DTR), açılışta RTS/DTR, port kilitleme ve RS-485
  donanım auto-RTS (Linux `TIOCSRS485`: aç + TX öncesi/sonrası gecikme + TX'te RTS seviyesi).
  `on` / `off` seçilen RTS ya da DTR, port açıldığı andan itibaren o durumdadır (`keep`
  sürücünün varsayılanını, yani açığı bırakır). Bu yüzden Windows'ta `off` seçilen hat hiç
  darbe vermez; Linux'ta ise port açılırken DTR ile RTS'yi çekirdeğin kendisi yükseltir,
  `off` bu darbeyi yalnızca kısaltır ve DTR ile resetlenen bir kartın (Arduino) her açılışta
  resetlenmesini önleyemez. Portun kabul etmediği bir ayar (çoğu USB adaptörde RS-485, modem
  hattı olmayan bir portta RTS/DTR) portun açılmasını engellemez: pano bunu eşlemenin
  durumunun altında gösterir, eşlemenin log'u bir kez yazar; kabul edilmeyen RS-485 modu
  yeniden kapatılır, böylece portun ayarlarındaki sonraki değişiklikler (bir RFC 2217
  istemcisinin baud hızı) çalışır.
- **Port kilitleme (Linux, macOS):** varsayılan olarak açık **Portu kilitle (flock)**, diğer
  ser2net eşlemelerini ve portu aynı şekilde kilitleyen programları (picocom, tio,
  miniterm) dışarıda tutar; ama minicom'u, hiç kilit kullanmayan programları (`cat`, `stty`,
  script'ler), oturum açma istemini ya da ModemManager'ı tutmaz. Varsayılan olarak kapalı
  iki seçenek, klasik ser2net'in yaptığı gibi daha fazlasını dışarıda tutar: **Port açıkken
  diğer programları dışarıda tut** (`TIOCEXCL`: başka bir programın açma denemesi root'unki
  dışında "meşgul" hatasıyla başarısız olur) ve **Başka bir programın UUCP kilit dosyasıyla
  kilitlediği portu açma** (minicom, cu: `/run/lock` ya da `/var/lock` içindeki `LCK..`
  dosyaları yalnızca okunur; ser2net kilit dosyası oluşturmaz ve hiçbirini silmez; pano
  kilitleyen programın adını söyler). Windows'ta bir port her zaman yalnızca tek bir
  programda açıktır ve bunların etkisi yoktur. Docker'da `--device` ile verilen port
  container'ın kendi cihaz düğümüdür ve flock düğümü kilitler; bu yüzden host'taki
  programları dışarıda tutması beklenmez (`-v /dev:/dev` ile düğüm host'unkidir);
  `TIOCEXCL` ise portun kendisi için geçerlidir.
- **Canlı port listesi:** ayrıcalık gerektirmez. Her 2 sn'de ucuz bir kontrol bir portun
  gelip gelmediğine bakar (Linux: sysfs'teki tty cihazları ve `/dev/serial` linkleri;
  Windows: kayıt defterinin listelediği COM portları); portlar yalnızca o zaman, arayüz ya
  da API 2 sn'den eski bir liste gösterecekken ve en az dakikada bir yeniden taranır.
  Linux'ta yalnızca pyserial'ın baktığı adlar değil, çekirdekteki her seri sürücünün portu
  listelenir (`/proc/tty/drivers`); böylece kartın kendi UART'ları da görünür (i.MX'te
  `ttymxc*`, `ttyLP*`, Jetson'da `ttyTHS*`, Zynq'te `ttyPS*`, `ttyMSM*`, `ttyMAX*`, 6.5
  öncesi kernel'lerde Raspberry Pi'nin `ttyS0`'ı ...). Arkasında donanım olmayan `ttyS`
  portları gizlenir; udev'in adları gösterilir ve seçilebilir: `/dev/serial/by-id` (seri
  numarasına göre: hangi USB soketinde olursa olsun yeniden takmaya dayanır) ve
  `/dev/serial/by-path` (USB soketine göre: adaptör aynı sokette kaldıkça doğrudur). by-id
  adı üretici, model ve seri numarasından oluşur; bu yüzden kendi seri numarası olmayan
  özdeş adaptörler (CH340, çoğu PL2303, hepsi aynı numarayı taşıyan klonlar) tek bir adı
  paylaşır, udev onu birine verir ve hangisine verdiği reboot ya da yeniden takmayla
  değişebilir: Portlar sayfası böyle adaptörleri işaretler (`aynı USB kimliği`,
  `by-id benzersiz değil`), form da bunu söyler; bunları USB soketine göre bağlayın.
  Eşleme formunda Cihaz'ın yanındaki ↻ formu kapatmadan portları yeniden listeler, **Başka
  bir cihaz yolu…** ise listede olmayan bir yolu alır (bir kartın `/dev/ttymxc1` gibi
  UART'ı, henüz takılmamış bir adaptör, Windows'ta bir `\\.\` adı); `/dev/serial0` gibi
  ayarlı bir link, gösterdiği cihazla birlikte görünür (`/dev/serial0 → /dev/ttyS0`) ve o
  portun kullanımda sayılmasını sağlar.
- **Cihazı USB kimliğiyle bulma:** eşleme formundaki kutu işaretlenince adaptör VID:PID
  (+ seri no) ile bağlanır; çıkarılıp takılınca `ttyUSB0`→`ttyUSB1` ya da COM numarası
  değişse de eşleme doğru cihazı açar. Kendi seri numarası olmayan özdeş adaptörler (CH340,
  çoğu PL2303) ve çok portlu bir çipin kanalları (FT4232H) aynı kimliği taşır: bunlardan
  ayarlı port aralarındaysa o, değilse başka bir eşlemenin açık tutmadığı ilki açılır ve
  pano tahmin yapmak zorunda kaldığını söyler. Bağlı adaptör yokken portunu artık başka bir
  USB adaptörü almışsa o açılmaz: bağlı adaptör geri gelene ya da eşleme yeni adaptöre
  bağlanana kadar (kutunun işaretini kaldırıp yeniden işaretleyin, kaydedin) eşleme
  `cihaz yok` gösterir.
- **IP seçici:** makineye atanmış IP'ler (tüm arayüzler, localhost, iki ailenin LAN
  adresleri) veya custom.
- **IPv4 ve IPv6:** `0.0.0.0`'daki bir eşleme yalnızca IPv4, `::`'daki yalnızca IPv6
  istemcileri alır (TCP'de de UDP'de de, Linux'ta da Windows'ta da); **Tüm arayüzler
  (IPv4 + IPv6)** ikisini birden alır (`config.json`'da ve REST API'de
  `"bind_ip": "0.0.0.0", "dual_stack": true`; 2.9'dan eski bir sürüm bunu `0.0.0.0` olarak
  okur). `0.0.0.0` ve `::` birer aileyi aldığından birindeki bir eşlemeyle ötekindeki bir
  başkası aynı portu paylaşabilir. `::1` (IPv6 localhost) da `127.0.0.1` gibi bir dinleme
  IP'sidir. IPv6 adresleri port yanında köşeli parantezle yazılır (`[::1]:4001`). Eski bir
  sürümden gelen, Linux'ta `::`'da dinleyen bir UDP eşlemesi IPv4 datagramlarını da
  alıyordu: `dual_stack` açık olarak gelir, yani almaya devam eder (yapılandırma yeniden
  kaydedilene kadar açılış log'u bunu söyler); `dual_stack` olmadan gönderilen biri de öyle
  (bir REST API betiği, eski bir küme düğümü).
- **Eksik ya da meşgul seri cihaz:** cihaz yokken (çıkarılmış, henüz tanınmamış) eşleme
  `cihaz yok`, açık port az önce kaybolduğunda ya da başka bir nedenle açılamadığında
  `yeniden bağlanıyor` gösterir; birkaç saniyede bir (en fazla yaklaşık 10 sn arayla), port
  listesi yeni bir cihaz gördüğünde ise hemen yeniden denenir (yeniden takılan bir adaptör
  yaklaşık 2 sn içinde kullanılır). USB kimliğine bağlı bir eşleme, her denemede portları
  taramak yerine cihazını bu listede bulur. Log, her denemede bir satır yazmak yerine ilk
  hatayı ve hatanın her değişmesini, aynı hatayı ise yalnızca 15 dakikada bir, ne zamandan
  beri kaç denemenin başarısız olduğuyla birlikte yazar; port yeniden açılınca ne kadar süre
  kapalı kaldığını söyler (başarısız olmayı sürdüren dışa bağlantılar için de aynısı).
  `/api/v1/status`, açılamayan denemeleri (`open_failures`) ve açık portun kaybedilmesini
  (`device_lost`) ayrı sayar; `reconnects` eskisi gibi ikisini birden sayar. Pano,
  söyleyebildiğinde hatanın altında ne anlama geldiğini de yazar (`/api/v1/status` içinde
  `hint`; `last_error` pyserial'ın metni olarak kalır): "Permission denied" için cihazın ait
  olduğu grubu, cihazın kendisinden okuyarak (`dialout`, Arch'ta `uucp`, …), ve neyin eksik
  olduğunu (kullanıcı o grupta değil, ser2net başladıktan sonra eklendi, container'da o grup
  yok: `--group-add <gid>`); kullanımdaki bir port için onu hangi eşlemenin açık tuttuğunu
  ya da başka bir programın ne tür bir kilit tuttuğunu.
- **Dinleyiciler kendiliğinden açılır:** başlarken adresini kullanamayan bir eşleme
  (açılışta IP henüz atanmamış ya da port hâlâ dolu) `error` durumunda "N sn sonra yeniden
  denenecek" gösterir ve çalışana kadar en fazla 30 sn arayla yeniden başlatılır (Durdur
  bunu bitirir). Seri portu ve MQTT bağlantısı ancak dinleyicisi açıldıktan sonra açılır;
  denemeler cihaza dokunmaz.
- **Onlarca eşleme:** tek ekrandan ekle/düzenle/sil/başlat/durdur, canlı durum; arama,
  durum filtresi, sıralama ve seçili eşlemeleri toplu başlat/durdur/yeniden başlat.
  Tablo yalnızca değiştiğinde yenilenir (titreme yok, seçimler korunur).
- **Erişim kontrolü (eşleme başına):** izinli IP/CIDR listesi, **yüksek-öncelikli**
  istemci IP'leri (doluyken eski istemciyi atar), max bağlantı, eski-kullanıcıyı-at (yeni
  istemci öncelikli olmayan en eskiyi atar, öncelikli bir istemciyi asla: yalnızca
  öncelikli istemciler bağlıyken reddedilir), salt-okunur, banner, open/close string,
  `closeon` (Modbus gateway banner, open/close string ve `closeon` kullanmaz).
- **Bağlantı yaşam döngüsü (eşleme başına, TCP):** **TCP keepalive ile ölü bağlantı
  tespiti** — FIN/RST göndermeden kaybolan istemci ya da uzak sunucu (elektrik kesintisi,
  kablo çekilmesi, Wi-Fi/VPN/NAT düşmesi) artık sonsuza dek `ESTABLISHED` kalmaz:
  varsayılan ayarlarla (açık · 60 sn boşta · 10 sn aralık · 3 deneme) ~90 sn içinde
  düşürülür ve yuvası boşalır — cihaz veri gönderirken de (Linux `TCP_USER_TIMEOUT`).
  Ayrıca dışa bağlanmada **bağlantı zaman aşımı** (varsayılan 10 sn), **idle timeout**
  ve **maksimum bağlantı süresi** (ör. 24 sa); her kopuş nedeniyle loglanır.
- **Gözlemlenebilirlik:** per-mapping trafik trace (hex/timestamp, döner dosyalar,
  arayüzden indirilir), Prometheus `/metrics`, audit log ekranı, canlı log görüntüleyici,
  **tarayıcı-içi seri konsol** (xterm.js, WebSocket — trafiği izle veya cihaza yaz) ve
  iki yönlü, zaman damgalı **canlı hex görünümü**. Panodaki **Gelen / Giden** sayaçları
  (`/api/v1/status`'ta `bytes_in` / `bytes_out`, `/metrics`'te
  `ser2net_mapping_bytes_in_total` / `ser2net_mapping_bytes_out_total`) seri cihazdan
  okunan ve cihaza yazılan baytlardır; cihazı kaç istemci paylaşırsa paylaşsın bir kez
  sayılır (serial↔serial: A → B / B → A). Ağdan gelip cihaza yazılamayan veri (port açık
  değilken ya da bir UDP datagram'ı kuyruğu dolu bulduğunda) atılmış sayılır
  (`bytes_dropped`, `ser2net_mapping_bytes_dropped_total`), panoda işaretlenir ve loglanır.
- **Bildirimler:** cihaz koptuğunda/geri geldiğinde, eşleme başlamadığında, dışa bağlanan
  bir eşleme uzak sunucusunu kaybettiğinde/yeniden bulduğunda, istemci bağlanıp düştüğünde
  ya da hatalı girişte webhook (JSON, Slack, Microsoft Teams, Discord, Telegram) ve/veya
  e-posta (SMTP); tekrarlar bastırılır, test mesajı gönderilir.
- **Dışa bağlanma durumu:** dışa bağlanan bir eşlemenin durumu seri tarafı göstermeye devam
  eder; uzak sunucuyla bağlantısı yanında gösterilir ("bağlantı yok" ve nedeni, Sorunlu
  filtresi bunu sayar; "bağlanıyor"), `/api/v1/status`'ta (`link`: `connecting` / `up` /
  `down`, `link_error`, `link_since`, `connect_failures`) ve filo görünümünde de. Kapanıp
  hemen yeniden kurulan bir bağlantı (maksimum bağlantı süresi, idle timeout) kesinti sayılmaz.
- **Güvenlik:** parola (ilk erişimde belirlenir, scrypt), CSRF, imzalı-çerez oturum,
  login oran sınırı, sıkı güvenlik başlıkları; oturumlar çıkışta, parola değişince, kullanıcı
  bazında ya da herkes için (Ayarlar) kapanır, unutulan parola `ser2net --set-password` ile
  sıfırlanır (bkz. Güvenlik).
  Güvenilir ağlar için giriş zorunluluğu **Ayarlar → Web arayüzü erişimi**'nden kapatılabilir
  (ziyaretçiler seçilen rolle parolasız kullanır; bkz. Güvenlik).
- **Çok-kullanıcı / RBAC:** `admin` / `operator` / `viewer` rolleri — viewer salt-okunurdur
  ve cihazların gönderdiklerini izler; operator eşlemeleri yönetir, cihazlara gönderilenleri
  de görür (hex görünümü, trace dosyaları: cihaz isteminde yazılan bir parola da buraya
  düşer); admin her şey + kullanıcı yönetimi. Tek-parola kurulumları otomatik olarak bir
  `admin` kullanıcısına geçer.
- **LDAP / Active Directory auth (opsiyonel):** kullanıcılar dizine bind ederek doğrulanır;
  LDAP grup üyeliği role eşlenir. Yerel hesaplarla birlikte çalışır; `ldap3` gerektirir.
  LDAPS / StartTLS'te sunucunun sertifikası denetlenir (sistemin CA'ları ya da kendi CA
  dosyanız).
- **OIDC tek-oturum (SSO, opsiyonel):** Keycloak/Entra ID/Google/Okta ile PKCE'li
  authorization-code akışı; id_token doğrulanır (imza/iss/aud/exp/nonce), claim→rol eşlenir.
  `authlib` gerektirir.
- **REST API:** otomasyon için JSON API (`/api/v1`) — eşleme CRUD, başlat/durdur/yeniden,
  durum ve portlar; **bearer-token** kimlik doğrulama (rol seçilebilir — `viewer`
  salt-okunur); OpenAPI 3.0 (`/api/v1/openapi.json`). Token, Ayarlar'dan üretilir.
- **LAN cluster (filo yönetimi):** aynı ağdaki düğümler birbirini **otomatik bulur**
  (imzalı UDP broadcast — mDNS yok) ve bir düğüm **tüm düğümlerin eşlemelerini tek
  tabloda** gösterir; her satır hangi bilgisayara ait olduğunu (ad + IP) ve her düğümün
  **sağlığını** (uptime · sürüm · çalışan/toplam) belirtir. Operatörler bu ekrandan
  başka bir host'un eşlemelerini **uzaktan başlat/durdur/yeniden başlat ve düzenle** edebilir.
  Broadcast'in ulaşmadığı yönlendirilmiş/L3 ağlar için **manuel peer** (host:port)
  eklenebilir. Güven = her düğümde **aynı paylaşılan anahtar** (Türkçe harfler dahil her
  karakter olabilir; kullanılmadan önce Unicode NFKC ile normalleştirilir, başka bir sistemde
  yazılan ya da yapıştırılan aynı anahtar da eşleşir); varsayılan kapalı, Ayarlar → LAN
  cluster'dan açılır. Düğümler arasındaki istekler anahtarı taşımak yerine onunla
  **imzalanır** (anahtarın kendisini yalnızca 2.8.0 ya da daha eski sürümle çalışan düğümler
  alır; bkz. Güvenlik → LAN cluster). Eşlere her zaman doğrudan bağlanılır, hiçbir zaman HTTP
  proxy üzerinden bağlanılmaz (`HTTP(S)_PROXY` / `ALL_PROXY` yalnızca webhook ve SSO için
  geçerlidir).
- **Dağıtım:** resmi **Docker** imajı + `docker-compose`; **systemd** birimi;
  Linux+Windows × Python 3.10–3.13 **CI** (GitHub Actions).
- **MQTT yayınlama (opsiyonel):** seri satırlarını eşleme başına bir MQTT broker'ına
  yayınlar (host/port/topic/QoS/TLS/kimlik), retained birth/death durumuyla. `paho-mqtt`
  gerektirir (opsiyonel; yoksa köprü normal çalışır).
- **Klasik ser2net'ten geçiş:** `ser2net.yaml` (4.x) ve `ser2net.conf` (3.x) dosyaları
  Ayarlar → içe aktarma ile önizlemeli olarak alınır; karşılığı olmayan seçenekler uyarı
  olarak gösterilir, burada daha açık hale gelecek bir bağlantı (gtlssh girişi, `CA=`
  dosyası bu makinede olmayan TLS istemci sertifikası, sertifikası bu makinede olmayan TLS,
  yalnızca yazılan cihaz) kapalı olarak gelir; CA dosyası burada olan
  `ssl(clientauth,CA=...)` istemci sertifikalarını denetlemeye devam eder (**İstemci CA
  sertifika dosyası**). ser2net'in port kilitleri de aktarılır: 4.x `flock` (varsayılan açık)
  **Portu kilitle** ile **Port açıkken diğer programları dışarıda tut** olur, `uucplock`
  (varsayılan açık; 3.x'te her zaman) **Başka bir programın kilitlediği portu açma** olur;
  bir uyarı da hâlâ farklı olanı söyler (eşleme portunu başladığı andan itibaren açık tutar
  ve kilit dosyası oluşturmaz). Dinleme adresleri ailesini korur (`ipv6,::1` `::1` kalır,
  adressiz `ipv6`/`ipv6n4` `::` olur). İkisini de belirtmeyen bir accepter (`tcp,2000`)
  ser2net'te IPv4 ve IPv6 istemcileri alıyordu; burada `0.0.0.0` olarak, yalnızca IPv4 için
  gelir ve bir uyarı **Tüm arayüzler (IPv4 + IPv6)**'ya geçirilebilecek olanları sayar.
- **Türkçe / İngilizce arayüz:** üst menüdeki **TR / EN** ile seçilir (tarayıcıda hatırlanır);
  seçilmemişse tarayıcının dili ya da admin'in **Ayarlar → Web arayüzü erişimi**'nde seçtiği
  varsayılan dil.
  Bildirimler varsayılan dille gider; log'lar, audit log ve REST API İngilizce kalır.
- **Tamamen offline:** tüm bağımlılıklar wheel olarak birlikte gelir; internet gerekmez.

---

## 🧰 Gereklilikler

- **Python 3.10+** (sistemde kurulu). Başka hiçbir şey gerekmez — bağımlılıklar
  `vendor/wheels/` içinde gelir ve ilk çalıştırmada `./lib`'e kurulur (offline).
- Linux'ta seri portu **açmak** için kullanıcının seri cihazların grubunda olması gerekir
  (port **listelemek** için ayrıcalık gerekmez). Bu grup Debian, Ubuntu, Raspberry Pi OS,
  Fedora, RHEL ve openSUSE'de `dialout`, Arch, Manjaro ve EndeavourOS'ta ise `uucp`'dir (bu
  dağıtımlarda `dialout` grubu yoktur); hangisi olduğunu cihaz söyler:
  `stat -L -c %G /dev/ttyUSB0`.
  ```bash
  sudo usermod -aG "$(stat -L -c %G /dev/ttyUSB0)" "$USER"   # ardından oturumu kapatıp aç
  ```
- Windows'ta COM portları için ek yetki gerekmez.
- (Opsiyonel) TLS self-signed üretimi için `openssl`.

---

## 🚀 Kurulum & Çalıştırma

```bash
# Linux / macOS
python3 ser2net.py            # veya: ./start.sh

# Windows
start.bat
```

### Standalone binary (başka hiçbir şey kurmak gerekmez)
Her sürümde Windows `.exe` ve Linux binary'si **GitHub Releases**'a eklenir. İkisi de
her şeyi içinde taşıyan tek bir dosyadır: Python, OpenSSL, VC++ runtime (Windows),
MQTT / LDAP / OIDC desteği. Python, `openssl` ya da başka bir paket gerekmez. İndir,
çalıştır; config/log `data/` altında binary'nin yanına yazılır.

| Dosya | Çalıştığı sistemler |
|---|---|
| `ser2net-windows-x64.exe` | Windows 10/11, Windows Server 2016 ve sonrası (x64) |
| `ser2net-linux-x64` | glibc ≥ 2.28 olan x86_64 Linux: RHEL / AlmaLinux / Rocky 8+, Debian 11+, Ubuntu 20.04+, Fedora, openSUSE Leap 15 (musl/Alpine değil — orada Docker kullanın) |
| `ser2net-linux-arm64` | glibc ≥ 2.28 olan 64-bit ARM Linux: Raspberry Pi 3/4/5 (64-bit Raspberry Pi OS 11+ veya Ubuntu), ARM endüstriyel gateway'ler, AWS Graviton, aynı dağıtımlar |

Her sürümde ayrıca `SHA256SUMS`, her binary için CycloneDX SBOM (`*.cdx.json`) ve lisans
metinleri yayımlanır; binary'ler GitHub'ın derleme kaynağı imzasıyla (build provenance)
doğrulanabilir: `sha256sum -c SHA256SUMS --ignore-missing`,
`gh attestation verify ser2net-linux-x64 -R haliskilic/ser2net`. Windows exe'si **henüz
imzalanmıyor** (bir imzalama servisi tanımlanınca imzalanacak —
[`docs/SIGNING.md`](docs/SIGNING.md)); bu yüzden ilk çalıştırmada SmartScreen uyarabilir.
Windows'ta `Get-FileHash` ile `SHA256SUMS`'a karşı ve `gh attestation verify` ile
doğrulayın, sonra *Ek bilgi → Yine de çalıştır* ([`docs/WINDOWS.md`](docs/WINDOWS.md)).

Bu, her PR'da ve her sürümde test edilir (`tests/test_standalone.py`):
- paketin içindekiler işletim sisteminin temel kütüphanelerinden (glibc ≤ 2.28 / Windows
  sistem DLL'leri) başka bir şeye ihtiyaç duymamalı (Linux'ta x86_64 ve arm64);
- binary boş bir `PATH` ile (Python yok, `openssl` yok) çalışmalı;
- Linux binary'si temiz AlmaLinux 8/9, Debian 11/12, Ubuntu 20.04/22.04/24.04/26.04,
  openSUSE Leap 15.6 ve Fedora konteynerlerinde çalışmalı; termios sabiti olmayan bir hızda
  (250000 baud) bir seri port da açabilmeli.

Linux notları:
- `chmod +x ser2net-linux-x64`; seri portlar cihazların grubunu gerektirir (`dialout`;
  Arch'ta `uucp`, bkz. Gereklilikler).
- Binary açılışta kendini `$TMPDIR`'e (varsayılan `/tmp`) açar. `/tmp` `noexec` ile
  bağlıysa `TMPDIR`'i öyle olmayan bir klasöre yönlendirin. Çalıştığı sürece bu kopyayı
  kilitli tutar ve erişim zamanlarını tazeler; böylece systemd-tmpfiles, tmpreaper gibi
  geçici dosya temizleyicileri uzun süre çalışan bir binary'nin dosyalarını silmez.
  Öldürülen bir sürecin geride bıraktığı kopya ise her zamanki gibi temizlenir.
- Sertifika kontrolleri (TLS üzerinden MQTT, OIDC, LDAPS / StartTLS) için dağıtımın CA
  paketini, ayarlıysa `SSL_CERT_FILE`'ı kullanır.

`ser2net --about` sürümleri (Python, OpenSSL, bileşenler) gösterir; hata bildirirken
ekleyin. `ser2net --licenses` binary'nin içindeki her bileşenin lisans metnini yazar
(sürümlere `*.THIRD-PARTY-LICENSES.txt` olarak da eklenir).

Kendin derlemek için:
- Windows: `pip install -r requirements.txt -r requirements-optional.txt pyinstaller && pyinstaller ser2net.spec`
- Linux (eski dağıtımlarda da çalışsın diye eski glibc'li bir konteynerde):
  `docker run --rm -v "$PWD:/src" -w /src almalinux:8 bash tools/build_standalone_linux.sh`

İlk çalıştırmada:
1. **Konsolda** arayüzün hangi IP'den erişileceği sorulur (makine IP'leri; IPv4, IPv6 ya
   da ikisi için tüm arayüzler; veya custom) ve port (varsayılan 8080). Başsız/servis
   ortamında güvenli varsayılan **127.0.0.1**.
2. Tarayıcıda açılan adrese gidin; **ilk ekranda admin parolasını belirleyin**. Sayfayı
   makinenin IP adresiyle ya da `localhost` ile açın: başka bir host adıyla (reverse proxy,
   Docker host'unun adı) gelindiğinde kurulum sayfası ayrıca `data/setup-code.txt`
   içindeki tek kullanımlık kodu ister; böylece başka bir web sayfası yeni bir kurulumu ele
   geçiremez (DNS rebinding). Tarayıcı yoksa: `ser2net --set-password admin` (bkz. Güvenlik).
3. Panodan **+ Eşleme ekle** ile COM/tty seçip IP:port'a eşleyin.

Bind IP'yi sonradan değiştirmek:
```bash
python3 ser2net.py --reconfigure
```

### Offline kurulum (internetsiz makine)
Bağımlılıklar `vendor/wheels/` içinde bulunur; `ser2net.py` ilk açılışta bunları
`./lib`'e kurar (`pip install --no-index`). İnternet gerektirmez. Set, Linux x86_64 ve
Windows x64'te Python 3.10–3.14'ü kapsar; `requirements.txt` değişince
`python3 tools/refresh_wheels.py` ile yenilenir (`tests/test_vendor_wheels.py` denetler).
Opsiyonel özellikleri (MQTT/LDAP/OIDC) offline kaynak kurulumunda etkinleştirmek için
wheel'lerini de indirip kurun: `pip download -r requirements-optional.txt -d vendor/wheels`
ardından `pip install --no-index --find-links vendor/wheels -r requirements-optional.txt`.
(Standalone binary bunları zaten içerir.)

### Docker
```bash
docker compose up -d        # önce docker-compose.yml'deki `devices:` ve `ports:` kısmını düzenleyin
```
Compose olmadan [`docs/DOCKER.md`](docs/DOCKER.md#plain-docker-run) içindeki `docker run`
komutunu kullanın (hazır imaj, `--restart unless-stopped`, `--init`, eşleme portları,
`/dev/serial/by-id` cihaz yolu, log sınırı).
Hazır imaj (amd64 + arm64, her sürümde): `docker pull ghcr.io/haliskilic/ser2net:latest`
(ya da `:2.8`, `:2.8.1`); `docker-compose.yml` bunu kullanır.
Container'da arayüz `0.0.0.0`'a bağlanır (`SER2NET_BIND_IP` / `SER2NET_PORT` env ile
ayarlanır — başsız ortamda interaktif seçici çalışamaz; `SER2NET_BIND_IP=*` IPv4 ve IPv6
tüm arayüzler demektir). Her eşlemenin portunu da yayınlayın
(yeni bir eşleme 4001 alır) ya da host ağını kullanın. Seri cihazın grubu cihazın kendisinden
alınır; olağan cihaz adlarında `--group-add` gerekmez. Süreç root olarak değil, `ser2net`
kullanıcısıyla (uid 10001) çalışır; eski (root ile çalışan) imajların yazdığı `/data`
volume'u ilk açılışta otomatik devralınır, bind mount edilen bir klasörde eski imajın root
olarak yazdığı dosyalar klasörün sahibine verilir; yükseltmede elle bir şey yapmak gerekmez.
İmajda `HEALTHCHECK` var (`ser2net --healthcheck`). Ayrıntılar:
[`docs/DOCKER.md`](docs/DOCKER.md).

---

## 🖥️ Kullanım

- **Eşleme ekle:** Ad, tür (Serial↔Network / Serial↔Serial), seri port + parametreler,
  ağ modu (server/client/udp), protokol, bind/remote IP:port, erişim kuralları.
- **Başlat/Durdur/Yeniden başlat/Kopyala/Sil:** her satırda.
- **Log:** eşlemenin geçmiş logu (en yeni üstte, restart sonrası kalıcı).
- **Monitor:** tarayıcıda canlı seri terminal (xterm.js); ağ eşlemelerinde operator'ler
  cihaza yazabilir. Hex görünümü iki yönü gösterir; viewer'lar yalnızca cihazın
  gönderdiklerini görür.
- **Trace** (operator): eşlemenin trace dosyaları; listelenir ve indirilir.
- **Ayarlar:** parola değiştir, **web arayüzü erişimi** (giriş zorunlu / parolasız rol),
  admin TLS (yol ver veya self-signed üret), **REST API token'ı** üret/iptal et, mapping
  **yedek al/yükle** (JSON), durum.
- **/metrics:** Prometheus formatında metrikler (kimlik doğrulamalı): eşleme başına
  çalışıp çalışmadığı, istemcileri, gelen / giden / atılan baytlar, yeniden bağlanmalar
  (bunlardan seri portun açılamadığı denemeler, `ser2net_mapping_open_failures_total`, ve
  açık portun kaybedilmesi, `ser2net_mapping_device_lost_total`) ve
  düşürülen yavaş istemciler (dışa bağlanan eşlemede bağlantının açık olup olmadığı,
  `ser2net_mapping_link_up`, ve başarısız bağlanma denemeleri,
  `ser2net_mapping_connect_failures_total`); süreç için açık dosya sayısı ve sınırı (`ser2net_open_fds`,
  `ser2net_fd_limit`, POSIX), yönetim arayüzünün bağlantıları, süren ve sıra bekleyen TLS
  el sıkışmaları ve kabulden hemen sonra reddedilen bağlantılar
  (`ser2net_connections_refused_total`, nedene göre).

### Başka programlarla paylaşılan seri portlar (Linux)

Programların hepsi kilit kullanmadıkça hiçbir şey diğer programları bir seri porttan uzak
tutmaz; çekirdek konsolu, oturum açma istemi (login prompt) ve ModemManager da bir portu
sormadan kullanır. ser2net bu durumda portun baytlarını onlarla paylaşır (araya karışan
karakterler, Modbus timeout'ları), oturum açma istemi de her yeniden başladığında portu
kapatır (hang-up). Portlar sayfası böyle portları işaretler (**çekirdek konsolu**, **oturum
açma istemi**, **ModemManager**, ser2net'in açamayacağı bir port için de **erişim yok**);
böyle bir portu açık tutan eşleme de bunu durumunun altında söyler (kapatılıp hemen yeniden
açılan port da orada sayılır).
- **Çekirdek konsolu, oturum açma istemi** (Raspberry Pi'nin GPIO UART'ı, bir VM'nin seri
  konsolu): Raspberry Pi'de `sudo raspi-config` → Interface Options → Serial Port çalıştır,
  login shell sorusuna *No*, seri donanım sorusuna *Yes* de. Diğer sistemlerde çekirdek
  komut satırından `console=ttyS0` değerini kaldır ve oturum açma istemini kapat:
  `sudo systemctl mask --now serial-getty@ttyS0.service`.
- **ModemManager** (masaüstü Ubuntu / Fedora; Arduino gibi USB CDC `ttyACM*` cihazlar):
  cihazı rahat bırakmasını bir udev kuralıyla söyle, örneğin
  `/etc/udev/rules.d/99-ser2net.rules` dosyasına
  `ATTRS{idVendor}=="2341", ENV{ID_MM_DEVICE_IGNORE}="1"` yazıp
  `sudo udevadm control --reload && sudo udevadm trigger` çalıştır.
- **Permission denied:** pano cihazın ait olduğu grubu ve neyin eksik olduğunu söyler (bkz.
  Gereklilikler); **meşgul port:** portu bu ser2net'in hangi eşlemesinin açık tuttuğunu ya da
  başka bir programın ne tür bir kilit tuttuğunu söyler.

---

## 🔌 REST API

Tarayıcı arayüzünün yanında, otomasyon için JSON bir API (`/api/v1`). Kimlik doğrulama
`Authorization: Bearer <token>` ile yapılır; token'ı **Ayarlar → REST API token**'dan
üretin (yalnızca bir kez gösterilir). Tam tanım: `GET /api/v1/openapi.json`.

```bash
TOKEN="s2n_..."   # Ayarlar'dan üretilen token
# tüm eşlemeler (config + canlı durum)
curl -H "Authorization: Bearer $TOKEN" http://HOST:8080/api/v1/mappings
# eşleme oluştur
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"PLC-1","kind":"net","serial":{"port":"/dev/ttyUSB0","baudrate":9600},
       "network":{"mode":"server","bind_ip":"0.0.0.0","port":4001}}' \
  http://HOST:8080/api/v1/mappings
# başlat / durdur / yeniden başlat
curl -X POST -H "Authorization: Bearer $TOKEN" http://HOST:8080/api/v1/mappings/<id>/stop
```
Uç noktalar: `GET/POST /mappings`, `GET/PUT/DELETE /mappings/{id}`,
`POST /mappings/{id}/{start|stop|restart}`, `GET /status`, `GET /ports`,
`GET /health` (kimlik doğrulamasız), `GET /openapi.json`.
Token, üretilirken seçilen rolü taşır: `viewer` token'ı yalnızca okuyabilir (GET) ve kayıtlı
sırları (MQTT parolası) maskeli görür. `config.json`'daki `api_token_role` bir rol değilse
`viewer` sayılır ve açılış log'u bunu yazar.

---

## 🔒 Güvenlik

Arayüz **varsayılan olarak parola korumalı**. Varsayılan bind **127.0.0.1**; ağa açmak
için açılışta IP seçimi gerekir ve TLS'siz ağ bind'inde uyarı verilir. LAN'a açılan
kurulumlarda TLS (`admin_ui.tls_*`) ve eşleme bazlı `allowed_client_ips` önerilir.
Raw TCP düz-metindir — güvensiz ağlarda dikkat. Allowed/priority listesinde tek başına
`0.0.0.0`/`::` "herkes" demektir, IPv4 ve IPv6 istemciler birlikte; `0.0.0.0/0` tüm IPv4,
`::/0` tüm IPv6 istemciler demektir. IPv4-mapped bir girdi (`::ffff:192.168.1.0/120`)
eşlediği IPv4 adreslerine uyar. Bir eşlemenin seri portu yalnızca cihaz yolu ya da COM
adı olabilir; pyserial URL işleyicileri (`socket://`, `rfc2217://`, `spy://`, `loop://`, ...)
port olarak kabul edilmez.

**Eşleme başına TLS:** TLS'li bir dinleyici (TCP sunucu, Modbus gateway) kendi sertifikasını
ve anahtarını sunar; **İstemci CA sertifika dosyası** (`network.tls_client_ca`) verilirse
yalnızca sertifikası bu dosyadaki bir sertifikaya zincirlenen istemcileri alır (stunnel
`verify` ya da Modbus/TCP Security istemcilerinin beklediği karşılıklı TLS) ve her istemcinin
sertifika konusunu loglar; izin listesi yine önce denetlenir. Dışarı bağlanan bir eşleme her
bağlantıda sunucu sertifikasının SHA-256 parmak izini loglar ve onu bir **CA sertifika
dosyasıyla** (`network.tls_ca`: sertifika ona zincirlenmeli ve uzak sunucunun adını
taşımalı) ve/veya bir **sunucu sertifikası parmak iziyle** (`network.tls_fingerprint`;
sertifika değişmeden önce yenisini de listeye ekleyin) denetler; sertifikası ve anahtarı
isteğe bağlıdır ve istemci sertifikası isteyen bir sunucuya sunulur. **CA dosyası ya da
parmak izi olmadan dışarı bağlanan TLS yalnızca şifreler:** ağ yolundaki herkes kendini
sunucu gibi gösterebilir. CA dosyası PEM (bir ya da daha çok sertifika) ya da DER olabilir
ve içindeki her sertifikaya güvenilir, kendinden imzalı bir sunucunun kendi sertifikası da.
Şifreli bir anahtar parolasını ister (**TLS anahtar parolası**, `network.tls_key_password`;
yönetim arayüzü için Ayarlar → Admin TLS altında `admin_ui.tls_key_password`): ser2net
konsolda asla parola sormaz; parola yoksa eşleme, yönetim arayüzünün anahtarındaysa açılış,
bunu söyleyen bir hatayla durur. Form ve REST API bir eşlemenin dosyalarını kaydederken
yükler, kayıtlı anahtar parolasını hiç göstermez (viewer API token'ı maskeli alır) ve
yönetim arayüzünün kendi anahtarını reddeder. Sertifika yönetilemeyen yerlerde güvenilmeyen
ağın önünde bir VPN (WireGuard) ya da stunnel seçenektir.

**Bağlantı sınırları:** TLS'li bir eşlemede `allowed_client_ips` listesinde olmayan bir host,
TLS el sıkışmasından önce kapatılır; el sıkışma 15 sn içinde bitmelidir ve bir adresten aynı
anda en fazla 32 (toplamda 128) el sıkışma yürür. Fazlası, bunlardan birinin bitmesini 15 sn'ye
kadar bekler (bir adresten en fazla 64, toplamda 256 bağlantı); böylece bir istemcinin toplu
yeniden bağlanması reddedilmez, sırayla karşılanır. Kapasite, `kick_old_user` ve "Busy"
satırı yine el sıkışmadan sonra gelir. Yönetim arayüzü, bağlandıktan ya da son yanıttan sonra
`admin_ui.request_timeout_s` (10 sn) içinde tam bir istek başlığı göndermeyen, ardından
istek gövdesi gelmeyi kesen (gövdeye bu süre artı gelen her KiB için bir saniye tanınır) ya
da bir yanıttan bu süre boyunca hiçbir şey almayan bağlantıyı kapatır (konsol WebSocket'i
süreye tabi değildir) ve aynı anda en fazla `admin_ui.max_connections`
(256), bir adresten en fazla `admin_ui.max_connections_per_ip` (64) bağlantı kabul eder;
TLS el sıkışması bir eşleminkiyle aynı sınırlara tabidir. Bu adres başına sınırlarda bir
IPv6 istemci /64 ağıyla sayılır (bir makine /64'ünün her adresini kullanabilir). Bu
anahtarlar yalnızca `config.json`'dadır (0 = sınır yok); bir reverse proxy arkasında tüm
kullanıcılar proxy'nin adresinden gelir, gerekirse adres başına sınırı yükseltin. Süreç açık dosya sınırına
yaklaşınca yeni bağlantılar (her eşlemeye ve arayüze) log'a bir satır yazılarak reddedilir;
böylece seri portlar, log'lar ve yapılandırma ihtiyaç duydukları dosyaları açabilir.

**UDP eşlemeleri:** `allowed_client_ips` her datagram için denetlenir, ama baktığı şey
datagram'ın kaynak adresidir ve ağdaki bir gönderici bu adresi taklit edebilir; yani UDP'de
izin listesi kimlik doğrulama değildir. Yanıt adresi yoksa izinli her gönderici (ya da izinli
bir adresi taklit eden biri) tek bir datagram'la cihazın çıktısını devralır, salt okunur
eşlemede bile; göndericinin her değişimi loga yazılır (en fazla dakikada bir). Çıktıyı doğru
makinede tutmak için bir **yanıt adresi** girin. Güvenilmeyen ağlarda TLS'li bir TCP eşlemesi
ya da bir güvenlik duvarı kullanın.

**MQTT komutları (Modbus edge modu):** yazılabilir noktalar, broker'ın `<topic>/<nokta>/set`
topic'ine ya da Sparkplug B'de `spBv1.0/<group>/NCMD/<node>` ve
`spBv1.0/<group>/DCMD/<node>/<cihaz>` topic'lerine yayın yapmasına izin verdiği her
istemciden değer alır. ser2net bu istemcilerin kimliğini kendisi doğrulamaz ve
`allowed_client_ips` yalnızca Modbus/TCP portunu kapsar; asıl yetkilendirme broker'ın erişim
kurallarıdır (ACL): komut topic'lerine yalnızca çıkışları anahtarlaması gereken istemciler
yayın yapabilsin, broker'a TLS ve kullanıcı adı/parola ile bağlanın. **Salt okunur** her
yazmayı reddeder. MQTT'den yazma alan eşlemeler panoda **CMD**, broker bağlantısında TLS ya
da kullanıcı adı yoksa **CMD ⚠** etiketiyle gösterilir (ayrıca açılışta loga uyarı düşer).
Örneğin `plant/line3` ana topic'i için bir Mosquitto `acl_file`:
```
user ser2net
topic readwrite plant/line3/#
user scada
topic write plant/line3/+/set
topic read plant/line3/#
user historian
topic read plant/line3/#
```

**Parolasız erişim (isteğe bağlı):** yalnızca güvenilir ağlar için, bir admin
**Ayarlar → Web arayüzü erişimi**'nden giriş zorunluluğunu kapatıp ziyaretçilere bir rol
(`viewer` salt-okunur / `operator` / `admin`) verebilir. Oturum açmak yine mümkündür
(kendi rolünüz geçerli olur), RBAC ve CSRF aynen uygulanır, **REST API her zaman
token ister**. Parolasız erişim yalnızca arayüze **IP adresi, `localhost` veya makinenin
kendi adıyla** gelindiğinde geçerlidir — başka bir alan adıyla (ör. DNS-rebinding yapan
kötü niyetli bir sayfa) gelen istekler giriş ekranına yönlendirilir. Ters proxy arkasında
proxy özgün adı `X-Forwarded-Host` ile iletmelidir (iletmiyorsa girişi açık tutun).
LAN cluster'daki **diğer düğümler** yalnızca oturum açınca görünür ve yönetilebilir;
erişim kapatılınca/rol değişince açık konsollar da kapanır. Açıkken arayüzde uyarı
gösterilir ve açılışta loga yazılır.

**Oturumlar:** çıkış yapınca o oturum sunucuda da biter (çerezin bir kopyası da artık
çalışmaz). **Ayarlar → Kullanıcılar**'da admin bir kullanıcının tüm oturumlarını kapatabilir
ya da yerel bir kullanıcıya yeni parola verebilir (bu da oturumlarını kapatır); herkes
*Diğer oturumlarımı kapat* ile kendi diğer oturumlarını kapatabilir. **Ayarlar → Web arayüzü
erişimi → Herkesin oturumunu kapat** yeni bir oturum anahtarı üretip admin'in kendi oturumu
dışındaki tüm oturumları bir anda kapatır; `config.json`'un ya da bir yedeğin kopyası sızmış
olabilirse bunu kullanın (indirilen yedeklerde anahtar yer almaz). LDAP ya da OIDC
kapalıyken o hesapların oturumları geçersizdir.

**Başarısız girişler** adres başına 5 dakikada 8 ile sınırlıdır. all.log her biri için bir
satır tutar, denetim kaydı da bunları kaydeder; hiçbir hesaba ait olmayan bir kullanıcı adı
yazıldığı gibi değil `(unknown user)` olarak kaydedilir (yanlış alana yazılmış bir parola
olabilir). 10 dakikada 20'yi aşan başarısız girişleri denetim kaydı yalnızca sayar ve o 10
dakikanın sonunda tek bir kayıtta toplar; böylece bir deneme seli diğer kayıtları dışarı
itemez. Bir eşlemenin konsolunu ya da hex görünümünü açmak, hesap (açık erişimde: adres),
eşleme ve tür başına 5 dakikada bir kez kaydedilir; 10 dakikada en çok 50 böyle kayıt yazılır,
fazlası aynı şekilde toplanır (all.log her biri için bir satır tutar). 256 karakterden uzun
adlar denenmez. İstek gövdeleri de sınırlıdır: giriş, kurulum ve
çıkış formları 64 KB, yapılandırma içe aktarma 16 MB, geri kalan her şey (formlar, REST API)
2 MB; daha büyüğü HTTP 413 alır.

**LDAP / OIDC grupları → roller:** bir grup ayarı (admin / operator / viewer grubu) tek bir
grubu adlandırır ve yalnızca o grubun adının tamamıyla eşleşir, asla bir parçasıyla değil.
LDAP'ta bu grubun adı (CN'i, ör. `ser2net-admins`), DN'inin baş kısmı
(`cn=ser2net-admins,ou=groups`) ya da tam DN'idir; OIDC'de sağlayıcının gönderdiği değer
ya da onun son parçasıdır (`ser2net-admins`, Keycloak'un `/team/ser2net-admins` değeriyle
ve `CORP\ser2net-admins` ile eşleşir). Büyük/küçük harf fark etmez, Türkçe İ/I/ı/i dahil
(`Bilgi İşlem` = `BİLGİ İŞLEM`). Bir grubun adının yalnızca bir parçası olan ayar
(`ser2net-admins` için `admins` ya da DN'indeki yalnızca bir OU) rol vermez: o kullanıcı
giriş yaptığında all.log'a ayarı ve grubu belirten bir WARNING yazılır, Ayarlar da o bölümü
kaydedene kadar bunu gösterir. Admin'leriniz yalnızca böyle bir gruptan geliyorsa
`ser2net --set-password AD` ile oluşturulan yerel bir admin'le girin (aşağıya bakın) ve
ayarı düzeltin. Grupları artık rol vermeyen bir dizin kullanıcısının bir sonraki girişi,
hâlâ açık olan oturumlarını da kapatır. `{username}@corp.local` ya da `CORP\{username}`
ile AD doğrudan bind'de, grupları okumak için hesap kullanıcı arama tabanında (boşsa
kendi domain'inin tamamında) userPrincipalName / sAMAccountName ile bulunur; *İç içe
grupları da say* (yalnızca AD) kullanıcının başka gruplar üzerinden üyesi olduğu grupları
da ekler.

**LDAP / OIDC hesapları:** dizinden (LDAP) ya da kimlik sağlayıcıdan (OIDC) ilk kez giriş
yapan kişiye burada parolasız bir hesap açılır; rolü her girişte gruplarından yeniden
belirlenir — son admin rolünü elinden alsa bile (all.log o zaman uyarır; `ser2net
--set-password AD` yerel bir admin oluşturur). Yerel hesaplar önce gelir: adı (büyük/küçük
harf fark etmeksizin) bir yerel hesabın adı olan bir LDAP/OIDC girişi reddedilir, all.log'a
ve denetim kaydına (`ldap_login_denied` / `oidc_login_denied`) yazılır; böyle bir giriş
yerel hesabı asla devralmaz, SSO hesabına çevirmez ya da rolünü düşürmez. Birini yerel
hesaptan SSO'ya taşımak için önce yerel hesabını silin (başka bir admin kalmalı). LDAP ile
OIDC arasında da aynısı geçerli: öbür yöntemin bir hesabının adıyla yapılan giriş reddedilir
(ikisi farklı kişiler olabilir); kişiyi taşımak için o hesabı silin. Dizinler
gibi ser2net de LDAP adlarında büyük/küçük harfe bakmaz (Türkçe İ/ı dahil): `hkilic`,
`Hkilic` ve `HKILIC` tek hesaptır; eski sürümlerin her yazılış için açtığı hesaplar o
kişinin bir sonraki girişinde birleşir (diğer yazılışların oturumları kapanır), *Oturumları
kapat* ve *Sil* hepsine uygulanır. OIDC adları birebir karşılaştırılır. Yeni bir yerel
hesabın adı mevcut bir adla yalnızca büyük/küçük harfte ayrılamaz. LDAP/OIDC adı
yazdırılabilir her metin olabilir (`CORP\jdoe`, `jdoe@corp.local`, `/` ya da `#` içeren
adlar; bu hesaplar da Kullanıcılar panelinden kapatılıp silinebilir); denetim karakteri ya
da görünmez karakter içeren, 255 karakterden uzun ya da parantez içindeki bir adla giriş
reddedilir (nedeni all.log'da).

**Kullanılmayan LDAP / OIDC hesapları:** **Ayarlar → Kullanıcılar** her hesabın son girişini
gösterir (denetim kaydından ve o andan sonraki girişlerden; config.json'a yazılmaz).
Kullanılmayan LDAP/OIDC hesaplarının kaldırılması **varsayılan olarak kapalıdır**: giriş
yapmayı bırakan bir hesap, siz silene kadar kalır. Açmak için aynı yerde *Giriş yapılmayan
LDAP / OIDC hesaplarını kaldırma süresi (gün)* değerini girin (`external_users_expire_days`:
30–3650 gün; 0 = kapalı): o kadar süredir giriş yapmamış LDAP/OIDC hesapları o zaman hemen,
açılışta ve her saat kaldırılır, her biri all.log'a ve denetim kaydına (`user_expired`)
yazılır. Admin'ler, yerel hesaplar ve son kalan hesaplar hiçbir zaman kaldırılmaz; denetim
kaydında hiç girişi olmayan bir hesap ise ancak kayıt o türden girişler için o kadar geriye
gidiyorsa kaldırılır. Bir LDAP/OIDC hesabının silinmesi ya da kaldırılması oturumlarını
hemen kapatır, ama erişime dizin karar verir: eşlenen bir grup ya da varsayılan rol geçerli
oldukça kişinin bir sonraki girişi yeni bir hesap oluşturur.

**OIDC (SSO):** kod isteği PKCE (S256) taşır. Sağlayıcının keşif belgesinde verdiği tüm
adresler (yetkilendirme, token, JWKS) ve yoldaki yönlendirmeler https:// olmalıdır (http://
yalnızca bu makinenin loopback adresine): TLS sonlandıran bir proxy arkasındaki Keycloak
http:// adresler bildirirse SSO girişi reddedilir ve all.log nedenini yazar (Keycloak'ın
proxy / frontend URL ayarlarını düzeltin), çünkü client secret, kod ve imza anahtarları açık
metin giderdi. Sağlayıcı kod yerine bir hatayla dönerse (ör. kullanıcı uygulamaya atanmamış:
`access_denied`) hata ve açıklaması all.log'a yazılır, kullanıcı hata koduna göre bir mesaj
görür; token endpoint'inin hata yanıtları da loglanır. Sağlayıcının doğrulanmamış dediği
(`email_verified: false`) bir e-posta adresi kullanıcı adı olamaz. Keşif belgesindeki issuer
yapılandırılandan farklıysa all.log bir kez uyarır. id_token'ın zamanları (iat / nbf / exp)
2 dakikalık saat farkıyla kabul edilir: bu sunucunun ve sağlayıcının saatlerini senkron tutun
(NTP); bir token zamanı yüzünden reddedilirse all.log bu sunucunun saatinin sağlayıcınınkinden
kaç saniye geri ya da ileri olduğunu yazar.

**TLS üzerinden LDAP (LDAPS / StartTLS):** dizin sunucusunun sertifikası denetlenir.
Sertifika sistemin güvendiği bir CA'dan gelmeli (Linux / Docker: CA paketi; Windows: sertifika
deposu, domain üyesi bir makinede AD CA'sı genellikle zaten oradadır) ve sunucu URI'sindeki
host adını içermelidir: sertifikanın verildiği adı kullanın (`ldaps://dc01.corp.local`; IP
adresini yalnızca sertifika o adresi içeriyorsa). Kendi CA'nız (Active Directory Certificate
Services) ya da self-signed bir sertifika için **Ayarlar → LDAP → CA sertifika dosyası**'na o
CA'nın sertifikasını girin: PEM ya da Windows'un dışa aktardığı DER (Docker: dosyayı `/data`
volume'una koyun); o zaman yalnızca bu dosyadaki sertifikalara güvenilir. Kabul edilmeyen bir
sertifika, nedeni ve ne ayarlanacağıyla all.log'a yazılır, Ayarlar da gösterir; o bağlantı
üzerinden hiçbir şey gönderilmez. *Sunucu sertifikasını denetleme* denetimi kapatır, bu
güvensizdir: ser2net ile dizin arasındaki ağdaki herkes parolaları okuyup herhangi biri olarak
giriş yapabilir. Kapalıyken, ayrıca parolalar şifrelenmeden gidiyorsa (StartTLS olmadan
`ldap://`) Ayarlar ve açılış logu uyarır. TLS üzerinden ser2net başka bir sunucuya
yönlendirmeyi (referral) izlemez (o sunucunun sertifikası denetlenmeden kalırdı): URI'yi
kullanıcıları ve gruplarını tutan bir sunucuya yöneltin. **2.8 ya da daha eski bir sürümden
yükseltince**, sertifikaya güvenilmiyorsa LDAP girişi durur: CA sertifika dosyasını ayarlayın
(yerel hesaplar çalışmaya devam eder; yerel admin yoksa `ser2net --set-password AD` ile bir
tane oluşturun).

**Parola unutuldu, giriş yapılamıyor, tarayıcı yok (hesap kurtarma):**
`ser2net --set-password AD`, yerel `AD` hesabına yeni parola verir (terminalde iki kez
sorulur; pipe ile verilirse stdin'in ilk satırı okunur) ve o hesabın tüm oturumlarını
kapatır. Henüz olmayan bir ad **admin** olarak oluşturulur; tek admin bir LDAP/OIDC hesabıysa
ve dizine ulaşılamıyorsa geri dönüş yolu budur. `ser2net --sign-out-all` web arayüzündeki
tüm oturumları kapatır. İkisi de ser2net dururken de çalışırken de çalışır: çalışan örnek
değişikliği hemen uygular, yeniden başlatmak gerekmez. Yapılandırmada başka hiçbir şey
değişmez, önceki `config.json` `backups/` içinde kalır, işlem denetim kaydına yazılır, gizli
hiçbir şey ekrana ya da loga yazılmaz. Servisle aynı `--data-dir`'i kullanın ve komutu
ser2net'in çalıştığı hesapla çalıştırın (root ile çalıştırılırsa kendiliğinden veri dizininin
sahibine geçer):

| Kurulum | Komut |
|---|---|
| kaynak / Linux binary (systemd) | `sudo -u ser2net python3 /opt/ser2net/ser2net.py --data-dir /opt/ser2net/data --set-password admin` (binary: `./ser2net-linux-x64 --set-password admin`) |
| Windows (yönetici komut istemi; servis çalışırken de) | `ser2net-windows-x64.exe --data-dir C:\ProgramData\ser2net --set-password admin` (servisin veri klasörü: `sc qc ser2net` gösterir) |
| Docker | `docker exec -it ser2net python /app/ser2net.py --no-bootstrap --data-dir /data --set-password admin` |

**Tarayıcısız ilk admin:** ilk açılıştan önce `ser2net --set-password admin` çalıştırın
(`config.json`'u oluşturur; `--reconfigure` ya da `SER2NET_BIND_IP` aksini söylemedikçe
arayüz `127.0.0.1:8080`'de dinler) ya da Docker'da `SER2NET_ADMIN_PASSWORD_FILE`'ı parolayı
içeren bir dosyaya (Docker secret) yönlendirin; ad için `SER2NET_ADMIN_USER` (varsayılan
`admin`). Değişken yalnızca hiç hesap yokken kullanılır, sonraki açılışlar onu yok sayar.

### LAN cluster

- **İmzalı istekler:** düğümler arasındaki her istek cluster anahtarıyla (ondan türetilen
  bir anahtarla) imzalanır — yöntem, yol, gövde, iki düğümün kimliği, zaman ve tek
  kullanımlık bir nonce — her yanıt da öyle. Bu düğümler arasında anahtarın kendisi hiç
  gönderilmez, ağda yakalanan bir istek (aynı ya da başka bir düğüme) yeniden gönderilemez
  ve bir yanıt değiştirilemez. Düğümlerin saatlerinin aynı olması gerekmez: bir düğüm her eşin saatini
  onun imzalı beacon'larından ve yanıtlarından öğrenir. Reddedilen istekler adres ve
  değişiklik başına bir kez loga (`cluster: refused …`) ve denetim kaydına
  (`cluster_auth_failed`) yazılır.
- **Eski düğümler (eski kimlik doğrulama):** 2.8.0 ya da daha eski sürümle çalışan
  düğümler yalnızca ham anahtarı bilir ve onu her istekte gönderir. *Eski kimlik doğrulama*
  işaretliyken (Ayarlar → LAN cluster; kademeli yükseltmede filo bölünmesin diye varsayılan
  olarak açık) bir düğüm bu anahtarı kabul eder ve yalnızca o düğümlerle kullanır: beacon'ı
  daha eski olduğunu söyleyenler (beacon'ın bildirdiği adreste) ve imzalı bir isteği eski
  sürümlerin yaptığı gibi reddeden manuel eşler (imzalı bir isteğe yanıt vermiş bir eş asla).
  LAN adresini bildirmeyen eski bir düğüm (varsayılan rota yok: beacon'ı 127.0.0.1 diyor) diğer
  düğümlerden ancak orada manuel eş olarak eklenince yönetilir; filo görünümü bunu belirtir.
  Filo görünümü bu düğümleri **imzasız anahtar** etiketiyle gösterir ve bir uyarıda listeler.
  **Tüm düğümler yükseltilince bu işareti her düğümde kaldırın.**
- **Anahtar:** ağdaki herkes zayıf bir anahtarı beacon'lardan çevrimdışı tahmin etmeyi
  deneyebilir; anahtar ise bir düğüme diğer tüm düğümlerin eşlemelerini başlatma, durdurma ve
  değiştirme yetkisi verir: en az 16 rastgele karakter kullanın (*Yeni anahtar üret ve
  kaydet* bir tane üretir ve bir kez gösterir). Ayarlar sayfası kayıtlı anahtarı hiç
  göstermez; boş bırakılan alan anahtarı korur.
- **Anahtarı değiştirmek:** *planlı* — her düğümde yeni anahtarı girin (ya da birinde üretip
  kopyalayın), *mevcut anahtarı 7 gün daha kabul et* işaretli kalsın; henüz değiştirilmemiş
  düğümler çalışmayı sürdürür ve filo görünümünde **önceki anahtar** etiketiyle görünür. Tüm
  düğümlere girilince her birinde *Önceki anahtarı artık kabul etme*'yi işaretleyin.
  *Sızdıysa* — *kabul et* işaretini kaldırın, böylece eski anahtar hemen reddedilir, ve
  anahtarı her düğümde değiştirin; o zamana kadar filo görünümü değiştirilmemiş düğümler için
  **cluster anahtarı reddedildi** gösterir, bir uyarı da farklı bir anahtarla yayın yapan
  düğümleri listeler. Yalnızca manuel eşlerin kullanıldığı yerlerde *otomatik keşif*
  kapatılabilir.

---

## ⚙️ Servis olarak çalıştırma

- **Docker:** resmi `Dockerfile` + `docker-compose.yml` (bkz. yukarısı ve
  [`docs/DOCKER.md`](docs/DOCKER.md)) — `restart: unless-stopped`, `init: true` ve
  `stop_grace_period: 30s` (durdurma isteği ser2net'e ulaşır, ser2net bu süre içinde
  kapanır), `/data` volume. Düz `docker run` ile çalıştırıyorsanız
  `--restart unless-stopped --init --stop-timeout 30` ekleyin (var olan bir container için:
  `docker update --restart unless-stopped ser2net`).
- **Linux (systemd):** `systemd/ser2net.service` — ayrı, yetkisiz bir kullanıcı,
  `SupplementaryGroups=dialout`, `Restart=on-failure`, sertleştirme direktifleri. Arch,
  Manjaro ve EndeavourOS'ta seri grup `uucp`'dir ve `dialout` grubu yoktur (unit
  `status=216/GROUP` ile başlamaz): grubu `systemctl edit ser2net` ile ayarla (`[Service]`,
  `SupplementaryGroups=`, ardından `SupplementaryGroups=uucp`; unit'in yorumunda yazıyor).
  **root ile çalıştırmayın.**
- **Windows:** ek bir program gerekmez. Yönetici olarak açılmış bir komut isteminde:
  ```bat
  ser2net-windows-x64.exe --install-service
  ```
  Exe kendini `C:\Program Files\ser2net\` altına kopyalar (yalnızca yöneticilerin
  değiştirebildiği bir yer; böylece sıradan bir kullanıcı, servisin SYSTEM olarak
  çalıştırdığı dosyayı değiştiremez) ve verisini `C:\ProgramData\ser2net\` altında tutar
  (ikisini de yalnızca SYSTEM ve Administrators değiştirebilir).
  Servis açılışta başlar, kullanıcı oturumu gerektirmez (LocalSystem), çökerse ya da
  hatayla çıkarsa 5 sn sonra yeniden başlar; arayüz adresi ilk kurulumda konsolda sorulur.
  Kaldırmak: `--uninstall-service` (veri klasörü kalır); birden fazla örnek için
  `--service-name`. Durdur/başlat: `sc stop ser2net` / `sc start ser2net` ya da *Hizmetler*
  (services.msc). Yeni bir exe ile `--install-service`'i yeniden çalıştırmak servisi
  yerinde yükseltir ve servisin kullandığı veri klasörünü korur (2.8.0 servisinde de).
  Başka hostlardan erişmek için gereken güvenlik duvarı kuralı, adres
  değişimi, yükseltme ve imzasız derlemelerdeki SmartScreen uyarısı için
  **[`docs/WINDOWS.md`](docs/WINDOWS.md)**.
- **Açılış sırası:** önce yönetim arayüzü hazırlanır. TLS çifti yüklenemezse açılış, hiçbir
  seri port açılmadan durur (nedeni `all.log`'da); böylece yeniden başlatma politikası
  DTR'ye duyarlı kartları döngüde resetleyemez. Arayüzün portu eşlemeler başlamadan
  alınır: arayüzün adres ve portundaki bir eşleme arayüzü engellemek yerine `error` gösterir.
  Arayüzün kendi adresi kullanılamazsa (portu başka bir program tutuyor ya da IP açılışta
  henüz atanmamış) eşlemeler yine başlar, arayüz en fazla 30 sn arayla yeniden dener
  (`all.log` bunu yazar). Arayüzün TCP adres ve portuna bir eşleme, keşif açıkken cluster'ın
  keşif portuna da bir UDP eşleme kaydedilemez (form, REST API, cluster, içe aktarma; kopyanın
  portu bunları atlar).
- **Çok sayıda eşleme:** çalışan bir eşleme yaklaşık üç dosyayı açık tutar (seri portu,
  dinleyicisi ve log'u), bağlı her istemci için bir tane daha. ser2net açılışta yumuşak
  açık dosya sınırını sert sınıra doğru yükseltir (en fazla 65536) ve sonucu log'a yazar
  (`open files: …`); systemd unit'i `LimitNOFILE=65536` ayarlar, Docker'daki düşük bir
  sert sınır `--ulimit nofile=65536` ile yükseltilir. Yapılandırılan eşlemeler sınıra
  yaklaşınca açılış log'unda bir uyarı çıkar.
- **Windows'ta çok sayıda bağlantı:** ser2net'in orada kullandığı event loop (`select()`) en
  fazla 512 soketi izler (dinleyiciler, istemciler, arayüz bağlantıları). Bu sınıra
  yaklaşınca yeni bağlantılar log'a bir satır yazılarak reddedilir ve servis çalışmaya
  devam eder. `SER2NET_WINDOWS_LOOP=proactor` ortam değişkeni (servis için: sistem ortam
  değişkeni) böyle bir sınırı olmayan proactor loop'una geçirir; yeni olduğundan,
  güvenmeden önce seri zamanlamayı (ör. Modbus RTU) onunla deneyin. Kullanılan loop açılış
  log'unda yazar.
- **Windows'ta seri yazmalar** Linux'taki gibi cihazı bekler: port başına sürücüye bir anda en
  fazla 8 KiB verilir, böylece ağdan hızlı gönderen biri sürücünün kuyruğunu doldurmak yerine
  seri hattın hızına yavaşlatılır. Bir saniyedir trafiği olmayan bir port 0,5 ms yerine
  10 ms'de bir yoklanır, veri gelir ya da gider gitmez yeniden 0,5 ms'de bir (bir isteğin
  yanıtı gecikmez). Bir seri sürücü bununla sorun çıkarırsa
  `SER2NET_WINDOWS_SERIAL=pyserial` (servis için: sistem ortam değişkeni) pyserial'ın kendi
  yazma biçimine (hız denetimi olmadan) döner; lütfen adaptörü ve sürücüyü bildirin.

---

## 🗂️ Yapılandırma & durum dosyaları

Tüm durum **veri dizininde** (varsayılan `data/`). Veri dizini yalnızca sahibe açık
kilitlenir (POSIX `0700`; Windows'ta `icacls` ile sahip/SYSTEM/Administrators), böylece
içindeki dosyalar başka kullanıcılarca okunamaz:
- `config.json` — admin IP, parola hash'i, tüm eşlemeler (atomik yazım).
- `backups/` — her değişiklikten önce `config.json`'un önceki hâli (son 50). Ayarlar →
  *Yapılandırma yedekleri*'nden indirilir ya da geri yüklenir. Geri yükleme önce giriş ve
  erişim için neyin değişeceğini gösterir, sonra eşlemeleri ve ayarları geri getirir;
  kullanıcı hesapları ve parolaları, REST API token'ı ve cluster anahtarı, onları da geri
  yüklemek için kutu işaretlenmedikçe olduğu gibi kalır (işaretlenirse diğer herkesin
  oturumu da kapanır). *Yalnızca eşlemeleri* içe aktarma önizlemesinden geçer. Geri yükleme
  de geri alınabilir; indirilen yedekte oturum anahtarı yer almaz.
- `all.log` — global etkinlik (boyutla döner); `audit.log` — girişler, config değişiklikleri,
  konsol / hex görünümü açılışları ve trace indirmeleri: kim/nereden/ne (döner). Ayarlar →
  *Audit log*'da aranabilir, CSV olarak indirilebilir.
- `logs/<id>.log` — eşleme başına geçmiş (saatlik bakım: >15 gün veya >100MB kırpılır).
- `traces/<id>-<ad>` — eşlemenin trafik kaydı (trace). Trace dosyaları **yalnızca burada**
  tutulur; boyut sınırına gelince döner (varsayılan 10 MB × 3 eski dosya). v2.7'deki tam
  yollar dosya adına indirgenir ve açılışta log'a yazılır.
- `tls/` — self-signed üretilirse sertifika/anahtar. Göreli bir TLS yolu (yönetim arayüzü
  ya da eşleme: sertifika, anahtar, CA dosyaları) veri klasöründeki bir dosyadır, örneğin
  `tls/cert.pem`; böylece veri klasörü taşınınca da çalışır. Taşımadan ya da başka bir
  makinede geri yüklemeden sonra (yerelden Docker `/data`'ya, başka bir `--data-dir`,
  başka bir host'tan yedek) artık orada olmayan ve bir `tls` klasörünü gösteren mutlak bir
  yol bu veri klasörünün `tls/` klasöründen alınır (açılışta loglanır, config.json yeni
  yolla kaydedilir); taşımadan önceki yedekler ve içe aktarmalar da aynı şekilde ele alınır.
- `setup-code.txt` — ilk kurulumun bir host adıyla yapılması için tek kullanımlık kod; yalnızca
  ilk admin hesabı oluşana kadar durur.

Eşleme ID'leri (yukarıdaki `<id>`; REST API'de de kullanılır) 1–64 harf, rakam ya da `_`
içerir; yeni eşlemeler 12 hex haneli bir ID alır. Başka biçimdeki bir ID (elle düzenlenmiş
ya da içe aktarılmış) config yüklenirken ya da dosya içe aktarılırken değiştirilir; açılış
logu ya da içe aktarma önizlemesi bunu belirtir.

`config.json` + `all.log` silinince sistem **sıfırdan** başlar (orphan loglar otomatik
temizlenir; `backups/` kendiliğinden geri yüklenmez, isterseniz onu da silin). Parola
unutulduysa bunun yerine `--set-password` kullanın (bkz. Güvenlik): başka hiçbir şey kaybolmaz.
`admin-requests/` yalnızca komut satırından gelen bir isteği, çalışan örnek alana kadar tutar.

**Bir kurulumu kopyalamak** (disk ya da VM imajı, kopyalanan veri klasörü ya da Docker
volume'u): `config.json` düğümün kimliğini tutar — cluster ID'sini (`instance_id`) ve
oturumları imzalayan anahtarı (`secret_key`). Kopyalar cluster'da birbirini gizler ve
birinde açılan oturum diğerinde de geçerli olur. İmajı almadan önce bu iki girdiyi
`config.json`'dan silin (bir sonraki açılışta yenileri üretilir). Zaten çalışan kopyalar
log'da, filo görünümünde ve Ayarlar → LAN cluster'da bildirilir; orada *Bu düğüme yeni bir
kimlik ver* kopyalardan birine yeni bir ID ve oturum anahtarı verir (o düğümde oturum açmış
herkesin oturumu kapanır).

**Oturum süresi:** Ayarlar → *Web arayüzü erişimi* → *Oturum süresi (dakika)* (varsayılan 8 saat).
**İçe aktarma:** önce önizleme gösterilir; *birleştir* (aynı adlı eşlemeleri güncelle,
yenileri ekle) ya da *hepsini değiştir* seçilir.

---

## 🧪 Test

Birleşik test koşucusu — taşınabilir (donanımsız, socat'sız) takım her işletim
sisteminde çalışır:
```bash
python3 tests/run_all.py            # taşınabilir takım (Windows + Linux)
python3 tests/run_all.py --socat    # + socat tabanlı veri-yolu testleri (Linux)
python3 tests/run_all.py -v         # geçen testlerin çıktısını da göster
```
socat (Linux) ile sanal seri portlar kullanılır; donanım gerekmez. Tek tek de
çalıştırılabilir, örn. `python3 tests/test_rest_api.py`. CI (GitHub Actions) her pull
request'te ruff, mypy, `pip-audit`, çevrimdışı wheel kontrolü, tam test matrisi
(ubuntu/windows × Python 3.10–3.13) ve coverage koşar
(`python3 tests/coverage_run.py`: testlerin başlattığı sunucular da ölçülür). Geliştirme
ortamı ve kurallar için [CONTRIBUTING.md](CONTRIBUTING.md).

`tests/test_dead_peer.py` ölü bağlantı tespitini **gerçek yarı-açık (half-open) TCP
bağlantılarıyla** da sınar: `SER2NET_BLACKHOLE_TESTS=1` verildiğinde bir loopback
akışının paketlerini düşürür (Linux: root/şifresiz sudo + `iptables`; Windows: Yönetici
+ `pip install pydivert`). Güvenlik duvarını değiştirdiği için yerelde isteğe bağlıdır
(Linux'ta tek kullanımlık bir network namespace içinde çalıştırmak önerilir); CI'da
her iki işletim sisteminde de her zaman koşar.

`tests/test_ui_layout.py` web arayüzünü gerçek bir tarayıcıda (Playwright + Chromium)
çok uzun eşleme adları ve cihaz yollarıyla, masaüstünden telefona farklı genişliklerde
açar. Panelinin dışına taşan bir şey olursa başarısız olur
(`pip install playwright && python -m playwright install chromium`; CI'da `ui` işi).
`tests/test_standalone.py` derlenmiş bir binary'yi sınar
(`SER2NET_STANDALONE_BIN=dist/ser2net`). CI'da bunu `standalone.yml` workflow'u yapar;
her sürüm de aynı workflow'dan geçer.

---

## 📜 Lisans

ser2net **çift lisanslıdır**:

- **Açık kaynak — [GNU AGPL-3.0](LICENSE):** kullanmak, incelemek, değiştirmek ve paylaşmak serbesttir. Dağıtırsan ya da değiştirilmiş bir sürümü ağ servisi olarak çalıştırırsan, tüm kaynak kodunu AGPL-3.0 ile sunman gerekir.
- **Ticari lisans:** AGPL-3.0 yükümlülükleri olmadan kapalı kaynak ürün, cihaz ya da SaaS için — bkz. [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md) veya **mail@haliskilic.com.tr**.

Birlikte gelen üçüncü-taraf bileşenler kendi lisanslarını korur — bkz. [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md);
binary'deki tam metinler: `ser2net --licenses`. LGPL lisanslı ldap3 binary'de kaynak
dosyaları olarak durur; `SER2NET_LIB_OVERRIDE=<klasör>` ile başka bir sürümü yüklenebilir.
Güvenlik açığı bildirimi: [SECURITY.md](SECURITY.md).

Katkılar memnuniyetle karşılanır! İlk pull request'inde tek seferlik bir [Katkıcı Lisans Sözleşmesi (CLA)](https://gist.github.com/haliskilic/7a7fdcd355f54d1db6943959d7b76823) imzalaman istenir; katkının telif hakkı sende kalır.

Telif © 2026 Halis KILIÇ.

## ❤️ Projeyi destekle

ser2net işine yarıyorsa **[GitHub Sponsors üzerinden destek olmayı ❤️](https://github.com/sponsors/haliskilic)** düşünebilirsin — sponsorluklar test donanımını ve projenin bakımına ayrılan zamanı karşılıyor.

GitHub hesabın yoksa **[bir kahve ısmarlayabilirsin ☕](https://buymeacoffee.com/haliskilic)**.

Yol haritası: [`ROADMAP.md`](ROADMAP.md) · English: [`README.en.md`](README.en.md)
