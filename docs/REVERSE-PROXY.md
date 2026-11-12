# Running ser2net behind a reverse proxy

A reverse proxy (nginx, Apache, Caddy, Traefik, IIS) in front of the web UI is the usual way
to give it HTTPS with a certificate your browsers already trust, or one name for several
services. ser2net itself then speaks plain HTTP to the proxy only. This page is what the
web UI needs from the proxy, the settings for it in **Settings → Reverse proxy**, tested
examples and what to look for when something doesn't work.

Only the web UI goes through the proxy. The mapping ports (raw TCP, telnet, RFC 2217,
Modbus TCP) are not HTTP: clients keep connecting to them directly. LAN-cluster nodes call
each other's UI port directly too (next section).

## Checklist

1. **Pass the browser's address on**, port included when it isn't 80/443: as `Host`
   (nginx `proxy_set_header Host $http_host;`, Apache `ProxyPreserveHost On`) or as
   `X-Forwarded-Host`. The web UI refuses forms, and the console and hex-view WebSockets,
   that a page of another site sends; it knows its own pages by that address (or by the
   public URL, below).
2. **Pass WebSocket upgrades through** (the console and the hex view).
3. **Send `X-Forwarded-For` and `X-Forwarded-Proto`**, and list the proxy under
   **Trusted proxies**. Only then do the sign-in limits, the audit log and all.log see each
   client's own address; otherwise every user has the proxy's, and 8 wrong passwords in
   5 minutes from anyone make the sign-in page answer HTTP 429 for everyone.
4. **Set the public URL** (`https://ser2net.example.com`): the address users open.
5. **Keep the UI port off the network**: bind it to `127.0.0.1` (a native install, with
   the proxy on the same machine), publish it to the host only in Docker
   (`127.0.0.1:8080:8080`), or not at all when the proxy runs in the same Docker network.
6. **One instance per host name at `/`**, or each under a path of its own: give the path in
   its public URL (`https://gw.example.com/line1`).
7. **Single sign-on:** register `<public URL>/auth/oidc/callback` at the identity provider
   (Settings → Single sign-on shows the exact value), or set *Redirect URI* there.
