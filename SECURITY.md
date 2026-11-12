# Security policy

[English](#english) · [Türkçe](#türkçe)

## English

### Supported versions
Security fixes go into the **latest release** (the newest `vX.Y.Z` on the
[Releases](https://github.com/haliskilic/ser2net/releases) page). Please upgrade before
reporting; an upgrade from any 2.x release keeps `config.json` as it is.

### Reporting a vulnerability
**Please don't open a public issue.** Report it privately, either way:
- GitHub: **Security → Report a vulnerability** (private advisory):
  <https://github.com/haliskilic/ser2net/security/advisories/new>
- e-mail: **mail@haliskilic.com.tr**

Include the version (`ser2net --about`), how it is installed (binary, Docker, source),
the steps to reproduce and the impact you see. You will get an answer and we will agree
on a disclosure date with you; please give a fix time before going public. Reporters
are credited in the release notes unless they prefer otherwise.

### Scope
In scope: the web UI and its sign-in (local accounts, LDAP, OIDC), roles, CSRF and
session handling, the REST API and its token, the console WebSocket, the LAN cluster
endpoints and key, the data bridges (access lists, TLS), the MQTT / Sparkplug commands of
Modbus mappings, config/backup/trace files, the standalone executables, the Docker image and
the release pipeline.

Not vulnerabilities by themselves: a setting an admin chose deliberately (e.g. login
switched off on a trusted network, plain HTTP, `0.0.0.0` binding), or findings that
need an attacker who already controls the host or the admin account.

### Hardening checklist
See the README's **Security** section: bind the UI to `127.0.0.1` or enable TLS, keep
login required, give people the least role they need, use access lists on the data
ports, keep the cluster key secret, and let only trusted clients publish to the MQTT
command topics of writable Modbus points (broker ACLs; TLS and credentials for the broker
connection): ser2net does not authenticate MQTT commands itself. On a UDP mapping the
access list checks a source address that can be forged: set a reply address, and on
untrusted networks prefer TCP with TLS, or a firewall.

### Stored secrets
`config.json` keeps in plain text what ser2net has to use itself: the LDAP bind password,
the OIDC client secret, the SMTP password, the webhook URL, each mapping's MQTT broker
password, the cluster key and the session key (`secret_key`) that signs sign-ins. Local
passwords are scrypt hashes; the REST API token is stored only as a hash. `backups/` holds
earlier copies of the file (a downloaded backup leaves out the session key), and the
mappings export (operator role) holds the MQTT passwords. On the host the data folder is
owner-only (POSIX `0700`, files `0600`; Windows: owner, SYSTEM and Administrators), so what
needs care are the copies: a copied data folder or Docker volume, a disk or VM image, a
downloaded backup, an export. Keep them like a password vault. If one leaked:
- change the third-party credentials where they are issued — the LDAP service account's
  password, the OIDC client secret, the SMTP password, the webhook URL (a new one from the
  chat service), the MQTT broker accounts — and enter the new ones in ser2net;
- set a new cluster key on every node (Settings → LAN cluster → *Generate a new key and
  save*, with "keep accepting the current one for 7 days" unticked);
- Settings → Web UI access → *Sign out everyone*: a new session key, so a sign-in made
  with the old one stops working;
- change the passwords of local accounts that could be guessed (their hashes are in the
  copy).

### Browser protections
The web UI sends a Content-Security-Policy that allows scripts only from its own files
(no inline scripts or event handlers, no `eval`), no `<base>`, no plugins, no framing, and
forms that post only to the UI itself. Inline *styles* are allowed (`style-src
'unsafe-inline'`): the console's xterm.js renderer adds `<style>` elements and style
attributes at runtime and has no nonce support (upstream issue xtermjs/xterm.js#4445);
this goes back to a strict `style-src` once a vendored xterm.js no longer needs it. htmx
runs with `eval`, `<script>` elements in fragments and its injected indicator styles off.
Every response but the static files is sent with `Cache-Control: no-store`, so pages that
show a secret (a new API token or cluster key, the mapping form, a backup or the export)
are kept neither in the browser's cache nor for its Back button. A change the browser
says a page of another site sent (`Sec-Fetch-Site`, `Origin`; another port of the same host
counts) is refused before its CSRF token is looked at, and the token is renewed at every
sign-in and sign-out. With admin TLS, responses carry `Strict-Transport-Security` for one
year by default; since it covers every port of the host name, Settings → Admin TLS →
*HSTS max-age* changes how long (0 sends `max-age=0`, which makes browsers forget it).
Behind a TLS reverse proxy the cookies are Secure too (`X-Forwarded-Proto: https`), HSTS
goes through it only when Settings → Reverse proxy asks for it, and `X-Forwarded-For` is
believed only from the proxies listed there (none by default), so a client can't choose
the address the sign-in limits and the audit log see ([docs/REVERSE-PROXY.md](docs/REVERSE-PROXY.md)).

## Türkçe

### Desteklenen sürümler
Güvenlik düzeltmeleri **son sürüme** gelir
([Releases](https://github.com/haliskilic/ser2net/releases) sayfasındaki en yeni
`vX.Y.Z`). Bildirmeden önce lütfen güncelleyin; 2.x'in herhangi bir sürümünden
yükseltmede `config.json` olduğu gibi kalır.

### Güvenlik açığı bildirme
**Lütfen herkese açık issue açmayın.** Özel olarak bildirin:
- GitHub: **Security → Report a vulnerability** (özel bildirim):
  <https://github.com/haliskilic/ser2net/security/advisories/new>
- e-posta: **mail@haliskilic.com.tr**

Sürümü (`ser2net --about`), kurulum şeklini (binary, Docker, kaynak), tekrarlama
adımlarını ve gördüğünüz etkiyi yazın. Size dönüş yapılır ve yayımlama tarihi sizinle
birlikte belirlenir; herkese açıklamadan önce düzeltme için süre tanıyın. İstemediğiniz
sürece adınız sürüm notlarında anılır.

### Kapsam
Kapsamda: web arayüzü ve girişi (yerel hesaplar, LDAP, OIDC), roller, CSRF ve oturum
yönetimi, REST API ve token'ı, konsol WebSocket'i, LAN cluster uç noktaları ve anahtarı,
veri köprüleri (erişim listeleri, TLS), Modbus eşlemelerinin MQTT / Sparkplug komutları,
config/yedek/trace dosyaları, standalone binary'ler, Docker imajı ve sürüm hattı.

Tek başına açık sayılmayanlar: bir admin'in bilerek seçtiği ayarlar (ör. güvenilir ağda
girişin kapatılması, düz HTTP, `0.0.0.0`'a bağlama) ya da saldırganın makineyi veya admin
hesabını zaten ele geçirmiş olmasını gerektiren bulgular.

### Sağlamlaştırma
README'nin **Güvenlik** bölümüne bakın: arayüzü `127.0.0.1`'e bağlayın ya da TLS açın,
girişi zorunlu tutun, herkese gereken en düşük rolü verin, veri portlarında erişim
listesi kullanın, cluster anahtarını gizli tutun ve yazılabilir Modbus noktalarının MQTT
komut topic'lerine yalnızca güvendiğiniz istemcilerin yayın yapmasına izin verin (broker
ACL'leri; broker bağlantısı için TLS ve kimlik bilgisi): ser2net MQTT komutlarının
kimliğini kendisi doğrulamaz. UDP eşlemelerinde erişim listesi taklit edilebilen bir kaynak
adresine bakar: bir yanıt adresi girin, güvenilmeyen ağlarda TLS'li TCP'yi ya da bir
güvenlik duvarını tercih edin.

### Saklanan gizli değerler
`config.json`, ser2net'in kendisinin kullanması gereken gizli değerleri açık metin tutar:
LDAP bind parolası, OIDC client secret, SMTP parolası, webhook URL'si, her eşlemenin MQTT
broker parolası, cluster anahtarı ve oturumları imzalayan oturum anahtarı (`secret_key`).
Yerel parolalar scrypt hash'i olarak, REST API token'ı yalnızca hash'i olarak saklanır.
`backups/` dosyanın önceki kopyalarını tutar (indirilen bir yedekte oturum anahtarı yoktur),
eşleme dışa aktarımı (operator rolü) da MQTT parolalarını içerir. Makinede veri klasörü
yalnızca sahibine açıktır (POSIX `0700`, dosyalar `0600`; Windows: sahip, SYSTEM ve
Administrators), bu yüzden dikkat isteyen kopyalardır: kopyalanan bir veri klasörü ya da
Docker volume'u, bir disk ya da VM imajı, indirilen bir yedek, bir dışa aktarım. Bunları
bir parola kasası gibi koruyun. Biri sızarsa:
- üçüncü taraf kimlik bilgilerini verildikleri yerde değiştirin — LDAP servis hesabının
  parolası, OIDC client secret, SMTP parolası, webhook URL'si (sohbet servisinden yenisi),
  MQTT broker hesapları — ve yenilerini ser2net'e girin;
- her düğümde yeni bir cluster anahtarı ayarlayın (Ayarlar → LAN cluster → *Yeni anahtar
  üret ve kaydet*; "mevcut anahtarı 7 gün daha kabul et" kutusu işaretsiz);
- Ayarlar → Web arayüzü erişimi → *Herkesin oturumunu kapat*: yeni bir oturum anahtarı
  üretir, eskisiyle açılmış oturumlar çalışmaz olur;
- tahmin edilebilecek yerel hesap parolalarını değiştirin (hash'leri kopyada).

### Tarayıcı tarafı korumalar
Web arayüzü, betiklere yalnızca kendi dosyalarından izin veren bir Content-Security-Policy
gönderir (satır içi betik ya da olay işleyici yok, `eval` yok); `<base>`, eklenti ve
çerçeveleme yasaktır, formlar yalnızca arayüzün kendisine gönderilebilir. Satır içi
*stillere* izin verilir (`style-src 'unsafe-inline'`): konsoldaki xterm.js çizicisi
çalışırken `<style>` öğeleri ve style öznitelikleri ekliyor ve nonce desteği yok (upstream
sorunu xtermjs/xterm.js#4445); gömülü xterm.js buna ihtiyaç duymadığında katı `style-src`'ye
dönülecek. htmx'te `eval`, parçalardaki `<script>` öğeleri ve kendi eklediği gösterge
stilleri kapalıdır. Statik dosyalar dışındaki her yanıt `Cache-Control: no-store` ile
gönderilir; böylece gizli bir değer gösteren sayfalar (yeni API token'ı ya da cluster
anahtarı, eşleme formu, bir yedek ya da dışa aktarma) ne tarayıcının önbelleğinde ne de
Geri tuşu için saklanır. Tarayıcının başka bir sitenin sayfasından geldiğini söylediği bir
değişiklik (`Sec-Fetch-Site`, `Origin`; aynı host'un başka bir portu da sayılır) CSRF
token'ına bakılmadan reddedilir; token her giriş ve çıkışta yenilenir. Admin TLS açıkken
yanıtlar varsayılan olarak bir yıllık `Strict-Transport-Security` taşır; host adının tüm
portlarını kapsadığı için süresi Ayarlar → Admin TLS → *HSTS max-age* ile değiştirilebilir
(0, tarayıcıların bunu unutmasını sağlayan `max-age=0` gönderir). TLS sonlandıran bir
reverse proxy arkasında da cookie'ler Secure olur (`X-Forwarded-Proto: https`), HSTS proxy
üzerinden yalnızca Ayarlar → Reverse proxy isterse gönderilir ve `X-Forwarded-For` yalnızca
orada listelenen proxy'lerden gelince dikkate alınır (varsayılan olarak hiçbiri); böylece bir
istemci giriş sınırlarının ve denetim kaydının gördüğü adresi seçemez
([docs/REVERSE-PROXY.md](docs/REVERSE-PROXY.md)).
