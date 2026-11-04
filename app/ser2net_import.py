"""Import mappings from a classic ser2net configuration (the C program by Corey Minyard).

Users migrating from it bring /etc/ser2net/ser2net.yaml (ser2net 4.x) or
/etc/ser2net.conf (ser2net 3.x). Neither can be read with a stock parser: a 4.x
"version 1" file repeats top-level keys (one `connection:` per port) and substitutes
`*(name)` references inside strings, which is not standard YAML (and PyYAML is not a
dependency anyway); 3.x has its own colon-separated line format. So this module
carries a small reader for the YAML subset ser2net configs use plus a 3.x line
parser, and turns every ser2net connection into a dict for MappingConfig.from_dict.

What has no equivalent here (TLS/certauth, LEDs, rotators, mDNS, chardelay, ...) is
reported as a warning that names the connection; a connection that cannot be
represented at all (a network or IPMI connector, a unix-socket accepter) is skipped
with a warning, so every returned dict passes MappingConfig.validate(). ser2net only
opens a serial device while a client is connected, so several of its ports may share
one device; a mapping here keeps its device open, so later connections on the same
device are imported disabled, and a repeated listening port is skipped: the import
can then be applied as a whole.
"""
from __future__ import annotations

import copy
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import MAX_TIMER_S, ConfigError, MappingConfig, _ip_overlaps, trace_file_name


# ---------------------------------------------------------------------------
# A reader for the YAML subset ser2net configs use: block mappings/sequences by
# indentation, plain / 'single' / "double" quoted scalars (multi-line too), flow
# [..] and {..}, |/> block scalars, &anchors and *aliases, comments, %YAML and
# ---/... markers. Repeated keys are kept in order (ser2net reads them all).
# ---------------------------------------------------------------------------
class _YamlError(Exception):
    def __init__(self, line: int, msg: str) -> None:
        super().__init__(f"line {line}: {msg}")


@dataclass
class _YMap:
    entries: list[tuple[str, Any, str]] = field(default_factory=list)  # key, value, anchor

    def get(self, key: str, default: Any = None) -> Any:
        """The last value of `key` (case-insensitive, as ser2net matches keys)."""
        for k, v, _anchor in reversed(self.entries):
            if k.lower() == key:
                return v
        return default


_ALIAS_NAME = re.compile(r"[^\s,\[\]{}]+")
_DQ_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v",
               "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\",
               "N": "\x85", "_": "\xa0", "L": " ", "P": " "}


def _indent(line: str) -> tuple[int, str]:
    body = line.lstrip(" \t")
    return len(line[: len(line) - len(body)].expandtabs(8)), body


def _marker(body: str, mark: str) -> bool:
    return body == mark or body.startswith((mark + " ", mark + "\t"))


def _boundary(ind: int, body: str) -> bool:
    """A %directive or a ---/... document marker."""
    return ind == 0 and (body.startswith("%") or _marker(body, "---") or _marker(body, "..."))


def _is_item(body: str) -> bool:
    return body == "-" or body.startswith(("- ", "-\t"))


def _cut_comment(s: str) -> str:
    """A plain scalar ends where a comment starts (a # after whitespace)."""
    m = re.search(r"\s#", s)
    return (s[: m.start()] if m else s).rstrip()


def _key(body: str) -> Optional[tuple[str, str]]:
    """(key, text after the colon) for a 'key: value' line, else None."""
    if body[0] in "\"'":
        end = body.find(body[0], 1)
        rest = body[end + 1:].lstrip(" \t") if end > 0 else ""
        if rest.startswith(":") and (len(rest) == 1 or rest[1] in " \t"):
            return body[1:end], rest[1:]
        return None
    if body[0] in "[]{},&*!|>%@`#" or _is_item(body):
        return None
    m = re.search(r":(?:[ \t]|$)", body)
    if m is None:
        return None
    key = body[: m.start()].rstrip()
    if not key or re.search(r"\s#", key):
        return None
    return key, body[m.start() + 1:]


def _close_quote(seg: str, quote: str) -> int:
    """Index of the closing quote in seg, or -1 (the string goes on)."""
    j = 0
    while j < len(seg):
        c = seg[j]
        if quote == '"' and c == "\\":
            j += 2
            continue
        if c == quote:
            if quote == "'" and seg[j + 1:j + 2] == "'":
                j += 2
                continue
            return j
        j += 1
    return -1


def _dq_unescape(s: str) -> str:
    def one(m: re.Match[str]) -> str:
        e = m.group(1)
        if e in _DQ_ESCAPES:
            return _DQ_ESCAPES[e]
        if e[0] in "xuU" and len(e) > 1:
            return chr(int(e[1:], 16))
        return "\\" + e  # not a YAML escape: keep it, it may be a ser2net one (\p, \d)
    return re.sub(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|.)", one, s, flags=re.S)


def _fold_quoted(parts: list[str], quote: str) -> str:
    """Join the lines of a multi-line quoted scalar (a line break folds to a space,
    an empty line to a newline, a trailing backslash in "..." joins directly)."""
    text, blank = parts[0], 0
    for n, raw in enumerate(parts[1:], 1):
        last = n == len(parts) - 1
        seg = raw.lstrip(" \t") if last else raw.strip(" \t")
        if not seg and not last:
            blank += 1
            continue
        text = text.rstrip(" \t")
        trailing = len(text) - len(text.rstrip("\\"))
        if quote == '"' and trailing % 2 == 1:
            text, sep = text[:-1], ""
        else:
            sep = "\n" * blank if blank else " "
        text, blank = text + sep + seg, 0
    return _dq_unescape(text) if quote == '"' else text.replace("''", "'")


def _flow_end(text: str) -> int:
    """Index just past the bracket closing text[0] ([ or {), or -1 if not on these lines."""
    depth, j, quote = 0, 0, ""
    while j < len(text):
        c = text[j]
        if quote:
            if c == "\\" and quote == '"':
                j += 1
            elif c == quote:
                quote = ""
        elif c in "\"'" and (j == 0 or text[j - 1] in " \t\n,[{:"):
            quote = c
        elif c == "#" and (j == 0 or text[j - 1] in " \t\n"):
            nl = text.find("\n", j)
            if nl < 0:
                return -1
            j = nl
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return -1


