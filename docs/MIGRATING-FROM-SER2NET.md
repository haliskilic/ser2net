# Moving from the classic ser2net

ser2netweb (called ser2net up to 2.9.x) is a separate program with the same purpose as the
classic `ser2net` daemon (Corey Minyard's C program, the `ser2net` package of Debian, Ubuntu,
Fedora and others); it is not derived from it. It reads the classic configuration —
`/etc/ser2net/ser2net.yaml` (ser2net 4.x) or `/etc/ser2net.conf` (3.x) — and turns each
connection into a mapping. This page is what to do and what works differently, before you
switch.

## Steps

1. **Install ser2netweb** (standalone binary, Docker or source: README → *Install & run*)
   and open its web UI. Don't start its service on the classic ports yet.
2. **Import the classic file with a preview.** In **Settings → Mappings export / import**,
   choose the file and **Preview import**: every connection shows as the mapping it
   becomes, with a note for each thing that is left out or works differently. Nothing
   changes until you **Apply import** (after step 4), and the current configuration is kept in
   `backups/` either way. *Merge* updates mappings of the same name and adds the others;
   *Replace* replaces all of them. Without a browser, the REST API takes the same file
   (an operator or admin token; `dry_run=true` answers what it would do):
   ```bash
   curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: text/plain" \
     --data-binary @/etc/ser2net/ser2net.yaml \
     "http://127.0.0.1:8080/api/v1/import?mode=merge&dry_run=true"
   ```
   The answer lists the rows and warnings; without `dry_run` it applies them (the warnings
   also go to `all.log` as `import warning:` lines).
3. **Read what comes in disabled, and why.** A connection that would be more open here than
   in the classic ser2net is imported *disabled*, with the reason marked ⚠ in the preview:
   its TLS certificate or key is not on this host, its client certificates (`ssl(clientauth)`)
   have no CA file here, `certauth` (gtlssh login) or `dtls`, a `remaddr` or bind address
   that can't be carried over, an open or close string with a byte of 0x80 or more, a
   device that only received (`wronly`, `rawlp`, `no-con-to-acc`), or a second connection
   on a device an earlier one uses. Fix each, then enable it.
4. **Stop and disable the classic daemon** before you apply the import, which starts the
   enabled mappings at once: the classic daemon holds the same listening ports and serial
   devices.
   ```bash
   sudo systemctl disable --now ser2net
   ```
   Its package's unit is also called `ser2net.service`. `systemd/ser2net.service` copied to
   `/etc/systemd/system/` takes that name over (systemd uses the copy in `/etc`), and
   `ser2net-install.sh` refuses to do that unless you pass `--replace-unit`. Removing the
   package (`sudo apt remove ser2net`, `sudo dnf remove ser2net`) avoids the clash; `apt
   remove` keeps `/etc/ser2net/ser2net.yaml`, `purge` deletes it. To keep both installed,
   run this one as an instance of `ser2net@.service` (README → *Several instances on one
   machine*).
5. **Ports below 1024.** The classic daemon usually ran as root; ser2netweb's systemd
   units run as the unprivileged `ser2net` user, which can't listen on a port below 1024
   (the preview names such ports). Give the service the `CAP_NET_BIND_SERVICE` capability
   (README → *Ports below 1024*) or move the mapping to port 1024 or above.
6. **Serial devices.** The `ser2net` user needs the devices' group (`dialout`, or `uucp` on
   Arch); see [`LINUX-SERIAL.md`](LINUX-SERIAL.md) when a port doesn't open.
7. Apply the import, enable the mappings you fixed and check the dashboard.

Importing the same file again later (*Merge*) takes only what the file says from it: a
mapping keeps what was set up here since, and never becomes more open (a disabled mapping
stays disabled; the bind address, the allowed clients and read-only are not widened; TLS is
not turned off).

## What works differently

- **The serial device stays open.** The classic ser2net opens a device when a client
  connects and closes it when the client leaves; a mapping here opens it when it starts and
  keeps it open. So:
  - `openstr` is sent once when the port opens (and again after a lost device is reopened),
    not for each client; `closestr` when the last client leaves (the port stays open);
  - DTR and RTS are set once at open, not toggled per client: a board that resets on DTR
    (an Arduino) resets when the mapping starts, not at each connection;
  - `hangup-when-done` and `nobreak` are kept in the mapping but have no effect (the preview
    says so);
  - other programs can't use the device between sessions: stop the mapping first.
