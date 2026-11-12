"""Migrating from the classic (C) ser2net: importing ser2net.yaml / ser2net.conf.

app/ser2net_import.py turns a ser2net 4.x YAML file or a 3.x ser2net.conf into mapping
dicts plus warnings. Checks:
  * the default ser2net.yaml Debian ships (define: &banner, multi-line connectors,
    *banner aliases): 4 mappings; the two that share a device with an earlier one come
    in disabled, so the whole import validates;
  * accepters: telnet, telnet(rfc2217), udp, bind address forms (host, localhost, ipv6),
    sctp warned about, unusable ones skipped;
  * protections that can't be carried over hold the mapping disabled (certauth, ssl
    clientauth, TLS files missing here, dtls, wronly/rawlp, no-con-to-acc), with every
    reason; TLS is kept when its files are here; a read-only trace is not imported;
  * connectors: serialdev/sdev options (9600n81, rtscts, xonxoff, dtr=, nobreak, ...),
    the framing as words (19200 EVEN 8DATABITS 1STOPBIT), dtr/rts as true/false,
    Windows //./COMn, network/IPMI connectors skipped;
  * options: kickolduser, max-connections, banner escapes, openstr/closestr/closeon,
    trace-both, timeout, enable: off, remaddr -> allowed IPs, unsupported -> one warning;
    an escape for a byte of 0x80 or more (sent as two UTF-8 bytes here) holds an open or
    close string's mapping and is noted for a close-on string or banner;
  * the default remaddr only for a connection without addresses of its own (4.x / 3.x);
  * *(name), $(name), =(name) and *alias substitution; defaults by position and class
    (rfc2217, banner/openstr/closeon too), delete_default; version 2 files;
  * YAML subset details: quotes, comments, flow [..]/{..}, block scalars, documents;
  * a 3.x ser2net.conf with BANNER/TRACEFILE/OPENSTR/CLOSESTR/CLOSEON/DEVICE/DEFAULT/
    CONTROLPORT/LED/SIGNATURE lines, every state and line continuation;
  * garbage, JSON, empty and non-ser2net YAML -> ConfigError;
  * limits: alias/define/escape expansion, file size, nesting and connection count
    refuse the file quickly; long flows and quoted scalars are read in one pass;
  * every produced dict passes MappingConfig.from_dict(d).validate(), and the set of
    them passes AppConfig.validate() (no port or serial-device clash).
Pure parsing: no network, no serial hardware.

Run: python3 tests/test_ser2net_import.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import AppConfig, ConfigError, MappingConfig
from app.ser2net_import import parse_ser2net_config

# /etc/ser2net/ser2net.yaml as the Debian ser2net 4.x package ships it
DEBIAN_DEFAULT = r"""%YAML 1.1
---
# This is a ser2net configuration file, tailored to be rather
# simple.
#
# Find detailed documentation in ser2net.yaml(5)
# A fully featured configuration file is in
# /usr/share/doc/ser2net/examples/ser2net.yaml.gz
#
# If you find your configuration more useful than this very simple
# one, please submit it as a bugreport

define: &banner \r\nser2net port \p device \d [\B] (Debian GNU/Linux)\r\n\r\n

connection: &con0096
    accepter: tcp,localhost,2000
    enable: on
    options:
      banner: *banner
      kickolduser: true
      telnet-brk-on-sync: true
    connector: serialdev,
              /dev/ttyS0,
              9600n81,local

connection: &con1096
    accepter: tcp,localhost,2001
    enable: on
    options:
      banner: *banner
      kickolduser: true
      telnet-brk-on-sync: true
    connector: serialdev,
              /dev/ttyS1,
              9600n81,local

connection: &con0192
    accepter: tcp,localhost,3000
    enable: on
    options:
      banner: *banner
      kickolduser: true
      telnet-brk-on-sync: true
    connector: serialdev,
              /dev/ttyS0,
              19200n81,local

connection: &con1192
    accepter: tcp,localhost,3001
    enable: on
    options:
      banner: *banner
      kickolduser: true
      telnet-brk-on-sync: true
    connector: serialdev,
              /dev/ttyS1,
              19200n81,local
"""


def parse(text, filename=""):
    """parse_ser2net_config + the guarantees every caller relies on."""
    maps, warns = parse_ser2net_config(text, filename)
    assert all(isinstance(w, str) and w for w in warns), warns
    for d in maps:
        MappingConfig.from_dict(d).validate()
    AppConfig(mappings=[MappingConfig.from_dict(d) for d in maps]).validate()
    return {d["name"]: d for d in maps}, warns


def has(warns, *parts):
    return any(all(p in w for p in parts) for w in warns)


def test_debian_default():
    maps, warns = parse(DEBIAN_DEFAULT, "ser2net.yaml")
    assert list(maps) == ["con0096", "con1096", "con0192", "con1192"], list(maps)
    a = maps["con0096"]
    assert a["serial"]["port"] == "/dev/ttyS0" and a["serial"]["baudrate"] == 9600
    assert (a["serial"]["parity"], a["serial"]["bytesize"], a["serial"]["stopbits"]) == ("N", 8, 1)
    assert a["network"] == {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                            "port": 2000, "kick_old_user": True}, a["network"]
    assert a["options"]["banner"] == \
        r"\r\nser2net port \p device \d [9600N81] (Debian GNU/Linux)\r\n\r\n", a["options"]
    assert maps["con0192"]["serial"]["baudrate"] == 19200
    assert maps["con1192"]["network"]["port"] == 3001
    assert [maps[n]["enabled"] for n in maps] == [True, True, False, False]
    assert has(warns, "con0192", "imported disabled", "/dev/ttyS0", "con0096")
    assert has(warns, "con0096", r"\B", "9600N81")
    assert not has(warns, "telnet-brk-on-sync"), warns  # meaningless on a raw TCP port
    assert not has(warns, "TCP wrappers"), warns  # every port listens on localhost only
    print("Debian default ser2net.yaml: 4 mappings, banner alias, multi-line connectors, "
          "shared devices imported disabled  OK")


ACCEPTERS = r"""
connection: &tel
  accepter: telnet,tcp,2001
  connector: serialdev,/dev/ttyUSB1,9600n81
connection: &rfc
  accepter: telnet(rfc2217),tcp,192.168.1.5,2002
  connector: serialdev,/dev/ttyUSB2,9600n81
  options:
    max-connections: 3
connection: &rfcoff
  accepter: telnet(rfc2217=false),tcp,2003
  connector: serialdev,/dev/ttyUSB3
connection: &udp
  accepter: udp,2004
  connector: serialdev,/dev/ttyUSB4
connection: &udptel
  accepter: telnet,udp,2005
  connector: serialdev,/dev/ttyUSB5