class _Flow:
    """Parser for one flow collection ([a, *b] / {name: speed, value: 115200})."""

    def __init__(self, text: str, anchors: dict[str, Any]) -> None:
        self.s, self.j, self.anchors = text, 0, anchors

    def _ws(self) -> None:
        while self.j < len(self.s):
            c = self.s[self.j]
            if c in " \t\n":
                self.j += 1
            elif c == "#" and (self.j == 0 or self.s[self.j - 1] in " \t\n"):
                nl = self.s.find("\n", self.j)
                self.j = len(self.s) if nl < 0 else nl
            else:
                return

    def value(self) -> Any:
        self._ws()
        c = self.s[self.j:self.j + 1]
        if c in ("[", "{"):
            return self._collection(c)
        if c in ("'", '"'):
            end = _close_quote(self.s[self.j + 1:], c)
            if end < 0:
                raise ValueError("unterminated quoted string")
            raw = self.s[self.j + 1:self.j + 1 + end]
            self.j += end + 2
            return _fold_quoted(raw.split("\n"), c)
        if c == "*":
            m = _ALIAS_NAME.match(self.s, self.j + 1)
            name = m.group(0) if m else ""
            if name not in self.anchors:
                raise ValueError(f"unknown alias *{name}")
            self.j += 1 + len(name)
            return copy.deepcopy(self.anchors[name])
        start = self.j
        while self.j < len(self.s):
            ch = self.s[self.j]
            nxt = self.s[self.j + 1:self.j + 2]
            if ch in ",[]{}" or (ch == ":" and nxt in ("", " ", "\t", "\n", ",", "]", "}")):
                break
            if ch == "#" and self.s[self.j - 1:self.j] in (" ", "\t", "\n"):
                break
            self.j += 1
        return " ".join(ln.strip() for ln in self.s[start:self.j].split("\n") if ln.strip())

    def _collection(self, opening: str) -> Any:
        close = "]" if opening == "[" else "}"
        self.j += 1
        items: list[Any] = []
        mapping = _YMap()
        while True:
            self._ws()
            if self.s.startswith(close, self.j):
                self.j += 1
                return items if opening == "[" else mapping
            item = self.value()
            self._ws()
            if opening == "{":
                val: Any = ""
                if self.s.startswith(":", self.j):
                    self.j += 1
                    val = self.value()
                    self._ws()
                mapping.entries.append((str(item), val, ""))
            else:
                items.append(item)
            if self.s.startswith(",", self.j):
                self.j += 1
            elif not self.s.startswith(close, self.j):
                raise ValueError(f"expected ',' or '{close}'")


class _YamlReader:
    def __init__(self, text: str) -> None:
        self.lines = text.split("\n")
        self.i = 0     # next line to read
        self.last = 0  # last line consumed (for error messages)
        self.anchors: dict[str, Any] = {}

    def _err(self, msg: str, index: Optional[int] = None) -> _YamlError:
        return _YamlError((self.last if index is None else index) + 1, msg)

    def _take(self) -> str:
        self.last = self.i
        self.i += 1
        return self.lines[self.last]

    def _peek(self) -> Optional[tuple[int, str]]:
        """(indent, text) of the next significant line; None at a document end."""
        while self.i < len(self.lines):
            ind, body = _indent(self.lines[self.i])
            if body and not body.startswith("#"):
                return None if _boundary(ind, body) else (ind, body)
            self.i += 1
        return None

    def documents(self) -> list[Any]:
        docs: list[Any] = []
        while self.i < len(self.lines):
            ind, body = _indent(self.lines[self.i])
            if not body or body.startswith("#") or _boundary(ind, body):
                self.i += 1
                continue
            docs.append(self._node(-1))
            if self._peek() is not None:  # dedented below a nested block: not YAML
                raise self._err("unexpected indentation", self.i)
        return docs

    def _node(self, parent: int) -> Any:
        """The block node on the next lines, indented deeper than `parent`."""
        nxt = self._peek()
        if nxt is None or nxt[0] <= parent:
            return ""
        ind, body = nxt
        if _is_item(body):
            return self._seq(ind)
        if _key(body) is not None:
            return self._map(ind, root=parent < 0)
        self._take()
        return self._value(body, parent)[0]

    def _map(self, ind: int, root: bool = False) -> _YMap:
        m = _YMap()
        while True:
            nxt = self._peek()
            if nxt is None or nxt[0] < ind:
                return m
            kv = _key(nxt[1]) if nxt[0] == ind else None
            if kv is None:
                if nxt[0] == ind and _is_item(nxt[1]):
                    return m  # a list at its key's indentation ended; the caller goes on
                raise self._err("expected 'key: value' here", self.i)
            self._take()
            value, anchor = self._value(kv[1], ind, in_map=True)
            if root and anchor and isinstance(value, (_YMap, list)):
                # ser2net: *con0 / *led1 stand for the name of a main-level entry
                self.anchors[anchor] = anchor
            m.entries.append((kv[0], value, anchor))

    def _seq(self, ind: int) -> list[Any]:
        items: list[Any] = []
        while True:
            nxt = self._peek()
            if nxt is None or nxt[0] < ind or (nxt[0] == ind and not _is_item(nxt[1])):
                return items
            if nxt[0] > ind:
                raise self._err("unexpected indentation", self.i)
            rest = nxt[1][1:]
            inner = rest.lstrip(" \t")
            if inner and not inner.startswith("#") and (_is_item(inner) or _key(inner)):
                # "- key: value": a nested block that starts on this line
                self.lines[self.i] = " " * (ind + 1 + len(rest) - len(inner)) + inner
                items.append(self._node(ind))
            else:
                self._take()
                items.append(self._value(rest, ind)[0])

    def _value(self, text: str, parent: int, in_map: bool = False) -> tuple[Any, str]:
        """The value after 'key:' / '- ' (that line is consumed already)."""
        s, anchor = text.strip(), ""
        while s[:1] in ("&", "!"):
            m = re.match(r"([&!])(\S*)\s*", s)
            if m is None:
                break
            if m.group(1) == "&":
                anchor = m.group(2)
            s = s[m.end():]
        node: Any
        if not s or s.startswith("#"):
            nxt = self._peek()
            if in_map and nxt is not None and nxt[0] == parent and _is_item(nxt[1]):
                node = self._seq(parent)  # "key:\n- a": a list may sit at the key's indentation
            else:
                node = self._node(parent)
        elif s[0] == "*" and s[1:2] not in ("(", "{"):  # *(name) is ser2net text, not an alias
            m2 = _ALIAS_NAME.match(s, 1)
            name = m2.group(0) if m2 else ""
            if name not in self.anchors:
                raise self._err(f"unknown alias *{name}")
            node = copy.deepcopy(self.anchors[name])
        elif s[0] in "\"'":
            node = self._quoted(s)
        elif s[0] in "[{":
            node = self._flow(s)
        elif s[0] in "|>":
            node = self._block_scalar(s, parent)
        else:
            node = self._plain(s, parent)
        if anchor:
            self.anchors[anchor] = node
        return node, anchor

    def _plain(self, first: str, parent: int) -> str:
        """A plain scalar; lines indented deeper than its key continue it, folded with
        one space (ser2net configs split long connector strings this way)."""
        out, breaks = _cut_comment(first), 0
        while self.i < len(self.lines):
            ind, body = _indent(self.lines[self.i])
            if not body or body.startswith("#"):
                breaks += 0 if body else 1
                self.i += 1
                continue
            if ind <= parent or _boundary(ind, body):
                break
            self._take()
            out += ("\n" * breaks if breaks else " ") + _cut_comment(body)
            breaks = 0
        return out

    def _quoted(self, s: str) -> str:
        quote, parts = s[0], [s[1:]]
        while (end := _close_quote(parts[-1], quote)) < 0:
            if self.i >= len(self.lines):
                raise self._err("unterminated quoted string")
            parts.append(self._take())
        tail = parts[-1][end + 1:].strip()
        if tail and not tail.startswith("#"):
            raise self._err("unexpected text after a quoted string")
        parts[-1] = parts[-1][:end]
        return _fold_quoted(parts, quote)

    def _flow(self, s: str) -> Any:
        text = s
        while (end := _flow_end(text)) < 0:
            if self.i >= len(self.lines):
                raise self._err("unterminated [...] or {...}")
            text += "\n" + self._take()
        tail = text[end:].strip()
        if tail and not tail.startswith("#"):
            raise self._err("unexpected text after ] or }")
        try:
            return _Flow(text[:end], self.anchors).value()
        except ValueError as e:
            raise self._err(str(e)) from None

    def _block_scalar(self, header: str, parent: int) -> str:
        m = re.match(r"([|>])([+-]?)([1-9]?)([+-]?)\s*(?:#.*)?$", header)
        if m is None:
            raise self._err("bad | or > block header")
        chomp = m.group(2) or m.group(4)
        indent = max(parent, 0) + int(m.group(3)) if m.group(3) else -1
        lines: list[str] = []
        while self.i < len(self.lines):
            ind, body = _indent(self.lines[self.i])
            if not body:
                lines.append("")
                self.i += 1
                continue
            if indent < 0:
                indent = ind
            if ind < indent or ind <= parent:
                break
            self._take()
            lines.append(" " * (ind - indent) + body)
        trailing = 0
        while lines and not lines[-1]:
            lines.pop()
            trailing += 1
        if m.group(1) == "|":
            text = "\n".join(lines)
        else:  # folded: lines join with a space, an empty line is a newline
            text, blank = "", 0
            for ln in lines:
                if not ln:
                    blank += 1
                    continue
                text += ("\n" * blank if blank else " ") + ln if text else ln
                blank = 0
        if chomp == "-" or not text:
            return text
        return text + "\n" * (1 + trailing if chomp == "+" else 1)


