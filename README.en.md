<!-- ser2netweb — README (English). Türkçe (default): README.md -->

**🌐 [Türkçe](README.md) · [English](README.en.md)**

# ser2netweb

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE) [![Commercial license](https://img.shields.io/badge/Commercial-license%20available-green.svg)](COMMERCIAL_LICENSE.md) [![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-db61a2?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/haliskilic)

**A cross-platform (Windows + Linux), pure-Python serial-to-network bridge with a
web management UI** — a web-managed alternative to the classic C `ser2net`. Map dozens
of serial ports (COM / ttyUSB / ttyACM / ttyS) to TCP/UDP endpoints, bidirectionally,
with low latency.

> **ser2netweb** (named ser2net up to 2.9.x) is an independent Python project. It is not
> affiliated with or derived from Corey Minyard's GPL-2.0 [ser2net](https://github.com/cminyard/ser2net),
> whose classic configuration files it can import, and its commercial license covers only
> this project.

---

## 📸 Screenshots

**Dashboard — mappings, live status, search and bulk actions, the LAN cluster panel:**

![Dashboard](docs/screenshots/02-dashboard.png)

**LAN cluster — every node's mappings in one table; per-node health + remote start/stop:**

![Cluster](docs/screenshots/06-cluster.png)

| Add mapping (all serial/network options) | In-browser serial console, hex view |
|---|---|
| ![Mapping form](docs/screenshots/03-add-mapping.png) | ![Console](docs/screenshots/05-console.png) |

| Settings → Notifications | Login |
|---|---|
| ![Settings](docs/screenshots/04-settings.png) | ![Login](docs/screenshots/01-login.png) |

---

## 📌 Purpose

Industrial devices, PLCs, instruments, GPS/modems, microcontrollers and console ports
usually speak **serial** (RS-232/485/USB-serial). `ser2netweb` bridges each serial port to
a **TCP/UDP endpoint** so any computer on the network can reach it, and manages
everything from a **password-protected web UI** — no CLI or hand-edited config files.

**Typical use cases:**
- Expose 10+ USB-serial devices attached to one server to networked applications
- Reach SCADA / Modbus-RTU devices from remote clients (raw or RFC2217)
- Network access to device consoles (switches, routers, embedded boards)
- RFC2217 for devices that need remote baud/parity changes
- Bridge two serial ports together (serial↔serial)

**Guides** (in `docs/`): [Docker](docs/DOCKER.md) · [Windows](docs/WINDOWS.md) ·
[Linux package repositories](docs/PACKAGES.md) ·
[Linux serial ports](docs/LINUX-SERIAL.md) · [reverse proxy](docs/REVERSE-PROXY.md) ·
[MQTT, Modbus points and Sparkplug B](docs/MODBUS-MQTT.md) ·
[LDAP / AD and OIDC sign-in](docs/SSO.md) ·
[moving from the classic ser2net](docs/MIGRATING-FROM-SER2NET.md) ·
[virtual COM ports on the client](docs/VIRTUAL-COM.md) · [privacy](docs/PRIVACY.md) ·
[code signing](docs/SIGNING.md) ·
[packaging for a distribution](docs/PACKAGING.md).

---

## ✨ Features

- **Transports:** TCP **server** (listen), TCP **client** (connect-out), **UDP**, and
  **serial↔serial** bridging; optional per-mapping **TLS** for TCP bridges (client
  certificates, server checks by CA file or fingerprint: see Security). UDP sends the
  device's output to the sender of the last datagram from an allowed IP, or always to a
  fixed reply address (**Reply to IP** and **Reply to port**); a **peer timeout** forgets a sender that went quiet.
  **Also send the device's output to** lists more destinations (`udp_targets`, `IP:port`)
  that always get it, whoever sends and before anyone has: an NMEA 0183 feed to UDP 10110
  on a LAN's broadcast address (with **Allow broadcast**, `udp_broadcast`), a multicast
  group, several hosts.
  Datagrams wait for the serial port in a queue of at most 64 KiB: what a sender faster
  than the line sends beyond that is dropped, counted and logged. UDP is always raw (a
  stored `telnet` is read as `raw`, which is what it always did) and has no connections a
  close-on string could close. A setting that does nothing — this one, MQTT on a
  serial↔serial bridge, MQTT on a Modbus gateway without poll points — is logged when the
  mapping starts and shown in its edit form and in the REST API's `warnings`.
- **Protocols:** `raw`, `telnet` (8-bit clean), `rfc2217` (remote live changes to
  baud/parity/data bits/stop bits/flow control), `modbus` (RTU↔TCP gateway). An RFC 2217
  request the port can't carry out (a malformed or reserved value, or one the serial port
  refuses) is answered with the current setting, as RFC 2217 says, and leaves the port as
  it was; the client stays connected. An RFC 2217 client also stays connected while a lost
  serial port is reopened, and the reopened port gets its settings; optionally the port
  returns to the mapping's settings when the client leaves (see
  [`docs/VIRTUAL-COM.md`](docs/VIRTUAL-COM.md)). A telnet or RFC 2217 subnegotiation longer
  than 1 KiB (real ones are a few bytes) ends that client's session, and the log says why.
  **Serial BREAK:** a telnet client's BREAK (IAC BRK: PuTTY's *Special Command → Break*,
  `send brk` in telnet) and an RFC 2217 client's (SET-CONTROL BREAK on/off, or IAC BRK)
  reach the serial line after the data sent before them (a telnet BREAK lasts 0.25 s); a
  read-only mapping sends none, and a BREAK an RFC 2217 client leaves on is turned off when
  it disconnects. Raw TCP has no in-band BREAK. Commands keep their order with the data:
  what a client sends before an RFC 2217 setting change goes out at the old setting. An
  RFC 2217 client is also answered on SIGNATURE (`ser2net`, or the mapping's **RFC 2217
  signature**), on the BREAK / DTR / RTS and inbound flow control queries and on the
  line/modem state masks; flow control is one mode at a time. An unsupported telnet option
  is refused each time it is asked for.