connection: &v6
  accepter: tcp,ipv6,::1,2006
  connector: serialdev,/dev/ttyUSB6
connection: &v6any
  accepter: tcp,ipv6,2007
  connector: serialdev,/dev/ttyUSB7
connection: &hostname
  accepter: tcp,myhost.example.com,2009
  connector: serialdev,/dev/ttyUSB9
connection: &tls
  accepter: telnet,ssl(key=/etc/ser2net/k.key),tcp,2010
  connector: serialdev,/dev/ttyUSB10
connection: &gtlssh
  accepter: telnet(rfc2217),mux,certauth(enable-password),ssl,sctp,2011
  connector: serialdev,/dev/ttyUSB11
connection: &unixsock
  accepter: unix,/run/ser2net.sock
  connector: serialdev,/dev/ttyUSB12
connection: &dynamic
  accepter: tcp,0
  connector: serialdev,/dev/ttyUSB13
"""


def test_accepters():
    maps, warns = parse(ACCEPTERS)
    net = {n: d["network"] for n, d in maps.items()}
    assert net["tel"]["protocol"] == "telnet" and net["tel"]["bind_ip"] == "0.0.0.0"
    assert net["rfc"]["protocol"] == "rfc2217" and net["rfc"]["bind_ip"] == "192.168.1.5"
    assert net["rfc"]["max_connections"] == 1 and has(warns, "rfc:", "single client")
    assert net["rfcoff"]["protocol"] == "telnet"
    assert (net["udp"]["mode"], net["udp"]["protocol"], net["udp"]["port"]) == ("udp", "raw", 2004)
    assert net["udptel"]["protocol"] == "raw" and has(warns, "udptel:", "telnet over UDP")
    # ::1 is not accepted as a bind IP here (config treats ::/8 as reserved): stay on loopback
    assert net["v6"]["bind_ip"] == "127.0.0.1" and has(warns, "v6:", "IPv6 loopback")
    assert net["v6any"]["bind_ip"] == "::"
    # a host name can't be bound: listening on 0.0.0.0 would be wider, so it comes in disabled
    assert net["hostname"]["bind_ip"] == "0.0.0.0" and maps["hostname"]["enabled"] is False
    assert has(warns, "hostname:", "myhost.example.com", "imported disabled")
    # TLS without its files on this host and a gtlssh login can't be carried over: those
    # mappings come in disabled rather than as open cleartext listeners
    assert all(maps[n]["enabled"] for n in maps if n not in ("hostname", "tls", "gtlssh"))
    assert maps["tls"]["enabled"] is False and not net["tls"].get("tls")
    assert has(warns, "tls:", "/etc/ser2net/k.key", "not on this host", "imported disabled")
    assert net["gtlssh"]["protocol"] == "rfc2217" and net["gtlssh"]["port"] == 2011
    assert maps["gtlssh"]["enabled"] is False
    assert has(warns, "gtlssh:", "certauth", "imported disabled") and has(warns, "gtlssh:", "SCTP")
    assert "unixsock" not in maps and has(warns, "unixsock:", "skipped", "not a TCP/UDP port")
    assert "dynamic" not in maps and has(warns, "dynamic:", "skipped", "'0'")
    print("accepters: telnet / rfc2217 / udp / bind forms; TLS and certauth held disabled, SCTP "
          "warned; unix socket and port 0 skipped  OK")


def test_protections():
    """A connection that would be more open here than in ser2net comes in disabled."""
    d = tempfile.mkdtemp(prefix="s2n_import_")
    cert, key = os.path.join(d, "ser2net.crt"), os.path.join(d, "ser2net.key")
    for path in (cert, key):
        with open(path, "w") as fh:
            fh.write("placeholder\n")  # only checked to exist here
    text = f"""
default:
  name: cert
  value: {cert}
  class: ssl
default:
  name: key
  value: {key}
  class: ssl
connection: &tls
  accepter: telnet(rfc2217),ssl,tcp,6101
  connector: serialdev,/dev/ttyUSB1,115200n81
connection: &tlsfiles
  accepter: telnet,ssl(key={key},cert={cert}),tcp,6102
  connector: serialdev,/dev/ttyUSB2
connection: &tlsmissing
  accepter: telnet,ssl(key={d}/other.key),tcp,6103
  connector: serialdev,/dev/ttyUSB3
connection: &clientauth
  accepter: telnet,ssl(clientauth),tcp,6104
  connector: serialdev,/dev/ttyUSB4
connection: &gtlssh
  accepter: telnet(rfc2217),mux,certauth(enable-password),ssl,tcp,6105
  connector: serialdev,/dev/ttyUSB5
connection: &dtls
  accepter: dtls,udp,6106
  connector: serialdev,/dev/ttyUSB6
connection: &wronly
  accepter: tcp,6107
  connector: serialdev,/dev/ttyUSB7,9600n81,wronly
connection: &lp
  accepter: tcp,6108
  connector: dev(wronly),/dev/lp0
connection: &nocon
  accepter: tcp,6109
  connector: serialdev,/dev/ttyUSB9
  options:
    no-con-to-acc: true
connection: &rdonly
  accepter: tcp,6110
  connector: serialdev,/dev/ttyUSB10
  options:
    no-acc-to-con: true
    trace-read: /var/log/rd
connection: &twrite
  accepter: tcp,6111
  connector: serialdev,/dev/ttyUSB11
  options:
    trace-write: /var/log/wr
connection: &three
  accepter: telnet,mux,certauth,ssl(clientauth=false,key={d}/none.key),tcp,myhost,6112
  connector: serialdev,/dev/ttyUSB12
connection: &shared1
  accepter: telnet,ssl(clientauth),tcp,6113
  connector: serialdev,/dev/ttyUSB13
connection: &shared2
  accepter: telnet,tcp,127.0.0.1,6114
  connector: serialdev,/dev/ttyUSB13