# ---------------------------------------------------------------------------
# One ser2net connection on its way to a mapping
# ---------------------------------------------------------------------------
@dataclass
class _Conn:
    name: str
    label: str                       # how warnings name it
    v3: bool = False                 # ser2net 3.x (escapes differ slightly)
    enabled: bool = True
    device: str = ""
    serial: dict[str, Any] = field(default_factory=lambda: {   # ser2net's default: 9600N81
        "baudrate": 9600, "parity": "N", "bytesize": 8, "stopbits": 1})
    advanced: dict[str, Any] = field(default_factory=dict)
    network: dict[str, Any] = field(default_factory=lambda: {
        "mode": "server", "protocol": "raw", "bind_ip": "0.0.0.0", "port": 0})
    options: dict[str, Any] = field(default_factory=dict)
    xonxoff: bool = False
    rtscts: bool = False
    strings: dict[str, str] = field(default_factory=dict)        # banner/openstr/... (raw)
    traces: dict[str, str] = field(default_factory=dict)         # both/read/write -> file
    trace_mods: dict[tuple[str, str], bool] = field(default_factory=dict)
    access_given: bool = False                                   # remaddr was set
    hold: str = ""                     # why it must come in disabled (wider access otherwise)
    remctl: bool = False                                         # 3.x RFC2217 flag
    dropped: list[str] = field(default_factory=list)             # options with no equivalent
    notes: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        if msg not in self.notes:
            self.notes.append(msg)

    def drop(self, option: str) -> None:
        if option not in self.dropped:
            self.dropped.append(option)

    def serial_text(self) -> str:
        """ser2net's \\B: the serial parameters, e.g. 9600N81."""
        s = self.serial
        return (f"{s.get('baudrate', 9600)}{s.get('parity', 'N')}"
                f"{s.get('bytesize', 8)}{int(s.get('stopbits', 1))}")

    def to_dict(self) -> dict[str, Any]:
        serial: dict[str, Any] = {"port": self.device, **self.serial}
        if self.advanced:
            serial["advanced"] = dict(self.advanced)
        return {"name": self.name, "enabled": self.enabled, "kind": "net", "serial": serial,
                "network": dict(self.network), "options": dict(self.options)}


_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")


def _bool(value: str) -> Optional[bool]:
    v = value.strip().lower()
    return True if v in _TRUE else False if v in _FALSE else None


def _split_gensio(spec: str) -> list[str]:
    """Split a gensio string at the commas outside (...) and quotes."""
    parts: list[str] = []
    buf, depth, quote, i = "", 0, "", 0
    while i < len(spec):
        ch = spec[i]
        if quote:
            quote = "" if ch == quote else quote
        elif ch == "\\" and i + 1 < len(spec):
            buf += spec[i:i + 2]
            i += 2
            continue
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append(buf.strip())
            buf, i = "", i + 1
            continue
        buf += ch
        i += 1
    parts.append(buf.strip())
    return [p for p in parts if p]


def _layer(part: str) -> tuple[str, list[str]]:
    """'telnet(rfc2217)' -> ('telnet', ['rfc2217'])."""
    m = re.match(r"([A-Za-z0-9_-]+)\s*\((.*)\)$", part, re.S)
    if m is None:
        return part, []
    return m.group(1), _split_gensio(m.group(2))


def _port_number(text: str) -> Optional[int]:
    text = text.strip()
    if text.isdigit():
        n = int(text)
        return n if 1 <= n <= 65535 else None
    try:
        return socket.getservbyname(text)
    except (OSError, UnicodeError):
        return None


def _listen_address(c: _Conn, fields: list[str]) -> bool:
    """[ipv4|ipv6,][host,]port -> network bind_ip/port; False if it has no usable port."""
    fields = [f.strip() for f in fields if f.strip()]
    family = ""
    while fields and fields[0].lower() in ("ipv4", "ipv6", "ipv6n4"):
        family = fields.pop(0).lower()
    if not fields:
        c.note("skipped: the accepter has no port")
        return False
    host, port_s = ("", fields[0]) if len(fields) == 1 else (fields[0], fields[1])
    if len(fields) > 2:
        c.note("only the first listen address is imported: " + ",".join(fields[:2]))
    port = _port_number(port_s)
    if port is None:
        c.note(f"skipped: listen port '{port_s}' is not a port number between 1 and 65535")
        return False
    c.network["port"] = port
    c.network["bind_ip"] = _bind_ip(c, host, family)
    return True


def _bind_ip(c: _Conn, host: str, family: str) -> str:
    """The bind IP for a listen host. Never wider than ser2net listened: a loopback
    stays loopback, and an address that cannot be used here (a host name) gives
    0.0.0.0 with the mapping imported disabled until its bind IP is set."""
    h = host.strip("[]")
    if not h:
        return "::" if family == "ipv6" else "0.0.0.0"
    if h.lower() == "localhost":
        return "127.0.0.1"
    try:
        ip: Optional[ipaddress.IPv4Address | ipaddress.IPv6Address] = ipaddress.ip_address(h)
    except ValueError:
        ip = None
    if ip is not None and ip.version == 6 and ip.is_loopback:
        c.note(f"listen address {host} (IPv6 loopback) is not accepted as a bind IP here: "
               "the mapping listens on 127.0.0.1")
        return "127.0.0.1"
    if ip is not None and not ip.is_multicast and not (
            ip.is_reserved and str(ip) not in ("0.0.0.0", "::")):
        return str(ip)
    c.hold = (f"listen address '{host}' cannot be used as a bind IP here: the mapping "
              "listens on 0.0.0.0 and is imported disabled; set its bind IP, then enable it")
    return "0.0.0.0"