8. **LAN cluster:** nodes call each other at `admin_ui.port` directly (the Advertise IP or
   the manual peers' address), never through the proxy, and the fleet view's *Open* link
   is that direct address. A node bound to `127.0.0.1` behind a proxy is out of its peers'
   reach: bind it to its LAN address and let a firewall keep everyone but the proxy and
   the other nodes off the port.

## Settings → Reverse proxy

| Setting | `config.json` | For one run instead | Default |
|---|---|---|---|
| Public URL | `admin_ui.public_url` | `SER2NET_PUBLIC_URL` | none |
| Trusted proxies | `admin_ui.trusted_proxies` | `SER2NET_TRUSTED_PROXIES` | none |
| Send HSTS on HTTPS through the proxy | `admin_ui.proxy_hsts` | — | off |

Changes apply at once. While an environment variable sets a value, the page shows it
read-only; the stored value applies again once the variable is removed (it is not written
to `config.json`). With nothing set, the web UI behaves as before, except that
X-Forwarded-For and X-Forwarded-Proto are no longer believed from anyone (see
[Upgrading from 2.8](#upgrading-from-28)).

**Public URL** — `http://` or `https://`, a host name or IP address, optionally a port and a
path; no user name, query or fragment, and a path may not start like one of the UI's own
pages (`/api`, `/settings`, `/static`, …). It is used:

- as an address of this UI's own pages: a form or the console sent by a page at it is
  accepted whatever `Host` the proxy passes on;
- for the SSO redirect URI when *Redirect URI* is left blank: `<public URL>/auth/oidc/callback`
  (without a public URL it is made from the request, as before; setting one changes it, so
  register the new value);
- in notifications: e-mails, Slack, Teams, Discord and Telegram messages end with it, and
  the generic webhook JSON gets a `"url"` field;
- in the start-up log (`admin UI on http://0.0.0.0:8080, published as https://…`) and
  under Settings → Admin interface.

It does not make cookies Secure on its own: someone opening the UI directly by
`http://address:port` (the fleet view's *Open* link does) keeps working.

**A path in the public URL** (`https://gw.example.com/ser2net`) serves the UI under that
path too: every link, form, redirect and WebSocket address of a page requested under it
starts with it, and so does the REST API's OpenAPI `servers` entry. A request without it
is served at `/` exactly as before, so the LAN, cluster peers and the health check are
unaffected. The proxy may pass the path on as it is (nginx `location /ser2net/ { proxy_pass
http://127.0.0.1:8080; }`, Caddy `handle`), or strip it and send exactly that path as
`X-Forwarded-Prefix` (Traefik's `StripPrefix`). Under a path, the UI's cookies have names of
their own and `Path=<path>`, so another instance at `/` of the same site doesn't mix its
cookies in; users sign in once more there.

**Trusted proxies** — IP addresses or networks (CIDR), separated by spaces or commas:
`127.0.0.1`, `172.18.0.0/16`, `2001:db8::/64`. Only on a connection from one of them are
`X-Forwarded-For` (the client's address) and `X-Forwarded-Proto` (whether the browser used
HTTPS) believed. The client is the last `X-Forwarded-For` entry that isn't a trusted proxy
(each proxy adds the address it was reached from at the end), so a client's own
`X-Forwarded-For` doesn't count; an entry that isn't an IP address is never taken. Don't
trust networks your clients are in (`0.0.0.0/0`, the LAN) while they can reach the UI's
port directly: anyone there could then choose the address the sign-in limits see.

`FORWARDED_ALLOW_IPS`, uvicorn's variable that worked up to 2.8.1, is still read when
neither the setting nor `SER2NET_TRUSTED_PROXIES` gives a list (the start-up log says so);
`*` there is warned about.

The connection limits of the UI's listener (`admin_ui.max_connections_per_ip`) count the
connections themselves, so they still see the proxy's address: raise the per-address limit
if many users share one proxy.

**Send HSTS on HTTPS through the proxy** — `Strict-Transport-Security` with the max-age set
under Settings → Admin TLS, on responses to requests that came over HTTPS
(`X-Forwarded-Proto: https`). Browsers then use only `https://` for that host name, on every
port, for that long: a direct `http://name:8080` stops opening in them. Off by default; the
proxy can send the header itself instead (examples below).

**Secure cookies** need no setting: the session, CSRF, language and SSO cookies are Secure
whenever the browser used HTTPS: with admin TLS, or when the request's first
`X-Forwarded-Proto` (or `Forwarded: proto=`) is `https`, from any client — that header can
only make cookies stricter. So nginx's `proxy_cookie_flags` isn't needed (nginx before
1.19.3, as on Ubuntu 22.04, doesn't have it). A browser that has the Secure cookies of
`https://ser2net.example.com` can't sign in at `http://ser2net.example.com:8080` until they
expire: use one of the two addresses.

## Examples

Each one: TLS at the proxy for `ser2net.example.com`, ser2net on `127.0.0.1:8080` on the
same machine (Trusted proxies: `127.0.0.1`), Public URL `https://ser2net.example.com`.

**nginx** (`$http_host` is what the browser sent, port included; nginx's default,
`Host $proxy_host`, is the upstream's address):

```nginx
map $http_upgrade $connection_upgrade { default upgrade; '' close; }   # in http { }

server {
    listen 443 ssl;
    server_name ser2net.example.com;
    # ssl_certificate / ssl_certificate_key …
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        # HSTS from the proxy instead of ser2net (optional):
        # add_header Strict-Transport-Security "max-age=31536000" always;
    }
}
```

Under a path (Public URL `https://gw.example.com/ser2net`): the same `proxy_set_header`
lines in `location /ser2net/ { proxy_pass http://127.0.0.1:8080; … }` — without a path
after the address, nginx passes `/ser2net/…` on as it is.

**Apache** 2.4.47 or later (`mod_proxy_http` and `mod_headers`; older versions need
`mod_proxy_wstunnel` for the WebSockets). `mod_proxy` sends `X-Forwarded-For` by itself:

```apache
<VirtualHost *:443>
    ServerName ser2net.example.com
    # SSLEngine on, SSLCertificateFile / SSLCertificateKeyFile …
    ProxyPreserveHost On
    RequestHeader set X-Forwarded-Proto "https"
    ProxyPass        / http://127.0.0.1:8080/ upgrade=websocket
    ProxyPassReverse / http://127.0.0.1:8080/
</VirtualHost>
```

Under a path: `ProxyPass /ser2net/ http://127.0.0.1:8080/ser2net/ upgrade=websocket` (and
the same `ProxyPassReverse`).

**Caddy** keeps `Host`, passes WebSockets and sends `X-Forwarded-For` / `-Proto` by default:

```caddyfile
ser2net.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

Under a path: `handle /ser2net* { reverse_proxy 127.0.0.1:8080 }` (`handle`, not
`handle_path`, which strips the path without saying so).

**Traefik** (Docker labels; Traefik keeps `Host`, passes WebSockets and sends the
`X-Forwarded-*` headers). Trust the address ser2net sees it at: the Docker network's
subnet, or a fixed `ipv4_address` of the Traefik container:

```yaml
    labels:
      - traefik.enable=true
      - traefik.http.routers.ser2net.rule=Host(`ser2net.example.com`)
      - traefik.http.routers.ser2net.tls=true
      - traefik.http.services.ser2net.loadbalancer.server.port=8080
```

Under a path: ``rule=Host(`gw.example.com`) && PathPrefix(`/ser2net`)`` and a `StripPrefix`
middleware for `/ser2net` (it sends `X-Forwarded-Prefix`).

**IIS** with Application Request Routing and URL Rewrite: install the *WebSocket Protocol*
feature and keep the browser's host name (ARR's default replaces it):

```bat
%windir%\system32\inetsrv\appcmd.exe set config -section:system.webServer/proxy -preserveHostHeader:true /commit:apphost
```

and let the rewrite rule (to `http://127.0.0.1:8080/{R:1}`) set the server variable
`HTTP_X_FORWARDED_PROTO` to `https` (add it to the site's allowed server variables). ARR
sends `X-Forwarded-For` when *Preserve client IP* is on (its default). For the Windows
service, set Trusted proxies in Settings rather than environment variables.

**Docker Compose** with Caddy in front and the UI port not published:

```yaml
services:
  ser2net:
    image: ghcr.io/haliskilic/ser2net:latest
    environment:
      SER2NET_PUBLIC_URL: https://ser2net.example.com
      SER2NET_TRUSTED_PROXIES: 172.30.0.0/24       # the network below
    ports: ["4001:4001"]                           # mappings only; no 8080
    networks: [web]
    # devices:, volumes: as in docker-compose.yml
  caddy:
    image: caddy:2
    command: caddy reverse-proxy --from ser2net.example.com --to ser2net:8080
    ports: ["80:80", "443:443"]
    networks: [web]
networks:
  web:
    ipam:
      config: [{subnet: 172.30.0.0/24}]
```

## When something doesn't work

all.log names what the web UI refused and why:

- `refused POST /settings/… from …: sent by a page of another site (Origin '…'; this UI was
  addressed as '…')` — a form failed with 403, or `refused WebSocket …` and the console
  shows *console disconnected*: the proxy doesn't pass the browser's address on (checklist
  item 1), or the Public URL isn't the address users open.
- `X-Forwarded-For from 172.18.0.5 is ignored: 172.18.0.5 is not a trusted proxy …` (once
  an hour) — list that address under Trusted proxies. Until then the audit log shows it
  for every user, and many wrong passwords from anyone answer 429 for everyone.
- The sign-in page reloads without signing in, over `http://`: the browser keeps Secure
  cookies of the `https://` address of the same name (see *Secure cookies*).
- The SSO provider says the redirect URI is not registered: compare it with the one in
  Settings → Single sign-on, and all.log's `OIDC login init … redirect URI …` line.

## Upgrading from 2.8

Up to 2.8.1 the web UI (uvicorn) believed `X-Forwarded-For` and `X-Forwarded-Proto` from
any connection from `127.0.0.1` or `::1`, whatever its value. From 2.9 it believes them only
from the trusted proxies. A proxy on the same machine that sent `X-Forwarded-For` therefore
shows `127.0.0.1` for every user (the audit log, the per-address sign-in limit) until
`127.0.0.1` is listed under Trusted proxies; all.log says so once an hour. A
`FORWARDED_ALLOW_IPS` you set keeps working. Secure cookies behind a TLS proxy need nothing
from you.