"""
    maps, warns = parse(text)
    on = {n for n, m in maps.items() if m["enabled"]}
    assert on == {"tls", "tlsfiles", "rdonly", "twrite", "shared2"}, on
    for name in ("tls", "tlsfiles"):  # the certificate is on this host: TLS is kept
        net = maps[name]["network"]
        assert (net["tls"], net["tls_cert"], net["tls_key"]) == (True, cert, key), net
        assert has(warns, f"{name}:", "TLS (ssl) is kept", cert)
    assert not maps["tlsmissing"]["network"].get("tls")
    assert has(warns, "tlsmissing:", "other.key", "not on this host", "imported disabled")
    for name in ("clientauth", "gtlssh"):  # TLS is set up, but what it relied on is missing
        assert maps[name]["network"]["tls"] is True, name
    assert has(warns, "clientauth:", "client certificates", "imported disabled")
    assert has(warns, "gtlssh:", "certauth", "without a login")
    assert has(warns, "dtls:", "dtls", "imported disabled rather than unencrypted")
    assert has(warns, "wronly:", "wronly", "write-only") and has(warns, "lp:", "wronly")
    assert has(warns, "nocon:", "no-con-to-acc", "imported disabled")
    assert not has(warns, "nocon: not imported")  # handled, not listed as dropped
    r = maps["rdonly"]
    assert r["network"]["read_only"] is True and not r["options"].get("trace_both")
    assert has(warns, "rdonly:", "trace-read is not imported", "passwords")
    assert maps["twrite"]["options"]["trace_both"] == "/var/log/wr"
    assert has(warns, "twrite:", "trace-write: both directions")
    for why in ("listen address 'myhost'", "certauth", "none.key"):  # every reason is shown
        assert has(warns, "three:", why), why
    assert not has(warns, "three:", "client certificates")  # clientauth=false
    # a held connection does not claim its device: the narrower one sharing it stays on
    assert not has(warns, "shared2:", "also used by")
    assert has(warns, "TCP wrappers", "hosts.allow")
    print("protections: TLS kept with its files; certauth, clientauth, missing TLS files, dtls, "
          "wronly and no-con-to-acc held disabled with every reason; read-only traces off  OK")

    text = """
default:
  name: clientauth
  value: true
  class: ssl
connection: &before
  accepter: telnet,ssl(clientauth=no),tcp,6201
  connector: serialdev,/dev/ttyS1
connection: &auth
  accepter: telnet,ssl,tcp,6202
  connector: serialdev,/dev/ttyS2
delete_default:
  name: clientauth
  class: ssl
connection: &after
  accepter: telnet,ssl,tcp,6203
  connector: serialdev,/dev/ttyS3
"""
    maps, warns = parse(text)
    assert has(warns, "auth:", "client certificates") and not has(warns, "before:", "client")
    assert not has(warns, "after:", "client certificates")
    assert has(warns, "after:", "/etc/ser2net/ser2net.crt") or has(warns, "after:", "is kept")
    print("ssl defaults (clientauth, delete_default) reach the ssl layers that follow  OK")

    # every connection uses a gtlssh login: nothing comes in enabled (and nothing is open)
    text = r"""%YAML 1.1
---
defines:
  hello: Port \N on \d\r\n
connections:
  bench1:
    accepter: telnet(rfc2217),mux,certauth(),ssl,tcp,4401
    connector: serialdev,/dev/ttyACM0,19200n81
    options:
      banner: =(hello)
  bench2:
    accepter: telnet(rfc2217),mux,certauth(),ssl,tcp,4402
    connector: serialdev,/dev/ttyACM1,115200n81
"""
    maps, warns = parse(text)
    assert list(maps) == ["bench1", "bench2"] and not any(m["enabled"] for m in maps.values())
    assert not has(warns, "TCP wrappers")
    conf = "\n".join(["TRACEFILE:t1:/var/log/t1",
                      "3001:raw:0:/dev/ttyS1:9600 tr=t1",
                      "3002:rawlp:0:/dev/ttyS2:"])
    maps, warns = parse(conf, "ser2net.conf")
    assert maps["ser2net-3001"]["enabled"] and not maps["ser2net-3001"]["options"].get("trace_both")
    assert has(warns, "ser2net-3001", "tr is not imported")
    assert maps["ser2net-3002"]["enabled"] is False
    assert has(warns, "ser2net-3002", "rawlp", "write-only")
    print("a gtlssh-only file comes in all disabled; 3.x tr= only and rawlp handled alike  OK")


CONNECTORS = r"""
connection: &a
  accepter: tcp,3001
  connector: serialdev,/dev/ttyUSB0,115200e71,rtscts,nobreak,hangup-when-done
connection: &b
  accepter: tcp,3002
  connector: sdev,/dev/ttyUSB1, 9600n82 xonxoff dtr=off rts=on
connection: &c
  accepter: tcp,3003
  connector: serialdev(uucplock=false),/dev/ttyUSB2,19200,rs485=10:20
connection: &d
  accepter: tcp,3004
  connector: serialdev,//./COM3,9600n81
connection: &e
  accepter: tcp,3005
  connector: serialdev,/dev/ttyUSB5,9600n81,local=false,xonxoff,rtscts,drain_time=50
connection: &net
  accepter: tcp,3006
  connector: tcp,remotehost,4000
connection: &remote2217
  accepter: tcp,3007
  connector: telnet(rfc2217),tcp,serverhost,3000
connection: &sol
  accepter: telnet,tcp,3008
  connector: ipmisol,lan -U admin -P secret 192.168.27.37,115200
"""


def test_connectors():
    maps, warns = parse(CONNECTORS)
    a, b = maps["a"]["serial"], maps["b"]["serial"]
    assert (a["baudrate"], a["parity"], a["bytesize"], a["stopbits"], a["flowcontrol"]) == \
        (115200, "E", 7, 1, "rtscts"), a
    assert a["advanced"] == {"nobreak": True, "hangup_when_done": True}, a
    assert (b["port"], b["stopbits"], b["flowcontrol"]) == ("/dev/ttyUSB1", 2, "xonxoff"), b
    assert (b["dtr_on_open"], b["rts_on_open"]) == ("off", "on"), b
    assert maps["c"]["serial"]["baudrate"] == 19200 and has(warns, "c:", "rs485=10:20")
    assert maps["d"]["serial"]["port"] == "COM3"
    assert maps["e"]["serial"]["flowcontrol"] == "rtscts" and has(warns, "e:", "only RTS/CTS")
    assert has(warns, "e:", "local=false") and has(warns, "e:", "not imported", "drain_time=50")
    for name in ("net", "remote2217", "sol"):
        assert name not in maps and has(warns, f"{name}:", "skipped", "not a local serial port")
    print("connectors: serial parameters, flow control, dtr/rts, nobreak/hangup, COM ports; "
          "network and IPMI connectors skipped  OK")


# gensio's serialdev (lib/sergensio_serialdev.c, process_defserial_parm) still takes the
# framing as words: nSTOPBIT(S) / nDATABITS with strcasecmp, the parity words through
# lookup_parity_str, a strcmp against NONE/EVEN/ODD/MARK/SPACE (capitals only); dtr= and
# rts= are read with gensio_pparm_bool, so true/false/1/0 work as well as on/off
SERIAL_WORDS = r"""
connection: &rtu
  accepter: tcp,3101
  connector: serialdev,/dev/ttyUSB0,19200 EVEN 8DATABITS 1STOPBIT
connection: &plain
  accepter: tcp,3102
  connector: serialdev,/dev/ttyUSB1,9600 NONE 1STOPBIT 8DATABITS