# gensio types that end a stack (the rest of the string is their address/device)
_NET_TERMINALS = ("tcp", "udp", "sctp")
_OTHER_TERMINALS = ("unix", "unixdgram", "stdio", "pty", "file", "echo", "dummy", "conacc",
                    "ipmisol", "serialdev", "sdev", "dev", "sound", "mdns", "net", "telnet",
                    "ssl", "certauth", "mux")


def _accepter(c: _Conn, spec: str) -> bool:
    """The listening side: telnet / telnet(rfc2217) over tcp|udp,[host,]port."""
    layers = _split_gensio(spec)
    for idx, part in enumerate(layers):
        name, opts = _layer(part)
        low = name.lower()
        if low in _NET_TERMINALS:
            if low == "udp":
                c.network["mode"] = "udp"
            elif low == "sctp":
                c.note("SCTP is not supported here: the mapping listens on TCP instead")
            return _listen_address(c, layers[idx + 1:])
        if low == "telnet":
            rfc = any(o.split("=", 1)[0].strip().lower() == "rfc2217"
                      and _bool(o.split("=", 1)[1] if "=" in o else "true") for o in opts)
            c.network["protocol"] = "rfc2217" if rfc else "telnet"
        elif low == "ssl":
            c.note("TLS (ssl) is not imported: the mapping listens without TLS; enable TLS "
                   "on it with a certificate and key if clients expect it")
        elif low == "certauth":
            c.note("certauth (gtlssh login) is not imported: clients connect without "
                   "authentication")
        elif low == "mux":
            pass  # gtlssh's channel layer, comes with certauth
        elif low in _OTHER_TERMINALS:
            break
        else:
            c.note(f"accepter layer '{name}' is not imported")
    c.note(f"skipped: accepter '{spec}' is not a TCP/UDP port")
    return False


_SPEED = re.compile(r"(?:speed=)?(\d+)([NEOMS])?([5-8])?([12])?", re.I)


def _windows_device(device: str) -> str:
    m = re.fullmatch(r"(?:\\\\\.\\|//\./)(COM\d+)", device, re.I)
    return m.group(1).upper() if m else device


def _connector(c: _Conn, spec: str, defaults: list[str]) -> bool:
    """The device side: serialdev,<device>[,<serial options>]."""
    layers = _split_gensio(spec)
    for idx, part in enumerate(layers):
        name, opts = _layer(part)
        low = name.lower()
        if low in ("serialdev", "sdev", "dev"):
            tokens = [t for f in layers[idx + 1:] for t in f.split()]
            if not tokens:
                c.note("skipped: the connector names no device")
                return False
            c.device = _windows_device(tokens[0])
            if low == "dev":
                c.note("'dev' (a device without serial settings) is imported as a serial "
                       "port; it may not open if it is not a tty")
            for tok in defaults + opts + tokens[1:]:
                _serial_option(c, tok)
            return True
        if low in _NET_TERMINALS or low in _OTHER_TERMINALS:
            break
        c.note(f"connector layer '{name}' is not imported")
    c.note(f"skipped: connector '{spec}' is not a local serial port")
    return False


def _serial_option(c: _Conn, tok: str) -> None:
    """One gensio serialdev option: 9600n81, rtscts, xonxoff=false, -local, dtr=off, ..."""
    m = _SPEED.fullmatch(tok)
    if m is not None:
        c.serial["baudrate"] = int(m.group(1))
        if m.group(2):
            c.serial["parity"] = m.group(2).upper()
        if m.group(3):
            c.serial["bytesize"] = int(m.group(3))
        if m.group(4):
            c.serial["stopbits"] = int(m.group(4))
        return
    name, eq, value = tok.partition("=")
    low = name.lower()
    flag: Optional[bool] = True
    if low.startswith("-"):
        low, flag = low[1:], False
    if low not in _SERIAL_FLAGS + ("dtr", "rts", "rs485"):
        if low not in ("custspeed", "uucplock", "flock", "readbuf"):  # nothing to do here
            c.drop(tok)
        return
    if eq and flag and low in _SERIAL_FLAGS:
        flag = _bool(value)
        if flag is None:
            c.note(f"serial option '{tok}' has no true/false value, ignored")
            return
    if low in ("xonxoff", "rtscts"):
        setattr(c, low, bool(flag))
    elif low in ("hangup-when-done", "hangup_when_done", "hupcl"):
        c.advanced["hangup_when_done"] = bool(flag)
    elif low == "nobreak":
        c.advanced["nobreak"] = bool(flag)
    elif low in ("dtr", "rts"):
        state = (value or "on").strip().lower() if flag else "off"
        if state in ("on", "off"):
            c.serial[f"{low}_on_open"] = state
        else:
            c.note(f"serial option '{tok}' is not on/off, ignored")
    elif low == "rs485":
        if value.strip().lower() not in ("", "off"):
            c.note(f"{tok} is not imported: set RS-485 in the mapping's serial settings")
    elif low == "rdonly":
        if flag:
            c.network["read_only"] = True
    elif low == "local" and not flag:
        c.note("local=false (watching the modem control lines) is not imported")


_SERIAL_FLAGS = ("xonxoff", "rtscts", "hangup-when-done", "hangup_when_done", "hupcl",
                 "nobreak", "rdonly", "local")


# ---------------------------------------------------------------------------
# ser2net string escapes
# ---------------------------------------------------------------------------
_C_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "t": "\t", "v": "\v", "\\": "\\",
              "?": "?", "'": "'", '"': '"'}
_TIME_ESCAPES = "YyMmADeUHhiSqPT"
_ESCAPE = re.compile(r"\\(x[0-9a-fA-F]{1,2}|[0-7]{1,3}|.)", re.S)
_GONE = ""  # marks a dropped escape until it is removed


def _convert(c: _Conn, text: str, kind: str, what: str) -> str:
    """ser2net escapes -> this program. kind 'banner' keeps the escapes the banner
    renderer knows (\\d \\p \\N \\I \\r \\n); 'bytes' (openstr/closestr/closeon, sent
    as they are) and 'file' (a trace file name) get plain text."""
    dropped: list[str] = []
    fixed: list[str] = []

    def one(m: re.Match[str]) -> str:
        e = m.group(1)
        ch = e[0]
        if ch == "x" and len(e) > 1:
            return chr(int(e[1:], 16))
        if ch in "01234567":
            return chr(int(e, 8) & 0xFF)
        if ch in _C_ESCAPES:
            return _C_ESCAPES[ch]
        if ch in "rn":
            return "\\" + ch if kind == "banner" else ("\r" if ch == "r" else "\n")
        if ch in "do":
            if kind == "banner":
                return "\\d"
            return c.device.rsplit("/", 1)[-1] if kind == "file" else c.device
        if ch == "p":
            return "\\p" if kind == "banner" else str(c.network.get("port", ""))
        if ch == "N" and not c.v3:
            return "\\N" if kind == "banner" else c.name
        if ch == "I" and kind == "banner":
            return "\\I"
        if ch == "B" or (ch == "s" and kind != "file"):
            fixed.append("\\" + ch)
            return c.serial_text()
        if ch in _TIME_ESCAPES or ch in "Is":
            dropped.append("\\" + ch)
            return _GONE
        return ch  # ser2net prints an unknown escape's character
    if kind == "banner":  # real line breaks (from "..." YAML) become the banner's escapes
        text = text.replace("\r", "\\r").replace("\n", "\\n")
    out = _ESCAPE.sub(one, text)
    # a dropped part of a file name takes its separator along: tr-\p-\Y-\M -> tr-2000
    out = re.sub(f"[-_.: ]?{_GONE}" if kind == "file" else _GONE, "", out)
    if fixed:
        c.note(f"{what}: {'/'.join(dict.fromkeys(fixed))} (serial parameters) is written as "
               f"the fixed text {c.serial_text()}")
    if dropped:
        parts = " ".join(dict.fromkeys(dropped))
        c.note(f"{what}: {parts} in the file name left out (one trace file per mapping here, "
               "rotated by size)" if kind == "file" else
               f"{what}: {parts} not supported here, left out")
    return out


