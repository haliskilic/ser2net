# Personal data in ser2net

[English](#english) · [Türkçe](#türkçe)

## English

This page lists what ser2net stores and sends that can identify a person, how long it is
kept, who can read it, and the settings that change it. It states facts about the software;
whether a deployment meets a law (KVKK, GDPR or another) depends on how it is used and is for
the organisation running it to decide. Nothing here is legal advice.

### What is stored, and for how long

Everything ser2net writes is in its data directory (default `data/`; Docker: `/data`;
systemd: `/opt/ser2net/data` for a source checkout, `/var/lib/ser2net` for the binary,
`/var/lib/ser2net/<name>` for an instance; Windows service: `C:\ProgramData\ser2net`, a
named one `C:\ProgramData\ser2net\<name>`), but for an entry in the Windows Application
event log when the service stops on an error. The directory is readable only by the account
ser2net runs as (POSIX `0700`, files `0600`; Windows: owner, SYSTEM and Administrators). Nothing in
it is encrypted: use full-disk encryption where the host needs it.

| Where | Personal data in it | Kept | Who can see it |
|---|---|---|---|
| `config.json` | the accounts: user names (an LDAP or SSO name is often an e-mail address or a UPN), roles, local password hashes (scrypt), the two-factor sign-in key of an account that uses it; the LDAP bind DN; the SMTP user name, the sender address, the notification e-mail recipients and Telegram chat id; the names of the REST API tokens; the client address lists of mappings | until changed | admins in Settings (no hashes, secrets masked); operators see the mappings and can export them |
| `backups/` | the `config.json` each change wrote and earlier copies, so also the accounts that were deleted since | the newest 50 (by count: on a busy node hours, on a quiet one for good); with a period under Settings → *Retention*, older ones are deleted, but never the newest 5 | admins: Settings → *Configuration backups* (list with each backup's accounts, download, restore, delete) |
| `all.log`, `all.log.1`–`.5` | sign-ins (account name, role, IP address), failed sign-ins (the account name, or `(unknown user)` for a name that is no account, and the IP address), one line per web UI request (IP address, method, path, status) and per REST API request but the monitoring polls (the same, and the token's name), client connects and disconnects (IP address and port; a TLS client certificate's subject), a copy of each audit record, LDAP / SSO refusals (the name) | rotated at 10 MB, 5 old files kept (about 60 MB): how long that is depends on the traffic; with a period under Settings → *Retention* (off by default), no record older than that many days | the account ser2net runs as (not shown in the UI) |
| `audit.log`, `audit.log.1` | time, client IP address, account, action and detail of sign-ins, sign-outs and failed sign-ins (also SSO sign-ins that failed and wrong two-factor codes), failed password changes, refused cluster requests, directory accounts removed for not signing in, configuration changes (a LAN cluster node's change as `cluster-peer:NODE/USER`) (through the REST API: the token's name as the account), console / hex view opens, console sessions that wrote to a device (how many bytes), trace and backup downloads, trace deletions, configuration and audit log exports, REST API requests refused for their token | rotated at 5 MB, 1 old file kept; with a period under Settings → *Retention*, up to 5 old files and no record older than that many days | admins: Settings → *Audit log* (search, filter by user, CSV) |
| `logs/<id>.log` | a mapping's events: client connects and disconnects (IP address and port; a TLS client certificate's subject), errors | 15 days or 100 MB, trimmed hourly (the last 1000 lines stay, a file over the size is cut to 90 MB); with a shorter period under Settings → *Retention*, no line older than that; deleted with the mapping | viewers and up (the mapping's log panel) |
| `traces/<id>-<name>` | with a mapping's trace on (off by default): the traffic itself, whatever the device and its clients send — also what is typed at a device's prompt | 10 MB × 3 old files by default (`trace_max_mb`, `trace_keep`); with a period under Settings → *Retention* (off by default), deleted that many days after their last write; deleted with the mapping, or by an operator in its Trace panel; those of a mapping an import or a restore removed until the next start | operators and up (list, download and delete, audited) |
| in memory only | sign-in attempt counters (IP address, name), ended sessions, last sign-in per account (read from `audit.log`), when each REST API token was last used, the last two-factor code accepted and the wrong codes per account | until their time window ends or ser2net stops | — |

Failed sign-ins, REST API requests refused for their token, console / hex view opens and
console sessions that wrote to a device are recorded one by one only up to a limit (20
failed sign-ins in 10 minutes), then summed up in one record. Up to 2.8.0, a failed sign-in
recorded the user name exactly as it was typed (which can be a password typed into the
wrong field); 2.8.1 records `(unknown user)` instead, but older records are not rewritten —
see *Erasing* below.

**Copies outside the data directory.** Every `all.log` line is also written to stdout:
under systemd the journal keeps it (`journalctl -u ser2net`, readable by root and the
`systemd-journal` and `adm` groups, under the journal's own retention), under Docker the
container log (until the container is removed; cap it with `logging:` in compose). A
Windows service writes nothing to stdout.

**The browser** keeps cookies: the session (signed, not encrypted: it holds the account
name; as long as a sign-in lasts — 8 hours by default, Settings → *Web UI access*), the CSRF
token (as long), the language (1 year), during an SSO sign-in its state (10 minutes) and,
between the password and the code of a two-factor sign-in, the account name (5 minutes).
`localStorage` holds the theme and the dashboard's filter. No other site's cookies or
scripts are used.

**Shown in the UI:** the dashboard lists each connected client's IP address and port to
every role, also to open-access visitors without an account, and so does the REST API's
status (with the token). Behind a reverse proxy the address recorded everywhere is the
proxy's, unless it is listed under Settings → *Reverse proxy* → *Trusted proxies*.

### What leaves the host

Only what you set up; nothing is on by default but the web UI itself.

- **Notifications** (Settings → *Notifications*, off by default): to the webhook (Slack,
  Microsoft Teams, Discord, Telegram, any JSON service) and/or by e-mail. Every message
  names the host, the instance and the mapping. *Client connected / disconnected* add the
  client's IP address and port; *Failed sign-in to this UI* adds the account name (or
  `(unknown user)`) and the IP address; *Serial device lost* and *Mapping failed to start*
  the error. *Leave out personal data* sends addresses only as their network (`10.0.5.x`,
  IPv6 /48) and no account names; *Send each recipient a message of their own* keeps the
  e-mail recipients from seeing each other. Where the data goes (and whether abroad) is up
  to the service you choose.
- **LDAP sign-in:** the user name and password go to the directory server you set; its
  groups are read to choose the role. Only the name and the role are kept.
- **SSO (OIDC) sign-in:** the browser goes to the identity provider; ser2net keeps only the
  user name (the claim you set) and the role.
- **MQTT / Sparkplug** (per mapping, off by default): the device's data goes to the broker.
- **LAN cluster** (off by default): signed UDP beacons on the local network (instance id,
  its name — the host name unless one is set —, web UI address, up to 32 of the host's IPv4
  addresses, a boot id and the time); nodes with the key see each other's mappings and client
  counts, not client addresses, and the audit log of a node a peer changed names the peer and
  its user (`cluster-peer:NODE/USER`).
- **No telemetry.** ser2net makes no connection of its own beyond the ones above: no update
  check, no usage statistics. The web UI loads only its own files (no fonts, CDNs or
  analytics; its Content-Security-Policy allows nothing else).

### Settings that keep less

| Setting | Effect | Default |
|---|---|---|
| Settings → *Audit log* → *Web UI requests in all.log* | only changes and refused requests, or none | every page and change |
| `SER2NETWEB_LOG_STDOUT=off` (systemd: a commented line in `ser2net.service` and `ser2net@.service`; add it to `ser2net-standalone.service` with `systemctl edit`) | the log lines only in `all.log`, no journal / Docker copy | on |
| Settings → *Notifications* → *Leave out personal data* | addresses as their network, no account names | off |
| Settings → *Notifications* → *Send each recipient a message of their own* | one e-mail per recipient | off |
| Settings → *Retention* | no log record, backup or trace older than that many days | off |
| Settings → *Users* → *Remove LDAP / OIDC accounts not signed in for (days)* | unused directory accounts removed (never an admin, a local account or the last accounts) | off |
| Settings → *Web UI access* → *Sign-in notice* | a notice (e.g. a privacy notice) on the sign-in page, with a link | none |

### Access requests

To show someone what is recorded about them: Settings → *Audit log*, filter by their
account (and search for their IP address), then *CSV*. On the host, search `all.log*` and
`logs/*.log` for the name and the address, and the journal (`journalctl -u ser2net | grep …`)
or `docker logs`.

### Erasing

**One person:**
1. Settings → *Users* → *Delete*. An LDAP / SSO account comes back at the next sign-in while
   the directory still gives it a role: remove it there too.
2. Settings → *Configuration backups* → *Remove it from the backups* (the backups made
   before the deletion still hold the account and its password hash).
3. Logs: ser2net doesn't edit its logs; the records go as they rotate (above). To remove them
   sooner, stop ser2net and delete or edit `audit.log*` and `all.log*` (that also removes the
   records of everyone else in them: decide by your own rules), and the journal
   (`journalctl --rotate` then `journalctl --vacuum-time=…`) or re-create the container.
4. Notifications already sent stay in the chat or mailbox they went to.

The same steps remove the typed user names that versions up to 2.8.0 recorded for failed
sign-ins (`login_failed` in `audit.log*`, `failed login for …` in `all.log*`, the journal and
the notifications).

**The whole installation:** stop ser2net and delete the data directory (keep `tls/` to
reuse the certificate); Docker: `docker compose down -v`. The copies outside it (above: the
journal, the container log, the Windows event log) go by their own rules. Deleting only `config.json` is not
enough: the audit log and the backups stay, and the next admin sees them. See the README's
*Configuration & state files*.

### For your records (VERBIS, record of processing)

A starting point to fill in; not legal advice, and not a complete record.

| Category | Data | Purpose (fill in) | Recipients | Kept |
|---|---|---|---|---|
| Identity | account names (maybe e-mail addresses) | access control to the serial gateway | your directory / identity provider, if used | until deleted + `backups/` |
| Transaction security | IP addresses, sign-ins and failed sign-ins, web UI requests, changes | information security, audit trail | notification service, if used | log rotation (above) |
| Communication | notification e-mail recipients | sending alerts | your mail server / chat service | until changed |
| Data of the bridged devices | trace files, MQTT data | (depends on the devices) | MQTT broker, if used | trace limits (above) |

Fill in the data controller, the people concerned (staff, contractors, …), the legal basis,
transfers abroad (which services, where) and your retention rules.

## Türkçe

Bu sayfa, ser2net'in bir kişiyi tanımlayabilecek hangi verileri sakladığını ve gönderdiğini,
ne kadar tuttuğunu, kimin okuyabildiğini ve bunu değiştiren ayarları listeler. Yazılımla
ilgili olguları anlatır; bir kurulumun bir yasaya (KVKK, GDPR ya da başka) uyup uymadığı
nasıl kullanıldığına bağlıdır ve buna onu çalıştıran kuruluş karar verir. Buradaki hiçbir şey
hukuki tavsiye değildir.

### Neler, ne kadar süre saklanır

ser2net'in yazdığı her şey veri dizinindedir (varsayılan `data/`; Docker: `/data`; systemd:
kaynak kurulumda `/opt/ser2net/data`, binary'de `/var/lib/ser2net`, bir örnekte
`/var/lib/ser2net/<ad>`; Windows servisi: `C:\ProgramData\ser2net`, adlandırılmış bir servis
`C:\ProgramData\ser2net\<ad>`); tek istisna, servis bir hatayla durduğunda Windows
Application olay günlüğüne yazılan kayıttır. Dizini yalnızca ser2net'in çalıştığı hesap okuyabilir
(POSIX `0700`, dosyalar `0600`; Windows: sahip, SYSTEM ve Administrators). İçindeki hiçbir
şey şifrelenmez: gereken makinelerde tam disk şifrelemesi kullanın.

| Nerede | İçindeki kişisel veri | Saklama | Kim görebilir |
|---|---|---|---|
| `config.json` | hesaplar: kullanıcı adları (LDAP ya da SSO adı çoğu zaman bir e-posta adresi ya da UPN'dir), roller, yerel parola hash'leri (scrypt), iki adımlı giriş kullanan bir hesabın anahtarı; LDAP bind DN; SMTP kullanıcı adı, gönderen adresi, bildirim e-posta alıcıları ve Telegram chat id; REST API token'larının adları; eşlemelerin istemci adres listeleri | değiştirilene kadar | Ayarlar'da admin'ler (hash'ler gösterilmez, gizli değerler maskelenir); operator'lar eşlemeleri görür ve dışa aktarabilir |
| `backups/` | her değişikliğin yazdığı `config.json` ve önceki kopyaları; dolayısıyla sonradan silinen hesaplar da | en yeni 50 (sayıya göre: yoğun bir düğümde saatler, sakin birinde süresiz); Ayarlar → *Saklama süresi*'nde bir süre belirlenince daha eskileri silinir, en yeni 5 hiçbir zaman | admin'ler: Ayarlar → *Yapılandırma yedekleri* (her yedeğin hesaplarıyla liste, indirme, geri yükleme, silme) |
| `all.log`, `all.log.1`–`.5` | girişler (hesap adı, rol, IP adresi), başarısız girişler (hesap adı ya da hesap olmayan bir ad için `(unknown user)` ve IP adresi), her web arayüzü isteği ve izleme sorguları dışında her REST API isteği için bir satır (IP adresi, yöntem, yol, durum; REST API'de token'ın adı da), istemci bağlanma ve kopmaları (IP adresi ve port; TLS istemci sertifikasının konusu), her denetim kaydının bir kopyası, LDAP / SSO retleri (ad) | 10 MB'ta döner, 5 eski dosya tutulur (yaklaşık 60 MB): bunun ne kadar süre olduğu trafiğe bağlıdır; Ayarlar → *Saklama süresi*'nde bir süre belirlenince (varsayılan kapalı) o kadar günden eski kayıt kalmaz | ser2net'in çalıştığı hesap (arayüzde gösterilmez) |
| `audit.log`, `audit.log.1` | girişlerin, çıkışların ve başarısız girişlerin (başarısız SSO girişleri ve yanlış iki adımlı giriş kodları da), başarısız parola değişikliklerinin, reddedilen küme isteklerinin, giriş yapmadığı için kaldırılan dizin hesaplarının, yapılandırma değişikliklerinin (bir LAN cluster düğümünün değişikliği `cluster-peer:DÜĞÜM/KULLANICI` olarak) (REST API üzerinden: hesap olarak token'ın adı), konsol / hex görünümü açılışlarının, cihaza yazan konsol oturumlarının (kaç bayt), trace ve yedek indirmelerinin, trace silmelerinin, yapılandırma ve denetim kaydı dışa aktarmalarının, token'ı yüzünden reddedilen REST API isteklerinin zamanı, istemci IP adresi, hesabı, işlemi ve ayrıntısı | 5 MB'ta döner, 1 eski dosya tutulur; Ayarlar → *Saklama süresi*'nde bir süre belirlenince en fazla 5 eski dosya ve o kadar günden eski kayıt kalmaz | admin'ler: Ayarlar → *Denetim kaydı* (arama, kullanıcıya göre filtre, CSV) |
| `logs/<id>.log` | bir eşlemenin olayları: istemci bağlanma ve kopmaları (IP adresi ve port; TLS istemci sertifikasının konusu), hatalar | 15 gün ya da 100 MB, saatte bir kırpılır (son 1000 satır kalır, boyutu aşan dosya 90 MB'a kısaltılır); Ayarlar → *Saklama süresi*'nde daha kısa bir süre belirlenince ondan eski satır kalmaz; eşlemeyle birlikte silinir | viewer ve üstü (eşlemenin log paneli) |
| `traces/<id>-<ad>` | bir eşlemenin trace'i açıkken (varsayılan kapalı): trafiğin kendisi, cihazın ve istemcilerinin gönderdiği her şey — bir cihazın komut isteminde yazılanlar da | varsayılan 10 MB × 3 eski dosya (`trace_max_mb`, `trace_keep`); Ayarlar → *Saklama süresi*'nde bir süre belirlenince (varsayılan kapalı) son yazılmalarından o kadar gün sonra silinir; eşlemeyle birlikte ya da bir operator tarafından Trace panelinde silinir; bir içe aktarma ya da geri yüklemenin kaldırdığı eşlemeninkiler sonraki açılışa kadar kalır | operator ve üstü (listeleme, indirme ve silme, denetim kaydına yazılır) |
| yalnızca bellekte | giriş denemesi sayaçları (IP adresi, ad), sonlandırılmış oturumlar, her hesabın son girişi (`audit.log`'dan okunur), her REST API token'ının son kullanımı, hesap başına kabul edilen son iki adımlı giriş kodu ve yanlış kodlar | zaman penceresi bitene ya da ser2net durana kadar | — |

Başarısız girişler, token'ı yüzünden reddedilen REST API istekleri, konsol / hex görünümü
açılışları ve cihaza yazan konsol oturumları bir sınıra kadar tek tek yazılır (10
dakikada 20 başarısız giriş), sonrası tek bir kayıtta toplanır. 2.8.0'a kadar başarısız bir
giriş, kullanıcı adını yazıldığı gibi kaydediyordu (yanlış alana yazılmış bir parola
olabilir); 2.8.1 bunun yerine `(unknown user)` yazar, ama eski kayıtlar yeniden yazılmaz —
aşağıda *Silme*'ye bakın.

**Veri dizini dışındaki kopyalar.** `all.log`'un her satırı stdout'a da yazılır: systemd
altında journal tutar (`journalctl -u ser2net`; root ile `systemd-journal` ve `adm` grupları
okuyabilir, journal'ın kendi saklama süresiyle), Docker altında container log'u (container
silinene kadar; compose'da `logging:` ile sınırlayın). Windows servisi stdout'a bir şey
yazmaz.

**Tarayıcı** cookie tutar: oturum (imzalıdır, şifreli değildir: hesap adını içerir; bir giriş
ne kadar sürüyorsa — varsayılan 8 saat, Ayarlar → *Web arayüzü erişimi*), CSRF token'ı (aynı
süre), dil (1 yıl), bir SSO girişi sırasında onun durumu (10 dakika) ve iki adımlı girişte
parola ile kod arasında hesap adı (5 dakika). `localStorage` temayı
ve panonun filtresini tutar. Başka bir sitenin cookie'si ya da betiği kullanılmaz.

**Arayüzde gösterilenler:** pano, bağlı her istemcinin IP adresini ve portunu her role,
hesabı olmayan açık erişim ziyaretçilerine de gösterir; REST API'nin durum yanıtı da
(token'la) gösterir. Bir reverse proxy arkasında, Ayarlar → *Reverse proxy* → *Güvenilen
proxy'ler*'de listelenmedikçe her yerde kaydedilen adres proxy'ninkidir.

### Host dışına neler çıkar

Yalnızca sizin kurduğunuz şeyler; web arayüzünün kendisi dışında hiçbiri varsayılan olarak
açık değildir.

- **Bildirimler** (Ayarlar → *Bildirimler*, varsayılan kapalı): webhook'a (Slack, Microsoft
  Teams, Discord, Telegram, herhangi bir JSON servisi) ve/veya e-postayla. Her mesaj host'u,
  kurulumu ve eşlemeyi adlandırır. *İstemci bağlandı / bağlantısı kesildi* istemcinin IP
  adresini ve portunu; *Bu arayüzde başarısız giriş denemesi* hesap adını (ya da
  `(unknown user)`) ve IP adresini; *Seri cihaz kayboldu* ve *Eşleme başlatılamadı* hatayı
  ekler. *Kişisel verileri gönderme* adresleri yalnızca ağlarıyla (`10.0.5.x`, IPv6 /48)
  gönderir, hesap adlarını hiç göndermez; *Her alıcıya ayrı bir mesaj gönder* e-posta
  alıcılarının birbirini görmesini önler. Verinin nereye (ve yurt dışına) gideceği
  seçtiğiniz servise bağlıdır.
- **LDAP girişi:** kullanıcı adı ve parola ayarladığınız dizin sunucusuna gider; rolü seçmek
  için grupları okunur. Yalnızca ad ve rol saklanır.
- **SSO (OIDC) girişi:** tarayıcı kimlik sağlayıcıya gider; ser2net yalnızca kullanıcı adını
  (ayarladığınız claim) ve rolü saklar.
- **MQTT / Sparkplug** (eşleme başına, varsayılan kapalı): cihazın verisi broker'a gider.
- **LAN cluster** (varsayılan kapalı): yerel ağda imzalı UDP beacon'ları (kurulum kimliği,
  adı — ayarlanmamışsa host adı —, web arayüzü adresi, host'un en çok 32 IPv4 adresi, bir
  açılış kimliği ve zaman); anahtarı olan düğümler birbirinin eşlemelerini ve istemci
  sayılarını görür, istemci adreslerini görmez; bir eşin değiştirdiği düğümün denetim kaydı
  o eşi ve kullanıcısını adlandırır (`cluster-peer:DÜĞÜM/KULLANICI`).
- **Telemetri yok.** ser2net yukarıdakiler dışında kendiliğinden bağlantı kurmaz: güncelleme
  denetimi yok, kullanım istatistiği yok. Web arayüzü yalnızca kendi dosyalarını yükler
  (font, CDN ya da analitik yok; Content-Security-Policy başka bir şeye izin vermez).

### Daha az tutan ayarlar

| Ayar | Etkisi | Varsayılan |
|---|---|---|
| Ayarlar → *Denetim kaydı* → *all.log'daki web arayüzü istekleri* | yalnızca değişiklikler ve reddedilen istekler, ya da hiçbiri | her sayfa ve değişiklik |
| `SER2NETWEB_LOG_STDOUT=off` (systemd: `ser2net.service` ve `ser2net@.service`'te yorum satırı olarak; `ser2net-standalone.service`'e `systemctl edit` ile ekleyin) | log satırları yalnızca `all.log`'da, journal / Docker kopyası yok | açık |
| Ayarlar → *Bildirimler* → *Kişisel verileri gönderme* | adresler ağlarıyla, hesap adları yok | kapalı |
| Ayarlar → *Bildirimler* → *Her alıcıya ayrı bir mesaj gönder* | her alıcıya ayrı e-posta | kapalı |
| Ayarlar → *Saklama süresi* | o kadar günden eski log kaydı, yedek ya da trace kalmaz | kapalı |
| Ayarlar → *Kullanıcılar* → *Giriş yapılmayan LDAP / OIDC hesaplarını kaldırma süresi* | kullanılmayan dizin hesapları kaldırılır (bir admin, yerel bir hesap ya da son hesaplar hiçbir zaman) | kapalı |
| Ayarlar → *Web arayüzü erişimi* → *Giriş bilgilendirmesi* | giriş sayfasında bir bildirim (ör. aydınlatma metni) ve bağlantısı | yok |

### Erişim talepleri

Birine hakkında neyin kaydedildiğini göstermek için: Ayarlar → *Denetim kaydı*, hesabına göre
filtreleyin (ve IP adresini arayın), sonra *CSV*. Makinede `all.log*` ve `logs/*.log`
dosyalarında adı ve adresi, journal'da (`journalctl -u ser2net | grep …`) ya da
`docker logs`'ta arayın.

### Silme

**Bir kişi:**
1. Ayarlar → *Kullanıcılar* → *Sil*. Bir LDAP / SSO hesabı, dizin ona hâlâ bir rol veriyorsa
   bir sonraki girişte geri gelir: onu orada da kaldırın.
2. Ayarlar → *Yapılandırma yedekleri* → *Yedeklerden çıkar* (silmeden önce alınan yedekler
   hesabı ve parola hash'ini hâlâ içerir).
3. Log'lar: ser2net log'larını düzenlemez; kayıtlar döndükçe gider (yukarıda). Daha erken
   kaldırmak için ser2net'i durdurun ve `audit.log*` ile `all.log*` dosyalarını silin ya da
   düzenleyin (bu, içlerindeki herkesin kayıtlarını da kaldırır: kendi kurallarınıza göre
   karar verin); journal için `journalctl --rotate` ardından `journalctl --vacuum-time=…`,
   Docker'da container'ı yeniden oluşturun.
4. Önceden gönderilmiş bildirimler gittikleri sohbette ya da posta kutusunda kalır.

Aynı adımlar, 2.8.0'a kadarki sürümlerin başarısız girişler için kaydettiği yazılmış
kullanıcı adlarını da kaldırır (`audit.log*`'da `login_failed`, `all.log*`'da
`failed login for …`, journal ve bildirimler).

**Tüm kurulum:** ser2net'i durdurun ve veri dizinini silin (sertifikayı yeniden kullanmak için
`tls/`'i bırakın); Docker: `docker compose down -v`. Dizinin dışındaki kopyalar (yukarıda:
journal, container log'u, Windows olay günlüğü) kendi kurallarıyla gider. Yalnızca
`config.json`'u silmek yetmez:
denetim kaydı ve yedekler kalır ve sonraki admin onları görür. README'nin *Yapılandırma &
durum dosyaları* bölümüne bakın.

### Kayıtlarınız için (VERBIS, işleme kaydı)

Doldurmanız için bir başlangıç; hukuki tavsiye değildir, eksiksiz bir kayıt da değildir.

| Kategori | Veri | Amaç (doldurun) | Alıcılar | Saklama |
|---|---|---|---|---|
| Kimlik | hesap adları (e-posta adresi olabilir) | seri ağ geçidine erişimin yönetimi | kullanılıyorsa dizininiz / kimlik sağlayıcınız | silinene kadar + `backups/` |
| İşlem güvenliği | IP adresleri, girişler ve başarısız girişler, web arayüzü istekleri, değişiklikler | bilgi güvenliği, denetim izi | kullanılıyorsa bildirim servisi | log dönüşü (yukarıda) |
| İletişim | bildirim e-posta alıcıları | uyarıların gönderilmesi | posta sunucunuz / sohbet servisiniz | değiştirilene kadar |
| Bağlanan cihazların verisi | trace dosyaları, MQTT verisi | (cihazlara bağlı) | kullanılıyorsa MQTT broker | trace sınırları (yukarıda) |

Veri sorumlusunu, ilgili kişi gruplarını (personel, yükleniciler, …), hukuki sebebi, yurt
dışına aktarımları (hangi servisler, nerede) ve kendi saklama kurallarınızı doldurun.