connection: &e72
  accepter: tcp,3103
  connector: serialdev,/dev/ttyUSB2,9600,EVEN,7databits,2stopbits,XONXOFF
connection: &lines
  accepter: tcp,3104
  connector: serialdev,/dev/ttyUSB3,9600n81,dtr=false,rts=1
connection: &lower
  accepter: tcp,3105
  connector: serialdev,/dev/ttyUSB4,9600,even,dtr=maybe,-rts
"""


def test_serial_words():
    maps, warns = parse(SERIAL_WORDS)

    def frame(name):
        s = maps[name]["serial"]
        return s["baudrate"], s["parity"], s["bytesize"], s["stopbits"], s["flowcontrol"]
    assert frame("rtu") == (19200, "E", 8, 1, "none")       # Modbus RTU 8E1, not 8N1
    assert frame("plain") == (9600, "N", 8, 1, "none")
    assert frame("e72") == (9600, "E", 7, 2, "xonxoff")
    for name in ("rtu", "plain", "e72", "lines"):
        assert not has(warns, f"{name}:"), [w for w in warns if w.startswith(name)]
    lines = maps["lines"]["serial"]
    assert (lines["dtr_on_open"], lines["rts_on_open"]) == ("off", "on"), lines
    low = maps["lower"]["serial"]
    assert low["parity"] == "N" and has(warns, "lower:", "'even'", "capitals only")
    assert "dtr_on_open" not in low and has(warns, "lower:", "dtr=maybe", "not on/off")
    assert low["rts_on_open"] == "off"
    assert not has(warns, "lower: not imported")
    print("serial words: 19200 EVEN 8DATABITS 1STOPBIT -> 19200E81 without a warning, "
          "7databits/2stopbits in any case, dtr/rts true/false/1/0  OK")


OPTIONS = r"""
led: &led1
  driver: sysfs
  options:
    device: "tpacpi::standby"

connection: &opts
  accepter: telnet,tcp,4001
  timeout: 600
  enable: off
  connector: serialdev,/dev/ttyACM0,57600n81
  options:
    kickolduser: true
    max-connections: 3
    banner: 'Hi \I, this is \N on \o:\p\t\x41 at \H:\i\r\n'
    openstr: 'AT\r\n'
    closestr: "ATH\r"
    closeon: 'ctrl-a\001nil\000'
    trace-both: '/var/log/trace-\p-\N'
    trace-hexdump: true
    trace-timestamp: false
    remaddr: 192.168.33.0/24,0;10.0.0.5,0
    no-acc-to-con: true
    telnet-brk-on-sync: true
    chardelay: false
    chardelay-scale: 25
    mdns: true
    mdns-name: rack-3
    led-tx: *led1
    authdir: /usr/share/ser2net/auth

connection: &locked
  accepter: tcp,4002
  connector: serialdev,/dev/ttyACM1
  options:
    remaddr: otherserver,0
"""


def test_options():
    maps, warns = parse(OPTIONS)
    d = maps["opts"]
    o, n = d["options"], d["network"]
    assert d["enabled"] is False
    assert (n["kick_old_user"], n["max_connections"], n["read_only"]) == (True, 3, True), n
    assert n["allowed_client_ips"] == ["192.168.33.0/24", "10.0.0.5"], n
    assert o["idle_timeout_s"] == 600
    assert o["banner"] == "Hi \\I, this is \\N on \\d:\\p\tA at :\\r\\n", repr(o["banner"])
    assert has(warns, "opts:", "banner:", r"\H \i", "left out")
    assert (o["openstr"], o["closestr"], o["closeon"]) == ("AT\r\n", "ATH\r", "ctrl-a\x01nil\x00")
    assert o["trace_both"] == "/var/log/trace-4001-opts" and o["trace_hexdump"] is True
    assert o["trace_timestamp"] is False
    assert MappingConfig.from_dict(d).options.trace_both == "trace-4001-opts"
    assert has(warns, "opts:", "traces/<mapping id>-trace-4001-opts")
    drop = next(w for w in warns if w.startswith("opts: not imported"))
    listed = [k.strip() for k in drop.split(":", 2)[2].split(",")]
    assert listed == ["telnet-brk-on-sync", "chardelay-scale", "mdns", "mdns-name", "led-tx",
                      "authdir"], listed  # chardelay: false is what happens here anyway
    assert not has(warns, "led-tx is not a single value")  # *led1 is the LED's name
    locked = maps["locked"]
    assert locked["enabled"] is False
    assert has(warns, "locked:", "imported disabled rather than open")
    print("options: kickolduser, max-connections, banner/openstr/closestr/closeon escapes, "
          "trace-both, timeout, enable: off, remaddr, read-only; the rest warned once  OK")


# ser2net (ser2net_str.c, process_str) sends the byte an escape like \377 or \xff stands
# for; a string here is sent as UTF-8 (bridge: .encode("utf-8")), so such a byte would go
# out as two. A YAML "\xff" is a character to libyaml, which hands ser2net its UTF-8 too.
HIGH_BYTES = r"""
connection: &init
  accepter: tcp,5201
  connector: serialdev,/dev/ttyS1
  options:
    openstr: '\377\r'
connection: &close
  accepter: tcp,5202
  connector: serialdev,/dev/ttyS2
  options:
    closeon: '\xff\xfeEND'
    banner: 'caf\xe9 \001'
connection: &yaml
  accepter: tcp,5203
  connector: serialdev,/dev/ttyS3
  options:
    closestr: "\xff"
    closeon: 'ctrl-a\001nil\000'
"""


def test_high_bytes():
    from app.engine.bridge import _render_banner
    maps, warns = parse(HIGH_BYTES)
    init, close, yaml = maps["init"], maps["close"], maps["yaml"]
    # what the bridge would send: \377 as c3 bf, not ff: the device gets other bytes
    assert init["options"]["openstr"].encode("utf-8") == b"\xc3\xbf\r"
    assert init["enabled"] is False
    assert has(warns, "init:", "openstr", "\\377", "0x80", "imported disabled")
    assert close["enabled"] is True
    assert close["options"]["closeon"].encode("utf-8") != b"\xff\xfeEND"
    assert has(warns, "close:", "closeon", "\\xff \\xfe", "won't match")
    assert has(warns, "close:", "banner", "\\xe9")
    assert not has(warns, "close:", "\\001")  # below 0x80: the byte itself
    banner = _render_banner(close["options"]["banner"], MappingConfig.from_dict(close),
                            "/dev/ttyS2", "10.0.0.9")
    assert banner == "caf\xe9 \x01".encode("utf-8"), banner
    assert yaml["enabled"] is True and not has(warns, "yaml:", "0x80"), warns
    assert yaml["options"]["closeon"].encode("utf-8") == b"ctrl-a\x01nil\x00"
    conf = "\n".join(["OPENSTR:o1:ATZ\\r\\x80", "3001:raw:0:/dev/ttyS1:o1"])
    maps, warns = parse(conf, "ser2net.conf")
    assert maps["ser2net-3001"]["enabled"] is False and has(warns, "\\x80", "0x80")
    print("escapes for bytes 0x80-0xFF: an open/close string comes in disabled, a close-on "
          "string or banner with a note; YAML escapes and bytes below 0x80 as they are  OK")


REMADDR = r"""
default:
  name: remaddr
  value: 10.0.0.0/8
  class: ser2net