# ---------------------------------------------------------------------------
# Connection options (4.x `options:` keys; 3.x keywords map onto the same names)
# ---------------------------------------------------------------------------
_TRACE_MOD = re.compile(r"trace-(?:(both|read|write)-)?(hexdump|timestamp)")


def _flag(c: _Conn, key: str, value: str) -> Optional[bool]:
    b = _bool(value)
    if b is None:
        c.note(f"{key}: '{value}' is not true/false, ignored")
    return b


def _option(c: _Conn, key: str, value: str) -> None:
    k = key.strip().lower()
    if k in ("timeout", "connection timeout"):
        _idle_timeout(c, value)
    elif k == "kickolduser":
        b = _flag(c, key, value)
        if b is not None:
            c.network["kick_old_user"] = b
    elif k == "max-connections":
        n = value.strip()
        if n.isdigit() and int(n) >= 1:
            c.network["max_connections"] = int(n)
        else:
            c.note(f"max-connections '{value}' is not a number >= 1, ignored")
    elif k in ("banner", "openstr", "closestr", "closeon"):
        c.strings[k] = value
    elif k in ("trace-both", "trace-read", "trace-write"):
        if value.strip():
            c.traces[k[6:]] = value.strip()
    elif (m := _TRACE_MOD.fullmatch(k)) is not None:
        b = _flag(c, key, value)
        if b is not None:
            c.trace_mods[(m.group(1) or "all", m.group(2))] = b
    elif k == "remaddr":
        _remaddr(c, value)
    elif k == "no-acc-to-con":  # clients' data is not passed to the device = read-only
        if _flag(c, key, value):
            c.network["read_only"] = True
    elif k == "telnet-brk-on-sync":  # only matters on telnet ports
        if _bool(value) and c.network["protocol"] != "raw":
            c.drop(key)
    elif k == "chardelay":
        pass  # batching of serial reads; data is forwarded as it arrives here
    else:
        c.drop(key)


def _idle_timeout(c: _Conn, value: str) -> None:
    v = value.strip()
    if v.isdigit():
        c.options["idle_timeout_s"] = min(int(v), MAX_TIMER_S)
        if int(v) > MAX_TIMER_S:
            c.note(f"timeout {v} s is longer than the maximum here: set to {MAX_TIMER_S} s")
    elif v:
        c.note(f"timeout '{value}' is not a number of seconds, ignored")


def _remaddr(c: _Conn, value: str) -> None:
    """remaddr=<addr>[;<addr>...] (allowed clients) -> allowed_client_ips. Only IP
    addresses and subnets can be represented; source ports are not checked here."""
    c.access_given = True
    allowed = c.network.setdefault("allowed_client_ips", [])
    for entry in re.split(r"[;\s]+", value.strip()):
        if not entry:
            continue
        if entry.startswith("!"):
            c.note(f"remaddr {entry}: connect-back addresses are not imported")
            continue
        fields = [f.strip() for f in entry.split(",") if f.strip()]
        while fields and fields[0].lower() in ("ipv4", "ipv6", "ipv6n4", "tcp", "udp", "sctp"):
            fields.pop(0)
        host = fields[0] if fields else ""
        try:
            ipaddress.ip_network(host, strict=False)
        except ValueError:
            c.note(f"remaddr {entry}: only IP addresses and subnets can be imported")
            continue
        if len(fields) > 1 and fields[1] != "0":
            c.note(f"remaddr {entry}: only the client address is checked here, not its port")
        if host not in allowed:
            allowed.append(host)


def _finish(c: _Conn) -> None:
    """Resolve what depends on the whole connection; collect the warnings."""
    net = c.network
    if c.rtscts and c.xonxoff:
        c.note("both RTS/CTS and XON/XOFF are on: only RTS/CTS is imported (one flow "
               "control per port here)")
    c.serial["flowcontrol"] = "rtscts" if c.rtscts else "xonxoff" if c.xonxoff else "none"
    if net["mode"] == "udp" and net["protocol"] != "raw":
        c.note(f"{net['protocol']} over UDP is not supported here: imported as raw UDP")
        net["protocol"] = "raw"
    if net["protocol"] == "rfc2217" and net.get("max_connections", 1) > 1:
        c.note("RFC2217 allows a single client here: max-connections set to 1")
        net["max_connections"] = 1
    for key in ("banner", "openstr", "closestr", "closeon"):
        if key in c.strings:
            c.options[key] = _convert(c, c.strings[key], "banner" if key == "banner" else "bytes",
                                      key)
    labels = ({"both": "tb", "read": "tr", "write": "tw"} if c.v3 else
              {k: f"trace-{k}" for k in ("both", "read", "write")})
    kind = next((k for k in ("both", "read", "write") if k in c.traces), "")
    if kind:
        path = _convert(c, c.traces[kind], "file", labels[kind])
        name = trace_file_name(path)
        if name:
            c.options["trace_both"] = path
            if kind != "both":
                c.note(f"{labels[kind]}: both directions are traced into one file here")
            if name != path:
                c.note(f"trace file: written as traces/<mapping id>-{name} in the data "
                       f"directory ({path} is not used)")
        for other in c.traces:
            if other != kind:
                c.note(f"{labels[other]} is not imported: one trace file per mapping")
        for mod in ("hexdump", "timestamp"):
            b = c.trace_mods.get((kind, mod), c.trace_mods.get(("all", mod)))
            if b is not None:
                c.options[f"trace_{mod}"] = b
    if c.access_given and not net.get("allowed_client_ips"):
        c.hold = ("remaddr could not be imported: the mapping is imported disabled rather "
                  "than open to every client")
    if c.hold:
        c.enabled = False
        c.note(c.hold)
    if c.dropped:
        c.note("not imported (no equivalent here): " + ", ".join(c.dropped))


def _notes(c: _Conn, imported: bool) -> list[str]:
    """A connection's warnings; for a skipped one only why it was skipped."""
    return [f"{c.label}: {n}" for n in c.notes if imported or n.startswith("skipped")]


