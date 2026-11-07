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
connection): ser2net does not authenticate MQTT commands itself.

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
kimliğini kendisi doğrulamaz.