connection: &own
  accepter: tcp,7101
  connector: serialdev,/dev/ttyS1
  options:
    remaddr: 192.168.1.5
connection: &inherits
  accepter: tcp,7102
  connector: serialdev,/dev/ttyS2
connection: &empty
  accepter: tcp,7103
  connector: serialdev,/dev/ttyS3
  options:
    remaddr: ""
connection: &hostname
  accepter: tcp,7104
  connector: serialdev,/dev/ttyS4
  options:
    remaddr: workstation7,0
connection: &connectback
  accepter: tcp,7105
  connector: serialdev,/dev/ttyS5
  options:
    remaddr: "!192.168.1.9,3000"
connection: &twice
  accepter: tcp,7106
  connector: serialdev,/dev/ttyS6
  options:
    remaddr: 192.168.1.5
    remaddr: 192.168.1.6;192.168.1.7,2000
delete_default:
  name: remaddr
  class: ser2net
connection: &nodefault
  accepter: tcp,7107
  connector: serialdev,/dev/ttyS7
  options:
    remaddr: ""
"""


def test_remaddr_default():
    """ser2net uses the default remaddr only for a connection that gives no address of its
    own; it never adds the two up (4.x: an empty remaddr gives none, 3.x: remaddr= counts)."""
    maps, warns = parse(REMADDR)
    acl = {n: m["network"].get("allowed_client_ips") for n, m in maps.items()}
    assert acl["own"] == ["192.168.1.5"], acl
    assert acl["inherits"] == ["10.0.0.0/8"] and has(warns, "inherits:", "from the defaults")
    assert acl["empty"] == ["10.0.0.0/8"], acl  # 4.2+: an empty remaddr keeps the default
    assert acl["twice"] == ["192.168.1.5", "192.168.1.6", "192.168.1.7"], acl
    assert has(warns, "twice:", "192.168.1.7,2000", "not its port")
    # own addresses that can't be imported: disabled, not opened to the default's range
    for name in ("hostname", "connectback"):
        assert not acl[name] and maps[name]["enabled"] is False, (name, acl[name])
        assert has(warns, f"{name}:", "imported disabled rather than open")
        assert not has(warns, f"{name}:", "from the defaults")
    assert not acl["nodefault"] and maps["nodefault"]["enabled"] is True  # no limit, as there
    assert not has(warns, "own:", "from the defaults") and not has(warns, "nodefault:", "remaddr")
    conf = "\n".join(["DEFAULT:remaddr:10.1.1.1",
                      "7201:raw:0:/dev/ttyS1:remaddr=10.2.2.2",
                      "7202:raw:0:/dev/ttyS2:9600",
                      "7203:raw:0:/dev/ttyS3:remaddr=hostx",
                      "7204:raw:0:/dev/ttyS4:remaddr="])
    maps, warns = parse(conf, "ser2net.conf")
    acl = {n: m["network"].get("allowed_client_ips") for n, m in maps.items()}
    assert acl["ser2net-7201"] == ["10.2.2.2"], acl
    assert acl["ser2net-7202"] == ["10.1.1.1"] and has(warns, "ser2net-7202", "from the defaults")
    assert not acl["ser2net-7203"] and maps["ser2net-7203"]["enabled"] is False
    assert not acl["ser2net-7204"] and maps["ser2net-7204"]["enabled"] is True  # remaddr= set
    print("remaddr: a connection's own addresses replace the default (4.x and 3.x); unusable "
          "own addresses hold it disabled instead of falling back to the default  OK")


SUBSTITUTION = r"""
%YAML 1.1
---
define: &dev /dev/ttyUSB7
define: &host "192.168.10.1"
define: &greet Hello from *(dev)
connection: &c1
  accepter: tcp,*(host),3100
  connector: serialdev,*(dev),9600n81
  options:
    banner: *greet
    openstr: '$(dev) is open'
    closestr: 'two **stars and *(* paren'
    closeon: 'x*(nope)y*{/etc/ser2net/pw}z'
connection: &c2
  accepter: tcp,3101
  connector: serialdev,/dev/ttyUSB8
  options:
    banner: 'see *(c1)'
"""

VERSION2 = r"""
%YAML 1.1
---
defines:
  banner1: Connected to port \N(\d)\r\n
  base: /dev/ttyUSB

defaults:
  speed:
    value: 38400n81
    class: serialdev
  kickolduser:
    value: true

connections:
  ttyUSB0:
    accepter: telnet(rfc2217),tcp,4001
    connector: serialdev,=(base)0
    options:
      banner: =(banner1)
  ttyUSB1:
    accepter: tcp,4002
    connector: serialdev,=(base)1,115200n81
---
connections:
  second:
    accepter: tcp,4003
    connector: serialdev,/dev/ttyS3
rotators:
  rot1:
    accepter: tcp,4010
    connections: [
      ttyUSB0,
      ttyUSB1
    ]
"""


def test_substitution():
    maps, warns = parse(SUBSTITUTION)
    c1 = maps["c1"]
    assert c1["serial"]["port"] == "/dev/ttyUSB7" and c1["network"]["bind_ip"] == "192.168.10.1"
    o = c1["options"]
    assert o["banner"] == "Hello from /dev/ttyUSB7", o
    assert o["openstr"] == "/dev/ttyUSB7 is open" and o["closestr"] == "two *stars and *( paren"
    assert o["closeon"] == "x*(nope)yz", o
    assert has(warns, "c1:", "unknown reference *(nope)")
    assert has(warns, "c1:", "*{/etc/ser2net/pw}")
    assert maps["c2"]["options"]["banner"] == "see c1"  # *(connection) is its name, as in ser2net
    print("version 1: *alias, *(name), $(name), ** and *(* escapes, *{file} left out  OK")

    maps, warns = parse(VERSION2)
    assert list(maps) == ["ttyUSB0", "ttyUSB1", "second"], list(maps)
    u0, u1 = maps["ttyUSB0"], maps["ttyUSB1"]
    assert u0["serial"]["port"] == "/dev/ttyUSB0" and u0["serial"]["baudrate"] == 38400
    assert u0["network"]["protocol"] == "rfc2217" and u0["network"]["kick_old_user"] is True
    assert u0["options"]["banner"] == r"Connected to port \N(\d)\r\n"
    assert u1["serial"]["baudrate"] == 115200 and maps["second"]["serial"]["baudrate"] == 38400
    assert has(warns, "rotator rot1", "not imported")
    print("version 2: defines/=(name), defaults, connections map, two documents, rotator  OK")


# ser2net.yaml(5): "The order in the file is important, you must set a default before it
# is used" - ser2net sets each connection up as it reads it, so a default reaches only the
# connections after it; delete_default needs the class whose value it deletes
DEFAULTS = r"""
connection: &before
  accepter: tcp,5001
  connector: serialdev,/dev/ttyS1