# ---------------------------------------------------------------------------
# ser2net 4.x (ser2net.yaml)
# ---------------------------------------------------------------------------
_V1_KEYS = {"connection", "define", "default", "delete_default", "rotator", "led", "admin"}
_V2_KEYS = {"connections", "defines", "defaults", "delete_defaults", "rotators", "leds",
            "admins"}
_TOP_KEY = re.compile(r"^(?:connections?|defines?|defaults?|delete_defaults?|rotators?|leds?|"
                      r"admins?|include)[ \t]*:(?:[ \t]|$)", re.I | re.M)
_SERIAL_DEFAULTS = {"speed", "nobreak", "rtscts", "xonxoff", "local", "hangup-when-done",
                    "dtr", "rts", "custspeed", "rs485", "uucplock", "flock"}
_OPTION_DEFAULTS = {"kickolduser", "max-connections", "remaddr", "telnet-brk-on-sync",
                    "chardelay", "trace-hexdump", "trace-timestamp", "trace-both-hexdump",
                    "trace-both-timestamp"}


def _substitute(s: str, aliases: dict[str, str], act: str, notes: list[str]) -> str:
    """ser2net's references inside strings: *(name) in version 1 files, =(name) in
    version 2 ('**' / '*(*' for the literal characters); *{file} / ={file} (a file on
    the ser2net host) cannot be read here. $(name) is replaced too for a known name."""
    out: list[str] = []
    i = 0
    while i < len(s):
        ch, nxt = s[i], s[i + 1:i + 2]
        ours = ch in act
        if ours and nxt == ch:
            out.append(ch)
            i += 2
            continue
        if (ours and nxt in ("(", "{")) or (ch == "$" and nxt == "("):
            if ours and s[i + 2:i + 3] == ch:
                out.append(ch + nxt)
                i += 3
                continue
            close = s.find(")" if nxt == "(" else "}", i + 2)
            if close > 0:
                ref = s[i + 2:close]
                if nxt == "{":
                    notes.append(f"{ch}{{{ref}}} (a file on the ser2net host) is left out")
                    i = close + 1
                    continue
                if ref in aliases:
                    out.append(aliases[ref])
                    i = close + 1
                    continue
                if ours:
                    notes.append(f"unknown reference {ch}({ref}) left as it is")
        out.append(ch)
        i += 1
    return "".join(out)


class _Yaml:
    def __init__(self, act: str) -> None:
        self.act = act                       # reference character(s): * (v1), = (v2)
        self.aliases: dict[str, str] = {}
        self.warnings: list[str] = []
        self.conns: list[_Conn] = []
        self.serial_defaults: dict[str, str] = {}
        self.option_defaults: dict[str, str] = {}

    def text(self, node: Any, notes: list[str], what: str) -> str:
        if isinstance(node, (_YMap, list)):
            notes.append(f"{what} is not a single value, ignored")
            return ""
        return _substitute(str(node), self.aliases, self.act, notes)

    def gtext(self, node: Any, what: str) -> str:
        """text() outside a connection: notes become warnings about `what`."""
        notes: list[str] = []
        value = self.text(node, notes, what)
        self.warnings.extend(f"{what}: {n}" for n in notes)
        return value

    def entry(self, key: str, value: Any, anchor: str) -> None:
        k = key.lower()
        if k == "define":
            if anchor:  # ser2net ignores a define without an &anchor
                self.aliases[anchor] = self.gtext(value, f"define {anchor}")
        elif k == "defines" and isinstance(value, _YMap):
            for name, v, _a in value.entries:
                self.aliases[name] = self.gtext(v, f"define {name}")
        elif k == "connection":
            self.connection(anchor, value)
        elif k == "connections" and isinstance(value, _YMap):
            for name, body, _a in value.entries:
                self.connection(name, body)
        elif k == "default" and isinstance(value, _YMap):
            self.default(*(self.gtext(value.get(f, ""), "default")
                           for f in ("name", "value", "class")))
        elif k == "defaults" and isinstance(value, _YMap):
            for name, body, _a in value.entries:
                if isinstance(body, _YMap):
                    self.default(name, self.gtext(body.get("value", ""), f"default {name}"),
                                 self.gtext(body.get("class", ""), f"default {name}"))
        elif k == "delete_default" and isinstance(value, _YMap):
            self.delete_default(self.gtext(value.get("name", ""), "delete_default"))
        elif k == "delete_defaults" and isinstance(value, _YMap):
            for name, _v, _a in value.entries:
                self.delete_default(name)
        elif k in ("rotator", "rotators"):
            names = ([anchor] if k == "rotator" else
                     [n for n, _v, _a in value.entries] if isinstance(value, _YMap) else [])
            for name in names or ["(unnamed)"]:
                self.warnings.append(f"rotator {name}: not imported (rotators are not "
                                     "supported here)")
        elif k in ("admin", "admins"):
            self.warnings.append("the ser2net admin interface is not imported (the web UI "
                                 "and REST API take its place)")
        elif k == "include":
            self.warnings.append(f"include {self.gtext(value, 'include')}: included files are "
                                 "not read; import each of them as well")
        elif k not in ("led", "leds", "defines", "connections", "defaults", "delete_default",
                       "delete_defaults", "default"):
            self.warnings.append(f"unknown top-level entry '{key}' ignored")

    def default(self, name: str, value: str, cls: str) -> None:
        n, cls = name.strip().lower(), cls.strip().lower()
        if not n or cls not in ("", "serialdev", "sdev", "ser2net"):
            return  # a default for another gensio (ssl keys, ipmisol, ...) does not apply
        if n in _SERIAL_DEFAULTS and cls != "ser2net":
            self.serial_defaults[n] = value.strip()
        elif n in _OPTION_DEFAULTS and cls in ("", "ser2net"):
            self.option_defaults[n] = value.strip()
        else:
            self.warnings.append(f"default {name}: {value} is not imported (no equivalent here)")

    def delete_default(self, name: str) -> None:
        self.serial_defaults.pop(name.strip().lower(), None)
        self.option_defaults.pop(name.strip().lower(), None)

    def connection(self, name: str, body: Any) -> None:
        if name:
            self.aliases[name] = name  # ser2net: *(con0) is the connection's name
        c = _Conn(name=name, label=name or "a connection without a name")
        if not isinstance(body, _YMap):
            c.note("skipped: not a mapping")
        else:
            accepter = self.text(body.get("accepter", ""), c.notes, "accepter").strip()
            connector = self.text(body.get("connector", ""), c.notes, "connector").strip()
            if not accepter or not connector:
                c.note("skipped: it needs both an accepter and a connector")
            elif _accepter(c, accepter):
                if not name:
                    c.name = c.label = f"ser2net-{c.network['port']}"
                defaults = [f"{k}={v}" for k, v in self.serial_defaults.items()]
                if _connector(c, connector, defaults):
                    self.settings(c, body)
                    _finish(c)
                    self.conns.append(c)
        self.warnings.extend(_notes(c, imported=bool(self.conns) and self.conns[-1] is c))

    def settings(self, c: _Conn, body: _YMap) -> None:
        enable = self.text(body.get("enable", "on"), c.notes, "enable")
        b = _bool(enable)
        if b is None:
            c.note(f"enable '{enable}' is not on/off: imported disabled")
        c.enabled = bool(b)
        for k, v in self.option_defaults.items():
            _option(c, k, v)
        for key, value, _a in body.entries:
            k = key.lower()
            if k == "options":
                if isinstance(value, _YMap):
                    for okey, oval, _oa in value.entries:
                        _option(c, okey, self.text(oval, c.notes, okey))
                elif str(value).strip():
                    c.note("options is not a mapping, ignored")
            elif k not in ("accepter", "connector", "enable"):
                _option(c, key, self.text(value, c.notes, key))  # timeout (and stray options)


