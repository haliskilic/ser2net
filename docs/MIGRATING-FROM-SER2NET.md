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
   Its package's unit is `ser2net.service`; ser2netweb's units are `ser2netweb.service`,
   `ser2netweb@.service` and the binary's (installed as `ser2netweb.service`), with their own
   user `ser2netweb`, so both can stay installed side by side, and `ser2netweb-install.sh`
   never touches the daemon's unit (up to 2.9.x ser2netweb's units had the daemon's name).
   Removing the package (`sudo apt remove ser2net`, `sudo dnf remove ser2net`) frees the
   ports for good; `apt remove` keeps `/etc/ser2net/ser2net.yaml`, `purge` deletes it.
5. **Ports below 1024.** The classic daemon usually ran as root; ser2netweb's systemd
   units run as the unprivileged `ser2netweb` user, which can't listen on a port below 1024
   (the preview names such ports). Give the service the `CAP_NET_BIND_SERVICE` capability
   (README → *Ports below 1024*) or move the mapping to port 1024 or above.
6. **Serial devices.** The `ser2netweb` user needs the devices' group (`dialout`, or `uucp` on
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
  - to get the classic behaviour back for a TCP server mapping, tick **Open the serial port
    only while a client is connected** (`network.open_on_connect`): the port is then opened
    for the first client and closed a little after the last one left, the open string goes
    out at each of those opens, and another program can use the device in between. The
    import does not turn it on;
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
- **UDP fixed remote addresses** become the mapping's UDP targets (**Also send the device's
  output to**), each with a note: on a UDP connection, 4.x `remaddr: !addr,port` and
  `connback: udp,addr,port`, and 3.x `remaddr=addr,port` with a port (the classic ser2net
  sends all of the device's traffic to such an address). Only an IP address with a port
  can be one, of the address family the mapping listens on; `255.255.255.255` also ticks
  **Allow broadcast**. A `remaddr` address still limits who may send; a `connback` one
  does not. On a TCP connection a connect-back address is not imported, as before.
- **chardelay** becomes **packing** when the file sets it on a raw connection: `chardelay:
  true`, one of `chardelay-scale` / `chardelay-min` / `chardelay-max`, or `sendon` (4.x); in
  3.x the `chardelay` keyword or one of the `chardelay-…=` options. The packing gap is the
  classic ser2net's own wait after a byte, (bits per character × 100000 × chardelay-scale)
  ÷ baud rate microseconds, at least `chardelay-min` and never more than `chardelay-max`,
  in whole milliseconds (9600 8N1 with the defaults: 2083 µs → 3 ms); the frame size is
  its buffer (`dev-to-net-bufsize`, 64 bytes), since there is no time limit like
  `chardelay-max` here and the frame size is what bounds the wait in a steady stream;
  `sendon` is the packing delimiter (at most 16 bytes). A file that says nothing about
  chardelay gets no packing, although the classic default is on: data is forwarded as it
  arrives here, which is what a 2-character wait amounts to on a network. `chardelay:
  false` is that too; telnet and RFC 2217 connections are not packed (their chardelay
  options are listed as not imported).
- **Rotators** become **port pools** (Settings → Port pools) that rotate as the classic
  one does (the search for a free connection starts after the one that got the last
  client), with the rotator's TCP connections as members. A pool tries its mappings in
  the order of the mappings (a note says so when the rotator lists them in another
  order), hands a client only to a mapping that runs (the classic rotator also used
  connections that were turned off) and gives it that mapping's protocol. Not imported,
  with the reason: a rotator on UDP, with `ssl` / `certauth` or on a host name, one whose
  port a connection uses, its `authdir` / `pamauth` / `allowed-users` options, and as
  members UDP connections and connections with TLS. A pool whose name already exists
  here is left as it is; the imported mappings join it.
- **Left out, with a note:** LEDs, mDNS, `telnet-brk-on-sync`, unknown options.

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
| UDP: `remaddr: !addr,port`, `connback` (4.x), `remaddr=addr,port` (3.x) | **Also send the device's output to** (UDP targets) |
| `chardelay: true`, `chardelay-scale` / `-min` / `-max`, `dev-to-net-bufsize`, `sendon` (raw) | **Packing gap**, **Packing frame size**, **Packing delimiter** |
| `rotator:` / `ROTATOR:` | a port pool with `rotate`; its TCP connections name it (**Port pool**) |
| `no-acc-to-con` | **Read-only** |
| `no-con-to-acc` | imported disabled |
| `banner`, `openstr`, `closestr`, `closeon` | the mapping's strings (escapes converted) |
| `signature` (RFC 2217) | **RFC 2217 signature** |
| `trace-both` / `trace-read` / `trace-write`, `trace-*-hexdump`, `-timestamp` | **Trace file name**, **Trace as hex dump** |
| LEDs, mDNS, `chardelay: false`, chardelay on telnet / RFC 2217 | not imported |
| `enable: off` / `port:off:` | imported disabled |
| `default:` (and classes), `define`, `*(name)` | applied to each connection as the classic ser2net applies them |

Every connection that comes in can be edited, started and exported like any other mapping;
the classic file itself is never changed.