default: { name: speed, value: 115200 }
default:
  name: kickolduser
  value: true
  class: ser2net
default:
  name: mdns
  value: true
default:
  name: speed
  value: 4800
  class: ipmisol
connection: &after
  accepter: tcp,5002
  connector: serialdev,/dev/ttyS2
delete_default:
  name: kickolduser
  class: ser2net
connection: &last
  accepter: tcp,5003
  connector: serialdev,/dev/ttyS3,9600
"""


def test_defaults():
    maps, warns = parse(DEFAULTS)
    assert maps["before"]["serial"]["baudrate"] == 9600
    assert not maps["before"]["network"].get("kick_old_user")
    assert maps["after"]["serial"]["baudrate"] == 115200
    assert maps["after"]["network"]["kick_old_user"] is True
    assert maps["last"]["serial"]["baudrate"] == 9600
    assert not maps["last"]["network"].get("kick_old_user")
    assert has(warns, "default mdns", "not imported") and not has(warns, "ipmisol")
    assert not has(warns, "delete_default")
    print("default: (flow and block form) applies to later connections; delete_default; "
          "unsupported defaults warned  OK")


# gensio (lib/gensio.c gensio_get_default): a class's own value comes before the base value
# whatever the order; gensio_del_default with a class deletes only that class's value
# (GE_NOTFOUND when there is none, and ser2net's yamlconf.c then refuses the file; it also
# refuses a delete_default without a class). The telnet gensio reads its rfc2217 default
# (a built-in default, class telnet) and ser2net its banner/openstr/closestr/closeon
# defaults (defaults.c, read in portconfig.c init_port_data) as each connection is set up.
DEFAULT_CLASSES = r"""
connection: &plain0
  accepter: telnet,tcp,5101
  connector: serialdev,/dev/ttyS1
default:
  name: rfc2217
  value: true
connection: &rfc
  accepter: telnet,tcp,5102
  connector: serialdev,/dev/ttyS2
connection: &explicit
  accepter: telnet(rfc2217=false),tcp,5103
  connector: serialdev,/dev/ttyS3
connection: &rawtcp
  accepter: tcp,5104
  connector: serialdev,/dev/ttyS4
default:
  name: rfc2217
  value: false
  class: telnet
connection: &classwins
  accepter: telnet,tcp,5105
  connector: serialdev,/dev/ttyS5
default:
  name: rfc2217
  value: true
connection: &stillclass
  accepter: telnet,tcp,5106
  connector: serialdev,/dev/ttyS6
default:
  name: speed
  value: 19200
  class: serialdev
default:
  name: speed
  value: 4800
connection: &speedclass
  accepter: tcp,5107
  connector: serialdev,/dev/ttyS7
delete_default:
  name: rfc2217
  class: telnet
delete_default:
  name: speed
  class: serialdev
connection: &baseagain
  accepter: telnet,tcp,5108
  connector: serialdev,/dev/ttyS8
default:
  name: banner
  value: 'Port \N\r\n'
  class: ser2net
default:
  name: openstr
  value: 'AT\r'
default:
  name: closeon
  value: "NO CARRIER\r\n"
default:
  name: signature
  value: "line1\r\nline2"
connection: &strings
  accepter: tcp,5109
  connector: serialdev,/dev/ttyS9
  options:
    openstr: 'ATZ\r'
delete_default:
  name: kickolduser
  class: ser2net
delete_default:
  name: openstr
"""


def test_default_classes():
    maps, warns = parse(DEFAULT_CLASSES)
    proto = {n: m["network"]["protocol"] for n, m in maps.items()}
    assert proto == {"plain0": "telnet", "rfc": "rfc2217", "explicit": "telnet",
                     "rawtcp": "raw", "classwins": "telnet", "stillclass": "telnet",
                     "speedclass": "raw", "baseagain": "rfc2217", "strings": "raw"}, proto
    assert not has(warns, "rfc2217"), warns  # the telnet default is imported, not warned
    assert maps["speedclass"]["serial"]["baudrate"] == 19200   # the class value wins
    assert maps["baseagain"]["serial"]["baudrate"] == 4800     # deleted: the base value
    o = maps["strings"]["options"]
    assert (o["banner"], o["openstr"], o["closeon"]) == ("Port \\N\\r\\n", "ATZ\r",
                                                         "NO CARRIER\r\n"), o
    assert "banner" not in maps["baseagain"]["options"]       # only after the default
    assert not any(w.startswith(("default banner", "default openstr")) for w in warns)
    sig = next(w for w in warns if w.startswith("default signature"))
    assert "line1\\r\\nline2" in sig and "\r" not in sig and "\n" not in sig, repr(sig)
    assert has(warns, "delete_default kickolduser", "class ser2net", "refuses")
    assert has(warns, "delete_default openstr", "no class", "refuses")
    print("defaults: the telnet rfc2217 default (base and class), class values before base "
          "values, banner/openstr/closeon defaults, delete_default per class  OK")


SYNTAX = r"""%YAML 1.1
---
# comments everywhere
define: &b1 "Line\r\nTab\there # not a comment"   # a comment
connection: &q
  accepter: tcp,6001     # trailing comment
  connector: "serialdev,/dev/ttyS6,9600n81"
  options:
    banner: *b1
    openstr: 'it''s C# time'
    closestr: Plain C#sharp with: colon
connection: &lit
  accepter: tcp,6002
  connector: >-
    serialdev,/dev/ttyS7,
    19200n81
  options:
    banner: |
      first \d
      second
    closeon: "unicode \u00e9 \x41"
rotator: &r
  accepter: tcp,6010
  connections: [
    *q, *lit  # aliases to connections
  ]
...
---
connection: &doc2
	accepter: tcp,6003
	connector: serialdev,/dev/ttyS8