def _from_yaml(text: str, label: str) -> tuple[list[_Conn], list[str]]:
    try:
        docs = _YamlReader(text).documents()
    except _YamlError as e:
        raise ConfigError(f"{label}: {e}") from None
    entries = [e for d in docs if isinstance(d, _YMap) for e in d.entries]
    keys = {k.lower() for k, _v, _a in entries}
    v1, v2 = bool(keys & _V1_KEYS), bool(keys & _V2_KEYS)
    if not (v1 or v2):
        raise ConfigError(f"{label} is not a ser2net configuration: it has no "
                          "connection, define or default entries.")
    y = _Yaml("=" if v2 and not v1 else "*=" if v2 else "*")
    if v1 and v2:
        y.warnings.append("the file mixes ser2net version 1 and version 2 YAML entries")
    for key, value, anchor in entries:
        y.entry(key, value, anchor)
    return y.conns, y.warnings


# ---------------------------------------------------------------------------
# ser2net 3.x (ser2net.conf)
# ---------------------------------------------------------------------------
_V3_STATE = re.compile(r":(rawlp|raw|telnet|off):")
_V3_DIRECTIVE = re.compile(r"(BANNER|SIGNATURE|OPENSTR|CLOSESTR|CLOSEON|DEVICE|TRACEFILE|"
                           r"CONTROLPORT|RS485CONF|DEFAULT|ROTATOR|LED):")
_V3_PARITY = {"NONE": "N", "EVEN": "E", "ODD": "O", "MARK": "M", "SPACE": "S",
              "none": "N", "even": "E", "odd": "O", "mark": "M", "space": "S"}
_V3_TRACE = {"tr": "read", "tw": "write", "tb": "both"}
_V3_IGNORED_DEFAULTS = ("local", "chardelay", "telnet_brk_on_sync")


def _v3_lines(text: str) -> list[tuple[int, str]]:
    """Logical lines (line number, text): blank and '#' lines dropped, a trailing
    backslash continues the line on the next one (as ser2net 3.x reads them)."""
    out: list[tuple[int, str]] = []
    buf, start = "", 0
    for n, line in enumerate(text.split("\n"), 1):
        if not buf:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            start, line = n, line.lstrip()
        if line.endswith("\\"):
            buf += line[:-1]
            continue
        out.append((start, buf + line))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


class _V3:
    def __init__(self) -> None:
        self.strings: dict[str, tuple[str, str]] = {}  # name -> (BANNER/OPENSTR/..., text)
        self.devices: dict[str, str] = {}
        self.tracefiles: dict[str, str] = {}
        self.defaults: dict[str, str] = {}
        self.warnings: list[str] = []
        self.conns: list[_Conn] = []

    def directive(self, n: int, kind: str, rest: str) -> None:
        name, _, value = rest.partition(":")
        if kind == "CONTROLPORT":
            self.warnings.append(f"line {n}: CONTROLPORT {rest.strip()} is not imported (the "
                                 "web UI and REST API take its place)")
        elif kind == "DEFAULT":
            if name in _V3_IGNORED_DEFAULTS or name in (
                    "speed", "databits", "stopbits", "parity", "xonxoff", "rtscts",
                    "hangup_when_done", "nobreak", "remctl", "kickolduser", "max-connections",
                    "remaddr"):
                self.defaults[name] = value.strip()
            else:
                self.warnings.append(f"line {n}: DEFAULT {name} is not imported")
        elif kind == "DEVICE":
            self.devices[name] = value.strip()
        elif kind == "TRACEFILE":
            self.tracefiles[name] = value.strip()
        elif kind in ("LED", "SIGNATURE", "ROTATOR"):
            what = {"LED": "LEDs", "SIGNATURE": "RFC2217 signatures", "ROTATOR": "rotators"}
            self.warnings.append(f"line {n}: {kind} {name} is not imported ({what[kind]} are "
                                 "not supported here)")
            self.strings[name] = (kind, value)
        else:  # BANNER OPENSTR CLOSESTR CLOSEON RS485CONF
            self.strings[name] = (kind, value)

    def port(self, n: int, line: str, m: re.Match[str]) -> None:
        state = m.group(1)
        timeout, _, rest = line[m.end():].partition(":")
        device, _, options = rest.partition(":")
        device = self.devices.get(device.strip(), device.strip())
        c = _Conn(name="", label=f"line {n}", v3=True)
        fields = []
        for f in line[: m.start()].split(","):
            if f.strip().lower() == "udp":
                c.network["mode"] = "udp"
            elif f.strip().lower() != "tcp":
                fields.append(f)
        if _listen_address(c, fields):
            port = c.network["port"]
            c.name = f"ser2net-udp-{port}" if c.network["mode"] == "udp" else f"ser2net-{port}"
            c.label = f"{c.name} (line {n})"
            if not device:
                c.note("skipped: no device")
            elif device.startswith("sol.") or " " in device:
                c.note("skipped: IPMI serial-over-LAN connections are not imported")
            else:
                self.settings(c, state, timeout, device, options)
                _finish(c)
                self.conns.append(c)
        self.warnings.extend(_notes(c, imported=bool(self.conns) and self.conns[-1] is c))

    def settings(self, c: _Conn, state: str, timeout: str, device: str, options: str) -> None:
        c.device = device
        c.enabled = state != "off"
        c.network["protocol"] = "telnet" if state == "telnet" else "raw"
        if state == "rawlp":
            c.note("rawlp (a printer port without serial settings) is imported as raw; the "
                   "device is opened as a serial port")
        _idle_timeout(c, timeout)
        for name, value in self.defaults.items():
            self.default(c, name, value)
        for tok in options.split():
            self.token(c, tok)
        if c.remctl:
            if state == "telnet":
                c.network["protocol"] = "rfc2217"
            else:
                c.note("remctl (RFC2217) needs a telnet port: ignored")

    def default(self, c: _Conn, name: str, value: str) -> None:
        if name in _V3_IGNORED_DEFAULTS:
            if name == "telnet_brk_on_sync" and _bool(value):
                _option(c, "telnet-brk-on-sync", "true")
        elif name in ("speed", "databits", "stopbits"):
            if value.isdigit():
                c.serial[{"speed": "baudrate", "databits": "bytesize"}.get(name, name)] = int(value)
        elif name == "parity":
            if value.upper() in _V3_PARITY:
                c.serial["parity"] = _V3_PARITY[value.upper()]
        elif name == "remaddr":
            if value:
                _remaddr(c, value)
        elif name in ("max-connections", "kickolduser"):
            _option(c, name, value)
        elif _bool(value) is not None:  # xonxoff rtscts hangup_when_done nobreak remctl
            flag = bool(_bool(value))
            if name in ("xonxoff", "rtscts", "remctl"):
                setattr(c, name, flag)
            else:
                c.advanced[name] = flag

    def token(self, c: _Conn, tok: str) -> None:
        if self.serial(c, tok) or self.option(c, tok):
            return
        named = self.strings.get(tok)
        if named is not None:
            kind, value = named
            if kind in ("BANNER", "OPENSTR", "CLOSESTR", "CLOSEON"):
                c.strings[kind.lower()] = value
            elif kind == "RS485CONF":
                c.note(f"RS485CONF {tok} is not imported: set RS-485 in the mapping's serial "
                       "settings")
            return  # LED / SIGNATURE: reported where they are defined
        if not self.serial(c, tok.upper()):
            c.note(f"unknown option '{tok}' ignored")

    @staticmethod
    def serial(c: _Conn, tok: str) -> bool:
        """The serial keywords (case as ser2net 3.x spells them)."""
        base, on = tok.lstrip("-"), not tok.startswith("-")
        if tok.isdigit():
            c.serial["baudrate"] = int(tok)
        elif tok in ("1STOPBIT", "2STOPBITS"):
            c.serial["stopbits"] = int(tok[0])
        elif re.fullmatch(r"[5-8]DATABITS", tok):
            c.serial["bytesize"] = int(tok[0])
        elif tok in _V3_PARITY:
            c.serial["parity"] = _V3_PARITY[tok]
        elif base in ("XONXOFF", "RTSCTS"):
            setattr(c, base.lower(), on)
        elif base in ("HANGUP_WHEN_DONE", "NOBREAK"):
            c.advanced[base.lower()] = on
        elif base == "LOCAL":
            if not on:
                c.note("-LOCAL (watching the modem control lines) is not imported")
        else:
            return False
        return True

    def option(self, c: _Conn, tok: str) -> bool:
        """The connection keywords of ser2net 3.x."""
        name, eq, value = tok.partition("=")
        base, on = name.lstrip("-"), not name.startswith("-")
        if not eq:
            mod = re.fullmatch(r"(?:(tr|tw|tb)-)?(hexdump|timestamp)", base)
            if base == "remctl":
                c.remctl = on
            elif base == "kickolduser":
                c.network["kick_old_user"] = on
            elif mod is not None:
                c.trace_mods[(_V3_TRACE.get(mod.group(1) or "", "all"), mod.group(2))] = on
            elif base == "telnet_brk_on_sync":
                if on:
                    _option(c, "telnet-brk-on-sync", "true")
            elif base != "chardelay":
                return False
            return True
        if name in _V3_TRACE:
            if value in self.tracefiles:
                c.traces[_V3_TRACE[name]] = self.tracefiles[value]
            else:
                c.note(f"{tok}: there is no TRACEFILE named {value}")
        elif name == "rs485":
            c.note(f"{tok} is not imported: set RS-485 in the mapping's serial settings")
        elif name == "max-connections":
            _option(c, name, value)
        elif name == "remaddr":
            _remaddr(c, value)
        elif name in ("chardelay-scale", "chardelay-min", "chardelay-max", "dev-to-net-bufsize",
                      "net-to-dev-bufsize", "dev-to-tcp-bufsize", "tcp-to-dev-bufsize"):
            c.drop(name)
        elif name not in ("led-rx", "led-tx"):  # LEDs: reported where they are defined
            return False
        return True


