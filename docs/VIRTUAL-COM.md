# Client-side virtual COM ports

ser2net exposes each serial port as a TCP/UDP endpoint. To make a **remote**
serial port appear as a **local** port to an application, you pair ser2net with a
client-side redirector. There are three tiers, cheapest first.

## 1. Apps that already speak pySerial / RFC2217 (no driver)

If your client app uses pySerial (most Python tooling, many SCADA/test scripts),
point it straight at the mapping — no virtual COM needed. Set the mapping's
**protocol to `rfc2217`** so baud/parity/RTS/DTR changes propagate:

```python
import serial
ser = serial.serial_for_url("rfc2217://HOST:4001", baudrate=19200, timeout=1)
ser.write(b"hello")            # travels to the remote serial device
print(ser.read(16))
ser.baudrate = 115200          # applied on the remote port over RFC2217
```

This path is covered by `tests/test_rfc2217.py` (a real `rfc2217://` client against
the server, including a live baud change), so interop is verified in CI.

What the server does with an RFC 2217 client's settings:

- A setting the remote port can't take (a value the serial driver refuses, or a
  malformed or reserved one) is answered with the current setting, as RFC 2217 says,
  and the port keeps it; pySerial then raises `ValueError: remote rejected value`, and
  the connection stays up.
- If the serial device goes away (a USB adapter unplugged), the client stays connected
  while ser2net reopens the port; settings it changes meanwhile are answered and take
  effect when the port is back. The reopened port is given the client's baud rate, data
  bits, parity, stop bits, flow control and RTS/DTR, so the session goes on as before
  (if the reopened port refuses one of them, the client is disconnected and the log says
  which). A connect-out (`client` mode) RFC 2217 mapping connects only while its serial
  port is open.
- By default what a client set stays on the port after it leaves, until the port is
  reopened. Tick **Restore the serial settings when the RFC 2217 client leaves**
  (`options.rfc2217_restore_on_disconnect`) to put the port back to the mapping's baud
  rate, data bits, parity, stop bits and flow control when the client disconnects, so the
  next client, the web console, MQTT and a trace see what the mapping says. RTS/DTR are
  left as they are, because toggling them resets many boards (ESP32/Arduino auto-reset).
- Commands keep their order with the data: what a client writes before a setting change
  goes out at the old setting (the change waits until that data has left the port, at most
  2 s), and data written before a purge is handed to the port before the purge.
- BREAK: pySerial's `send_break()` and `break_condition` (SET-CONTROL BREAK on/off), and a
  telnet IAC BRK, reach the serial line after the data written before them. On a read-only
  mapping they don't, although the SET-CONTROL is acknowledged as asked (pySerial's `open()`
  fails on any other answer; the queries report the real state). A BREAK a client leaves on
  is turned off when it disconnects.
- Queries and other requests are answered: SIGNATURE (`ser2net`, or the mapping's
  **RFC 2217 signature**), the BREAK / DTR / RTS state, the inbound flow control, and the
  line/modem state masks. Flow control is one mode at a time (XON/XOFF turns RTS/CTS off and
  the other way round). A DSR/DTR mapping reports DSR/DTR flow control; "no flow control"
  leaves it on, since pySerial asks for that at every `open()`. What pySerial's server can't
  set (inbound-only, DCD, DTR or DSR flow control) is refused with the current setting.
  FLOWCONTROL-SUSPEND/RESUME are taken but don't hold the client's data back.

## 2. Linux — a real `/dev/tty*` for legacy apps

**An `rfc2217` mapping: ttynvt** (free and open source, a CUSE character device; build it
from its source where the distribution doesn't package it) makes a tty whose settings reach the remote port, as a local serial port
would: the app's baud rate, parity, RTS and DTR go to the device.

```bash
sudo ttynvt -n ttyNVT0 -S HOST:4001     # rfc2217 mapping on HOST:4001 -> /dev/ttyNVT0
# now point the legacy app at /dev/ttyNVT0 (give your user access to it as to a serial port)
```

**A `raw` mapping: `socat`** creates a pseudo-terminal that your app opens like a normal
serial port. It carries only the data: whatever baud rate or parity the app sets stays on
the PTY, so fix the serial settings in the mapping on the server. Linking it into `/dev`
needs root, and the PTY root creates belongs to root unless socat is told otherwise, so
give it your user and group:

```bash
# raw mapping on HOST:4001  ->  local /dev/ttyV0, usable by you
sudo socat -d -d PTY,link=/dev/ttyV0,raw,echo=0,user="$USER",group=dialout,mode=660 TCP:HOST:4001
# now point the legacy app at /dev/ttyV0
```

Don't use socat with an `rfc2217` or `telnet` mapping: their negotiation bytes, which an
rfc2217 mapping sends as soon as a client connects, would reach the app as data. (pySerial's
`python3 -m serial.rfc2217` is a self-test, not a redirector, and its `tcp_serial_redirect`
example serves a local serial port over TCP — the other direction.)

## 3. Windows — a real `COMx` for legacy apps

Windows apps that can only open `COM3` need a virtual COM driver on the client.

**Free (open source):** [com0com](https://sourceforge.net/projects/com0com/) + the
`com2tcp-rfc2217.bat` script of hub4com (from the same project), against an **`rfc2217`**
mapping:

```bat
REM create a linked virtual pair, e.g. COM10 <-> CNCB0  (com0com setup GUI)
REM then bridge CNCB0 to the ser2net rfc2217 mapping (in hub4com's folder):
com2tcp-rfc2217.bat \\.\CNCB0 HOST 4001
REM the legacy app opens COM10; its baud rate, parity, RTS and DTR reach the device
```

`com2tcp --telnet` (com0com's older tool) speaks plain telnet, not RFC 2217: the data gets
through, but the settings the app makes on COM10 never reach the remote port. Use it only
against a `raw` or `telnet` mapping whose serial settings are fixed on the server.

> Note: com0com's last release is 2018; on Secure-Boot Windows 10/11 you may need
> the signed build or to allow the test certificate. `hub4com` can fan one virtual
> port out to several consumers.

**Commercial (turnkey, signed drivers)** — point any of these at the ser2net
**`rfc2217`** mapping port; they create the COMx and handle the redirection:

- HW group **HW VSP3** (free single-port tier)
- Tactical Software **Serial/IP**
- Electronic Team **Serial to Ethernet Connector**

## Pairing two machines (no client driver at all)

To tunnel a serial link between two hosts running ser2net (like Moxa "Pair
Connection"): run one mapping as **TCP server** and, on the other host, a mapping
in **TCP client** mode with the same serial settings pointing at the server's
`IP:port`. Each side's local serial port now mirrors the other's.

## Which protocol?

- **raw** — lowest overhead; the serial settings are the mapping's, fixed on the server
  (socat PTY, `com2tcp --telnet`, apps that open a TCP socket).
- **rfc2217** — the client sets baud/parity/RTS/DTR remotely; needed by flashing tools and
  test instruments, and used by pySerial's `rfc2217://`, ttynvt, `com2tcp-rfc2217.bat` and
  the commercial drivers. Not for socat, which needs `raw`.
- **telnet** — 8-bit-clean stream with IAC escaping, no port-control.