"""


def test_yaml_syntax():
    maps, warns = parse(SYNTAX)
    q = maps["q"]
    assert q["serial"]["port"] == "/dev/ttyS6" and q["network"]["port"] == 6001
    assert q["options"]["banner"] == "Line\\r\\nTab\there # not a comment", q["options"]
    assert q["options"]["openstr"] == "it's C# time"
    assert q["options"]["closestr"] == "Plain C#sharp with: colon"
    lit = maps["lit"]
    assert lit["serial"]["port"] == "/dev/ttyS7" and lit["serial"]["baudrate"] == 19200
    assert lit["options"]["banner"] == "first \\d\\nsecond\\n", lit["options"]
    assert lit["options"]["closeon"] == "unicode \u00e9 A"
    assert maps["doc2"]["serial"]["port"] == "/dev/ttyS8"  # tab indentation, 2nd document
    assert has(warns, "rotator r", "not imported")
    print("YAML subset: quotes, comments, folded/literal blocks, flow lists of aliases, "
          "documents, tab indentation  OK")


CONF3 = "\n".join([
    "# ser2net.conf (3.x)",
    "BANNER:b1:Welcome to \\p on \\d [\\s] \\N\\r\\n",
    "BANNER:b2:line one\\r\\n\\",
    "line two\\r\\n",
    "TRACEFILE:t1:/var/log/s2n/\\p-\\Y\\M\\D.log",
    "OPENSTR:o1:AT\\r",
    "CLOSESTR:c1:ATH\\r",
    "CLOSEON:co1:\\004",
    "DEVICE:usb:/dev/serial/by-id/usb-FTDI_1:2-if00",
    "CONTROLPORT:localhost,2000",
    "LED:rx:sysfs:device=input3::capslock",
    "SIGNATURE:sig1:ser2net port 1",
    "",
    "2000:raw:600:/dev/ttyS0:9600 NONE 1STOPBIT 8DATABITS XONXOFF LOCAL -RTSCTS b1",
    "127.0.0.1,2001:telnet:0:/dev/ttyS1:115200 EVEN 7DATABITS 2STOPBITS RTSCTS remctl \\",
    "    kickolduser max-connections=2",
    "DEFAULT:speed:19200",
    "ipv4,udp,2002:raw:30:/dev/ttyS2:4800 tb=t1 hexdump",
    "2003:rawlp:0:/dev/lp0:",
    "2004:off:0:usb:b2 o1 c1 co1 HANGUP_WHEN_DONE NOBREAK max-connections=3",
    "2005:telnet:0:/dev/ttyS5:chardelay-scale=15 telnet_brk_on_sync led-tx=rx sig1 bogus odd",
    "2006:raw:0:sol.lan -U admin -P x host:115200",
    "2007:raw:0:/dev/ttyS0:9600 rs485=conf1 remaddr=ipv4,10.1.2.3,0",
    "2000:telnet:0:/dev/ttyS9:9600",
    "this is not a ser2net line",
])


def test_v3():
    maps, warns = parse(CONF3, "ser2net.conf")
    assert list(maps) == ["ser2net-2000", "ser2net-2001", "ser2net-udp-2002", "ser2net-2003",
                          "ser2net-2004", "ser2net-2005", "ser2net-2007"], list(maps)
    m0 = maps["ser2net-2000"]
    assert m0["serial"]["flowcontrol"] == "xonxoff" and m0["options"]["idle_timeout_s"] == 600
    assert m0["options"]["banner"] == "Welcome to \\p on \\d [9600N81] N\\r\\n", m0["options"]
    m1 = maps["ser2net-2001"]  # continued line
    assert m1["network"]["bind_ip"] == "127.0.0.1" and m1["network"]["protocol"] == "rfc2217"
    assert m1["network"]["max_connections"] == 1 and m1["network"]["kick_old_user"] is True
    assert (m1["serial"]["parity"], m1["serial"]["bytesize"], m1["serial"]["stopbits"],
            m1["serial"]["flowcontrol"]) == ("E", 7, 2, "rtscts")
    u = maps["ser2net-udp-2002"]
    assert u["network"]["mode"] == "udp" and u["serial"]["baudrate"] == 4800
    assert u["options"]["trace_both"] == "/var/log/s2n/2002.log" and u["options"]["trace_hexdump"]
    assert has(warns, "ser2net-udp-2002", r"\Y \M \D", "file name")
    assert maps["ser2net-2003"]["serial"]["baudrate"] == 19200  # DEFAULT:speed from here on
    assert maps["ser2net-2003"]["enabled"] is False and has(warns, "ser2net-2003", "rawlp")
    off = maps["ser2net-2004"]
    assert off["enabled"] is False
    assert off["serial"]["port"] == "/dev/serial/by-id/usb-FTDI_1:2-if00"  # DEVICE: alias
    assert off["options"]["banner"] == "line one\\r\\nline two\\r\\n"
    assert (off["options"]["openstr"], off["options"]["closestr"], off["options"]["closeon"]) == \
        ("AT\r", "ATH\r", "\x04")
    assert off["serial"]["advanced"] == {"hangup_when_done": True, "nobreak": True}
    assert off["network"]["max_connections"] == 3
    t5 = maps["ser2net-2005"]
    assert t5["network"]["protocol"] == "telnet" and t5["serial"]["parity"] == "O"
    assert has(warns, "ser2net-2005", "unknown option 'bogus'")
    assert has(warns, "ser2net-2005", "not imported", "chardelay-scale", "telnet-brk-on-sync")
    assert has(warns, "ser2net-2006", "IPMI") and "ser2net-2006" not in maps
    m7 = maps["ser2net-2007"]
    assert m7["network"]["allowed_client_ips"] == ["10.1.2.3"]
    assert has(warns, "ser2net-2007", "rs485")
    assert m7["enabled"] is False and has(warns, "ser2net-2007", "/dev/ttyS0", "ser2net-2000")
    assert has(warns, "line 24", "TCP port 2000 is already used")
    for what in ("CONTROLPORT", "LED rx", "SIGNATURE sig1"):
        assert has(warns, what, "not imported"), what
    assert has(warns, "line 25", "not a ser2net.conf line")
    print("ser2net.conf (3.x): every state, BANNER/TRACEFILE/OPENSTR/CLOSESTR/CLOSEON/DEVICE/"
          "DEFAULT, continuation; CONTROLPORT/LED/SIGNATURE warned  OK")


def test_names_and_clashes():
    text = r"""
connection: &dup
  accepter: tcp,7001
  connector: serialdev,/dev/ttyS1
connection: &dup
  accepter: tcp,7002
  connector: serialdev,/dev/ttyS2
connection: &sameport
  accepter: tcp,127.0.0.1,7001
  connector: serialdev,/dev/ttyS3
connection: &udpsame
  accepter: udp,7001
  connector: serialdev,/dev/ttyS4