def _from_v3(lines: list[tuple[int, str]]) -> tuple[list[_Conn], list[str]]:
    p = _V3()
    for n, line in lines:
        d = _V3_DIRECTIVE.match(line)
        m = _V3_STATE.search(line)
        if d is not None:
            p.directive(n, d.group(1), line[d.end():])
        elif m is not None:
            p.port(n, line, m)
        else:
            p.warnings.append(f"line {n}: not a ser2net.conf line, ignored")
    return p.conns, p.warnings


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def _assemble(conns: list[_Conn], warnings: list[str]) -> list[dict]:
    """Drop what does not validate or listens where an earlier one does, make the names
    unique and keep one active mapping per serial device."""
    kept: list[_Conn] = []
    for c in conns:
        try:
            MappingConfig.from_dict(c.to_dict()).validate()
        except (TypeError, ValueError) as e:  # ConfigError is a ValueError
            warnings.append(f"{c.label}: skipped: {e}")
            continue
        net = c.network
        proto = "udp" if net["mode"] == "udp" else "tcp"
        other = next((o for o in kept if o.network["port"] == net["port"]
                      and ("udp" if o.network["mode"] == "udp" else "tcp") == proto
                      and _ip_overlaps(o.network["bind_ip"], net["bind_ip"])), None)
        if other is not None:
            warnings.append(f"{c.label}: skipped: {proto.upper()} port {net['port']} is "
                            f"already used by '{other.name}'")
            continue
        kept.append(c)
    used: set[str] = set()
    owner: dict[str, str] = {}
    for c in kept:
        base, k = c.name, 1
        while c.name in used:
            k += 1
            c.name = f"{base} ({k})"
        used.add(c.name)
        if c.name != base:
            warnings.append(f"{c.label}: renamed to '{c.name}' (the name is used twice)")
        if c.enabled and c.device in owner:
            c.enabled = False
            warnings.append(f"{c.label}: imported disabled: {c.device} is also used by "
                            f"'{owner[c.device]}', and a serial port can be open in only one "
                            "active mapping here")
        elif c.enabled:
            owner[c.device] = c.name
    return [c.to_dict() for c in kept]


def looks_yaml(text: str) -> bool:
    """True for a ser2net 4.x ser2net.yaml, False for anything else (a 3.x ser2net.conf)."""
    for line in text.split("\n"):
        s = line.strip()
        if s and not s.startswith("#"):
            if s.startswith("%YAML") or _marker(s, "---"):
                return True
            break
    return bool(_TOP_KEY.search(text))


def parse_ser2net_config(text: str, filename: str = "") -> tuple[list[dict], list[str]]:
    """Mappings (dicts for MappingConfig.from_dict) + human-readable warnings.

    `text` is a ser2net 4.x ser2net.yaml or a 3.x ser2net.conf (detected from the
    content; `filename` only names the file in messages). Raises ConfigError when it
    is neither, or when the YAML is broken."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    label = re.split(r"[\\/]", filename)[-1] if filename else ""
    if not text.strip():
        raise ConfigError("The file is empty.")
    if looks_yaml(text):
        conns, warnings = _from_yaml(text, label or "ser2net.yaml")
    else:
        lines = _v3_lines(text)
        if not any(_V3_DIRECTIVE.match(ln) or _V3_STATE.search(ln) for _n, ln in lines):
            raise ConfigError(f"{label or 'The file'} is not a ser2net configuration: expected "
                              "a ser2net.yaml (ser2net 4.x) or ser2net.conf (ser2net 3.x) file.")
        conns, warnings = _from_v3(lines)
    mappings = _assemble(conns, warnings)
    if not mappings:
        warnings.append("No connection could be imported." if conns else
                        "The file has no ser2net connections to import.")
    return mappings, warnings
