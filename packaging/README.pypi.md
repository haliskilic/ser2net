# ser2netweb

Serial-to-network bridge with a web UI: share local serial ports (RS-232 / RS-485, USB
adapters) over the network as raw TCP, telnet, RFC 2217 or UDP, run a Modbus RTU/TCP
gateway, publish serial data and Modbus points to MQTT and Sparkplug B, all set up and
watched in a browser, with accounts, roles, an audit log, TLS and LDAP / OpenID Connect
sign-in.

ser2netweb (named ser2net up to 2.9.x) is an independent Python project. It is not
related to or derived from Corey Minyard's ser2net daemon
([github.com/cminyard/ser2net](https://github.com/cminyard/ser2net)), whose classic
configuration files it can import.

## Install

```bash
pipx install ser2netweb              # or: pipx install "ser2netweb[all]"
ser2netweb                           # asks for the web UI's address on the console, then serves it
```

Python 3.10 or newer on Linux, macOS or Windows. The extras add the optional features:
`mqtt` (paho-mqtt), `ldap` (ldap3), `oidc` (authlib), `tls` (cryptography: self-signed
certificates in-process), `all` (every one); later: `pipx inject ser2netweb paho-mqtt`.
On Linux, opening a serial port needs membership in its group (`dialout` on Debian,
Ubuntu, Fedora; `uucp` on Arch).

A pip / pipx install keeps its data (`config.json`, backups, logs, traces) in the user's
data folder, never next to the package:

| OS | Data folder |
|---|---|
| Linux and other Unix | `$XDG_DATA_HOME/ser2netweb`, else `~/.local/share/ser2netweb` |
| macOS | `~/Library/Application Support/ser2netweb` |
| Windows | `%LOCALAPPDATA%\ser2netweb` |

`--data-dir DIR` (or `--config FILE`) chooses another folder. `ser2netweb --help` lists the
options, `ser2netweb --about` shows the versions and where each component is loaded from.

To run it as a service (systemd, the Windows service), use the standalone executables or
the `.deb` / `.rpm` packages of a release instead: they come with a hardened systemd unit
and `--install-service`. Docker: `ghcr.io/haliskilic/ser2netweb`.

## Links

- Documentation: [README](https://github.com/haliskilic/ser2netweb/blob/main/README.en.md)
  (English), [README.md](https://github.com/haliskilic/ser2netweb/blob/main/README.md) (Türkçe)
- Changes: [CHANGELOG.md](https://github.com/haliskilic/ser2netweb/blob/main/CHANGELOG.md)
- Releases (executables, packages, image):
  [github.com/haliskilic/ser2netweb/releases](https://github.com/haliskilic/ser2netweb/releases)
- Security: [SECURITY.md](https://github.com/haliskilic/ser2netweb/blob/main/SECURITY.md)

Licence: GNU AGPL-3.0-only, or a commercial licence
([COMMERCIAL_LICENSE.md](https://github.com/haliskilic/ser2netweb/blob/main/COMMERCIAL_LICENSE.md)).
