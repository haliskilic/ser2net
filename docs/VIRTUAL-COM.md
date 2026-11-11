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

## 2. Linux — a real `/dev/tty*` for legacy apps (free, socat)

`socat` creates a pseudo-terminal that your app opens like a normal serial port:

```bash
# raw protocol mapping on HOST:4001  ->  local /dev/ttyV0
socat -d -d PTY,link=/dev/ttyV0,raw,echo=0 TCP:HOST:4001
# now point the legacy app at /dev/ttyV0
```

For an `rfc2217` mapping, use pySerial's helper instead so port settings tunnel:

```bash
python3 -m serial.rfc2217 rfc2217://HOST:4001    # or the pyserial example tcp_serial_redirect
```

## 3. Windows — a real `COMx` for legacy apps

Windows apps that can only open `COM3` need a virtual COM driver on the client.

**Free (open source):** [com0com](https://sourceforge.net/projects/com0com/) +
`com2tcp`:

```bat
REM create a linked virtual pair, e.g. COM10 <-> CNCB0  (com0com setup GUI)
REM then bridge CNCB0 to the ser2net mapping with RFC2217 (telnet) framing:
com2tcp --telnet \\.\CNCB0 HOST 4001
REM the legacy app opens COM10
```

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

- **raw** — lowest overhead; the client must already know the baud/parity (socat
  PTY, app-configured COM redirector).
- **rfc2217** — the client sets baud/parity/RTS/DTR remotely; required for test
  instruments and the pySerial `rfc2217://` path above. Use this when in doubt.
- **telnet** — 8-bit-clean stream with IAC escaping, no port-control.