- **Modbus gateway:** bridges Modbus/TCP masters to serial Modbus/RTU slaves —
  multiple masters, transaction serialization on the shared bus + transaction-id
  integrity, and a `0x0B` (gateway-timeout) exception when an RTU slave doesn't answer
  within the response timeout, which starts once the request has gone out on the line
  (set in the mapping form's Options: 1 s by default, 0.01–60 s). A reply ends when it is
  as long as its header says (after 50 ms of silence for function codes whose header
  doesn't size the reply: FC8, FC43, user-defined ones), so the latency of USB adapters
  and the Windows clock don't cut replies. A reply has to answer its
  request (the same function or its exception, the byte count of the quantity read, a
  write's echo), and after a request that got no reply in time the gateway waits until the
  line has been quiet for 250 ms (at most the response timeout) before it sends the next
  one, so a late reply is dropped, and the late slave logged, instead of being handed to
  the next master or poll point. Bytes that are not the reply are dropped and show up in
  the trace as `ser>drop`, in the console and in the hex view. A broadcast (unit 0) write
  (FC5/6/15/16/22) goes on the bus and is answered, after a 200 ms turnaround (at most the
  response timeout), with the echo a slave would send; a read of unit 0 gets `0x0B` at once
  without reaching the bus.
  If the serial port goes away (a USB adapter unplugged), masters get `0x0A` (gateway path
  unavailable) at once meanwhile (so does a request waiting for its reply at that moment)
  and the port is reopened like any mapping's (state `reconnecting`, device lost/back
  notifications). Masters are admitted like other
  clients (allowed IPs, max connections, priority IPs, kick-old-user); a **read-only**
  gateway answers every request that is not a read (writes such as FC5/6/15/16/22/23,
  diagnostics that restart or silence a slave, unknown function codes) with exception
  `0x01` without putting it on the bus. Banner, open/close strings and `closeon` don't
  apply (they would break the Modbus framing).
  **Edge mode:** also polls coils and registers on an interval and publishes the decoded
  values to MQTT (bool, uint/int 16/32/64, float 32/64, text, a register's bit, scaling,
  word order: see Modbus points below); `"writable": true` points take values from MQTT
  (`<topic>/<point>/set`, one value for that point only: FC5 for a coil, FC6/FC16 for a
  holding register's own registers; a JSON list or other text gets `error: …` on
  `.../set/result`). A point on unit 0 is never read; a write to it is
  broadcast and not read back. The mapping form and the log warn about points on unit 0
  and on units 248–255, which a serial line reserves. **Read-only** refuses these writes
  too (`error: mapping is read-only`; a refused DCMD is logged), and the dashboard marks a
  mapping that takes MQTT writes with **CMD** (see Security: the broker decides who may
  send them). Points are read unit by unit, and each unit's health is followed: a unit
  that gives no reply to any of its points in 3 polls in a row (or whose serial port stays
  gone that long) is **offline**, logged once, and from then on polled with one point per
  cycle (each in turn), so it holds the bus for one response timeout per cycle; when it
  answers, all its points are read again. A slave exception (such as `0x02` for a register
  it doesn't have) is an answer; such a point is logged with its unit when it starts
  failing (at most once a minute) and when it reads again. Requests from Modbus/TCP
  masters are not affected. Each unit's state is in the status (REST API) and `/metrics`,
  the dashboard names the units that stopped answering, the **Modbus unit stopped
  answering** notifications (and answering again; off until ticked) report them, and plain MQTT can
  publish it, retained, to `<topic>/unit<N>/status` (`online` / `offline`; **Publish each
  unit's state** in the mapping form; it keeps the last state while the mapping is
  stopped, so read it together with `<topic>/status`). Optionally as a **Sparkplug B**
  edge node with one device per Modbus unit (`unit1`, …): NBIRTH/NDEATH with `bdSeq`,
  DBIRTH/DDATA with `seq`, writes via DCMD, rebirth via NCMD. An offline unit gets a
  DDEATH and is left out of the births; when it answers again it gets a DBIRTH with the
  values just read (a point whose read fails is sent as null in a birth, not with its old
  value). Data goes out only after the session's NBIRTH and DBIRTHs, each metric is
  stamped with the time it was read, and a value that stays NaN is sent once. `bdSeq`
  (Int64) continues across restarts (`data/sparkplug-bdseq.json`), so a host can tell the
  Will of a session cut off by a power loss from the new session. DDATA keeps the metric
  datatypes (the specification only advises against them, and older Tahu hosts and simple
  consumers rely on them). A **primary host ID** (optional) makes the node follow that
  host application's STATE (Sparkplug 3.0 `spBv1.0/STATE/<id>`, or the older
  `STATE/<id>`): the births go out only while it is online, and when it goes offline the
  node sends NDEATH and reconnects to wait for it. There is one broker per mapping (no
  broker list to walk; a DNS name or a virtual IP in front of redundant brokers gives
  basic failover), and nothing is stored for a host that is away (no store and forward). A
  command is written once, when it is published: a retained `/set` that the broker sends
  again after a reconnect, a restart or a configuration save is ignored (and logged)
  unless **Re-apply a retained /set after every connect** is ticked; a retained Sparkplug
  NCMD/DCMD is always ignored, an empty message (clearing a retained one) is not a
  command, and a command published while ser2netweb is not connected is not delivered later.
- **MQTT commands (edge mode):** writes from `/set` and Sparkplug DCMD are made one at a
  time, in the order they came, and take turns on the bus with the poll and the Modbus/TCP
  masters; the answers on `.../set/result` keep that order. At most 32 wait for the bus: a
  command beyond them gets `error: busy`, and one that waited more than 10 s for its turn
  is not written (`error: expired: …`). A `/set` over 64 KiB gets `error: too large: …`; a
  Sparkplug NCMD/DCMD over 256 KiB, a DCMD with more metrics than its device has points and
  an NCMD with more than 64 are ignored, and what a command leaves out (metrics that are not
  writable points of the device, nulls) is logged in one line, at most once a minute. A
  rebirth asked for less than 2 s after the last births goes out once those 2 s are up. The
  broker's own limits (Mosquitto `message_size_limit`) and ACLs remain the first guard.
  Answers go out at QoS 1 (or the mapping's QoS if higher), as `/set` is subscribed. A
  `/set` may also be JSON, `{"value": 25, "id": "abc"}` (the id is optional: a string of up
  to 64 characters or an integer); its answer is JSON too, `{"id": "abc", "ok": true,
  "value": 25}` with the value read back from the device (null if it can't be read), or
  `{"id": "abc", "ok": false, "error": "…"}`, while a plain value keeps the plain `ok` /
  `error: …`. A unit that doesn't answer a write gives `error: no reply from unit N within
  T s`. Sparkplug B: after every DCMD to a writable point the point is read back and sent
  in a DDATA, changed or not (also when the write was refused, or the slave answered with
  an exception), so the host sees how it went (not for a point on unit 0 or a write-only
  one, which are never read back); a unit that did not answer gets no DDATA, and once it
  gives no reply to 3 polls in a row it goes offline (DDEATH). A point can limit what MQTT may write to it (optional, in the points
  JSON): `"min"` and `"max"` (in the point's own units, as published) and `"allowed": [0,
  1, 2]` (`[true]` for a coil that may only be switched on); a value outside them is
  refused before it reaches the bus (`error: 3000 is outside the range 0..1500 of
  'setpoint'`). They don't limit Modbus/TCP masters, and a v2.8 node (a fleet edit from
  one) or a downgrade drops them. `"poll": false` makes a writable point write-only, for a
  command register that can't be read: never polled, a write not read back. An integer
  type rounds a fraction (ties to even: `12.5` → 12) and the answer is still `ok`; the log
  line of the write names the raw value written (`raw value 12`). **Fail-safe outputs:**
  ser2netweb writes no safe value by itself when the broker or the host goes away. Use a
  heartbeat: the host writes a counter to a holding register through `/set` (or a DCMD)
  every few seconds, and the PLC's own logic (a watchdog timer) puts its outputs in a safe
  state when the counter stops changing. A device's own communication-loss timeout alone
  won't do, as ser2netweb's polling keeps talking to it. Stopping the mapping (also a save, a restart or an
  import that changes it) answers every write before the MQTT connection closes: the one
  on the bus may finish (at most 2 s; else `error: cancelled: mapping stopped (sent,
  outcome unknown)` or `(not sent)`), the ones still waiting get `error: cancelled: mapping
  stopped`, and a command that comes meanwhile gets `error: mapping stopped`; the log
  says which were not made.
- **Modbus points (edge mode):** each point is `{"name", "unit", "fn", "address", "dtype",
  "scale", …}` in the points JSON of the mapping form, or with **Edit the points as
  fields** one box of fields per point (the JSON stays what the form saves; keys without a
  field, such as `"min"`, are kept). Types: `uint16`/`int16` (one register),
  `uint32`/`int32`/`float32` (two), `int64`/`uint64`/`float64` (four, written with FC16),
  `"string"` with its `"length"` in registers (two characters each; it ends at a NUL,
  trailing spaces are dropped), and `"bool"`: a coil or discrete input, or with `"bit":
  0–15` one bit of a holding or input register; strings and register bits are read-only.
  `"word_order"` says how the device lays out a value's bytes, for reads and writes:
  `"ABCD"` (the default, high register first), `"CDAB"` (low register first, the common
  "word swap" of 32-bit meters), `"BADC"` (each register's bytes swapped) or `"DCBA"`; a
  string's characters come high byte first (`"ABCD"`) or low byte first (`"BADC"`).
  Sparkplug B sends them as Int64, UInt64, Double, String and Boolean (the other types keep
  their datatypes). Values are checked when a mapping is edited (form, REST API, import,
  fleet edit): `"unit": "1"` and `"scale": "0.1"` count as numbers, `"writable": "false"`
  as false, and what can't be used (a scale that is not a number, a misspelt key such as
  `"adress"` in the form) is refused with a message naming the point; the form says where
  invalid JSON breaks (line and column) and keeps what was typed. The keys added in 2.9
  (`word_order`, `bit`, `length`, `min`, `max`, `allowed`, `poll`) are taken only with the
  right type; a stored value the gateway can't use (a hand edit) is ignored, the default
  used, with a warning in the mapping's log at start. **Read neighbouring
  points with one request** (off by default) reads a unit's points of one function that
  lie close together with one request (at most 125 registers or 2000 coils, with up to the
  given number of unused registers in between): 100 points at 9600 baud then take a
  fraction of a second instead of several seconds per poll. A block the slave refuses
  (exception 02 or 03) is read point by point from then on (logged once), and a block
  without a reply counts as no reply for each of its points. **Read these points now** in
  the edit form reads the points as typed (not saved) through the running gateway and
  shows their values; the status (REST API) lists each point's last value, when it was
  read and why the last read failed. **Unit map** (Modbus gateways, optional): `255=1,
  0=1` sends a master's requests for unit 255 and unit 0 to RTU unit 1 and answers them
  with the id the master sent (many masters address a gateway's device as 255 or 0).
  Retries are not added: a unit that stops answering is polled once per cycle (see
  above). A v2.8 node (a fleet edit from one) or a downgrade drops `word_order`, `bit`,
  `length`, `"poll": false` (a write-only point is polled there again), the block reads and
  the unit map, and its polling fails on the new types. Every topic, payload, write and
  Sparkplug B metric in tables, with a REST API example:
  [`docs/MODBUS-MQTT.md`](docs/MODBUS-MQTT.md).
- **Client-side virtual COM:** recipes to surface a remote port as a local `COMx`/`tty`
  (pySerial `rfc2217://`; Linux ttynvt for an rfc2217 mapping, `socat` for a raw one;
  Windows com0com + hub4com's `com2tcp-rfc2217.bat`, HW VSP3) — see
  [`docs/VIRTUAL-COM.md`](docs/VIRTUAL-COM.md).
- **Full serial config:** baud (incl. custom), data/stop bits, parity, flow control
  (none/RTS-CTS/XON-XOFF/DSR-DTR), RTS/DTR on open, port locking, and RS-485 hardware
  auto-RTS (Linux `TIOCSRS485`: enable + pre/post-TX delays + RTS-during-TX level). RTS or
  DTR set to `on` / `off` is in that state from the moment the port opens (`keep` leaves
  the driver's default, on). On Windows a line set to `off` therefore never pulses; on
  Linux the kernel itself raises DTR and RTS when a port is opened, so `off` only shortens
  that pulse and can't keep a board that resets on DTR (an Arduino) from resetting at each
  open. A setting the port doesn't take (RS-485 on most USB adapters, RTS/DTR on a port
  without modem lines) doesn't stop the port from opening: the dashboard shows it under the
  mapping's state and the mapping's log says it once, and a refused RS-485 mode is switched
  off again so later changes of the port's settings (an RFC 2217 client's baud rate) work.
- **Port locking (Linux, macOS):** **Lock the port (flock)**, on by default, keeps out other
  ser2netweb mappings and programs that lock a port the same way (picocom, tio, miniterm), but
  not minicom, programs that use no lock (`cat`, `stty`, scripts), a login prompt or
  ModemManager. Two options, off by default, keep out more, as classic ser2net did:
  **Keep other programs out while the port is open** (`TIOCEXCL`: another program's open
  fails as busy, except for root's), and **Don't open a port another program has locked
  with a UUCP lock file** (minicom, cu: `LCK..` files in `/run/lock` or `/var/lock`, only
  read; ser2netweb makes none and never removes one; the dashboard names the program). On
  Windows a port is always open in one program only, and these do nothing. In Docker a port
  passed with `--device` is a device node of the container's own, and a flock locks the
  node, so it isn't expected to keep out programs on the host (with `-v /dev:/dev` it is the
  host's node); `TIOCEXCL` holds for the port itself.
- **Live port list:** privilege-free. Every 2 s a cheap check tells whether a port came or
  went (Linux: the tty devices in sysfs and the `/dev/serial` links; Windows: the COM
  ports the registry lists), and the ports are enumerated again only then, when the UI or
  the API shows a list older than 2 s, and at least every minute. On Linux every port of a
  kernel serial driver is listed (`/proc/tty/drivers`), not only the names pyserial looks
  for, so a board's own UARTs are there too (`ttymxc*`, `ttyLP*` on i.MX, `ttyTHS*` on
  Jetson, `ttyPS*` on Zynq, `ttyMSM*`, `ttyMAX*`, a Raspberry Pi's `ttyS0` on kernels
  before 6.5, ...). The kernel's placeholder `ttyS` ports without hardware are hidden, and
  udev's names are shown and selectable: `/dev/serial/by-id` (by serial number: survives
  re-plugging, in any USB socket) and `/dev/serial/by-path` (by USB socket: right while
  the adapter stays in that socket). A by-id name is made from vendor, model and serial
  number, so identical adapters without a serial number of their own (CH340, most PL2303,
  clones that all carry one number) share one, which udev gives to one of them, and which
  one can change at a reboot or a re-plug: the *Detected serial ports* list marks such adapters
  (`same USB identity`, `by-id not unique`) and the form says so; bind those by USB
  socket. In the mapping form, ↻ next to Device lists the ports again without closing the
  form, and **Other device path…** takes a path that is not listed (a board's UART such as
  `/dev/ttymxc1`, an adapter not plugged in yet, a `\\.\` name on Windows); a configured
  link such as `/dev/serial0` is shown with the device it leads to
  (`/dev/serial0 → /dev/ttyS0`) and counts as that port in use. **IP picker** from the
  machine's own addresses (every interface, localhost, the LAN addresses of both families)
  or custom.
- **IPv4 and IPv6:** a mapping on `0.0.0.0` takes IPv4 clients only, one on `::` IPv6
  clients only (TCP and UDP, on Linux and Windows alike); **All interfaces (IPv4 + IPv6)**
  takes both (`"bind_ip": "0.0.0.0", "dual_stack": true` in `config.json` and the REST
  API; a release before 2.9 reads it as `0.0.0.0`). As each of `0.0.0.0` and `::` is one
  family, a mapping on one and another on the other can share a port. `::1` (IPv6
  localhost) is a bind IP like `127.0.0.1`. IPv6 addresses are written in brackets next
  to a port (`[::1]:4001`). A UDP mapping on `::` from an older release on Linux took
  IPv4 datagrams too: it comes up with `dual_stack` on, so it still does (the start-up log
  says so until the configuration is saved again), and so does one sent without
  `dual_stack` (a REST API script, an older cluster node).
- **Find a device by its USB identity:** tick the box in the mapping form and the adapter
  is bound by VID:PID (+ serial number), so re-plugging it as `ttyUSB1` or under another
  COM number still opens the right device. Identical adapters without a serial number of
  their own (CH340, most PL2303) and the channels of a multi-port chip (FT4232H) share one
  identity: of those the configured port is opened when it is one of them, else one no
  other mapping has open, and the dashboard says it had to guess. When the bound adapter is
  not there and its port is now another USB adapter, that one is not opened: the mapping
  shows `device missing` until the bound adapter is back or the mapping is bound to the new
  one (untick and tick the box again, then save).
- **A missing or busy serial device:** the mapping shows `device missing` while the device
  isn't there (unplugged, not enumerated yet) and `reconnecting` right after an open port
  went away or when it can't be opened for another reason; it is tried again every few
  seconds (at most about 10 s apart), and at once when the port list sees a new device (a
  re-plugged adapter is used within about 2 s). A mapping bound to a USB identity finds
  its device in that list instead of enumerating the ports at every retry. The log says
  the first failure and each change of the error, then the same error only every 15
  minutes with how many attempts failed since when, and once the port opens again how long
  it was down, instead of a line at every attempt (the same for a connect-out that keeps
  failing). `/api/v1/status` counts failed opens (`open_failures`) and losses of the open
  port (`device_lost`) apart; `reconnects` counts both, as before. Under the error the
  dashboard says what it means when it can (`hint` in `/api/v1/status`; `last_error` stays
  pyserial's text): for "Permission denied" the group the device belongs to, read from the
  device itself (`dialout`, `uucp` on Arch, …), and what is missing (the user is not in
  it, was added after ser2netweb started, the container lacks it: `--group-add <gid>`); for a
  port in use, which mapping has it open, or what kind of lock another program holds. The
  same `hint` names why the system refused a listener's port (below 1024 without
  `CAP_NET_BIND_SERVICE`, a port Windows keeps), in English in the REST API and in the
  page's language on the dashboard, where it replaces the general sentence.
- **Listeners come up on their own:** a mapping whose address can't be used when it
  starts (its IP is not assigned yet at boot, or the port is still taken) shows `error`
  with "retrying in N s" and is started again, at most 30 s apart, until it works (a Stop
  ends that). Its serial port and MQTT connection are opened only once its listener is up,
  so the retries don't touch the device.
- **Dozens of mappings:** add/edit/delete/start/stop from one screen, with live status;
  search, state filter, sorting and start/stop/restart of the selected mappings (*Select
  all shown*; a bulk action acts on the ticked rows the filter shows and asks first). The
  search ignores letter case and accents, Turkish İ/ı included (`istanbul` finds
  *İSTANBUL DEPO*, `corlu` *Çorlu*), and names sort with numbers as numbers (*Hat 2*
  before *Hat 10*; detected ports COM2 before COM10). The
  table is redrawn only when what it shows changed: new byte counts update just their
  cells, connection times count on in the browser, and a background tab doesn't refresh
  it — so no flicker, and a selection, a tooltip or a click stays. The rendering runs
  outside the event loop that drives the bridges. In / Out are shown in B / KB / MB / GB
  (exact counts in the REST API and `/metrics`).
- **Per-mapping access control:** allowed IP/CIDR list, **high-priority** client IPs
  (a priority client evicts the oldest when the port is full), max connections,
  kick-old-user (a new client evicts the oldest non-priority one, never a priority
  client: while only priority clients are connected it is refused), read-only, banner,
  open/close strings, `closeon` (a Modbus gateway uses no banner, open/close strings or
  `closeon`).
- **Connection lifecycle (per mapping, TCP):** **dead-peer detection via TCP keepalive**
  — a client or remote server that vanishes without a FIN/RST (power cut, cable pull,
  Wi-Fi/VPN/NAT drop) no longer stays `ESTABLISHED` forever: with the defaults (on ·
  60 s idle · 10 s interval · 3 probes) it is dropped within ~90 s and its slot freed —
  also while the device keeps sending (Linux `TCP_USER_TIMEOUT`). Plus a **connect
  timeout** for connect-out (default 10 s), an **idle timeout** and a **max connection
  time** (e.g. 24 h); every disconnect is logged with its reason. By default TCP clients
  stay connected while the serial device is unavailable (unplugged, reconnecting) and new
  ones are taken; what they send meanwhile is dropped and counted (`bytes_dropped`). With
  **Disconnect clients while the serial device is unavailable**
  (`network.disconnect_without_device`, raw / telnet / rfc2217, off by default) they are
  disconnected when the port goes away and new ones are turned away (after the short wait
  an RFC 2217 client gets, `options.rfc2217_net_timeout_s`) until it is back, so a poller,
  a SCADA master or a virtual-COM redirector sees the outage; a connect-out mapping then
  connects only while its port is open.
- **Observability:** per-mapping traffic trace (hex/timestamp, rotated files, downloadable
  from the UI), Prometheus `/metrics`, an audit log page, live log viewer, an in-browser
  **serial console** (xterm.js over WebSocket — watch traffic or type to the device) and a
  timestamped **live hex view** of both directions. The dashboard's **In / Out** counters
  (also `bytes_in` / `bytes_out` in `/api/v1/status`, `ser2net_mapping_bytes_in_total` /
  `ser2net_mapping_bytes_out_total` in `/metrics`) are the bytes read from and written to
  the serial device, counted once however many clients share it (serial↔serial: A → B /
  B → A). Data from the network that could not be written to the device (its port was not
  open, or a UDP datagram found the queue full) is counted as dropped (`bytes_dropped`,
  `ser2net_mapping_bytes_dropped_total`), flagged on the dashboard and logged.
- **Notifications:** a webhook (JSON, Slack, Microsoft Teams, Discord, Telegram) and/or
  e-mail (SMTP). On by default: a device is lost or comes back, a mapping fails to start, a
  connect-out mapping loses or regains its remote server. Ticked in Settings when wanted:
  clients come and go, a sign-in fails, the data disk is almost full (below the free space
  at which traces pause; all.log warns in any case), **no data from a device** (a mapping's *No-data alarm* in its
  options: the port is open but the device has sent nothing for that many seconds, or, for
  a device that speaks only when asked, what was sent to it got no answer; also shown on
  the dashboard, in the mapping's log and in `/metrics`, with the time of the device's last
  byte, `ser2net_mapping_last_rx_timestamp_seconds`) and data from it again, a **cluster
  node offline** (a node heard from that is no longer heard from for 20 s, or a configured
  peer that misses two answers a minute apart) and back, and **ser2netweb started** (15 s
  after a start: which mappings run, and whether the previous run was ended by the event
  loop watchdog), and a **Modbus unit** that a gateway polls **stops answering** (no reply
  to any of its points in 3 polls in a row) or answers again, and a **TLS certificate about
  to expire** (the admin UI's and each enabled TLS mapping's, read once a day: one message
  when fewer than 30, 7 and 1 days are left and one once it has ended — once per
  certificate and threshold, also across restarts; a renewed certificate starts afresh),
  and the **system clock stepped** while ser2netweb runs (see *Time synchronisation*).
  Repeats are suppressed, and a test message can be sent. The same events are logged in all.log whether or not they are
  sent.
  With a public URL (Settings → Reverse proxy) each message ends with it, and the JSON
  format has a `url` field. The client events send the client's IP address, a failed
  sign-in the account name (or `(unknown user)`) and the IP address; *Leave out personal
  data* sends addresses only as their network (`10.0.5.x`, IPv6 /48) and no account names,
  and e-mail can go to each recipient separately. Messages are in the default language set
  under *Web UI access* (English while that is the browser's language). SMTP user names and passwords and webhook URLs may contain Turkish
  (any non-ASCII) letters: the credentials are sent UTF-8 encoded (SMTP AUTH PLAIN / LOGIN /
  CRAM-MD5), the URL IDNA- and percent-encoded.
- **Connect-out status:** a connect-out mapping's state stays the serial side's; its link
  to the remote server is shown next to it ("link down" with the reason, which the
  Problems filter counts; "connecting"), in `/api/v1/status` (`link`: `connecting` / `up`
  / `down`, `link_error`, `link_since`, `connect_failures`) and in the fleet view. A link
  closed and connected again at once (max connection time, idle timeout) is no outage.
- **Security:** password (set on first access, scrypt), optional two-factor sign-in
  (TOTP codes from an authenticator app) per local account, CSRF, signed-cookie sessions,
  login rate-limiting, strict security headers; sessions end at logout, on a password
  change, per user or for everyone (Settings), and a lost password is reset with
  `ser2netweb --set-password` (see Security).
  For trusted networks the login requirement can be switched off under **Settings → Web
  UI access** (visitors then use the UI with a chosen role; see Security).
- **Multi-user / RBAC:** `admin` / `operator` / `viewer` roles — viewer is read-only and
  watches what the devices send; operator manages mappings and also sees what is sent to
  the devices (hex view, trace files: where a password typed at a device prompt ends up);
  admin does everything plus user management. Existing single-password setups migrate
  automatically to one `admin` user.
- **LDAP / Active Directory auth (optional):** users authenticate by binding to the
  directory; LDAP group membership maps to a role. Works alongside local accounts;
  needs `ldap3`. LDAPS / StartTLS check the server's certificate (the system's CAs or
  your own CA file). Setup for Active Directory and OpenLDAP: [`docs/SSO.md`](docs/SSO.md).
- **OIDC single sign-on (optional):** authorization-code flow with PKCE, with Keycloak /
  Entra ID / Google / Okta; the id_token is validated (signature/iss/aud/exp/nonce) and a
  claim maps to a role. Needs `authlib`. Keycloak, Entra ID and Google settings, and what
  to look for in `all.log`: [`docs/SSO.md`](docs/SSO.md).
- **REST API:** a JSON API (`/api/v1`) for automation — mapping CRUD, start/stop/restart,
  status and ports; **bearer-token** auth (with a role — `viewer` is read-only); OpenAPI
  3.0 (`/api/v1/openapi.json`). The token is generated in Settings.
- **LAN cluster (fleet management):** nodes on the same network **auto-discover** each
  other (signed UDP broadcast — no mDNS) and one node shows **every node's mappings in a
  single table**, each row tagged with its host (name + IP) and each node's **health**
  (uptime · version · running/total). Operators can **remotely start/stop/restart and edit**
  a peer's mappings from that view. For routed/L3 networks broadcast can't reach, add
  **manual peers** (host:port). Trust = the **same shared key** on every node (any
  characters, Turkish letters too; it is Unicode-normalised (NFKC) before use, so the same
  key typed or pasted elsewhere matches); off by default, enable under Settings → LAN cluster.
  Requests between nodes are **signed** with the key instead of carrying it (only nodes
  running 2.8.0 or older still get the key itself; see Security → LAN cluster). Peers are
  always contacted directly, never through an HTTP proxy (`HTTP(S)_PROXY` / `ALL_PROXY`
  apply to webhooks and SSO only).
- **Deployment:** official **Docker** image + `docker-compose`; **systemd** unit;
  Linux+Windows × Python 3.10–3.14 **CI** (GitHub Actions).
- **MQTT publishing (optional):** publish each mapping's serial lines to an MQTT
  broker (host/port/topic/QoS/TLS/credentials) with a retained birth/death status:
  `<topic>/status` reads `online` after every connect to the broker (a reconnect too),
  and `offline` when the mapping stops or, as the Last-Will, when the broker loses the
  connection. With a blank client ID the broker can't tie a new connection to the one
  that broke, whose Will it may publish only after the new `online` (it waits up to 1.5 ×
  the 30 s keepalive), so `online` is sent once more 50 s after each connect. The
  connection is logged as it changes (connecting, connected, refused with the broker's
  reason, can't connect with the error — DNS, no answer, a TLS certificate — and lost; each
  problem once until it changes), as is a subscription the broker refuses and the first
  failed publish to a topic. The mapping's status (`mqtt` in the REST API: state, broker,
  since, error, dropped messages) and `/metrics` carry it, and the dashboard shows
  **⚠ MQTT: no connection to …** with the reason. A base topic with `+` or `#` (no message
  can be published to it) keeps MQTT off for the mapping, with a log line. Edits (the
  mapping form, the REST API, an import, a cluster node's save) refuse such a base topic,
  a plain-format point name with `+`, `#` or the null character (`/` is fine), and a
  point whose topic the mapping already uses: `status`, `unit<N>/status` with the unit
  states on, or another point's `<point>/set` or `/set/result`. Two enabled mappings on
  the same broker (host:port) can't share an explicit client ID (the broker keeps one
  connection per ID), a Sparkplug B group and edge node ID, or a command topic — a `/set`
  (or a serial mapping's `<topic>/tx`) both would take, or one that the other publishes
  its data to. Mappings that only
  publish to the same topics (a shared base topic, whose `<topic>/status` then shows
  whichever connected last) are allowed, with a warning in the mapping's log when it
  starts. A config.json that already has such a clash keeps loading and running: start-up
  and each start of the mapping log it, and an edit of either mapping has to resolve it
  (or disable one of them); starting a disabled mapping that would clash with an enabled
  one (Start, a bulk start, the REST API, a cluster node) is refused with the reason, as for
  a shared serial port. Two ser2netweb installations can't see each other's mappings:
  give each its own topics and client IDs. Over **TLS**
  the broker's certificate must chain to a CA the system trusts — or, with a **CA
  certificate file** (PEM, or DER), to one in that file only — and name the broker host
  (a DNS name or an IP address); **Check the broker's host name** off accepts a
  certificate that doesn't (it must still come from a trusted CA). Every certificate in
  the CA file is trusted as it is: an intermediate CA, or a self-signed broker's own
  certificate, is enough. A **client
  certificate** (PEM; its key in its own file or in the same one, an encrypted key with
  its **password**) is presented to a broker that asks for one (mutual TLS, such as AWS
  IoT Core); the form never shows a stored key password (a blank field keeps it, *Remove
  the stored password* clears it). The mapping form and the REST API refuse files that
  can't be used; an
  import takes such a mapping with a warning, and a mapping whose files are gone keeps
  loading and runs without MQTT (logged, shown on the dashboard) — never in clear text.
  A relative path names a file in the data folder, as for a mapping's TLS files.
  In Docker put the files under `/data` (for example `/data/certs/`); the Windows service
  must be able to read them. While the
  broker is away, QoS 0 messages are lost (counted as dropped); at QoS 1/2 up to 10 000
  messages per mapping wait and go out after the reconnect — beyond that the newest are
  dropped (counted, logged once per outage) — and polled Modbus values keep only the
  latest value of each point instead (no burst of stale readings after an outage).
  Stopping or restarting a mapping waits at most 3 s for the broker to take its last
  message (`offline`, or a Sparkplug NDEATH; beyond that the Last-Will says the same),
  without holding up the other mappings or the UI. **Retain the data** (plain format)
  publishes the serial lines and the polled Modbus values retained, so a subscriber that
  connects later (Home Assistant, Node-RED, Telegraf) gets the last one at once instead of
  waiting for the next poll or line (Sparkplug B never retains data, command results are
  not retained; to drop a retained value, publish an empty retained message to its
  topic). Modbus values are plain text by default; **Value format: JSON** publishes
  `{"value": 21.5, "ts": 1731400000123, "quality": "good"}` (`ts`: when it was read, in ms
  since the epoch; a bit is `true` / `false`, a NaN reading `null`), and once
  `{"value": null, …, "quality": "bad"}` for a point whose read fails or whose unit stops
  answering, until it reads again. The client speaks MQTT 3.1.1.
  Needs `paho-mqtt` (optional; the bridge runs normally without it). A line ends at **LF**
  (the default; a CR before it is left out), **CR** (scales, analyzers and other devices
  that end their records with CR; an LF next to it is left out) or **CR or LF**
  (`mqtt.line_end`); past 64 KiB without a line end the oldest bytes are dropped, and the
  log says so once. **Write messages on `<topic>/tx` to the device** (`mqtt.tx_enabled`,
  off by default) writes each message to the serial port as it is, in one piece between
  what TCP clients send, and answers on `<topic>/tx/result` (`ok` / `error: …`: read-only,
  too long, busy with 64 KiB waiting, port not open); a retained one is never written. A
  **poll string** (`options.poll_string`, with `\r \n \t \\ \xHH`) is sent every **poll
  interval** while the port is open and no TCP client is connected (or always, with **Also
  while a TCP client is connected**), so a device that only answers queries reaches MQTT, a
  trace or a UDP target without an external poller holding the TCP connection.
- **Migrating from classic ser2net:** `ser2net.yaml` (4.x) and `ser2net.conf` (3.x) files
  import through Settings with a preview, which shows each mapping as it will be (enabled
  or disabled, TLS, any client or how many allowed, read-only) with its own warnings in
  its row; applying it writes the warnings to the log. Options without an equivalent are
  listed as warnings, and a connection that would be more open here (a gtlssh login, TLS
  client certificates without their `CA=` file on this host, TLS whose certificate is not on
  this host, a write-only device) comes in disabled; `ssl(clientauth,CA=...)` with the CA file
  here keeps checking client certificates (**Client CA certificate file**). ser2net's port locking carries over: 4.x `flock` (on by default) becomes **Lock
  the port** plus **Keep other programs out**, `uucplock` (on by default; 3.x always)
  **Don't open a port another program has locked**; one warning says what still differs
  (a mapping holds its port open from its start, and makes no lock files). Listen
  addresses keep their family (`ipv6,::1` stays `::1`, `ipv6` without an address is `::`,
  IPv6 only; `ipv6n4`, an IPv6 socket that takes IPv4 clients too, is **All interfaces
  (IPv4 + IPv6)**); an accepter with neither (`tcp,2000`), on which ser2net took IPv4 and IPv6
  clients, comes in on `0.0.0.0`, IPv4 only, and a warning names those to switch to **All
  interfaces (IPv4 + IPv6)**. An RFC 2217 connection's signature (4.x `signature`, 3.x
  `SIGNATURE`) becomes its **RFC 2217 signature**; `hangup-when-done` and `nobreak` are kept
  but have no effect here (the port stays open between connections), and the import says so.
  Merging such a file again later takes only what the file says and keeps what was set on
  its mappings here (USB identity, RS-485, MQTT, Modbus points, a disabled mapping stays
  disabled; the RFC 2217 signature comes only with the first import); the bind IP, the
  allowed clients and read-only are taken from the file only when that is no more open than
  the mapping is, and TLS is never turned off: it never opens a mapping up, and the preview
  lists what each update changes and keeps.
- **Turkish / English UI:** pick **TR / EN** in the top bar (remembered per browser);
  otherwise the browser's language or the admin's default (Settings → Web UI access), which
  notifications use as well. Logs, the audit log and the REST API stay English.
- **Fully offline:** the required dependencies are bundled as wheels, so no internet is
  needed (the optional ones: see *Offline install*; the standalone binary has them all).

---

## 🧰 Requirements

- **Python 3.10+** installed, with pip (Debian / Ubuntu / Raspberry Pi OS ship it apart:
  `sudo apt install python3-pip`). Nothing else — dependencies ship in `vendor/wheels/` and
  install to `./lib` on first run (offline), on any OS and CPU. An older `python3` (RHEL /
  AlmaLinux / Rocky 8 and 9) is refused with a message: install a newer one
  (`sudo dnf install python3.11`; `./start.sh` picks it up) or use the standalone binary.
  CI tests 3.10–3.14; a Python past its end of life (3.10) stays supported until a
  dependency pinned for it has a known vulnerability no release for it fixes, announced
  one minor release ahead ([CONTRIBUTING → Python versions](CONTRIBUTING.md#python-versions)).
- Linux: membership in the serial devices' group to **open** serial ports (listing ports
  needs no privilege). It is `dialout` on Debian, Ubuntu, Raspberry Pi OS, Fedora, RHEL and
  openSUSE, but `uucp` on Arch, Manjaro and EndeavourOS (which have no `dialout` group); a
  device says which: `stat -L -c %G /dev/ttyUSB0`.
  ```bash
  sudo usermod -aG "$(stat -L -c %G /dev/ttyUSB0)" "$USER"   # then log out/in
  ```
  A port that won't open, a device that disappears, a Raspberry Pi UART:
  [`docs/LINUX-SERIAL.md`](docs/LINUX-SERIAL.md).
- Windows: no extra privileges for COM ports.
- Optional: `cryptography` (`requirements-optional.txt`) to generate a self-signed admin
  certificate, else the `openssl` command is used if there is one.

---

## 🚀 Install & run

| Way | Runs on | Needs |
|---|---|---|
| [Standalone binary](#standalone-binary-nothing-else-to-install) | Windows x64; Linux x86_64 and arm64 with glibc ≥ 2.28 (Raspberry Pi 3/4/5 with a 64-bit OS) | nothing |
| [winget](#windows-package-managers-winget-scoop-chocolatey) (`winget install haliskilic.ser2netweb`) | Windows 10/11, Windows Server 2016 or newer (x64) | winget; data in `%LOCALAPPDATA%\ser2netweb` |
| [Scoop](#windows-package-managers-winget-scoop-chocolatey) (bucket `haliskilic/scoop-bucket`) | Windows 10/11, Windows Server 2016 or newer (x64) | Scoop; data in `~\scoop\persist\ser2netweb\data` |
| [Chocolatey](#windows-package-managers-winget-scoop-chocolatey) (`choco install ser2netweb`) | Windows 10/11, Windows Server 2016 or newer (x64) | Chocolatey; data in `%LOCALAPPDATA%\ser2netweb` |
| [`.deb` / `.rpm` package](#linux-packages-deb--rpm) (apt / dnf) | Debian 11+, Ubuntu 20.04+, 64-bit Raspberry Pi OS; Fedora, RHEL / AlmaLinux / Rocky 8+ (x86_64, arm64) | nothing; data in `/var/lib/ser2netweb` |
| [APT repository](#package-repositories-apt-dnf-zypper) | Debian 11+, Ubuntu 20.04+, 64-bit Raspberry Pi OS (amd64, arm64) | nothing; updates come with `apt upgrade` |
| [DNF / YUM repository](#package-repositories-apt-dnf-zypper) (zypper too) | Fedora, RHEL / AlmaLinux / Rocky 8+, openSUSE Leap 15.6+ (x86_64, aarch64) | nothing; updates come with `dnf upgrade` / `zypper update` |
| [Docker](#docker) (GHCR, Docker Hub) | Linux hosts: amd64, arm64, arm/v7 (32-bit Raspberry Pi OS) | Docker |
| [pipx / pip](#pipx--pip) (PyPI) | any OS and CPU | Python 3.10+, pipx; data in the user's data folder |
| [Homebrew](#homebrew-macos-linux) (`brew install haliskilic/tap/ser2netweb`) | macOS (Apple silicon, Intel), Linux | Homebrew; data in `$(brew --prefix)/var/ser2netweb` |
| [AUR](#arch-linux-aur) (`ser2netweb-bin`) | Arch Linux, Manjaro, EndeavourOS (x86_64, aarch64) | an AUR helper or `makepkg`; data in `/var/lib/ser2netweb` |
| [Snap](#snap) (`snap install ser2netweb`) | Linux with snapd (amd64, arm64): USB serial adapters only | snapd; data in `/var/snap/ser2netweb/common/data` |
| From source (below) | any OS and CPU: also macOS, 32-bit ARM, Windows on ARM | Python 3.10+ with pip; offline from the bundled wheels |

```bash
# Linux / macOS / Raspberry Pi (any CPU)
python3 ser2netweb.py            # or: ./start.sh

# Windows
start.bat
```

### Standalone binary (nothing else to install)
Every release attaches a Windows `.exe` and a Linux binary to **GitHub Releases**. Each
is one file with everything inside — Python, OpenSSL, the VC++ runtime (Windows), MQTT /
LDAP / OIDC support — so no Python, `openssl` or other package is needed. Download and
run; config/logs are written to `data/` next to the binary.

| File | Runs on |
|---|---|
| `ser2netweb-windows-x64.exe` | Windows 10/11, Windows Server 2016 or newer (x64) |
| `ser2netweb-linux-x64` | x86_64 Linux with glibc ≥ 2.28: RHEL / AlmaLinux / Rocky 8+, Debian 11+, Ubuntu 20.04+, Fedora, openSUSE Leap 15 (not musl/Alpine — use Docker there) |
| `ser2netweb-linux-arm64` | 64-bit ARM Linux with glibc ≥ 2.28: Raspberry Pi 3/4/5 (64-bit Raspberry Pi OS 11+ or Ubuntu), ARM industrial gateways, AWS Graviton, the same distributions |

Up to 2.9.x these files were called `ser2net-*` (`ser2net-linux-x64`, …); from 3.0.0 on a
release has them under the `ser2netweb-*` names only, so a download script that names the
old ones needs the new names.

Other platforms — 32-bit ARM (32-bit Raspberry Pi OS, armv7 gateways), macOS, Windows on ARM:
run from source (`python3 ser2netweb.py`), which works offline there too (see *Offline install*),
or on 32-bit ARM the Docker image (arm/v7).
Windows 11 on ARM also runs the x64 exe (emulated).

Every release also publishes `SHA256SUMS`, a CycloneDX SBOM per binary (`*.cdx.json`), the
license texts, `ser2netweb-standalone.service`, the systemd unit for the Linux binaries,
`ser2netweb-install.sh`, which installs a binary with it (see *Run as a service*), and the
Linux binaries as `.deb` / `.rpm` packages (see *Linux packages*); the binaries and the
packages carry GitHub build provenance:
`sha256sum -c SHA256SUMS --ignore-missing`,
`gh attestation verify ser2netweb-linux-x64 -R haliskilic/ser2netweb`. The Windows exe is **not
code-signed yet** (it will be once a signing service is configured —
[`docs/SIGNING.md`](docs/SIGNING.md)), so SmartScreen may warn on first run; verify it on
Windows with `Get-FileHash` against `SHA256SUMS` and `gh attestation verify`, then *More
info → Run anyway* ([`docs/WINDOWS.md`](docs/WINDOWS.md)).

Every pull request and release proves it (`tests/test_standalone.py`): the bundle may
need nothing but OS-base libraries (glibc ≤ 2.28 / Windows system DLLs; Linux on x86_64
and arm64), the binary must
work with an empty `PATH` (no Python, no `openssl`), and the Linux one must run in clean
AlmaLinux 8/9, Debian 11/12, Ubuntu 20.04/22.04/24.04/26.04, openSUSE Leap 15.6 and Fedora
containers, including a serial port at a rate without a termios constant (250000 baud).

Linux notes: `chmod +x ser2netweb-linux-x64`; serial ports need the devices' group
(`dialout`; `uucp` on Arch, see Requirements). The binary unpacks itself to `$TMPDIR`
(default `/tmp`) on start — if `/tmp` is mounted `noexec`, point `TMPDIR` at a directory
that isn't (`ser2netweb-install.sh` does that for the service). While it runs it keeps that
copy locked and its access times fresh, so tmp cleaners (systemd-tmpfiles, tmpreaper)
don't delete it under a long-running instance; a copy left behind by a killed process
still ages out as usual. For certificate checks (MQTT over TLS, OIDC, LDAPS / StartTLS,
HTTPS webhooks, SMTP over TLS) it uses the distribution's CA bundle, or `SSL_CERT_FILE` /
`SSL_CERT_DIR` if set (unless a CA file is set for the MQTT mapping or for LDAP).

`ser2netweb --about` shows the versions (Python, OpenSSL, components) and where each
component is loaded from, with the known folders shortened (`<python>`, `<ser2netweb>`,
`<bundle>`, `<override>`, `~`) so the account name in a path stays out — add it to bug
reports, and mask IP addresses, user names and the host name in log lines you paste.
`ser2netweb --licenses` prints the license text of every component inside the
executable (releases also attach it as `*.THIRD-PARTY-LICENSES.txt`).

Build it yourself — Windows: `pwsh tools\build_standalone_windows.ps1` (what the release
runs: the pinned python-build-standalone interpreter, the hash-locked packages, license
texts and SBOM), or with a Python 3.12 of your own
`pip install --require-hashes --only-binary=:all: -r requirements-lock.txt -r requirements-build.txt && pyinstaller ser2netweb.spec`
(the spec then writes the `--licenses` text itself, from that environment);
Linux (in an old-glibc container, so the result runs on older distributions too):
`docker run --rm -v "$PWD:/src" -w /src almalinux:8 bash tools/build_standalone_linux.sh`.

On first run:
1. The **console** asks which local IP the admin UI binds to (machine IPs, every
   interface for IPv4, IPv6 or both, or custom) and a port (default 8080).
   Headless/service default is the safe **127.0.0.1**.
2. Open the printed URL; **set the admin password** on the first screen. Open it by the
   machine's IP address or `localhost`: reached through another host name (a reverse
   proxy, the Docker host's name) the setup page also asks for the one-time code in
   `data/setup-code.txt`, so that no other web page can claim a new install (DNS
   rebinding). No browser at hand: `ser2netweb --set-password admin` (see Security).
3. From the dashboard, **+ Add mapping**: pick a COM/tty and map it to an IP:port.

Change the bind IP later:
```bash
python3 ser2netweb.py --reconfigure
```

### Windows package managers (winget, Scoop, Chocolatey)
The same `ser2netweb-windows-x64.exe` of each release (checked against its SHA-256), put on
`PATH` as `ser2netweb` and kept up to date by the package manager:
```powershell
winget install haliskilic.ser2netweb        # later: winget upgrade haliskilic.ser2netweb
scoop bucket add haliskilic https://github.com/haliskilic/scoop-bucket
scoop install haliskilic/ser2netweb         # later: scoop update ser2netweb
choco install ser2netweb                    # (elevated) later: choco upgrade ser2netweb
```
The package manager owns the program's folder, so ser2netweb keeps its data out of it
(`ser2netweb --about` names the package manager, `ser2netweb --help` the data folder;
`--data-dir` chooses another):

| | Data (`config.json`, backups, logs) |
|---|---|
| winget | `%LOCALAPPDATA%\ser2netweb` (3.0.0: `data\` in winget's package folder, which upgrades keep and later versions go on using) |
| Scoop | `~\scoop\persist\ser2netweb\data`, the folder Scoop keeps across updates (`scoop uninstall --purge ser2netweb` deletes it) |
| Chocolatey | `%LOCALAPPDATA%\ser2netweb` (from the first release that recognises Chocolatey's folder; 3.0.0 isn't pushed there) |

As a service: `ser2netweb --install-service` from an elevated prompt copies the exe to
`C:\Program Files\ser2netweb\` (data in `C:\ProgramData\ser2netweb\`), as with the
downloaded exe, so the package manager's upgrades don't reach the service: after one, run
`ser2netweb --install-service` again; before removing the package,
`ser2netweb --uninstall-service` ([`docs/WINDOWS.md`](docs/WINDOWS.md#winget-scoop-and-chocolatey)).

### Linux packages (.deb / .rpm)
Every release also has the Linux binary as a package, for x86_64 and arm64:
`ser2netweb_X.Y.Z_amd64.deb` / `ser2netweb_X.Y.Z_arm64.deb` (Debian 11+, Ubuntu 20.04+, 64-bit
Raspberry Pi OS) and `ser2netweb-X.Y.Z-1.x86_64.rpm` / `ser2netweb-X.Y.Z-1.aarch64.rpm`
(Fedora, RHEL / AlmaLinux / Rocky 8+), X.Y.Z being the release's version. Download it (and
`SHA256SUMS` to check it), then:
```bash
sudo apt install ./ser2netweb_X.Y.Z_amd64.deb      # Debian, Ubuntu, Raspberry Pi OS
sudo dnf install ./ser2netweb-X.Y.Z-1.x86_64.rpm   # Fedora, RHEL, AlmaLinux, Rocky
sudo systemctl enable --now ser2netweb             # installing it doesn't start it
```
It installs the binary as `/usr/bin/ser2netweb`, its unit as
`/usr/lib/systemd/system/ser2netweb.service` (`ser2netweb-standalone.service` with that path:
the same sandbox), the account `ser2netweb` and its data directory `/var/lib/ser2netweb`
(0700), and the licence texts in `/usr/share/doc/ser2netweb`. Installing it neither enables
nor starts the service; `sudo systemctl enable --now ser2netweb` does, and the web UI then
listens on `127.0.0.1:8080` (for the network: `sudo systemctl edit ser2netweb`, see *Run as
a service*). Without `--data-dir` the packaged `ser2netweb` works on that data directory:
`sudo -u ser2netweb ser2netweb --set-password admin` sets the first password. A newer
package (`apt install` / `dnf install` of its file) restarts a running service; removing it
(`sudo apt remove ser2netweb`, `sudo dnf remove ser2netweb`) stops and disables the service
and keeps `/var/lib/ser2netweb` and the account (delete them by hand). It is unrelated to the
classic ser2net daemon's `ser2net` package: both can be installed.
`gh attestation verify ser2netweb_X.Y.Z_amd64.deb -R haliskilic/ser2netweb` checks where it
was built. The package doesn't move a 2.9.x install (`ser2net.service`, `/var/lib/ser2net`): `ser2netweb-install.sh`
does (*Upgrading from 2.9.x* under *Run as a service*). A binary installed by
`ser2netweb-install.sh` has its unit in `/etc/systemd/system`, which hides the package's:
switch with `sudo sh ser2netweb-install.sh --uninstall` (the data stays), then the package.

### Package repositories (apt, dnf, zypper)
The same packages also come from signed repositories on GitHub Pages,
<https://haliskilic.github.io/ser2netweb/>, so the package manager keeps them updated (the
newest three releases, x86_64 and arm64; [docs/PACKAGES.md](docs/PACKAGES.md) has updating,
removing and how they are checked):
```bash
# Debian, Ubuntu, Raspberry Pi OS (64-bit)
sudo curl -fsSLo /usr/share/keyrings/ser2netweb-archive-keyring.gpg https://haliskilic.github.io/ser2netweb/ser2netweb-archive-keyring.gpg
sudo curl -fsSLo /etc/apt/sources.list.d/ser2netweb.sources https://haliskilic.github.io/ser2netweb/ser2netweb.sources
sudo apt update
sudo apt install ser2netweb
# Fedora, RHEL, AlmaLinux, Rocky
sudo curl -fsSLo /etc/yum.repos.d/ser2netweb.repo https://haliskilic.github.io/ser2netweb/ser2netweb.repo
sudo dnf install ser2netweb
# openSUSE
sudo rpm --import https://haliskilic.github.io/ser2netweb/ser2netweb-archive-keyring.asc
sudo zypper addrepo https://haliskilic.github.io/ser2netweb/ser2netweb.repo
sudo zypper refresh
sudo zypper install ser2netweb
sudo systemctl enable --now ser2netweb             # installing it doesn't start it
```
They are signed with the key `B587 60E7 C0B0 515E 81D2  50F9 8164 D694 711B B799` (RSA
4096, until 2031-11-12; `ser2netweb-archive-keyring.asc` / `.gpg` on the site and
[`packaging/ser2netweb-archive-keyring.asc`](packaging/ser2netweb-archive-keyring.asc)):
dnf shows it on the first install, accept it only with this fingerprint. The `.rpm` files themselves aren't signed;
dnf and zypper check the signed repository metadata, which holds the SHA-256 of every
package (`repo_gpgcheck=1`, `gpgcheck=0`).

### pipx / pip
The PyPI package `ser2netweb`, for any OS and CPU with Python 3.10 or newer:
```bash
pipx install ser2netweb             # or "ser2netweb[all]": also MQTT, LDAP, OIDC, certificates
ser2netweb                          # the first run asks for the web UI's address
pipx upgrade ser2netweb
```
pip installs the dependencies from PyPI (ranges around the pins of `requirements.txt`), so it
needs the network or an index mirror; the wheels in `vendor/wheels` are for the source
checkout. The extras `mqtt` (paho-mqtt), `ldap` (ldap3), `oidc` (authlib), `tls`
(cryptography: self-signed certificates in-process) and `all` add the optional packages
(later: `pipx inject ser2netweb paho-mqtt`). A pip / pipx install keeps its data in the
user's data folder, never next to the package:

| OS | Data folder |
|---|---|
| Linux and other Unix | `$XDG_DATA_HOME/ser2netweb`, else `~/.local/share/ser2netweb` |
| macOS | `~/Library/Application Support/ser2netweb` |
| Windows | `%LOCALAPPDATA%\ser2netweb` |

`--data-dir` (or `--config`) chooses another; `ser2netweb --help` names this one and
`ser2netweb --about` says `Build: Python package (pip / pipx)`. `python -m ser2netweb` runs
the same. For a service take the binary, the packages or Docker: a pip install isn't a
Windows service (`--install-service` refuses), and it has no systemd unit of its own.

### Homebrew (macOS, Linux)
The tap `haliskilic/tap` has a formula that builds ser2netweb from the release's source into
a virtual environment of Homebrew's Python 3.14, with every component the binaries carry
(MQTT, LDAP, OIDC; `cryptography` from Homebrew's own formula):
```bash
brew trust haliskilic/tap                 # once: Homebrew loads another tap's formulae once trusted
brew install haliskilic/tap/ser2netweb
brew services start ser2netweb            # the web UI on 127.0.0.1:8080, also after a restart
ser2netweb --data-dir "$(brew --prefix)/var/ser2netweb" --set-password admin
```
The service runs as you (a launchd agent on macOS, a systemd user unit on Linux) with its
data, `all.log` too, in `$(brew --prefix)/var/ser2netweb`; `brew services info ser2netweb`
shows it. After `brew upgrade ser2netweb`: `brew services restart ser2netweb`;
`brew uninstall ser2netweb` keeps the data folder. `--data-dir` as above works with every
version; after 3.0.0 that folder is also the default of a Homebrew install, and
`ser2netweb --about` says `Build: Homebrew formula`. 3.0.0 without `--data-dir` kept its data
in the user's data folder (as a pip install does): while that folder has a `config.json` and
`$(brew --prefix)/var/ser2netweb` has none, later versions go on using it. Serial ports: `/dev/cu.usbserial-*` /
`/dev/cu.usbmodem*` on macOS; on Linux your account needs the ports' group (`dialout`, `uucp`
on Arch). The tap proposes each new release's formula within a day (its workflow "bump");
`packaging/homebrew/make_formula.py` makes it.

### Arch Linux (AUR)
The AUR package `ser2netweb-bin` is the release's binary for x86_64 and aarch64, installed
as the `.deb` installs it:
```bash
yay -S ser2netweb-bin                     # or paru, or by hand:
# git clone https://aur.archlinux.org/ser2netweb-bin.git && cd ser2netweb-bin && makepkg -si
sudo systemctl enable --now ser2netweb    # installing it doesn't start it
sudo -u ser2netweb ser2netweb --set-password admin
```
It installs `/usr/bin/ser2netweb`, the unit `/usr/lib/systemd/system/ser2netweb.service`
(`ser2netweb-standalone.service` with that path and `SupplementaryGroups=uucp`, Arch's serial
group), the account `ser2netweb` and `/var/lib/ser2netweb` (sysusers.d / tmpfiles.d, as the
`.deb`) and the licence texts in `/usr/share/licenses/ser2netweb-bin`. An upgrade doesn't
restart a running service (`sudo systemctl try-restart ser2netweb`); removing the package
keeps the data and the account. It is unrelated to the classic daemon's `ser2net` package:
both can be installed. Every release updates it (`.github/workflows/aur.yml`, which builds and
installs it in an Arch container first); `packaging/aur/make_pkgbuild.py` makes the
`PKGBUILD`.

### Snap
The snap `ser2netweb` (strict confinement; amd64, arm64) is the release's binary with a
service that is off until you enable it:
```bash
sudo snap install ser2netweb
sudo snap connect ser2netweb:raw-usb            # USB serial adapters (not connected by itself)
sudo snap connect ser2netweb:hardware-observe   # the port list's details (optional)
sudo snap start --enable ser2netweb.daemon      # the web UI on 127.0.0.1:8080
sudo ser2netweb --data-dir /var/snap/ser2netweb/common/data --set-password admin
sudo snap set ser2netweb bind-ip=0.0.0.0 port=8080   # the web UI on the network (use TLS there)
```
A strictly confined snap opens a serial device only through an interface. `raw-usb`, which
snapd itself provides on every Ubuntu and which you connect by hand, allows `/dev/ttyUSB*`
and `/dev/ttyACM*`: USB serial adapters, also by their `/dev/serial/by-id` names.
`serial-port` needs a slot that a gadget snap (Ubuntu Core) or snapd's experimental hotplug
provides. Built-in UARTs (`/dev/ttyS*`, a Raspberry Pi's `/dev/ttyAMA0` / `serial0`) can't
be reached from the snap on a regular Ubuntu: use the `.deb`, the binary or Docker for them.
The service runs as root inside the confinement (as snap services do), with its data in
`/var/snap/ser2netweb/common/data`, which `sudo snap remove --purge ser2netweb` deletes;
`sudo snap logs ser2netweb.daemon` shows its log. `snap set ser2netweb port=8090` alone is
that port on `127.0.0.1`; `snap unset ser2netweb bind-ip port` goes back to `config.json`'s
address. A snap with classic confinement (no sandbox) would need a
Snap Store review the maintainer asks for; there is none.

### Command-line options
`python3 ser2netweb.py [options]`, `ser2netweb-linux-x64 [options]`, `ser2netweb-windows-x64.exe
[options]` (`--help` lists them). `ser2net.py`, the start script's name up to 2.9.x, still
works and runs the same, so units and scripts that name it need no change:

| Option | What it does |
|---|---|
| `--data-dir DIR` | where `config.json`, its backups, the logs and traces live (default: `data/` next to `ser2netweb.py` or the binary, for Scoop's in its persist folder; `/var/lib/ser2netweb` for the `.deb` / `.rpm` / AUR package's; the user's data folder for a pip / pipx, winget or Chocolatey install, see *pipx / pip* and *Windows package managers*; `$(brew --prefix)/var/ser2netweb` for Homebrew's, or the user's data folder that 3.0.0 used there, see *Homebrew*; `/var/snap/ser2netweb/common/data` for the snap's, as root); give ser2netweb a folder of its own |
| `--config FILE` | a `config.json` elsewhere; its folder is the data directory then |
| `--reconfigure` | ask again for the admin UI's address and port on the console, then start |
| `--online` | let the dependency bootstrap fall back to PyPI for a wheel `vendor/wheels/` lacks (source installs) |
| `--no-bootstrap` | skip the dependency bootstrap (the packages are importable already) |
| `--version` | print the version and exit |
| `--about` | print the versions of Python, OpenSSL and the components, and where each is loaded from |
| `--licenses` | print the licence texts of ser2netweb and every bundled component |
| `--healthcheck` | exit 0 when this data directory's running instance answers on `/healthz`, else 1 |
| `--set-password USER` | set a local account's password (making it an admin account if it doesn't exist), sign its sessions out and turn its two-factor sign-in off; also while ser2netweb runs |
| `--sign-out-all` | end every web UI session (a new session key) |
| `--reload` | have the running ser2netweb of this data directory apply `config.json` again (SIGHUP and `systemctl reload` do the same) |
| `--install-service`, `--uninstall-service`, `--service-name NAME`, `--allow-unsafe-location` | the Windows service ([`docs/WINDOWS.md`](docs/WINDOWS.md)) |

`--set-password`, `--sign-out-all`, `--reload`, `--about`, `--licenses`, `--version` and
`--healthcheck` do their work and exit; the others start ser2netweb.

### Environment variables
What a container, a service unit or a script sets for a run (each is explained with its
feature):

| Variable | What it sets |
|---|---|
| `SER2NETWEB_BIND_IP`, `SER2NETWEB_PORT` | the admin UI's address and port, which win over `config.json` at every start (`*`: every interface, IPv4 and IPv6); each works on its own: the port alone keeps `config.json`'s bind IP (3.0.0 ignored a port set without a bind IP) |
| `SER2NETWEB_ADMIN_PASSWORD_FILE`, `SER2NETWEB_ADMIN_USER` | the first admin account, from a file (a Docker secret), while there is no account |
| `SER2NETWEB_NODE_NAME` | this instance's name |
| `SER2NETWEB_PUBLIC_URL`, `SER2NETWEB_TRUSTED_PROXIES` | what Settings → Reverse proxy sets |
| `SER2NETWEB_LOG_STDOUT=off` | the log lines in `all.log` only |
| `SER2NETWEB_LOW_PORTS=1` | Docker: ports below 1024 on the host network |
| `SER2NETWEB_LOOP_WATCHDOG` | the event loop watchdog's limit in seconds (`0`: off) |
| `SER2NETWEB_WINDOWS_LOOP=proactor`, `SER2NETWEB_WINDOWS_SERIAL=pyserial` | Windows: the event loop, the serial port class |
| `SER2NETWEB_LIB_OVERRIDE` | a folder whose Python modules load before the bundled ones (ldap3, LGPL) |
| `SER2NETWEB_SOURCE_URL` | the footer's source code link, for a modified copy |

Up to 2.9.x their names began with `SER2NET_` (`SER2NET_BIND_IP` and so on). The old names
still work, so compose files, units and scripts that use them need no change; when both
names of a variable are set, the `SER2NETWEB_` one wins, also when it is set to an empty
value.

### Offline install (no-internet machine)
Dependencies live in `vendor/wheels/`; `ser2netweb.py` installs them to `./lib` on first
launch (`pip install --no-index`). No internet needed. The set covers Python 3.10–3.14 on
every OS and CPU: everything but psutil is pure Python, and psutil (the network interface
list) is bundled for Linux x86_64 and Windows x64. Elsewhere — ARM Linux (32- and 64-bit
Raspberry Pi OS), macOS, Alpine/musl, Windows on ARM or a 32-bit Windows Python — the first
launch installs the set without it and says so; the interface lists then come from Python's
standard library, which may miss an interface (`--about` shows `psutil not installed`;
`python3 bootstrap.py --online` adds it from PyPI where it has a wheel for the machine or a
C compiler is there). CI runs this offline first start on Linux x86_64 and arm64, Windows,
macOS and 32-bit ARM (armv7, emulated). After a `requirements.txt` change, refresh the set with
`python3 tools/refresh_wheels.py` (`tests/test_vendor_wheels.py` checks it). Each wheel's
sha256 is in `vendor/wheels/SHA256SUMS`, and the first launch refuses to install from a set
where a listed wheel differs (CI compares every wheel with the file PyPI publishes); wheels
you add yourself aren't listed and install as they are.
To enable the optional features (MQTT/LDAP/OIDC) on an offline source install, download
their wheels too: `pip download -r requirements-optional.txt -d vendor/wheels`, then
`pip install --no-index --find-links vendor/wheels -r requirements-optional.txt`. (The
standalone binary already bundles them.)

### Upgrading a source install
Update the files (`git pull`, or unpack the new source over the old one) and restart
ser2netweb. When the new version pins other dependency versions, its first start installs
them into `lib/`, offline and once (a few seconds); packages you added to `lib/` stay, and
the old versions' metadata is removed. `lib/.ser2net-deps.json` records what `lib/` was
installed for (the pins, the Python version, the platform), so a new Python is handled the
same way. Upgrading from 2.8.x or older, this first start replaces what those versions left
in `lib/` (possibly 2.7's starlette and anyio, which have published advisories).
When that can't be done — no pip for that Python, `lib/` not writable for the account
ser2netweb runs as, or another ser2netweb still running from the same folder — ser2netweb starts
with the old `lib/` and
says why in its log (`WARNING: … ./lib …`); when this version can't run on the old packages
at all (2.8.1 and newer on 2.7's), the start stops with one line that says so.
`python3 bootstrap.py` installs the set by hand (stop ser2netweb first, and run it as the
account ser2netweb runs as, e.g. `sudo -u ser2netweb python3 /opt/ser2netweb/bootstrap.py`); with
`--no-bootstrap` that is the way after every upgrade, and the start-up log warns while
`lib/` is out of date. The one-off commands (`--set-password`, `--sign-out-all`, `--reload`,
`--about`, `--install-service`), which may run beside a running ser2netweb, leave `lib/` as it is and
only warn.

### Docker
```bash
docker compose up -d        # edit `devices:` and `ports:` in docker-compose.yml first
```
Without compose, use the `docker run` command in
[`docs/DOCKER.md`](docs/DOCKER.md#plain-docker-run) (the published image,
`--restart unless-stopped`, `--init`, the mapping ports, a `/dev/serial/by-id` device, log
limits).
Published image (every release; amd64, arm64, and arm/v7 for 32-bit ARM such as 32-bit
Raspberry Pi OS): `docker pull ghcr.io/haliskilic/ser2netweb:latest`
(or `:3.0`, `:3.0.1`); `docker-compose.yml` uses it. Up to 2.9.x the image was
`ghcr.io/haliskilic/ser2net`: that image no longer exists (since 2026-11-13 a pull of it
fails, and the 2.x images are no longer on GHCR), so change the image to
`ghcr.io/haliskilic/ser2netweb`;
moving a 2.9.x container or compose file over: [`docs/DOCKER.md`](docs/DOCKER.md#upgrading-from-29x).
The same image, by digest and with the same tags, is on Docker Hub as
`docker.io/haliskilic/ser2netweb` (`docker pull haliskilic/ser2netweb:latest`). Between releases `:latest` and `:X.Y`
are rebuilt on a newer Debian/Python base when it changes (weekly check; `:X.Y.Z` stays as
released) — see [`docs/DOCKER.md`](docs/DOCKER.md).
Inside the container the UI binds to `0.0.0.0` (set via `SER2NETWEB_BIND_IP` /
`SER2NETWEB_PORT` — the interactive picker can't run headless; `SER2NETWEB_BIND_IP=*` is every
interface, IPv4 and IPv6). Publish each mapping's port
too (a new mapping gets 4001), or use the host network. A serial device's group is taken
from the device itself, so `--group-add` isn't needed for the usual device names. The
process never runs as root: as the `ser2netweb` user (uid 10001), or as the owner of a
bind-mounted `/data` folder (or the `--user` given); a `/data` volume written by an older (root-running)
image is taken over on the first start, and in a bind-mounted folder the files an older
image wrote as root are handed to the folder's owner, so upgrading needs no manual step
(but on the host network with a mapping port below 1024: `SER2NETWEB_LOW_PORTS=1`, see
[`docs/DOCKER.md`](docs/DOCKER.md#ports-and-networking)).
The image has a `HEALTHCHECK` (`ser2netweb --healthcheck`). Behind a reverse proxy,
`SER2NETWEB_PUBLIC_URL` and `SER2NETWEB_TRUSTED_PROXIES` set what Settings → Reverse proxy does
([`docs/REVERSE-PROXY.md`](docs/REVERSE-PROXY.md)). Details:
[`docs/DOCKER.md`](docs/DOCKER.md).

### Moving from the classic ser2net
Coming from the classic `ser2net` daemon (the distribution package)? Import its
`/etc/ser2net/ser2net.yaml` or `/etc/ser2net.conf` in **Settings → Mappings export /
import**: the preview shows each connection as the mapping it becomes, which come in
disabled and why, and what works differently. Before you apply it (that starts the enabled
mappings):
- stop and disable the classic daemon (`sudo systemctl disable --now ser2net`), which holds
  the same ports and devices (its unit is `ser2net.service`, ser2netweb's
  `ser2netweb.service`: both can stay installed);
- give the service `CAP_NET_BIND_SERVICE` for ports below 1024, or move them up (the classic
  daemon usually ran as root; *Ports below 1024* under Run as a service);
- check the TLS / client certificate rows and the ones that came in disabled.

A mapping keeps its serial device open, so an open string goes out once when the port opens
(not for each client) and other programs can't use the device between sessions. The steps,
what differs and an option table: [`docs/MIGRATING-FROM-SER2NET.md`](docs/MIGRATING-FROM-SER2NET.md).

---

## 🖥️ Usage

- **Add mapping:** name, type (Serial↔Network / Serial↔Serial), serial port +
  parameters, network mode (server/client/udp), protocol, bind/remote IP:port, access
  rules.
- **Start/Stop/Restart/Copy/Delete:** per row. Stop and Restart ask first while clients
  are connected (they are disconnected; a stopped mapping stays stopped); Delete also
  removes the mapping's log and trace files. **Copy** makes a disabled copy named
  `<name> (copy)` (`(copy 2)`, …) on the next free port (for its transport and address,
  not one of ser2netweb's own; searched downwards past 65535), with no MQTT client ID, the
  base topic `<topic>-copy` (`-copy-2`, …) and a Sparkplug B edge node ID made from its
  new name, so it can run next to the original once its serial port is changed.
  A save, Start or Restart whose mapping doesn't start says why in one notice (with what
  to do when another program listens on its port, and, while a listener is retried, that
  it starts as soon as it can listen there).
- **Edit:** an edit form opened before the mapping changed (another user or tab, a start or
  stop, the REST API) is not saved: it comes back with your values and a note, and saving
  it again replaces the changes. A form whose mapping was deleted meanwhile comes back as a
  new mapping.
- **Log:** the mapping's history (newest first, persists across restarts).
- **Monitor:** live serial terminal in the browser (xterm.js); operators type to the
  device on network mappings that aren't read-only (a Modbus gateway's console only
  watches its bus, and opens in the hex view); their console's **Send BREAK** puts the
  serial line in the break condition for 0.25 s, as a telnet client's BREAK does (audited
  as `mapping_break`). The hex view shows both directions; viewers
  see only what the device sends. When the mapping stops or restarts, the console says so
  and connects again by itself; a browser that doesn't keep up is told how many bytes it
  missed. For a device that sends Turkish (or other) letters in a single-byte code page,
  set the mapping's *Console text encoding* (UTF-8 by default; ISO-8859-9, Windows-1254,
  CP857, ISO-8859-1, Windows-1252, CP437): the console shows its text with it and sends
  what is typed with it, and the hex view's text column too. The bridge passes the bytes
  unchanged either way.
- **Trace** (operators): the mapping's trace files, to list, download and delete (one
  file or all, audited). **Timed recording:** for a mapping whose trace is off, *Record*
  traces it for 15 minutes, 1 hour or 24 hours and then turns the trace off by itself —
  for catching a fault without leaving a trace on that fills the disk. Clients stay
  connected (the mapping is not restarted), the traffic goes to
  `traces/<id>-capture.log`, *Stop now* ends it early, and the end time is stored
  (`options.trace_until` in `config.json`), so a restart neither loses nor prolongs it. A
  mapping whose *Trace file name* is set is traced all the time and is never turned off
  by this. Starts, stops and the automatic end are in the audit log.
- **Settings:** change password, **two-factor sign-in**, **web UI access** (login required /
  open access with a role), admin TLS (provide paths, upload the files or generate self-signed; a previous
  generated pair is kept as `*.bak`; new connections get it at once, no restart),
  generate/revoke **REST API tokens** or change the first token's role only (a one-time
  token has a Copy button), **Mappings export / import** (JSON), **Configuration backups**
  (restore a saved `config.json`; *Reload config.json*), **Retention**, and the
  **name of this instance** (Admin interface; or `SER2NETWEB_NODE_NAME`): notifications, the
  browser tab, the top bar and the LAN cluster's fleet view use it instead of the host name,
  which instances on one machine share; the generic JSON webhook also gets `hostname` and
  `instance_id`. The Admin interface panel shows the address and protocol the UI serves, a
  changed address in `config.json` that waits for the next start, and how this installation
  changes it (Docker: `SER2NETWEB_BIND_IP` / `SER2NETWEB_PORT`; Windows service: reinstall with
  `--reconfigure`; else a start with `--reconfigure`). A saved form returns to the page at
  its own section, which says what was done (`303` to `/settings#<section>`); a reload or
  Back does not post it again (no second token, no second restore). A refused form (`400`)
  keeps what was typed, with the reason (passwords and keys are typed again). A TLS change
  that switches the UI between HTTP and HTTPS answers with the page itself, which names the
  address to continue at. The two-factor sign-in page works the same way.
- **/metrics:** Prometheus-format metrics (a web UI session; with the REST API token:
  `/api/v1/metrics`, see REST API): per mapping whether it is enabled
  (`ser2net_mapping_enabled`: 0 for a mapping stopped on purpose, so it is not mistaken for
  one that failed), whether it runs, its state (`ser2net_mapping_state`, one series per
  state, 1 for the current one), its clients, bytes in / out / dropped, reconnects (of
  those, failed opens of the serial port, `ser2net_mapping_open_failures_total`, and losses
  of the open port, `ser2net_mapping_device_lost_total`) and dropped slow clients (a
  connect-out mapping also whether its link is up, `ser2net_mapping_link_up`, and its
  failed connect attempts, `ser2net_mapping_connect_failures_total`); for the process the
  version (`ser2net_build_info`), its start time (`ser2net_start_time_seconds`), its open
  files and their limit (`ser2net_open_fds`, `ser2net_fd_limit`, POSIX), the admin UI's
  connections, TLS handshakes in progress and waiting for one to end, the connections
  refused right after accept (`ser2net_connections_refused_total`, by reason), the data
  folder's disk (`ser2net_data_free_bytes`, `ser2net_data_size_bytes`) and what traces and
  logs take there (`ser2net_traces_bytes`, `ser2net_logs_bytes`); when the TLS
  certificates in use end (`ser2net_tls_certificate_expiry_seconds`, Unix time: the admin
  UI's and each enabled TLS mapping's; also `certificates` in `/api/v1/status`, with the
  days left); for a Modbus gateway's
  polled units whether each answers (`ser2net_modbus_unit_up`) and its failed reads
  (`ser2net_modbus_unit_poll_errors_total`); for a mapping that publishes to MQTT whether
  it is connected to its broker (`ser2net_mqtt_connected`) and the messages it could not
  hand to it (`ser2net_mqtt_messages_dropped_total`); also `ser2net_up`, the steps of the system clock seen since the start
  (`ser2net_clock_steps_total`), the number of
  mappings (`ser2net_mappings_total`), whether a mapping is past its no-data alarm
  (`ser2net_mapping_no_data`) and the event loop's late heartbeats
  (`ser2net_event_loop_stalls_total`, its lag `ser2net_event_loop_lag_seconds`). Every
  series has its `# HELP` and `# TYPE` line. Without a session a scraper gets `401` (a browser is sent to
  the sign-in page), and successful scrapes are not written to `all.log`.

### Serial ports shared with other programs (Linux)

Nothing keeps other programs off a serial port unless all of them lock it, and the kernel
console, a login prompt and ModemManager use a port without asking. ser2netweb then shares
the port's bytes with them (stray characters, Modbus timeouts), and a login prompt hangs
the port up whenever it restarts. The dashboard's *Detected serial ports* list marks such
ports (**kernel console**, **login prompt**, **ModemManager**, and **no access** for a port
ser2netweb may not open), and
a mapping that has one open says so under its state (a port hung up and reopened at once
is counted there as well).
- **Kernel console, login prompt** (a Raspberry Pi's GPIO UART, a VM's serial console): on
  a Raspberry Pi run `sudo raspi-config` → Interface Options → Serial Port, and answer *No*
  to the login shell and *Yes* to the serial hardware. Elsewhere remove `console=ttyS0`
  from the kernel command line, and turn the login prompt off:
  `sudo systemctl mask --now serial-getty@ttyS0.service`.
- **ModemManager** (desktop Ubuntu / Fedora; USB CDC `ttyACM*` devices such as an Arduino):
  tell it to leave the device alone with a udev rule, for example
  `/etc/udev/rules.d/99-ser2netweb.rules` with
  `ATTRS{idVendor}=="2341", ENV{ID_MM_DEVICE_IGNORE}="1"`, then
  `sudo udevadm control --reload && sudo udevadm trigger`.
- **Permission denied:** the dashboard names the group the device belongs to and what is
  missing (see Requirements); **busy:** the mapping of this ser2netweb that has the port open,
  or the kind of lock another program holds.

More — a table from what the dashboard shows to the cause, groups, udev rules, stable
device names, finding who holds a port, brltty and the Raspberry Pi UARTs (the Pi 3/4 mini
UART has no parity: Modbus RTU 8E1 needs the PL011) — in
[`docs/LINUX-SERIAL.md`](docs/LINUX-SERIAL.md).

---

## 🔌 REST API

Alongside the browser UI, a JSON API at `/api/v1` for automation. Authenticate with
`Authorization: Bearer <token>`; generate the token under **Settings → REST API token**
(shown only once). Full description: `GET /api/v1/openapi.json`.

```bash
TOKEN="s2n_..."   # generated in Settings
# all mappings (config + live status)
curl -H "Authorization: Bearer $TOKEN" http://HOST:8080/api/v1/mappings
# create a mapping
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"PLC-1","kind":"net","serial":{"port":"/dev/ttyUSB0","baudrate":9600},
       "network":{"mode":"server","bind_ip":"0.0.0.0","port":4001}}' \
  http://HOST:8080/api/v1/mappings
# change some fields only (JSON merge patch): here the TCP port
curl -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/merge-patch+json" \
  -d '{"network":{"port":4002}}' http://HOST:8080/api/v1/mappings/<id>
# start / stop / restart
curl -X POST -H "Authorization: Bearer $TOKEN" http://HOST:8080/api/v1/mappings/<id>/stop
# record the traffic for 15 minutes, then list and fetch the trace files
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"minutes":15}' http://HOST:8080/api/v1/mappings/<id>/capture
curl -H "Authorization: Bearer $TOKEN" http://HOST:8080/api/v1/mappings/<id>/traces
curl -H "Authorization: Bearer $TOKEN" -O http://HOST:8080/api/v1/mappings/<id>/traces/<id>-capture.log
```
Endpoints: `GET/POST /mappings`, `GET/PUT/PATCH/DELETE /mappings/{id}`,
`POST /mappings/{id}/{start|stop|restart}`, `GET/DELETE /mappings/{id}/traces`,
`GET/DELETE /mappings/{id}/traces/{name}`, `POST/DELETE /mappings/{id}/capture`,
`POST /import`, `GET /status`, `GET /ports`,
`GET /metrics`, `GET /info` (ser2netweb's version, the API's version and its `features`),
`GET /health` and `GET /openapi.json` (both unauthenticated). The web UI's own endpoints
(`/api/status`, the dashboard's table, which answers `204` or only the changed counters to a
page that sends back its `X-Content-Hash`) are for the browser: scripts use `/api/v1`.

**`PUT` replaces the whole mapping.** Every field the body leaves out gets its default, as in
a new mapping: listen on `0.0.0.0` port 4001, no client allow-list, not read-only, enabled
(a stopped mapping starts), TLS, MQTT and the trace off, no USB identity (`serial.match`),
and `kind` `net` (a serial↔serial bridge becomes a network listener). So either `GET` the
mapping, change it and `PUT` all of it back, or use **`PATCH`**: an RFC 7396 JSON merge patch
that changes only what the body names. An object merges into the stored one
(`{"network":{"port":4002}}` keeps the bind address and the allow-list), a list or a value
replaces it, and `null` resets a field to its default (`{"network":{"allowed_client_ips":null}}`).
The result is checked like any edit; a refused patch changes nothing (HTTP 400). The
previous configuration of every change stays in `backups/`.
**Trace files:** `GET /mappings/{id}/traces` lists a mapping's trace files (`name`, `size`
in bytes, `time` last written as Unix time; also `tracing`, the `file` written now and
`capture_until`). Listing them and downloading one (`GET .../traces/{name}`, recorded in
the audit log) need an **operator or admin token** — traces hold what is sent to the device
too, and a viewer gets no Trace panel in the web UI either (`403` for a viewer token). `DELETE .../traces/{name}` deletes one file, `DELETE .../traces` all of them (a trace
that is on goes on in a new file). `POST /mappings/{id}/capture` with `{"minutes": n}`
(1 to 1440) starts a timed recording as the Trace panel does, again while one runs sets a
new end, `DELETE .../capture` ends it; a mapping that is traced by its `trace_both` answers
`409`. `options.trace_until` (when the capture ends) is read-only: a `PUT`, `PATCH` or
`POST /mappings` ignores what its body has there (a `PUT` without it keeps the capture),
and so do imports; a copy of a mapping has no capture.
`GET /mappings/{id}` answers the mapping's revision in `ETag`: send it back in `If-Match`
with a `PUT` or `DELETE` to change the mapping only if nobody changed it since (else `412`,
`precondition_failed`); without `If-Match` they work as before.
Start and stop are idempotent: starting a mapping that already runs with its settings, or
stopping a stopped one, changes nothing (`"changed": false`; its clients, serial port and
MQTT connection stay) — use `restart` to restart a running mapping, e.g. to send its open
string again (a renewed TLS certificate at the same paths is also read by a reload of
`config.json`, without dropping clients). A `PUT` or `PATCH` with the mapping's own
settings changes nothing either.
A value may be sent as text (`"9600"`, `"false"` is false); one that is not of the setting's
type is refused with `400` and the setting's path (`serial.baudrate: "fast" is not a whole
number.`). A protective switch (`read_only`, `tls`) takes only JSON `true` / `false`: text is
refused (`network.read_only: use true or false (JSON booleans), not a string.`), and so is an
import of such a file. In a hand-edited `config.json`, text in a protective switch
(`read_only`, `tls`, `require_login`, `tls_verify`, `start_tls`, `mask_personal_data`, the
MQTT `tls_check_hostname`) keeps it on unless empty, as before 2.9.0, with a start-up note. The switches that turn something on only when set to
JSON `true` (`dual_stack`, `udp_broadcast`, `disconnect_without_device`, `poll_with_clients`,
the MQTT `tx_enabled`, `proxy_hsts`, `email_separately`) refuse text as
well; in a hand-edited `config.json` it leaves them off, with a start-up note. Keys that are
not settings are ignored and named in the response's `warnings` list. An unexpected server error is a JSON `500` too.
A change that can't be saved changes nothing: `400` (`validation`) when the configuration
would not validate — a start that would share a serial port, an MQTT client ID, a
Sparkplug B edge node or a command topic with another enabled mapping, for example — and `500` (`save_failed`) when `config.json` can't be written (a full or
read-only disk). A stop always stops; when `config.json` can't be written, the response's
`warning` says that the mapping starts again when ser2netweb restarts.

**Many mappings in one change:** `POST /import` takes what **Settings → Mappings export / import**
writes or `GET /mappings` answers (`{"mappings": [...]}`), a whole `config.json` (only its
mappings are used) or a classic `ser2net.yaml` / `ser2net.conf`. `?mode=merge` (the default)
updates a mapping with the same id, else the same name, and adds the others (a mapping it
updates keeps the settings the body doesn't have);
`?mode=replace` makes the body's mappings the only ones. It is one save — one backup, all
or nothing: a result that isn't valid (two mappings on one port, ...) is refused whole
(HTTP 400) and nothing changes. `?dry_run=true` answers what it would do (each mapping
added, updated, unchanged or removed, and why it would be refused) without changing
anything:
```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data-binary @ser2netweb-mappings.json "http://HOST:8080/api/v1/import?mode=merge&dry_run=true"
```
**Prometheus with the token:** `GET /api/v1/metrics` answers the same metrics as `/metrics`
(which needs a web UI session) to any API token:
```yaml
scrape_configs:
  - job_name: ser2netweb
    metrics_path: /api/v1/metrics
    authorization:
      credentials_file: /etc/prometheus/ser2netweb-token   # the token, e.g. a viewer one
    static_configs:
      - targets: ["HOST:8080"]
```
The token has the role chosen when it was generated (*Change the role only* changes it and
keeps the token): a `viewer` token may only read (GET)
and gets stored secrets (the MQTT password and client key password, a mapping's TLS key
password) masked as
`********`; a `PUT` or `PATCH` that sends that mask back keeps the stored password. An `api_token_role` in `config.json` that is not a
role counts as `viewer`, and the start-up log says so.

**One token per client:** under **Settings → REST API token → More tokens** an admin adds
named tokens next to that one, each with its own role (`viewer` reads only, `operator` may
also change) and, if wanted, an expiry in days — say a read-only token for Prometheus or a
dashboard and a write token for the deployment script, so not every client holds a token
that can change mappings. The audit log names the token that acted (`api-token:NAME`; the
first token stays `api-token`), revoking one leaves the others working, an expired one gets
HTTP 401 `{"error":"unauthorized","detail":"API token expired"}`, and Settings shows when each
was last used (since ser2netweb started; never written to disk). The first token works as before
(`api_token_hash` / `api_token_role` in `config.json`); named tokens are kept in
`api_tokens`, which 2.8 and older drop (their first token keeps working).

---

## 🔒 Security

The UI is **password-protected by default** and binds to **127.0.0.1** by default.
Exposing it to the network requires an explicit IP choice at startup and warns when
bound without TLS. For LAN deployments, use TLS (`admin_ui.tls_*`) and per-mapping
`allowed_client_ips`. Raw TCP is plaintext — be careful on untrusted networks. A bare
`0.0.0.0`/`::` in an allow/priority list means "any client", IPv4 and IPv6 alike;
`0.0.0.0/0` is every IPv4 client and `::/0` every IPv6 one. An IPv4-mapped entry
(`::ffff:192.168.1.0/120`) matches the IPv4 addresses it maps. An entry that is no IP address
or network (a host name, a range, a comma-joined list — only a hand-edited `config.json` can
have one, every edit refuses it) is left out, which the start-up log, the mapping's start and
its edit form say; an allow-list with no usable entry at all refuses every client (before
2.9.0 it admitted everyone). A mapping's serial port is a
device path or COM name only; pyserial URL handlers (`socket://`, `rfc2217://`, `spy://`,
`loop://`, ...) are not accepted as a port.

**Per-mapping TLS:** a TLS listener (TCP server, Modbus gateway) presents its certificate and
key; with a **Client CA certificate file** (`network.tls_client_ca`) it takes only clients
whose certificate chains to a certificate in that file (mutual TLS, as stunnel `verify` or
Modbus/TCP Security clients expect) and logs each client's certificate subject; the
allow-list is still checked first. A connect-out link logs the SHA-256 fingerprint of the
server's certificate at every connect and checks it with a **CA certificate file**
(`network.tls_ca`: the certificate must chain to it and name the remote host) and/or a
**server certificate fingerprint** (`network.tls_fingerprint`; list the new one too before
a certificate change); its certificate and key are optional and presented to a server that
asks for a client certificate. **Without a CA file or a fingerprint, connect-out TLS only
encrypts:** anyone on the network path can pose as the server. A CA file is PEM (one or more
certificates) or DER, and any certificate in it is trusted, a self-signed server's own one
too. An encrypted key needs its password (**TLS key password**, `network.tls_key_password`;
`admin_ui.tls_key_password` for the admin UI, *Key password* under Settings → Admin TLS):
ser2netweb never
asks for one on the console, and without it the mapping, or the start for the admin UI's
key, fails with an error that says so. The form and the REST API load a mapping's files when
it is saved and refuse a key that is the admin UI's own; the form never shows the stored
key password, and the REST API masks it for a viewer token.
**Uploading the files in the browser** (admins): instead of copying files onto the host,
*Upload a certificate and key from this computer…* under Settings → Admin TLS takes the
admin UI's pair, sets it and serves it to new connections at once; in a mapping's form,
*Upload TLS files from this computer…* takes a certificate with its key and / or the CA
file of the field it stands next to, and puts their paths into the form's fields (they
apply when the form is saved). PEM files of at most 256 KB each (a CA file may be DER).
Before anything is kept they are loaded as ser2netweb will load them: a key that is not the
certificate's, an encrypted key without its password or a CA file without a certificate
is refused, and so is the admin UI's key for a mapping (or a mapping's for the admin UI),
also as a copy written another way or with another certificate (the public keys are
compared). They are kept in the data folder's `tls/` as `uploaded-<hash>-cert.pem` /
`-key.pem` / `-ca.pem`, each named by its own hash (an upload never replaces a key in use),
readable only by the account ser2netweb runs as, and every upload is in the
audit log (`admin_tls_upload`, `tls_upload`). Files uploaded earlier stay there until you
delete them (a configuration backup may still name them); the path fields work as before
for files that are already on the host. Where certificates can't be managed, a VPN
(WireGuard) or an stunnel in front of an untrusted network is the alternative.

**HSTS:** with admin TLS, pages and REST API answers carry `Strict-Transport-Security` with
a `max-age` of one year. A browser that trusts the certificate and reaches the UI by host
name (not by IP address, and not after clicking through a certificate warning) then uses
only https:// for that name, on every port, for that long after each visit: other plain-HTTP
or self-signed web UIs on the same name stop opening in it, and so does this one once its
TLS is switched off. **Settings → Admin TLS → HSTS max-age** (`admin_ui.hsts_max_age`, 0 to
63072000 seconds) changes it at once; 0 sends `max-age=0`, which makes browsers forget it.
Before switching TLS off, set 0 and let the users open the UI once.

**The admin UI's certificate:** Settings → *Admin TLS (HTTPS)* shows the configured certificate's
name, its other names and when it is valid (UTC), flagged when it has ended, isn't valid
yet or ends within 30 days; the start-up log has the same line (a WARNING then), for a
self-signed certificate and a CA-issued one alike. *Generate a self-signed certificate*
makes one valid for 825 days in all, from a day before now, so clients whose clock is a
little behind accept it at once (a source install without the `cryptography` package
uses the `openssl` command, whose certificate starts now). Its dates come from the
server's clock: made while that clock is wrong, the certificate looks expired or not yet
valid to clients whose clock is right, so the page warns when the clock reads earlier than
it can (see *Time synchronisation*).

**Connection limits:** on a TLS mapping, a host not in `allowed_client_ips` is closed before
the TLS handshake; a handshake must be over within 15 s, and at most 32 run at once from one
address (128 in all). More connections wait up to 15 s for one of those to end (at most 64
from one address, 256 in all), so a client's burst of reconnects is served rather than
refused. Capacity, `kick_old_user` and the "Busy" line still come after the handshake. The admin UI closes a connection that sends no complete request head within
`admin_ui.request_timeout_s` (10 s) of connecting or of its last response, whose request
body then stops coming (the body gets that time plus a second per KiB that arrives), or
whose client takes nothing of a response for that long (a console WebSocket is not
timed), and takes at most `admin_ui.max_connections` (256) connections
at once, `admin_ui.max_connections_per_ip` (64) from one address; its TLS handshake has the
same limits as a mapping's. An IPv6 client counts by its /64 network for these
per-address limits (a host can use any address of its /64). These keys are in
`config.json` only (0 = no limit); behind a reverse proxy every user has the proxy's
address, so raise the per-address limit if needed. When the process gets close to its open-file limit, new connections (to any
mapping or the UI) are refused with a log line, so serial ports, logs and the
configuration keep the files they need.

**UDP mappings:** `allowed_client_ips` is checked for every datagram, but what it checks is
the datagram's source address, which a sender on the network can forge, so for UDP it is no
authentication. Without a reply address, any allowed sender (or one forging an allowed
address) takes the device's output over with a single datagram, even on a read-only
mapping; the log notes each change of sender (at most once a minute). Set a **reply
address** to keep the output on the right host. On untrusted networks use a TCP mapping with
TLS, or a firewall.

**MQTT commands (Modbus edge mode):** writable points take values from any client the
broker lets publish to `<topic>/<point>/set` or, for Sparkplug B, to
`spBv1.0/<group>/NCMD/<node>` and `spBv1.0/<group>/DCMD/<node>/<device>`. ser2netweb does not
authenticate these clients itself, and `allowed_client_ips` covers only the Modbus/TCP port,
so the broker's access rules (ACLs) are the authorization: let only the clients that must
switch outputs publish to the command topics, and connect to the broker with TLS and a
username/password. **Read-only** refuses every write. The dashboard marks a mapping that
takes MQTT writes with **CMD**, and with **CMD ⚠** (plus a warning in its log at start)
when its broker connection has no TLS or no username. The same holds for a serial mapping
that writes `<topic>/tx` to the device (**CMD** too). A Mosquitto `acl_file` for the base
topic `plant/line3`, for example:
```
user ser2netweb
topic readwrite plant/line3/#
user scada
topic write plant/line3/+/set
topic read plant/line3/#
user historian
topic read plant/line3/#
```

**Open access (optional):** for trusted networks only, an admin can untick "Require
login" under **Settings → Web UI access** and give visitors a role (`viewer` read-only /
`operator` / `admin`). Signing in still works (you get your own role), RBAC and CSRF
apply unchanged, and the **REST API always needs its token**. Open access is honoured
only when the UI is reached by **IP address, `localhost` or this machine's own name** —
requests via any other host name (e.g. a malicious page doing DNS rebinding) are sent to
the login page. Behind a reverse proxy, the proxy must pass the original name in
`X-Forwarded-Host` (otherwise keep login on). **Other LAN-cluster nodes** are only shown
and managed after signing in, and open consoles are closed when access is revoked or the
role changes. While it is on, the UI shows a warning and startup logs it.

**Sign-in notice (optional):** **Settings → Web UI access → Sign-in notice** sets a text the
sign-in and first-run setup pages show before anyone signs in — a privacy notice, or the
system-use notification that IEC 62443 (SR 1.12) and NIST SP 800-53 (AC-8) ask for — plus an
optional http(s) link to the full text. It is plain text (line breaks kept, at most 2000
characters, never HTML; `admin_ui.login_notice`, `admin_ui.login_notice_url`); with open
access, visitors who don't sign in find it under *Notice* in the top bar. Changes are
audited (`login_notice_set`). None is set by default.

**Requests from other sites:** a change (any POST, PUT, PATCH or DELETE of the web UI) is
refused with 403 when the browser says a page of another site sent it: `Sec-Fetch-Site`
other than `same-origin` (or `none`: typed into the address bar), or an `Origin` that isn't
the address the UI was reached by —
another port of the same host and a sibling subdomain count as other sites. This comes on
top of the CSRF token, which is new after every sign-in and sign-out and lasts as long as
the session. Behind a reverse proxy, the proxy must pass the address the browser uses as
`Host` or `X-Forwarded-Host`, port included, or that address must be the public URL
(Settings → Reverse proxy); all.log names the refused `Origin` and the address the UI saw.
Scripts that send no `Origin` header, the REST API and cluster peers are not affected.

**Behind a reverse proxy:** the console and hex-view WebSockets are checked the same way: a
browser's handshake is accepted when it says it comes from this UI's own page
(`Sec-Fetch-Site: same-origin`, sent over HTTPS) or when its `Origin` is the address in
`Host`, `X-Forwarded-Host` or `Forwarded: host=`, port included, or the public URL. So a
proxy must pass the browser's address on — nginx `proxy_set_header Host $http_host;`,
Apache `ProxyPreserveHost On`, IIS ARR `preserveHostHeader` (Caddy and Traefik do it by
default) — and pass WebSocket upgrades through; over plain HTTP (but to `localhost`)
browsers send no `Sec-Fetch-Site`, so there the address is all that counts. all.log names a refused
`Origin` once a minute, with the addresses the UI saw. **Settings → Reverse proxy** has
the rest: the **public URL** (the address users open; its origin counts as the UI's own,
a blank SSO redirect URI is made from it, notifications link to it, and a path in it —
`https://gw.example.com/ser2netweb` — serves the UI under that path too, with cookies of its
own there, while `/` keeps working directly), the **trusted proxies** (only from them are
`X-Forwarded-For` and `X-Forwarded-Proto` believed — none by default; up to 2.8.1 every
connection from `127.0.0.1` / `::1` was believed) and HSTS through the proxy (off by
default). The cookies are Secure whenever the browser used HTTPS, also through a TLS proxy
that sends `X-Forwarded-Proto: https`. `SER2NETWEB_PUBLIC_URL` / `SER2NETWEB_TRUSTED_PROXIES` set
them for one run instead; `FORWARDED_ALLOW_IPS` still works. Examples for nginx, Apache,
Caddy, Traefik and IIS, and what all.log says when something is missing:
[docs/REVERSE-PROXY.md](docs/REVERSE-PROXY.md).

**Passwords** of local accounts are stored as scrypt hashes (N=2^14, r=8, p=5, OWASP's
minimum; the hash names its cost, so the cost can be raised later). A hash made by 2.8.1 or
older keeps working and is renewed with today's cost at that account's next sign-in (its
sessions stay; `config.json` is saved and kept in `backups/`, as every save). Passwords are
compared as Unicode NFKC: a letter with its accent as one character or two (text pasted
from macOS or a PDF), a no-break space and a space, full-width forms are the same password.
A directory (LDAP) gets the password exactly as typed. **Going back to 2.8.1 or older:**
those versions can't read the hashes 2.9 makes, so an account that signed in, or got a
password, under 2.9 can't sign in there: give it a new password with `ser2netweb --set-password NAME` (2.8.1), or, on
2.8.0 and older, which have no such command, put back a `config.json` from `backups/` made
before the upgrade.

**Sessions:** a page opened without a session goes to the sign-in page and, after the
sign-in, back to that page (`/login?next=<page>`); `/metrics` does that for a browser and
answers `401` to a scraper. Logging out ends that session on the server (a copy of the
cookie stops working too). Under **Settings → Users** an admin can sign a user out everywhere or set a
local user's new password (which signs them out as well); everyone can *Sign out my other
sessions*. **Settings → Web UI access → Sign out everyone** makes a new session key and
ends every session at once (the admin who clicks it is signed in again in that browser) —
use it when a copy of
`config.json` or a backup may have leaked (downloaded backups leave the key out). While
LDAP or OIDC is switched off, its accounts' sessions don't count. Browsers keep cookies per
host name, not per port, so on a port other than 8080 the UI's cookies carry a tag of the
port (`ser2net_session_1a2b3c4d`), and over HTTPS the `__Host-` prefix: instances reached
under one host name on different ports (side by side, containers, SSH tunnels to
`localhost`, the fleet view's *Open* link to a peer on the same address) keep their own
sign-ins, and an `http://` address still works after an `https://` one of the same name.
Sign-ins from before 2.9 carry over.

**Failed sign-ins** are limited to 8 per address in 5 minutes (an IPv6 address counts by
its /64; behind a reverse proxy, each client's own address once the proxy is a trusted
proxy, else the proxy's, shared by all) and to 10 per user
name in 15 minutes, from any address and letter case aside; past either limit that address
or that name gets HTTP 429 until its oldest failures are older than that — nothing is locked
for good, and with LDAP the directory is asked no more often, which keeps such attempts
below its own lock-out. A wrong current password in *Change your password* counts under the
account's name too, and is logged and recorded in the audit log (`password_change_failed`).
Password checks run two at a time and directory sign-ins four; while many wait already, a
sign-in gets HTTP 429 (*Too many sign-ins at the same time*) without being tried. A name
without a local account takes as long to refuse as a wrong password. all.log has a line for
each failed sign-in, and the audit log records them; a user name that is no account's is
recorded as `(unknown user)`, never as typed (it may be a password typed into the wrong
field). Past 20
failed sign-ins in 10 minutes the audit log only counts them and sums them up in one record
at the end of those 10 minutes, so a flood of attempts can't push the other records out.
Opening a mapping's console or hex view is recorded once per account (with open access: per
address), mapping and kind in 5 minutes, and at most 50 such records in 10 minutes, summed up
the same way (all.log keeps a line for every one). At most 4 consoles and hex views (together)
are open per account (with open access: per address) and mapping, 16 per mapping and 64 in
all; one that takes nothing for 30 s while its traffic is being dropped is closed. The
console's WebSocket says why it closed (the page shows it): `1013` too many open, `4001` the
mapping stopped or restarted (the page connects again), `4002` signed out or the access
changed, `4003` took nothing for 30 s, `4004` the mapping was deleted, `1008` refused at
the start (no session, or a page of another site). Closing a console that wrote to the
device is recorded too (`console_close`, with the number of bytes, never what was typed). A
failed SSO sign-in (the identity provider refused it, its answer didn't check out, or the
groups give no role) is recorded as `oidc_login_failed` with a short reason and notified like
a failed local or LDAP sign-in, within the same limit. A REST API request refused for its token
(none, a wrong or an expired one) is recorded as `api_auth_failed` once per address a minute
(the next record says how many there were), at most 20 in 10 minutes. A request refused before
it reaches the page (no session, a failed CSRF check, a WebSocket without a session) gets an
all.log line once per address and answer a minute, the next one saying how many more there
were; a WebSocket from another site one line a minute in all. These records and the
console limits count an address as it is; the connection, handshake and sign-in limits
count an IPv6 /64 as one address. Line breaks and other control characters in names (a mapping name,
a user name from the directory) are written as spaces in all.log, stdout, the mapping logs,
the audit log and notification titles, so they can't start a line of their own.
Names over 256 characters aren't tried. Request bodies are limited too: the sign-in,
two-factor, setup and sign-out forms to 64 KB, a configuration import (form or REST API) to
16 MB, a cluster node's request to 1 MB and everything else (forms, the REST API) to 2 MB; a
larger one gets HTTP 413.

**LDAP / OIDC groups → roles:** a group setting (admin / operator / viewer group) names one
group and matches only its whole name, never a part of it. For LDAP that is the group's
name (its CN, e.g. `ser2netweb-admins`), the start of its DN (`cn=ser2netweb-admins,ou=groups`)
or its full DN; for OIDC the value as the provider sends it, or its last part
(`ser2netweb-admins` matches Keycloak's `/team/ser2netweb-admins` and `CORP\ser2netweb-admins`).
Letter case doesn't matter, Turkish İ/I/ı/i included (`Bilgi İşlem` = `BİLGİ İŞLEM`). A
setting that is only part of a group's name (`admins` for `ser2netweb-admins`, or just an OU of
its DN) gives no role: all.log names the setting and the group in a WARNING at that user's
sign-in, and Settings shows it until you save that section. If your only admins came from
such a group, sign in with a local admin created by `ser2netweb --set-password NAME` (see
below) and correct the setting. When a directory user's groups give no role any more,
their next sign-in also ends the sessions they still had. AD direct bind with
`{username}@corp.local` or `CORP\{username}` finds the account by userPrincipalName /
sAMAccountName under the user search base (blank: its whole domain) to read its groups;
*Also count nested groups* (AD only) adds the groups a user is in through other groups.

**LDAP / OIDC accounts:** someone who signs in through the directory (LDAP) or the identity
provider (OIDC) for the first time gets an account here without a password; its role is
worked out from their groups at every sign-in — also when that takes the last admin role
away (all.log then warns; `ser2netweb --set-password NAME` makes a local admin). Local accounts
come first: an LDAP/OIDC sign-in under the name of a local account (letter case aside) is
refused and written to all.log and the audit log (`ldap_login_denied` /
`oidc_login_denied`); it never takes over the local account, turns it into an SSO account or
demotes it. To move someone from a local account to SSO, delete the local account first
(another admin must remain). The same holds between LDAP and OIDC: a sign-in under the name
of an account of the other method is refused (the two may be different people); delete that
account to move the person. Like directories, ser2netweb ignores letter case in LDAP names
(Turkish İ/ı too): `hkilic`, `Hkilic` and `HKILIC` are one account; the accounts earlier
versions made for each spelling merge at that person's next sign-in (the other spellings'
sessions end), and *Sign out* and *Delete* cover all of them. OIDC names are compared
exactly. A new local account's name can't differ from an existing one only in letter case.
An LDAP/OIDC name can be any printable text (`CORP\jdoe`, `jdoe@corp.local`, names with `/`
or `#`; such accounts can be signed out and deleted in the Users panel too); a sign-in under
a name with control or invisible characters, longer than 255 characters or in parentheses is
refused (all.log says why).

**Unused LDAP / OIDC accounts:** **Settings → Users** shows each account's last sign-in
(from the audit log and the sign-ins since; not written to config.json). Removing unused
LDAP/OIDC accounts is **off by default**: an account that stops signing in stays until you
delete it. To turn it on, set *Remove LDAP / OIDC accounts not signed in for (days)* in the
same place (`external_users_expire_days`: 30–3650 days; 0 = off): LDAP/OIDC accounts not
signed in for that long are then removed at once, at start-up and every hour, each written
to all.log and the audit log (`user_expired`). Admins, local accounts and the last remaining
accounts are never removed, and an account without any sign-in in the audit log only when
the log goes back that far for its kind of sign-in. Deleting (or removing) an LDAP/OIDC
account ends its sessions at once, but access is up to the directory: while a mapped group
or the default role still applies, the person's next sign-in makes a new account.

**OIDC (SSO):** the code request carries PKCE (S256). Every address the provider's discovery
document gives (authorization, token, JWKS) and every redirect on the way must be https://
(http:// only to this machine's loopback address): if a Keycloak behind a TLS-terminating
proxy announces http:// addresses, SSO sign-in is refused and all.log says why (fix
Keycloak's proxy / frontend URL settings), because the client secret, the code and the
signing keys would travel in clear text. When the provider sends the user back with an error
instead of a code (e.g. the user is not assigned to the application: `access_denied`), the
error and its description go to all.log and the user sees a message for that error code;
the token endpoint's error answers are logged too. An e-mail address the provider calls
unverified (`email_verified: false`) is never used as the user name. If the discovery
document names another issuer than the configured one, all.log warns once. The id_token's
times (iat / nbf / exp) are accepted with up to 2 minutes of clock difference: keep the
clocks of this server and of the provider synchronised (NTP); when a token is refused for its
times, all.log says how many seconds this server's clock is behind or ahead of the
provider's.

**SSO redirect URI:** **Settings → Single sign-on (OIDC)** shows the redirect URI to register
at the provider: the *Redirect URI* you set or, while it is blank, the one made from the
page's request, or from the public URL when one is set (Settings → Reverse proxy:
`<public URL>/auth/oidc/callback`); from the request: `X-Forwarded-Proto` or the request's
scheme, `X-Forwarded-Host` or `Host` (of a list, the first), then `/auth/oidc/callback`;
`Forwarded` and `X-Forwarded-Port` are not read, so the address
working installs registered stays the same. When it is http:// for a host other than
loopback, or differs from the browser's address (a reverse proxy in between changed the
address), the page warns and shows the value for the browser's address: enter that as
*Redirect URI*. Every SSO sign-in start writes the redirect URI it sends to all.log.

**LDAP over TLS (LDAPS / StartTLS):** the directory server's certificate is checked. It
must come from a CA the system trusts (Linux / Docker: its CA bundle; Windows: its
certificate store, where a domain member usually has the AD CA already) and name the host
in the server URI: use the name the certificate was issued for (`ldaps://dc01.corp.local`;
an IP address only if the certificate lists it). For your own CA (Active Directory
Certificate Services) or a self-signed certificate, set **Settings → LDAP / Active
Directory → CA certificate file** to that CA's certificate, PEM or DER as Windows exports
it, by its full path (Docker: put it in the `/data` volume and enter `/data/corp-ca.pem`;
unlike the TLS files of a mapping, a relative path is not looked up in the data folder);
then only the certificates in that file are trusted. A refused certificate
is logged in all.log with the reason and what to set, and Settings shows it; nothing is
sent over that connection. *Don't check the server certificate* turns the check off, which
is insecure — anyone on the network between ser2netweb and the directory could read the
passwords and sign in as anyone; Settings and the start-up log warn while it is off, and
while passwords go unencrypted (`ldap://` without StartTLS). Over TLS ser2netweb doesn't
follow a referral to another server (that server's certificate would go unchecked): point
the URI at a server that holds the users and their groups. **After upgrading from 2.8.0
or older**, LDAP sign-in stops if the certificate isn't trusted: set the CA certificate file
(local accounts keep working; without a local admin, create one with
`ser2netweb --set-password NAME`).

**Two-factor sign-in (TOTP):** off unless an account turns it on. A local account sets it
up under **Settings → Two-factor sign-in**: it adds the key shown there to an authenticator
app (Google or Microsoft Authenticator, Aegis, FreeOTP, KeePassXC, … — typed in, or the
`otpauth://` link pasted or turned into a QR code with any QR maker; ser2netweb adds no
dependency for it) and confirms with a code and its password. From then on, signing in takes
the password and the app's 6-digit code (turning it on ends the account's other sessions).
A code is accepted for its 30-second step and one step either side (a clock a little off),
and only once; wrong codes count against the address together with wrong passwords, and five
for one account in 5 minutes block it for a while. The codes follow the clock: keep the
server's time right (NTP). Turning it off takes the password and a code. **Lost authenticator:** an
admin turns it off for that account under **Settings → Users** (*Turn off 2FA*, which signs
it out); for the only admin, `ser2netweb --set-password NAME` on the server sets a new password
and turns it off (see below). LDAP and OIDC accounts get a second factor from the directory
or the identity provider: MFA is available today through OIDC with an IdP that enforces it
(Keycloak, Entra ID, Google, …) or an authenticating reverse proxy in front of the UI. The
REST API tokens, the cluster key and open access are not affected. The secret is kept in
`config.json` (`users[].totp_secret`, only while it is on); 2.8 and older drop it, and the
account then signs in with its password alone.

**Lost password, locked out, no browser (account recovery):**
`ser2netweb --set-password NAME` sets a new password for the local account `NAME` (asked
twice on the terminal; piped, the first line of stdin), turns its two-factor sign-in off
(for a lost authenticator) and signs that account out everywhere. A name that doesn't exist yet is created as an **admin** — the way back in
when the only admin is an LDAP/OIDC account and the directory is unreachable.
`ser2netweb --sign-out-all` ends every web UI session. Both work with ser2netweb stopped or
running: a running instance applies the change at once, no restart needed. Nothing else
in the configuration changes, the previous `config.json` stays in `backups/`, the audit
log records it, and nothing secret is printed or logged. Use the same `--data-dir` as the
service and run it as the account ser2netweb runs as (as root, it switches to the data
directory's owner by itself):

| Install | Command |
|---|---|
| source / Linux binary (systemd) | `sudo -u ser2netweb python3 /opt/ser2netweb/ser2netweb.py --data-dir /opt/ser2netweb/data --set-password admin` (binary with `ser2netweb-standalone.service`: `sudo -u ser2netweb /usr/local/bin/ser2netweb --data-dir /var/lib/ser2netweb --set-password admin`; started by hand: `./ser2netweb-linux-x64 --set-password admin`; a 2.9.x install not moved over yet: the same with `ser2net`) |
| Windows (elevated prompt, also while the service runs) | `ser2netweb-windows-x64.exe --data-dir C:\ProgramData\ser2netweb --set-password admin` (the service's data directory: `sc qc ser2netweb` shows it) |
| an instance of `ser2netweb@.service` | `sudo -u ser2netweb python3 /opt/ser2netweb/ser2netweb.py --no-bootstrap --data-dir /var/lib/ser2netweb/<name> --set-password admin` |
| Docker | `docker exec -it ser2netweb python /app/ser2netweb.py --no-bootstrap --data-dir /data --set-password admin` (the container's name; 2.9.x's compose file named it `ser2net`) |

**First admin without the browser:** run `ser2netweb --set-password admin` before the first
start (a source install under systemd: after the service's first start, which makes its
`data/`; it creates `config.json`; the UI then listens on `127.0.0.1:8080` unless
`--reconfigure` or `SER2NETWEB_BIND_IP` says otherwise), or — Docker — point
`SER2NETWEB_ADMIN_PASSWORD_FILE` at a file (a Docker secret) holding the password, with
`SER2NETWEB_ADMIN_USER` for the name (default `admin`). The variable is used only while there
is no account at all; later starts ignore it.

### LAN cluster

- **Signed requests:** every request between nodes is signed with (a key derived from)
  the cluster key — method, path, body, both nodes' ids, a time and a one-time nonce —
  and so is every reply. Between such nodes the key itself is never sent, a captured
  request can't be sent again (to that node or another), and a reply can't be altered. The nodes' clocks need not
  agree: a node learns each peer's clock from its signed beacons and replies. Refused
  requests are logged once per address and change (`cluster: refused …`) and recorded in the
  audit log (`cluster_auth_failed`).
- **Who acted:** a remote start/stop/restart or edit is recorded in the audit log of both
  nodes: on the node the browser is signed in to under the user's name, and on the node it
  changed as `cluster-peer:NODE/USER` — the sending node's name (its instance name, else its
  host name; without spaces or `/`) and its user, as that node states it in the signed request (a claim of a node with the cluster key, not an
  account of the changed node). A request from a node running 2.8.1 or older stays
  `cluster-peer`.
- **Older nodes (legacy authentication):** nodes running 2.8.0 or older only know the raw
  key, sent in every request. While *Legacy authentication* is ticked (Settings → LAN
  cluster; on by default, so a rolling upgrade doesn't split the fleet) a node accepts it and
  uses it with exactly those nodes: ones whose beacon says they are older (at the address the
  beacon advertises), and manual peers that refuse a signed request the way older releases do
  (never one that has answered a signed request). An older node that advertises no LAN
  address (no default route: its beacon says 127.0.0.1) is only managed from other nodes once
  it is added there as a manual peer; the fleet view says so. The fleet view tags these nodes
  **unsigned key** and lists them in a banner. **Once every node is upgraded, untick it on
  every node.**
- **The key:** anyone on the network can try to guess a weak key offline from the beacons,
  and the key lets a node start, stop and change the mappings of every other node: use at
  least 16 random characters (*Generate a new key and save* makes one, shown once). The
  Settings page never shows the stored key; a blank field keeps it.
- **Changing the key:** *planned* — on each node enter the new key (or generate it on one and
  copy it) with *keep accepting the current one for 7 days* ticked; nodes not changed yet keep
  working, and the fleet view tags them **previous key**. When every node has it, tick *Stop
  accepting the previous key now* on each. *Leaked* — untick *keep accepting*, so the old key is
  refused at once, and change it on every node; until then the fleet view shows **cluster key
  rejected** for the nodes not changed yet, and a banner lists the nodes that broadcast with a
  different key. *Find other nodes automatically (UDP broadcast)* can be switched off where
  only manual peers are used.

### Upgrading a cluster

- **Upgrade every node, then edit across nodes.** A node edits another node's mapping with
  its own form, so a setting one of the two versions doesn't have can't be edited that way.
  From 2.9.0 on the fleet view tags a peer running an **older version** or a **newer
  version**, a banner lists the versions while they differ, the edit form of such a peer says
  so and leaves out the settings an older peer doesn't keep, and a peer is offered only what
  its version can do (before 2.6.0: view only; 2.6.0: start, stop and restart, no edit).
- **Edits from older nodes:** a 2.9.0 node keeps every setting an older node's form can't
  send, for example the MQTT client ID (no field before 2.9.0), the USB identity and the
  Modbus points' `writable` (before 2.8.0), RS-485 (2.6.1 / 2.6.2), and this release's
  settings (the no-data alarm, MQTT TLS, the points' options, the unit map …). Editing an older node, it sends that node's own
  trace path back unchanged and the settings its form leaves out for a Modbus gateway
  (banner, close-on string, client buffer).
- **Released versions don't change:** an edit **from** a node before 2.9.0 **to** another
  node before 2.9.0 loses what it always lost (every such edit clears the MQTT client ID; a
  2.7 node's edit clears the `writable` flags of a 2.8 node's Modbus points). Upgrade those
  nodes first, or edit their mappings on the node itself until then.
- **Order:** upgrade the nodes one at a time; with *Legacy authentication* on (the default)
  nodes running 2.8.0 or older keep working until the last one is upgraded; then untick it on
  every node (see LAN cluster above).

---

## ⚙️ Run as a service

- **Docker:** official `Dockerfile` + `docker-compose.yml` (see above and
  [`docs/DOCKER.md`](docs/DOCKER.md)) — `restart: unless-stopped`, `init: true` and
  `stop_grace_period: 30s` (a stop reaches ser2netweb, which finishes within that), `/data`
  volume. A plain `docker run` needs `--restart unless-stopped --init --stop-timeout 30` for
  that (an existing container: `docker update --restart unless-stopped ser2netweb`).
- **Linux (systemd):** three units — `ser2netweb.service` (a source checkout),
  `ser2netweb-standalone.service` (the Linux binary) and the template `ser2netweb@.service`
  (several instances). All run ser2netweb as a dedicated unprivileged user (`ser2netweb`, in
  the `dialout` group for the serial ports), restart it 3 s after a failure and sandbox it
  (read-only system, own `/tmp`, a system call filter). **Do not run it as root.**
  `systemctl reload ser2netweb` (or `ser2netweb@<name>`) reads `config.json` again (see
  Configuration & state files). Up to 2.9.x the units, the user and the folders were called
  `ser2net` (`ser2net.service`, `/opt/ser2net`, `/var/lib/ser2net`), as the classic ser2net
  daemon's package names its own: *Upgrading from 2.9.x* below moves such an install over.
  On Arch, Manjaro and EndeavourOS the serial group is `uucp` and there is no `dialout`
  group (the unit would fail with `status=216/GROUP`): set it with
  `systemctl edit ser2netweb` (`[Service]`, `SupplementaryGroups=`, then
  `SupplementaryGroups=uucp`; `ser2netweb.service`'s comment has it). `ser2netweb-install.sh`
  stops on such a system (it needs a `dialout` group): install the binary by hand there.
  When a port doesn't open: [`docs/LINUX-SERIAL.md`](docs/LINUX-SERIAL.md).
  - *Source checkout* in `/opt/ser2netweb`: `systemd/ser2netweb.service`.
    ```bash
    sudo useradd --system --user-group --home-dir /opt/ser2netweb -M -s /usr/sbin/nologin ser2netweb
    sudo git clone https://github.com/haliskilic/ser2netweb.git /opt/ser2netweb
    sudo cp /opt/ser2netweb/systemd/ser2netweb.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now ser2netweb
    ```
    The first start makes `data/` and `lib/` for the `ser2netweb` user and installs the
    bundled dependencies into `lib/` (offline). It needs Python 3.10 or newer as
    `/usr/bin/python3`: on RHEL / AlmaLinux / Rocky 8 and 9 and Debian 11 point
    `ExecStart=` at a newer interpreter (RHEL: `dnf install python3.11`, then
    `/usr/bin/python3.11`) or use the binary.
  - *Standalone binary*: `ser2netweb-standalone.service` (attached to every release, and in
    `systemd/`), installed as `ser2netweb.service`; its data is in `/var/lib/ser2netweb`,
    which systemd makes. `ser2netweb-install.sh` (attached to every release too) does it all:
    download `ser2netweb-linux-x64` (or `-arm64`), `ser2netweb-standalone.service`,
    `ser2netweb-install.sh` and `SHA256SUMS` into one folder, then
    ```bash
    sudo sh ser2netweb-install.sh                         # the web UI on 127.0.0.1:8080
    sudo sh ser2netweb-install.sh --listen 0.0.0.0:8080   # or on the network (use TLS there)
    ```
    It checks the files against `SHA256SUMS` and that the binary runs on this machine,
    creates the `ser2netweb` user and `/var/lib/ser2netweb`, installs the binary as
    `/usr/local/bin/ser2netweb` and the unit as `/etc/systemd/system/ser2netweb.service`,
    enables and starts the service and waits until it answers. Run it again with a newer
    release to upgrade (the data stays); `--uninstall` removes the service and the binary
    (the data and the user stay). It won't hide another `ser2netweb.service` (a package's, a
    source install in `/opt/ser2netweb`) unless you stop that one and pass
    `--replace-unit`; where `/tmp` is mounted `noexec` it has the binary unpack itself in
    `/var/cache/ser2netweb` instead (a drop-in). `--root DIR` only stages the files under DIR.
    The same by hand:
    ```bash
    sudo install -m 0755 ser2netweb-linux-x64 /usr/local/bin/ser2netweb
    sudo useradd --system --user-group --home-dir /var/lib/ser2netweb -M -s /usr/sbin/nologin ser2netweb
    sudo cp ser2netweb-standalone.service /etc/systemd/system/ser2netweb.service
    sudo systemctl daemon-reload && sudo systemctl enable --now ser2netweb
    ```
  - *The `.deb` / `.rpm` package* (*Linux packages*): the binary's unit as
    `/usr/lib/systemd/system/ser2netweb.service` with `/usr/bin/ser2netweb`, the account and
    `/var/lib/ser2netweb` are installed with it; `sudo systemctl enable --now ser2netweb`
    starts it. What follows (`systemctl edit`, `sudo -u ser2netweb …`) applies to it as well.
  - The web UI listens on `127.0.0.1:8080` until you say otherwise. To reach it from the
    network, run `sudo systemctl edit ser2netweb`, add the lines below (use TLS on a network
    address, see Security) and `sudo systemctl restart ser2netweb`:
    ```ini
    [Service]
    Environment=SER2NETWEB_BIND_IP=0.0.0.0 SER2NETWEB_PORT=8080
    ```
    Set the first password in the browser, or with `--set-password` (the table under
    Security). Run such commands as the `ser2netweb` user (`sudo -u ser2netweb …`): the
    account commands (`--set-password`, `--sign-out-all`, `--reload`) switch to the data
    directory's owner by themselves when run as root, but a start or `--reconfigure` as
    root would leave files the service can't read. Logs:
    `journalctl -u ser2netweb` and `all.log` in the data directory.
  - Several instances: the template `systemd/ser2netweb@.service` (below).
  - Optional: the drop-in `systemd/ser2netweb.service.d/notify.conf` (`Type=notify`,
    `WatchdogSec=120`) makes systemd count ser2netweb as started only once its mappings run
    and its UI listens (or tries again to), and restart it when its event loop stops
    answering. Neither unit nor `ser2netweb-install.sh` installs it, and it is not a release
    file (take it from the source tree):
    ```bash
    sudo mkdir -p /etc/systemd/system/ser2netweb.service.d
    sudo cp systemd/ser2netweb.service.d/notify.conf /etc/systemd/system/ser2netweb.service.d/
    sudo systemctl daemon-reload && sudo systemctl restart ser2netweb
    ```
    (an instance: `ser2netweb@.service.d`). Remove it before going back to a version before
    2.9.0, which never reports ready.
  - *Upgrading from 2.9.x*: the units, the user and the folders have new names; the data
    stays as it is (the Prometheus metric names, `config.json` and the cluster protocol are
    unchanged too). Leave the classic ser2net daemon's `ser2net.service` alone (its package
    lives in `/usr/lib/systemd/system`), and remove only what an old install of this
    project put in `/etc/systemd/system`.
    - *The binary installed with `ser2net-install.sh`*: download the new release's files
      (as above) and run `sudo sh ser2netweb-install.sh`. It recognises 2.9.x's
      `ser2net.service` by its content (never by its name: the classic daemon's has it too),
      stops and disables it, moves `/var/lib/ser2net` to `/var/lib/ser2netweb` for the new
      `ser2netweb` user, keeps the `--listen` address and moves your drop-ins of the old unit
      along (check them for the old names), and once the new service answers removes the old
      unit and `/usr/local/bin/ser2net` (only when it says it is 2.9.x's binary). It changes
      nothing when `/var/lib/ser2netweb` already holds data too, and a `ser2net.service` it
      doesn't recognise is left alone. The user `ser2net` stays unless you add
      `--remove-old-user`, which keeps it anyway while the classic daemon is installed or
      anything runs as it. If the new service doesn't come up, the script says how to go
      back to 2.9.x.
    - *A source checkout in `/opt/ser2net` with `ser2net.service`*: either keep it as it is
      (`ser2net.py` still starts 3.x, so `sudo git -C /opt/ser2net pull` and
      `sudo systemctl restart ser2net` upgrade it under the old names), or move it over:
      ```bash
      sudo systemctl disable --now ser2net
      sudo useradd --system --user-group --home-dir /opt/ser2netweb -M -s /usr/sbin/nologin ser2netweb
      sudo mv /opt/ser2net /opt/ser2netweb && sudo git -C /opt/ser2netweb pull
      sudo chown -R ser2netweb:ser2netweb /opt/ser2netweb/data /opt/ser2netweb/lib
      sudo rm /etc/systemd/system/ser2net.service      # this checkout's unit only
      sudo cp /opt/ser2netweb/systemd/ser2netweb.service /etc/systemd/system/
      sudo systemctl daemon-reload && sudo systemctl enable --now ser2netweb
      ```
      Drop-ins made with `systemctl edit ser2net` (a listen address, `uucp`, ports below
      1024) are in `/etc/systemd/system/ser2net.service.d/`: move them first
      (`sudo mv /etc/systemd/system/ser2net.service.d /etc/systemd/system/ser2netweb.service.d`).
    - *Instances of `ser2net@.service`*, for each instance (here `line1`):
      ```bash
      sudo systemctl disable --now ser2net@line1
      sudo mkdir -p /var/lib/ser2netweb /etc/ser2netweb
      sudo mv /var/lib/ser2net/line1 /var/lib/ser2netweb/line1
      sudo mv /etc/ser2net/line1.env /etc/ser2netweb/line1.env    # if it has one
      sudo cp /opt/ser2netweb/systemd/ser2netweb@.service /etc/systemd/system/
      sudo systemctl daemon-reload && sudo systemctl enable --now ser2netweb@line1
      ```
      systemd hands the moved folder to the `ser2netweb` user at the first start
      (`StateDirectory=`). `/etc/ser2net/` is also the classic daemon's folder: move only
      the `<name>.env` files. Once no instance is left, `sudo rm
      /etc/systemd/system/ser2net@.service`.
    - The old `ser2net` user can go once nothing runs as it any more (`sudo userdel
      ser2net`), unless the classic ser2net daemon is installed: its package may use an
      account of that name too.
- **Ports below 1024** (Modbus/TCP's 502, the web UI on 80 or 443): Linux lets only root,
  or a process with the `CAP_NET_BIND_SERVICE` capability, listen on them, and both units
  run ser2netweb as an unprivileged user. A mapping on such a port then fails with a hint on
  the dashboard, and an admin UI port below 1024 gets a start-up warning (the web UI can't
  start). To allow them, give the service that one capability with
  `sudo systemctl edit ser2netweb`, then `sudo systemctl restart ser2netweb`:
  ```ini
  [Service]
  AmbientCapabilities=CAP_NET_BIND_SERVICE
  CapabilityBoundingSet=CAP_NET_BIND_SERVICE
  ```
  It works with the units' `NoNewPrivileges=true`; whoever may edit mappings can then
  listen on any free port below 1024. `setcap cap_net_bind_service=+ep` on python3 does not
  help (`NoNewPrivileges` ignores file capabilities) and would give that right to every
  Python program on the machine. Or use port 1024 or above. Docker on the host network:
  `SER2NETWEB_LOW_PORTS=1` ([`docs/DOCKER.md`](docs/DOCKER.md#ports-and-networking)).
- **Windows:** nothing else needed. From an elevated ("Run as administrator") prompt:
  ```bat
  ser2netweb-windows-x64.exe --install-service
  ```
  The exe copies itself to `C:\Program Files\ser2netweb\` (admin-only, so a standard user
  can't replace the file the service runs as SYSTEM) and keeps its data in
  `C:\ProgramData\ser2netweb\` (both writable by SYSTEM and Administrators only). The
  service, `ser2netweb`, starts at boot, needs no signed-in user
  (LocalSystem) and restarts 5 s after it crashes or exits with an error; the console asks
  once for the UI address. Remove it with `--uninstall-service` (the data folder is kept,
  with the configuration, backups, logs, audit log and traces: delete it to remove those);
  run several instances via `--service-name`. Stop/start: `sc stop ser2netweb` /
  `sc start ser2netweb` or *Services* (services.msc). Re-running `--install-service` from a
  newer exe upgrades it in place and keeps the data directory the service uses. Over 2.x's
  service (called `ser2net`, in `C:\Program Files\ser2net\` and `C:\ProgramData\ser2net\`)
  the same command moves it over: it stops it, installs `ser2netweb` (moving the data to
  `C:\ProgramData\ser2netweb\` when it was in 2.x's default place), deletes the old service
  once the new one runs and puts it all back if it doesn't; the old firewall rule then
  needs replacing (docs/WINDOWS.md, *Upgrading from 2.x*). The service is *Running* once
  its UI listens (*Starting* until then), so
  `--install-service` reports a service that stops while it starts (with its exit code and
  where to look) instead of success. **[`docs/WINDOWS.md`](docs/WINDOWS.md)** covers the
  firewall rule you need to reach it from other hosts, changing the address, upgrading and
  the SmartScreen warning on unsigned builds. Use the service for production: a console
  window can be closed or signed out of. A click into a console window (QuickEdit) pauses
  its output until Esc or Enter; ser2netweb keeps forwarding meanwhile, only the console's
  lines wait (and past a limit are dropped and counted; `all.log` has them all).
- **Start order:** the admin UI comes first. Its TLS pair must load, or the start stops
  (with the reason in `all.log`) before any serial port is opened, so a restart policy
  can't reset DTR-sensitive boards in a loop. Its port is taken before the mappings start:
  a mapping on the UI's address and port shows `error` instead of keeping the UI out. When
  the UI's own address can't be used (another program has the port, or the IP is not
  assigned yet at boot), the mappings start anyway and the UI tries again, at most 30 s
  apart (`all.log` says so). A mapping can't be saved on the UI's TCP address and port, nor
  a UDP one on the cluster's discovery port while discovery is on (form, REST API, cluster,
  import; a copy's port skips them).
- **A stuck event loop is restarted.** All mappings and the web UI run on one event loop.
  When a call blocks it, ser2netweb writes every thread's stack to `hang.log` in the data
  directory and exits (code 70) after 90 s, so the restart policy (Docker, systemd, the
  Windows service) starts it again; the next start keeps the file as `hang.log.1` and says
  so in `all.log`. `SER2NETWEB_LOOP_WATCHDOG=<seconds>` changes the limit, `0` switches it
  off. 90 s is far above what a busy instance needs to get back to its loop (about a
  second), and a machine that slept or a container that was paused is not counted.
  `/metrics` has the loop's lag (`ser2net_event_loop_lag_seconds`). `/healthz` answers
  `ok` while the loop runs and is only a liveness check: Docker marks a container that
  stops answering *unhealthy* but restarts nothing for it; under Kubernetes use it as a
  liveness probe with room for a busy moment, e.g.
  `livenessProbe: {httpGet: {path: /healthz, port: 8080}, periodSeconds: 30,
  timeoutSeconds: 5, failureThreshold: 4}` (add `scheme: HTTPS` with TLS).
- **Many mappings:** a running mapping keeps about three files open (its serial port, its
  listener and its log), plus one per connected client. At start ser2netweb raises its soft
  open-file limit toward the hard limit (at most 65536) and logs the result (`open files:
  …`); the systemd unit sets `LimitNOFILE=65536`, and a low hard limit in Docker is raised
  with `--ulimit nofile=65536`. The start-up log warns when the configured mappings need
  close to the limit.
- **Many connections on Windows:** the event loop ser2netweb uses there (`select()`) watches at
  most 512 sockets (listeners, clients, the UI's connections). Near that, new connections
  are refused with a log line, and the service keeps running. The environment variable
  `SER2NETWEB_WINDOWS_LOOP=proactor` (for the service: a system environment variable) switches
  to the proactor loop, which has no such limit; it is new, so test serial timing (e.g.
  Modbus RTU) with it before relying on it. The start-up log names the loop in use.
- **Serial writes on Windows** wait for the device, as on Linux: at most 8 KiB per port are
  handed to the driver at a time, so a fast network sender is slowed to the serial line's
  speed instead of filling the driver's queue. A port with no traffic for a second is
  polled every 10 ms instead of every 0.5 ms, and at once again every 0.5 ms when data comes
  or goes (a reply after a request is not delayed). Should a serial driver misbehave with this,
  `SER2NETWEB_WINDOWS_SERIAL=pyserial` (for the service: a system environment variable) goes
  back to how pyserial writes (no pacing); please report the adapter and driver.

### Several instances on one machine

Each instance needs its own:

- **data directory** (`--data-dir`): a second ser2netweb on the same one stops at start. Don't
  copy a data directory to make another instance — the copy has the same cluster identity
  (the fleet view then shows a "copy of this installation" warning) and session key:
  start the new one empty and import the mappings (**Settings → Mappings export / import**).
- **web UI port:** the console picker (first start, `--reconfigure`, `--install-service`)
  tries the port it offers and, when another program (another instance) has it, says so
  and offers the next free one; a choice you type is kept. A first start without a terminal
  (a service, a container) keeps its port and logs the warning. A UI whose port is taken
  starts its mappings anyway and keeps trying the port until it is free. Without the picker:
  `SER2NETWEB_BIND_IP` + `SER2NETWEB_PORT`.
- **mapping ports and serial devices:** a TCP/UDP port and a serial device belong to one
  instance. A mapping saved while another program holds its port or device is kept, and the
  UI says why it didn't start; it is tried again, and starts once its port is free.
- **name:** **Settings → Admin interface → Name of this instance** (or
  `SER2NETWEB_NODE_NAME`) — notifications, the browser tab, the top bar and the fleet view
  then say which instance it is.
- imported mappings that publish to MQTT need **ids** of their own: a client id that is
  set, and a Sparkplug edge node id (the mapping's name when blank) — a broker drops a
  client whose id another one takes over, and two Sparkplug nodes of one id in a group
  confuse its host application.

The browser keeps each instance's sign-in apart when they are on different ports
(`http://host:8080`, `http://host:8081`); 2.8 and older shared one sign-in per host name, so
reach those under different host names (the IP address and the name) or in separate browser
profiles.

- **Windows:** `ser2netweb-windows-x64.exe --install-service --service-name line2`
  (data in `C:\ProgramData\ser2netweb\line2`) (and a firewall rule per service:
  [docs/WINDOWS.md](docs/WINDOWS.md)).
- **Linux (systemd):** the template `systemd/ser2netweb@.service` runs one instance per
  name, with its data in `/var/lib/ser2netweb/<name>` and its address in
  `/etc/ser2netweb/<name>.env` (`SER2NETWEB_BIND_IP=0.0.0.0`, `SER2NETWEB_PORT=8081`); the
  instance name is its name in notifications. Install the unit and the bundled
  dependencies once, and the dependencies again after each upgrade (the instances start
  with `--no-bootstrap`, and their start-up log warns while `lib/` is out of date):
  ```bash
  sudo cp /opt/ser2netweb/systemd/ser2netweb@.service /etc/systemd/system/ && sudo systemctl daemon-reload
  sudo install -d -o ser2netweb -g ser2netweb /opt/ser2netweb/lib
  sudo -u ser2netweb python3 /opt/ser2netweb/bootstrap.py        # again after each upgrade
  sudo systemctl enable --now ser2netweb@line1 ser2netweb@line2
  ```
  (2.9.x's `ser2net@.service` instances: *Upgrading from 2.9.x* under Run as a service.)
- **Docker:** one container per instance, each with its own volume, `container_name` and
  published ports (`8081:8080`), and `SER2NETWEB_NODE_NAME` (or `hostname:`) for its name
  ([docs/DOCKER.md](docs/DOCKER.md)).

### Time synchronisation

ser2netweb takes every date from the system clock and never sets it (the systemd unit
forbids it: `ProtectClock=true`). A wrong clock puts wrong times in `all.log`, `audit.log`,
traces and backup names, can make the 15-day log trim and, with a retention period, the
deletion of old logs and backups keep or drop the wrong ones (the newest 50 backups are
chosen by name, which a clock that went back doesn't change), and makes date checks fail: sign-in sessions, two-factor
codes, SSO, and TLS certificates (MQTT, SMTP, webhooks, SSO, and a self-signed certificate
made while the clock was wrong). How long something has been running (uptime, connection
times, the cluster's peer list) is measured with a clock that no clock step changes.

**A clock set while ser2netweb runs** (by hand, by time sync catching up, by a VM host) is
noticed: every 5 seconds the system clock is compared with a clock no step changes, and a
jump of 1 second or more is written to `all.log` with its direction and size, recorded in
the audit log (`clock_step`; at most one line a minute, which says how many more there
were), marked in every running trace with a `!!` line where its times jump, counted in
`/metrics` (`ser2net_clock_steps_total`; `clock_steps` in `/api/v1/status`) and sent as the
optional **System clock stepped** notification. NTP's gradual slewing is not a step, and
neither is a resume from suspend or hibernation: the comparison clock counts the time
asleep (`CLOCK_BOOTTIME` on Linux, `CLOCK_MONOTONIC` on macOS, `GetTickCount64` on
Windows).

- **Linux:** keep `systemd-timesyncd` or `chrony` running, pointed at an NTP server the
  machine can reach; on an offline plant network a local one (a router, a domain
  controller, a GPS time server): `NTP=10.0.0.1` in `/etc/systemd/timesyncd.conf`, or
  `server 10.0.0.1 iburst` in `/etc/chrony/chrony.conf`. `timedatectl` shows whether the
  clock is synchronised.
- **Raspberry Pi and other boards without an RTC** (Pi 3/4; the Pi 5's RTC needs its
  battery): until NTP answers, the clock starts in the past. `fake-hwclock` (installed on
  Raspberry Pi OS) at least brings back the last saved time at boot; an RTC module keeps
  the real one.
- **Windows:** the Windows Time service: `w32tm /config /manualpeerlist:10.0.0.1
  /syncfromflags:manual /update`, then `w32tm /resync`; `w32tm /query /status` shows its
  source. A domain member takes its time from the domain controllers.
- **Docker:** a container uses the host's clock: synchronise the host. (Its time zone is
  separate: `TZ`, see [`docs/DOCKER.md`](docs/DOCKER.md).)
- **Waiting for the clock at boot:** the systemd unit waits for the network, not for time
  sync, because on a network without an NTP server ser2netweb would then never start. To
  wait anyway, enable `systemd-time-wait-sync.service` and add `After=time-sync.target`
  and `Wants=time-sync.target` with `systemctl edit ser2netweb`.
- **What ser2netweb checks:** the start-up log has a WARNING when the clock reads earlier
  than the newest configuration backup, the last change to `config.json` or the making of
  this version. Settings → *Admin interface* shows the server's time (UTC, and local with
  its zone), the same warning for admins, and a note when this browser's clock and the
  server's differ by more than 2 minutes.

---

## 🗂️ Configuration & state files

All state lives in the **data dir** (default `data/`; `--data-dir`, or with `--config` the
config file's folder). The data directory is locked to owner-only (POSIX `0700`; Windows
`icacls` owner/SYSTEM/Administrators), so the files inside aren't readable by other users,
and on Linux/macOS the files the server creates are owner-only too. That applies to a folder
ser2netweb made or one holding only its own files (the ones below): a folder with other files
in it (`--config /etc/ser2netweb.json`, a home directory) keeps its permissions, only the
config file (and `backups/`, `logs/`, `traces/`) is made private, and the start-up log warns
when other accounts can use the folder — give ser2netweb a folder of its own:
- `config.json` — admin IP, password hashes, all mappings (atomic write), and the secrets
  ser2netweb has to use itself, in plain text (see *Secrets* below).
- `backups/` — the `config.json` every change writes, and the one it replaces when that
  isn't kept yet (newest 50): the newest backup is always the current `config.json`.
  Starting or stopping a mapping adds no backup but updates the newest one, so it doesn't
  push out the backups of real changes. A new backup's name sorts first also after the
  clock went back (it takes the newest backup's time with the next number; the list then
  also shows when it was really saved). The
  list shows which release wrote each one. Download or restore one under Settings →
  *Configuration backups*. A restore first shows what changes
  for sign-in and access, then brings back the mappings and settings; the user accounts
  and passwords, the REST API tokens and the cluster key stay as they are unless you tick
  the box to restore them too (that also signs everyone else out). *Only its mappings*
  goes through the import preview. A restore is applied like a reload of `config.json`
  (see below): mappings it leaves as they are keep running with their clients, its admin
  TLS is used at once (files that can't be loaded refuse the restore), its admin UI address
  at the next start. A restore can be undone too; a downloaded backup leaves out the
  session key. The list names each backup's accounts; an admin can delete
  one backup or all of them there. Deleting a user leaves the account (with its password
  hash) in the backups made before — a restore with accounts would bring it back — so
  *Remove it from the backups* takes an account that no longer exists out of every backup
  that has it (the rest of each backup stays as it was). All of it is audited.
- `all.log` — global activity (size-rotated): also every REST API request but the
  successful polls of `/status`, `/health`, `/ports`, `/metrics` and `/openapi.json`, with the
  token's name, and the warnings and errors of the web server and other libraries (an
  address the admin UI can't use, a request it can't parse, the traceback of a request that
  failed; each message at most once a minute). On Windows, when another program holds it
  open and it can't be rotated, it is written on and rotated later, its older files kept.
  The Windows service also writes `service.log` (what Python itself reports, e.g. a crash;
  the previous start's kept as `service.log.1`) and, when it stops on an error, an entry in
  the Application event log; `audit.log` — sign-ins and failed ones, config changes, console / hex view
  opens, consoles that wrote to a device, trace downloads, mapping and audit exports and
  refused REST API tokens: who/from where/what (rotated). Searchable under Settings → *Audit
  log* (letter case and accents ignored, as on the dashboard), CSV export (UTF-8, commas, for
  scripts) and *CSV for Excel* (UTF-16 with tabs: opens in Excel by double-click with Turkish
  letters and columns intact, whatever the regional settings). Every `all.log` line is also
  written to stdout,
  which systemd's journal or Docker's container log keep under their own retention;
  `SER2NETWEB_LOG_STDOUT=off` keeps the lines in `all.log` only. `all.log` has a line with the
  client's IP address for every web UI page and change: Settings → *Audit log* → *Web UI
  requests in all.log* can log only changes and refused requests, or none (sign-ins and
  the audit records are logged either way).
- **Retention.** By default the logs keep their size limits however old the records are
  (all.log 10 MB × 6 files, audit.log 5 MB × 2, the newest 50 backups; a mapping log 15
  days). Logs hold IP addresses, user names and raw serial data, so a period can be set
  under Settings → *Retention* (admins): then all.log and audit.log begin a new file once
  their first record is a fifth of that many days old and delete a file once its first
  record is older — nothing older is kept, and at least four fifths of the period stay as
  far as the sizes allow (audit.log then keeps up to 6 files) — the mapping logs keep at
  most that many of their 15 days, and configuration backups named for an older time go,
  never the newest 5. Checked at once when the period is saved (the mapping logs at the next
  hourly check) and then every hour; a clock set ahead while ser2netweb
  runs deletes nothing early (ages count from the clock at start-up plus the time it has
  run). Restoring a backup from before 2.9 resets the period to 0 (off). The copy of every
  log line on the standard output is kept by whatever collects it: Docker caps it with the
  `logging:` options of `docker-compose.yml` (or the `local` driver), systemd's journal with
  `MaxRetentionSec=` / `SystemMaxUse=` in `/etc/systemd/journald.conf`.
- `logs/<id>.log` — per-mapping history (hourly maintenance: lines older than 15 days are
  dropped, but never the last 1000 the Log panel shows, so a clock that jumps ahead or a
  corrected date doesn't empty it; with a shorter period set under Settings → *Retention*,
  nothing older than that stays, however few lines are left; a log over 100 MB is cut to
  90 MB).
- `traces/<id>-<name>` — a mapping's traffic trace. Traces are kept **only here** and
  rotate at their size limit (default 10 MB × 3 old files). Full paths from v2.7 configs
  are reduced to the file name, which is logged at start-up. A trace that can't be written
  (folder removed, disk full, no file handle left) pauses instead of stopping: the
  dashboard row and the Trace panel say why, ser2netweb tries again every few seconds (making
  the folder again) and the trace then notes how many bytes are missing. A trace also
  pauses before the disk fills up: while the data folder's disk has less than 256 MB free
  (5% of a larger disk, at most 1 GB), checked at each new file and every 8 MB written; it
  goes on by itself once there is 10% more. Rotated files past a lowered *Rotated trace
  files kept* are deleted when the trace starts. Traces hold the raw traffic in both
  directions, personal data and passwords typed to a device included:
  delete them in the Trace panel once they are no longer needed (the file being written
  begins again with the next traffic), or have them deleted after a number of days without
  writes under Settings → *Retention* (off by default; checked every hour; the same panel
  shows the disk's free space and when traces are paused, `/metrics` has
  `ser2net_data_free_bytes`, and the *data disk almost full* notification can say so; a file a mapping
  keeps writing into begins anew once it holds traffic from that many days ago, so it goes
  too, that many days after its last write). Deleting a
  mapping deletes its traces; those of mappings a replace import or a restore removed stay until
  the next start (an undone restore finds them), which deletes them and logs how many.
- `tls/` — certificate/key if self-signed is generated, and the TLS files uploaded in the
  browser (`uploaded-<hash>-cert.pem`, `-key.pem`, `-ca.pem`). A relative TLS path (admin UI, a
  mapping or its MQTT connection: certificate, key, CA files; not the LDAP CA file) is a file
  in the data directory, e.g. `tls/cert.pem`,
  so it keeps working when the data directory moves. An absolute path that is no longer
  there after a move or a restore on another machine (native to Docker `/data`, another
  `--data-dir`, a backup from another host) and pointed into a `tls` folder is taken from
  this data directory's `tls/` (logged at start-up, and config.json is saved with the new
  path); backups and imports from before the move are handled the same way.
- `setup-code.txt` — the one-time code for first-run setup through a host name; it exists
  only until the first admin account does.
- `cert-notices.json` — which *TLS certificate about to expire* notifications were sent
  (per certificate file: its end date and the threshold), so a restart does not send them
  again. Deleted, they are sent once more.
- `sparkplug-bdseq.json` — the `bdSeq` of each Sparkplug B edge node's (group/node) last
  session; after a restart the numbering goes on from there. Deleted, the next start begins
  at 0.
- `hang.log` (and `hang.log.1`) — every thread's stack when the event loop watchdog ended
  ser2netweb (see Run as a service); `service.log` (and `.1`) — the Windows service's own
  log (above, with `all.log`).
- `ser2net.lock` — held while ser2netweb runs, so a second ser2netweb can't use the same data
  directory; `admin-requests/` — where `--set-password`, `--sign-out-all` and `--reload` hand
  their request to the running ser2netweb (see below).

**Secrets:** `config.json` holds, in plain text, the LDAP bind password, the OIDC client
secret, the SMTP password, the webhook URL (which often carries a token), each mapping's
MQTT broker password and MQTT client key password, the TLS key passwords (a mapping's and
the admin UI's), the cluster
key (enough to read and change the mappings of every node) and the session key `secret_key`
(with the accounts in the same file, enough to make a valid sign-in), and the two-factor
sign-in key of each account that uses it. Local passwords are kept as scrypt hashes and the
REST API tokens only as hashes. `backups/` holds earlier copies
of the same file (a downloaded backup leaves out the session key), and the mappings export
holds the MQTT passwords and the MQTT and TLS key passwords. On this machine only the account ser2netweb runs as can
read them; treat every copy — a copied data folder or Docker
volume, a disk or VM image, a downloaded backup, an export — like a password vault.
[SECURITY.md](SECURITY.md) (*Stored secrets*) lists what to change after such a copy leaked.

**Personal data:** account names (an LDAP or SSO name is often an e-mail address), IP
addresses, sign-ins and failed sign-ins are recorded in `config.json`, `backups/`,
`all.log*` (and its stdout copy in the journal or Docker's log), `audit.log*` and `logs/`.
Notifications, LDAP, SSO, MQTT and the cluster send data off the host only when you set them
up, and there is no telemetry. **[docs/PRIVACY.md](docs/PRIVACY.md)** lists what is kept
where and for how long, who can see it, the settings that keep less, and how to answer an
access request or erase one person or the whole installation.

**Times** in `all.log`, stdout, `logs/<id>.log` and `audit.log` (also its viewer and CSV
export) are the server's local time with its offset from UTC: `2026-11-06 12:00:00+03:00`,
so the hour repeated when daylight saving time ends stays apart and a container's UTC lines
can't pass for local time. Lines written before 2.9 have no offset; the first 19 characters
are the same, so old and new lines in one file read alike (in older versions too). Lines in
trace files keep `YYYY-MM-DD HH:MM:SS` in the server's local time (the panels name its
zone), backups are named in UTC, and the live hex view shows the server's times in the
browser's time zone and says which. A log shipper that matches the old format with a regular expression needs
the optional offset (`[+-]HH:MM`) after the seconds. In Docker the zone is UTC unless `TZ`
is set ([`docs/DOCKER.md`](docs/DOCKER.md)).

Mapping ids (the `<id>` above, also used by the REST API) are 1–64 letters, digits or `_`,
and not a Windows device name (`con`, `nul`, `com1`, `lpt1` …); new mappings get 12 hex
digits. An id in any other form (hand-edited or imported) is
replaced when the config is loaded or the file is imported; the start-up log or the import
preview says so.

**Changing `config.json` while ser2netweb runs:** ser2netweb reads it at start-up and then works
from its copy in memory. After an edit by hand or by a tool (Ansible, a copied file), have it
read the file again — no restart: `systemctl reload ser2netweb` (an instance:
`systemctl reload ser2netweb@<name>`; all three units have `ExecReload`) or `kill -HUP <pid>`
(SIGHUP, Linux/macOS; `docker kill -s HUP ser2netweb`), `ser2netweb --reload` with the same
`--data-dir` (`/var/lib/ser2netweb` for the binary's unit, `/var/lib/ser2netweb/<name>` for an
instance; any platform, also for the Windows service and in a container) or Settings →
*Configuration backups* → *Reload config.json*. The file is checked as an edit is: when it
can't be read or doesn't validate (a value of the wrong type in a changed mapping, two
mappings on one port, no user accounts, a TLS pair that doesn't load, MQTT TLS files that
can't be used, an MQTT client ID, command topic or Sparkplug B node another enabled mapping
uses on the same broker) nothing changes and `all.log` says why. Other values are read as
at start-up: text in a number's place is converted, an unusable value takes its default,
with a load note in `all.log` (see REST API: *A value may be sent as text*). Otherwise it is applied part by part: a mapping whose settings didn't
change keeps running with its clients (one that should run but doesn't is started), a
changed one restarts, a removed one stops, a new one starts; the cluster restarts only when
its settings changed. The admin UI's certificate files and those of unchanged TLS mappings
are read again for new connections (a renewed certificate: e.g. certbot's
`--deploy-hook "systemctl reload ser2netweb"`). The admin UI's address takes effect only at the
next start (the log, the reply and Settings say so), as do `--data-dir` / `--config` and the
environment variables. In a terminal, SIGHUP from the terminal closing still ends ser2netweb
(cleanly). The log line and the audit record (`config_reload`) say what changed. An edit that
isn't reloaded is noticed within seconds (a WARNING in `all.log`, a banner in Settings). A
change saved meanwhile — in the UI, a start or stop, an LDAP/OIDC sign-in — is not refused,
but it writes the configuration in use: the edited file is kept in `backups/` first, and
`all.log`, the audit log (`config_outside_edit_replaced`) and Settings name that backup, with a
link to restore it. So reload before saving anything else.

**Starting over:** stop ser2netweb, then delete the data directory (or everything in it; keep
`tls/` to reuse a generated certificate). Deleting only `config.json` resets the accounts,
settings and mappings, but `audit.log` (and its rotated copies), the rotated `all.log.1`–`.5` and
`backups/` stay (the logs and traces of mappings that no longer exist are deleted at the
next start), and whoever completes
the first-run setup next sees that audit log — the earlier users' names, addresses and
failed sign-ins — and those backups, with the earlier accounts and secrets, in Settings.
Don't do it while ser2netweb runs: its next save writes the configuration it has in memory,
accounts included, back to `config.json`. Copies outside the data directory stay too: the
journal (`journalctl -u ser2netweb`), Docker's container log (`docker compose down -v` removes
it with the `/data` volume, see [docs/DOCKER.md](docs/DOCKER.md)) and messages
notifications have sent. To get back in after a lost password, use `--set-password`
instead (see Security): it keeps everything else. `admin-requests/` only holds a
command-line request until the running instance takes it.

**Going back to an older version:** stop the service and copy the data folder first. A
release drops the settings it doesn't know at its first save — 2.7.x at once in Docker (with
`SER2NETWEB_BIND_IP` it saves at every start) — and 2.7.x keeps no backups; it also refuses
Modbus coil / discrete-input points (fn 1/2), so a config with them doesn't start there.
Since 2.9.0 the newest backup is always the current `config.json`, so the newer file is still
in `backups/` after an older release replaced it: after upgrading again, restore it under
Settings → *Configuration backups*. A `config.json` written by a newer release (its
`written_by`), or with settings this release doesn't know, is reported at start-up and on
that page. With this release's systemd units, a release before 2.9.0 ends on
`systemctl reload` (it took SIGHUP as stop) and systemd leaves it stopped: put the older
release's unit back, and remove the `notify.conf` drop-in, which such a release never
answers.

**Copying an installation** (a disk or VM image, a copied data folder or Docker volume):
`config.json` holds the node's identity — its cluster id (`instance_id`) and the key that
signs sign-ins (`secret_key`). Copies would hide each other in the cluster, and a sign-in to
one would be valid on the other. Delete both entries from `config.json` before taking the
image (new ones are made at the next start). Copies that already run are reported in the log,
the fleet view and Settings → LAN cluster; there, *Give this node a new identity* gives one of
them a new id and session key (everyone signed in to it is signed out).

**Session lifetime:** Settings → *Web UI access* → *Session lifetime* (default 8 hours).
When a session ends, the dashboard says so instead of going on showing old states (it
doesn't reload, so an open form keeps its changes; with unsaved changes the link signs in
in a new tab), and signing in again leads back to the page you were on. What an action did,
or why it failed (with the server's reason), appears at the bottom of the window; a panel
whose refreshes fail is marked *not up to date*. The mapping form asks before unsaved
changes are thrown away. In the console, **Shift+Tab** moves the keyboard focus out (Tab
goes to the device).
**Import:** shows a preview first; choose *merge* (update mappings with the same name, add
the new ones) or *Replace*. Settings an export of an older version doesn't have (2.7's
Modbus points have no `writable`, no trace rotation, no Sparkplug B, ...) are named in the
preview; a merge keeps their current values, *Replace* gives them the defaults. The
one-step import of 2.7 scripts (a POST without `step=preview`) still replaces all mappings
from a JSON file; a classic ser2net file is taken only through the preview.
**Export** writes the mappings as a UTF-8 JSON file (non-ASCII names and topics as they are,
like `config.json`); the import reads it (and the escaped files of older versions).

---

## 🧪 Testing

Unified test runner — the portable suite (no hardware, no socat) runs on every OS:
```bash
python3 tests/run_all.py            # portable suite (Windows + Linux)
python3 tests/run_all.py --socat    # + socat-based data-path tests (Linux)
python3 tests/run_all.py -v         # also show the output of passing tests
```
The socat tests use virtual serial ports (Linux); no hardware needed, and single files run
directly (`python3 tests/test_rest_api.py`). The browser layout test, the half-open TCP test,
the standalone-executable test, what CI runs and the development setup:
[CONTRIBUTING.md](CONTRIBUTING.md).

---

## 📜 License

ser2netweb is **dual-licensed**:

- **Open source — [GNU AGPL-3.0](LICENSE):** free to use, study, modify and share. If you distribute it, or run a modified version as a network service, you must make your complete source code available under the AGPL-3.0.
- **Commercial license:** for closed-source products, devices or SaaS without the AGPL-3.0 obligations — see [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md) or email **mail@haliskilic.com.tr**.

Bundled third-party components keep their own licenses — see [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md);
the full texts inside an executable: `ser2netweb --licenses`. The LGPL-licensed ldap3 stays
in the executable as plain source files; `SER2NETWEB_LIB_OVERRIDE=<dir>` loads another
version of it. Reporting a vulnerability: [SECURITY.md](SECURITY.md).

Every page of the web UI ends with the licence and a **Source code** link (signed in: also
the version and **Licences**, a page with the licence texts this copy carries — LICENSE,
NOTICE, the commercial licence and the third-party notices, or in an executable its
`THIRD-PARTY-LICENSES.txt`). A modified version that others use over a network has to offer
them its source (AGPL-3.0 section 13): point the link at it with `SER2NETWEB_SOURCE_URL`
(`https://git.example.com/our-ser2netweb`; a value that is not an http(s) address is not used,
with a start-up warning).

Contributions are welcome! On your first pull request you'll be asked to sign a one-time [Contributor License Agreement](https://gist.github.com/haliskilic/7a7fdcd355f54d1db6943959d7b76823); you keep the copyright to your work.

Copyright © 2026 Halis KILIÇ.

## ❤️ Support the project

If ser2netweb is useful to you, please consider **[sponsoring my work on GitHub Sponsors ❤️](https://github.com/sponsors/haliskilic)** — sponsorships pay for test hardware and the time to keep the project maintained.

No GitHub account? You can also **[buy me a coffee ☕](https://buymeacoffee.com/haliskilic)**.

Roadmap: [`ROADMAP.md`](ROADMAP.md) · Türkçe: [`README.md`](README.md)