"""
    maps, warns = parse(text)
    assert list(maps) == ["dup", "dup (2)", "udpsame"], list(maps)
    assert has(warns, "renamed to 'dup (2)'")
    assert has(warns, "sameport: skipped", "TCP port 7001", "'dup'")
    print("duplicate names get (2); a port already listened on is skipped (UDP may share)  OK")


def test_errors():
    for bad in ("hello world\nthis is not a config\n", '{"mappings": []}', "", "  \n\n",
                "[general]\nport = 2000\n"):
        try:
            parse_ser2net_config(bad, "x.conf")
        except ConfigError as e:
            assert str(e), bad
        else:
            raise AssertionError(f"accepted: {bad!r}")
    for bad, want in (("---\nservices:\n  web:\n    image: nginx\n", "not a ser2net"),
                      ("connection: &a\n  accepter: tcp,1\n connector: x\n", "line 3"),
                      ('connection: &a\n  accepter: "tcp,1\n', "unterminated"),
                      ("connection: &a\n  options: *missing\n", "unknown alias")):
        try:
            parse_ser2net_config(bad, "ser2net.yaml")
        except ConfigError as e:
            assert want in str(e) and "ser2net.yaml" in str(e), (want, str(e))
        else:
            raise AssertionError(f"accepted: {bad!r}")
    maps, warns = parse_ser2net_config("%YAML 1.1\n---\n# all commented out\n"
                                       "define: &confver 1.0\n# connection: &con0\n")
    assert maps == [] and has(warns, "no ser2net connections"), warns
    print("garbage, JSON, empty, non-ser2net and broken YAML -> ConfigError; an all-comments "
          "file -> no mappings + a warning  OK")


def refused(text, want, filename="ser2net.yaml"):
    """The file is refused with a ConfigError naming the limit, quickly."""
    t = time.monotonic()
    try:
        parse_ser2net_config(text, filename)
    except ConfigError as e:
        assert want in str(e), (want, str(e)[:300])
    else:
        raise AssertionError(f"accepted ({want})")
    assert time.monotonic() - t < 10, f"took {time.monotonic() - t:.1f} s ({want})"


def quick(text, filename="ser2net.yaml"):
    t = time.monotonic()
    maps, warns = parse(text, filename)
    assert time.monotonic() - t < 10, f"took {time.monotonic() - t:.1f} s"
    return maps, warns


HEAD = "connection: &c\n  accepter: tcp,4001\n  connector: serialdev,/dev/ttyS0\n  options:\n"


def test_limits():
    """Files whose aliases, defines or escapes would expand without bound, or that are
    huge or deeply nested, are refused at once; long but sane input is read in one pass."""
    flow = [HEAD + "    a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 8):  # each line repeats the previous one ten times
        flow.append(f"    a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 10) + "]")
    refused("\n".join(flow) + "\n", "YAML nodes")
    block = ["connections:", "  c0:", "    accepter: tcp,4001",
             "    connector: serialdev,/dev/ttyS0", "    options: &o0"]
    block += ["      kickolduser: true"] * 10
    for i in range(1, 8):
        block += [f"  c{i}:", f"    accepter: tcp,{4001 + i}",
                  "    connector: serialdev,/dev/ttyS0", f"    x: &o{i}"]
        block += [f"      - *o{i - 1}"] * 10
    refused("\n".join(block) + "\n", "YAML nodes")
    chain = ["define: &d0 xxxxxxxxxx"]
    chain += [f"define: &d{i} " + f"*(d{i - 1})" * 10 for i in range(1, 8)]
    refused("\n".join(chain) + "\n" + HEAD + "    banner: '*(d7)'\n", "longer than 64 KB")
    refused(HEAD.replace("serialdev,/dev/ttyS0", "serialdev,/dev/" + "y" * 3000)
            + "    openstr: '" + "\\d" * 1000 + "'\n", "c: openstr: the value is longer")
    conf = "BANNER:b1:" + "x" * 60000 + "\n" + "".join(f"{i}:raw:0:/dev/ttyS{i}:b1\n"
                                                      for i in range(1, 400))
    refused(conf, "more than 16 MB", "ser2net.conf")
    refused("BANNER:b1:" + "x" * 70000 + "\n", "line 1 is longer than 64 KB", "ser2net.conf")
    refused(HEAD + "    x: " + "[" * 100 + "]" * 100 + "\n", "nested more than 64 levels")
    refused("connection: &c\n" + "".join(" " * (i + 1) + "k:\n" for i in range(100)), "nested")
    refused(HEAD + "    x: {[a]: b}\n", "cannot be a key")
    refused("# " + "x" * (1024 * 1024) + "\n" + HEAD, "larger than 1 MB")
    refused("".join(f"connection: &c{i}\n  accepter: tcp,{1 + i}\n"
                    f"  connector: serialdev,/dev/ttyS{i}\n" for i in range(1001)),
            "more than 1000 connections")

    maps, warns = quick(HEAD + "    banner: [\n" + "".join("      a,\n" for _ in range(8000))
                        + "      b ]\n    kickolduser: true\n")
    assert maps["c"]["network"]["kick_old_user"] is True and has(warns, "banner is not a single")
    refused(HEAD + "    x: [\n" + "".join("      a,\n" for _ in range(8000)), "unterminated [")
    maps, _w = quick(HEAD + "    banner: \"x\n" + "".join("      ab\n" for _ in range(20000))
                     + "      end\"\n")
    assert maps["c"]["options"]["banner"] == "x" + " ab" * 20000 + " end"
    maps, warns = quick(HEAD + "".join(f"    unknown{i}: 1\n" for i in range(30000)))
    assert sum("not imported (no equivalent here)" in w for w in warns) == 1
    maps, _w = quick(HEAD + "    banner: '" + "$(" * 30000 + "'\n")
    assert maps["c"]["options"]["banner"] == "$(" * 30000
    maps, warns = quick(HEAD + "".join(f"    max-connections: x{i}\n" for i in range(5000)))
    assert len(warns) == 1002 and warns[1000] == "… and 4000 more warnings", warns[999:]
    assert has(warns[1001:], "TCP wrappers")  # the file-level notes come after the cut
    print("limits: alias / define / escape expansion, size, nesting and connection count "
          "refused quickly; long flows, quoted scalars, many options read in one pass  OK")


def main():
    test_debian_default()
    test_accepters()
    test_protections()
    test_connectors()
    test_serial_words()
    test_options()
    test_remaddr_default()
    test_high_bytes()
    test_substitution()
    test_defaults()
    test_default_classes()
    test_yaml_syntax()
    test_v3()
    test_names_and_clashes()
    test_errors()
    test_limits()
    print("\nPASS: ser2net.yaml / ser2net.conf import (every mapping validates)")


if __name__ == "__main__":
    main()