- **One mapping per device.** Several classic connections may share one device (each opens
  it only while used); here the first keeps it and the later ones come in disabled. A
  listening port that appears twice is imported once.
- **Locking carries over:** 4.x `flock` (on by default) becomes **Lock the port (flock)**
  plus **Keep other programs out while the port is open** (`TIOCEXCL`), `uucplock` becomes
  **Don't open a port another program has locked with a UUCP lock file**; ser2netweb
  makes no lock files of its own.
- **TLS:** `ssl` with its certificate and key on this host keeps TLS; client certificates
  (`clientauth`) are checked against the CA file it names (`CA=`). UDP has no TLS here.
- **Clients:** `remaddr` becomes the allowed client IPs (addresses and subnets only, not
  ports); `kickolduser` and `max-connections` carry over; an RFC 2217 mapping takes one
  client.
- **UDP** is raw only, and SCTP becomes TCP. A network or IPMI connector (not a local
  serial port) and a unix-socket accepter are skipped.
- **Banners and strings:** `\d`, `\p`, `\N`, `\I`, `\r`, `\n` and the other escapes are
  converted; one that has no equivalent is named in the preview. A byte of 0x80 or more is
  sent as UTF-8 (two bytes) here.
- **Traces:** one trace file per mapping, both directions, in the data folder's `traces/`
  and rotated by size (`trace-read` alone is not imported: a trace here records what
  clients send too).
- **Left out, with a note:** LEDs, rotators, mDNS, `telnet-brk-on-sync`, unknown options.
  `chardelay` (how the classic ser2net batches what it reads) is dropped without one: data
  is forwarded as it arrives.

## Option by option

| Classic (4.x / 3.x) | Here |
|---|---|
| `accepter: tcp,[host,]port` / `port:raw:` | TCP server on that address and port, protocol `raw` |
| `telnet,tcp,…` / `port:telnet:` | protocol `telnet` |
| `telnet(rfc2217),tcp,…` / `remctl` | protocol `rfc2217` |
| `udp,…` | UDP (raw) |
| `ipv4,` / `ipv6,` / `ipv6n4,` | the address family; `ipv6n4` takes IPv4 and IPv6 (*All interfaces (IPv4 + IPv6)*) |
| `ssl(key=,cert=,CA=,clientauth)` | TLS with those files; client certificates from that CA |
| `certauth`, `dtls` | imported disabled |
| `connector: serialdev,<device>,9600n81,…` | the serial port and its framing (also as words: `EVEN 8DATABITS 1STOPBIT`) |
| `rtscts`, `xonxoff` | flow control (one of them; RTS/CTS wins) |
| `dtr=`, `rts=` (`-dtr`, `-rts`) | RTS / DTR on open |
| `rs485=` | not imported (a note): set RS-485 in the mapping's serial settings |
| `flock`, `uucplock` | port locking (above) |
| `hangup-when-done`, `nobreak` | kept, no effect |
| `rdonly` | **Read-only** |
| `local=false` | not imported (no watching of the modem lines) |
| `wronly`, `rawlp` | imported disabled |
| `kickolduser`, `max-connections` | **Kick old client**, **Max connections** |
| `timeout` | **Idle timeout (s)** |
| `remaddr` | **Allowed client IPs / CIDRs** |
| `no-acc-to-con` | **Read-only** |
| `no-con-to-acc` | imported disabled |
| `banner`, `openstr`, `closestr`, `closeon` | the mapping's strings (escapes converted) |
| `signature` (RFC 2217) | **RFC 2217 signature** |
| `trace-both` / `trace-read` / `trace-write`, `trace-*-hexdump`, `-timestamp` | **Trace file name**, **Trace as hex dump** |
| LEDs, rotators, mDNS, `chardelay` | not imported |
| `enable: off` / `port:off:` | imported disabled |
| `default:` (and classes), `define`, `*(name)` | applied to each connection as the classic ser2net applies them |

Every connection that comes in can be edited, started and exported like any other mapping;
the classic file itself is never changed.
