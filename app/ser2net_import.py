"""Import mappings from a classic ser2net configuration (the C program by Corey Minyard).

Users migrating from it bring /etc/ser2net/ser2net.yaml (ser2net 4.x) or
/etc/ser2net.conf (ser2net 3.x). Neither can be read with a stock parser: a 4.x
"version 1" file repeats top-level keys (one `connection:` per port) and substitutes
`*(name)` references inside strings, which is not standard YAML (and PyYAML is not a
dependency anyway); 3.x has its own colon-separated line format. So this module
carries a small reader for the YAML subset ser2net configs use plus a 3.x line
parser, and turns every ser2net connection into a dict for MappingConfig.from_dict
(each warning about one is a Note that knows its mapping: the preview shows it there).

What has no equivalent here (LEDs, rotators, mDNS, ...) is reported as a warning that
names the connection; chardelay (how ser2net batches what it reads) is dropped without
one: data is forwarded as it arrives here. A connection that would be more open here than in
ser2net is imported disabled, with a warning that says why: its gtlssh login
(certauth) cannot be carried over, its TLS client certificates (ssl clientauth) only with
the CA file they are checked against (CA=) on this host, its TLS certificate is not on
this host (with the files there it keeps TLS), the device only
received data (wronly, rawlp, no-con-to-acc), its bind address or remaddr cannot be
used here. A connection that cannot be represented at all (a network or IPMI
connector, a unix-socket accepter) is skipped with a warning, so every returned dict
passes MappingConfig.validate(). ser2net only opens a serial device while a client is
connected, so several of its ports may share one device; a mapping here keeps its
device open, so later connections on the same device are imported disabled, and a
repeated listening port is skipped: the import can then be applied as a whole. As the
device stays open, an openstr is sent once when it opens, not for each client (a note says
so), and hangup-when-done / nobreak are kept but do nothing (a note too). A listening port
this process can't use (below 1024 on Linux without the capability) gets a note. ser2net's
locking carries over: 4.x flock (flock plus TIOCEXCL) and uucplock, on by default, become
exclusive + tiocexcl and respect_uucp_locks (UUCP lock files are honoured, not made), 3.x
UUCP locking respect_uucp_locks; one warning says what still differs (the port is held
open, no lock files are made).
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import (
    MAX_TIMER_S, RFC2217_SIGNATURE_MAX, ConfigError, MappingConfig, _ip_overlaps,
    bind_ip_problem, canonical_device, canonical_ip, trace_file_name, url_port,
)
from .engine import bindhint
from .i18n import _

# What one file may cost. A ser2net configuration is a few KB, but aliases, defines and
# string escapes can repeat text without limit, so each kind of growth has a bound and
# a file past one is refused (ConfigError) instead of tying up the service.
MAX_TEXT = 1024 * 1024        # characters in the file
MAX_CONNECTIONS = 1000        # connections (and ser2net.conf ports) in the file
_MAX_VALUE = 64 * 1024        # characters in one value, defines and escapes expanded
_MAX_WORK = 16 * 1024 * 1024  # characters of values and messages produced in all
_MAX_NODES = 100_000          # YAML nodes, each alias counted as all it repeats
_MAX_DEPTH = 64               # YAML collections nested in each other
_MAX_WARNINGS = 1000          # warnings listed; the rest are only counted


class _Budget:
    """The characters (_MAX_WORK) one file may still produce."""

    def __init__(self) -> None:
        self.left = _MAX_WORK

    def spend(self, n: int) -> None:
        self.left -= n
        if self.left < 0:
            raise ConfigError(_("The file expands to more than {0} MB of text with its "
                                "aliases, defines and references, so it is not imported.")
                              .format(_MAX_WORK // 2**20))


def _too_long(what: str) -> ConfigError:
    return ConfigError(_("{0}: the value is longer than {1} KB with its defines and escapes "
                         "expanded, so the file is not imported.").format(what, _MAX_VALUE // 1024))


def _too_many_connections() -> ConfigError:
    return ConfigError(_("The file has more than {0} connections; import it in parts.")
                       .format(MAX_CONNECTIONS))


# ---------------------------------------------------------------------------
# A reader for the YAML subset ser2net configs use: block mappings/sequences by
# indentation, plain / 'single' / "double" quoted scalars (multi-line too), flow
# [..] and {..}, |/> block scalars, &anchors and *aliases, comments, %YAML and
# ---/... markers. Repeated keys are kept in order (ser2net reads them all).
# ---------------------------------------------------------------------------
class _YamlError(Exception):
    def __init__(self, line: int, msg: str) -> None:
        super().__init__(_("line {0}: {1}").format(line, msg))


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


def _close_quote(seg: str, quote: str, start: int = 0) -> int:
    """Index of the closing quote in seg (looking from `start`), or -1 (the string goes on)."""
    j = start
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
    out, blank = [parts[0]], 0
    run = -1  # backslashes the text ends with (-1: the first line is not trimmed yet)
    for n, raw in enumerate(parts[1:], 1):
        last = n == len(parts) - 1
        seg = raw.lstrip(" \t") if last else raw.strip(" \t")
        if not seg and not last:
            blank += 1
            continue
        if run < 0:  # only the first line can end in blanks: the others are stripped
            out[0] = out[0].rstrip(" \t")
            run = len(out[0]) - len(out[0].rstrip("\\"))
        if quote == '"' and run % 2 == 1:  # an escaped line break: join directly
            out[-1] = out[-1][:-1]
            if not out[-1]:
                out.pop()
            run, sep = run - 1, ""
        else:
            sep = "\n" * blank if blank else " "
        tail = len(seg) - len(seg.rstrip("\\"))
        run = run + tail if seg and tail == len(seg) and not sep else tail
        out += [sep, seg]
        blank = 0
    text = "".join(out)
    return _dq_unescape(text) if quote == '"' else text.replace("''", "'")


class _FlowEnd:
    """Finds the bracket that closes a flow collection ([ or {), one line at a time, so
    a collection over many lines is scanned once."""

    def __init__(self) -> None:
        self.depth, self.quote = 0, ""

    def scan(self, line: str) -> int:
        """Index just past the closing bracket in `line`, or -1 if it comes later."""
        depth, quote, j = self.depth, self.quote, 0
        while j < len(line):
            c = line[j]
            if quote:
                if c == "\\" and quote == '"':
                    j += 1  # (at the end of a line: the line break is escaped)
                elif c == quote:
                    quote = ""
            elif c in "\"'" and (j == 0 or line[j - 1] in " \t,[{:"):
                quote = c
            elif c == "#" and (j == 0 or line[j - 1] in " \t"):
                break  # a comment: the rest of the line
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        self.depth, self.quote = depth, quote
        return -1


class _Flow:
    """Parser for one flow collection ([a, *b] / {name: speed, value: 115200})."""

    def __init__(self, text: str, reader: "_YamlReader") -> None:
        self.s, self.j, self.r = text, 0, reader

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
        self.r.count()
        c = self.s[self.j:self.j + 1]
        if c in ("[", "{"):
            return self._collection(c)
        if c in ("'", '"'):
            end = _close_quote(self.s, c, self.j + 1)
            if end < 0:
                raise ValueError(_("unterminated quoted string"))
            raw = self.s[self.j + 1:end]
            self.j = end + 1
            return _fold_quoted(raw.split("\n"), c)
        if c == "*":
            m = _ALIAS_NAME.match(self.s, self.j + 1)
            name = m.group(0) if m else ""
            if name not in self.r.anchors:
                raise ValueError(_("unknown alias *{0}").format(name))
            self.j += 1 + len(name)
            return self.r.alias(name)
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
        self.r.nest(1)
        items: list[Any] = []
        mapping = _YMap()
        while True:
            self._ws()
            if self.s.startswith(close, self.j):
                self.j += 1
                self.r.nest(-1)
                return items if opening == "[" else mapping
            item = self.value()
            self._ws()
            if opening == "{":
                if not isinstance(item, str):
                    raise ValueError(_("a [...] or {...} cannot be a key"))
                val: Any = ""
                if self.s.startswith(":", self.j):
                    self.j += 1
                    val = self.value()
                    self._ws()
                mapping.entries.append((item, val, ""))
            else:
                items.append(item)
            if self.s.startswith(",", self.j):
                self.j += 1
            elif not self.s.startswith(close, self.j):
                raise ValueError(_("expected ',' or '{0}'").format(close))


class _YamlReader:
    def __init__(self, text: str) -> None:
        self.lines = text.split("\n")
        self.i = 0     # next line to read
        self.last = 0  # last line consumed (for error messages)
        self.anchors: dict[str, Any] = {}
        self.sizes: dict[str, int] = {}  # anchor -> the nodes it stands for
        self.nodes = 0                   # nodes read, aliases counted in full (count())
        self.depth = 0                   # collections around the one being read

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
                raise self._err(_("unexpected indentation"), self.i)
        return docs

    def count(self, n: int = 1) -> None:
        """Count nodes read. An alias shares the node it names (nothing changes a node once
        it is read), so it costs nothing to copy, but it stands for every node in it: the
        file is refused past _MAX_NODES of them rather than repeating them without end."""
        self.nodes += n
        if self.nodes > _MAX_NODES:
            raise self._err(_("the file stands for more than {0} YAML nodes (an alias counts "
                              "as all it repeats)").format(_MAX_NODES))

    def alias(self, name: str) -> Any:
        self.count(self.sizes[name])
        return self.anchors[name]

    def nest(self, step: int) -> None:
        self.depth += step
        if self.depth > _MAX_DEPTH:
            raise self._err(_("collections are nested more than {0} levels deep")
                            .format(_MAX_DEPTH), self.i)

    def _node(self, parent: int) -> Any:
        """The block node on the next lines, indented deeper than `parent`."""
        nxt = self._peek()
        if nxt is None or nxt[0] <= parent:
            return ""
        ind, body = nxt
        self.nest(1)
        node: Any
        if _is_item(body):
            node = self._seq(ind)
        elif _key(body) is not None:
            node = self._map(ind, root=parent < 0)
        else:
            self._take()
            node = self._value(body, parent)[0]
        self.nest(-1)
        return node

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
                raise self._err(_("expected 'key: value' here"), self.i)
            self._take()
            value, anchor = self._value(kv[1], ind, in_map=True)
            if root and anchor and isinstance(value, (_YMap, list)):
                # ser2net: *con0 / *led1 stand for the name of a main-level entry
                self.anchors[anchor], self.sizes[anchor] = anchor, 1
            m.entries.append((kv[0], value, anchor))

    def _seq(self, ind: int) -> list[Any]:
        items: list[Any] = []
        while True:
            nxt = self._peek()
            if nxt is None or nxt[0] < ind or (nxt[0] == ind and not _is_item(nxt[1])):
                return items
            if nxt[0] > ind:
                raise self._err(_("unexpected indentation"), self.i)
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
        start = self.nodes
        self.count()
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
                raise self._err(_("unknown alias *{0}").format(name))
            node = self.alias(name)
        elif s[0] in "\"'":
            node = self._quoted(s)
        elif s[0] in "[{":
            node = self._flow(s)
        elif s[0] in "|>":
            node = self._block_scalar(s, parent)
        else:
            node = self._plain(s, parent)
        if anchor:
            self.anchors[anchor], self.sizes[anchor] = node, self.nodes - start
        return node, anchor

    def _plain(self, first: str, parent: int) -> str:
        """A plain scalar; lines indented deeper than its key continue it, folded with
        one space (ser2net configs split long connector strings this way)."""
        out, breaks = [_cut_comment(first)], 0
        while self.i < len(self.lines):
            ind, body = _indent(self.lines[self.i])
            if not body or body.startswith("#"):
                breaks += 0 if body else 1
                self.i += 1
                continue
            if ind <= parent or _boundary(ind, body):
                break
            self._take()
            out.append(("\n" * breaks if breaks else " ") + _cut_comment(body))
            breaks = 0
        return "".join(out)

    def _quoted(self, s: str) -> str:
        quote, parts = s[0], [s[1:]]
        while (end := _close_quote(parts[-1], quote)) < 0:
            if self.i >= len(self.lines):
                raise self._err(_("unterminated quoted string"))
            parts.append(self._take())
        tail = parts[-1][end + 1:].strip()
        if tail and not tail.startswith("#"):
            raise self._err(_("unexpected text after a quoted string"))
        parts[-1] = parts[-1][:end]
        return _fold_quoted(parts, quote)

    def _flow(self, s: str) -> Any:
        scan, lines = _FlowEnd(), [s]
        while (end := scan.scan(lines[-1])) < 0:
            if self.i >= len(self.lines):
                raise self._err(_("unterminated [...] or {...}"))
            lines.append(self._take())
        text = "\n".join(lines)
        end += len(text) - len(lines[-1])
        tail = text[end:].strip()
        if tail and not tail.startswith("#"):
            raise self._err(_("unexpected text after ] or }"))
        try:
            return _Flow(text[:end], self).value()
        except ValueError as e:
            raise self._err(str(e)) from None

    def _block_scalar(self, header: str, parent: int) -> str:
        m = re.match(r"([|>])([+-]?)([1-9]?)([+-]?)\s*(?:#.*)?$", header)
        if m is None:
            raise self._err(_("bad | or > block header"))
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
            parts: list[str] = []
            blank = 0
            for ln in lines:
                if not ln:
                    blank += 1
                    continue
                if parts:
                    parts.append("\n" * blank if blank else " ")
                parts.append(ln)
                blank = 0
            text = "".join(parts)
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
    budget: _Budget                  # the file's (shared by its connections)
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
    access_given: bool = False                    # remaddr gave an address (a client limit)
    remaddr_set: bool = False                     # 3.x remaddr= (even empty): no default then
    any_family: bool = False  # no family or address: ser2net listened on IPv4 and IPv6
    holds: list[str] = field(default_factory=list)  # why it comes in disabled (else more open)
    ssl: Optional[dict[str, Any]] = None             # the accepter's ssl layer (_ssl_layer)
    remctl: bool = False                                         # 3.x RFC2217 flag
    flock: bool = True       # gensio serialdev flock (flock + TIOCEXCL), on by default
    uucplock: bool = True    # gensio serialdev uucplock (UUCP lock files), on by default
    devname: str = ""                # 3.x: the device as the port line names it (\o)
    dropped: dict[str, None] = field(default_factory=dict)       # options with no equivalent
    notes: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)             # why it is not imported
    noted: set[str] = field(default_factory=set)                 # = set(notes)
    flagged: set[str] = field(default_factory=set)               # notes about its protection

    def note(self, msg: str, security: bool = False) -> None:
        """`security`: the note says the mapping is held disabled or has lost a limit on
        its clients (Note.security)."""
        if msg not in self.noted:
            self.budget.spend(len(self.label) + len(msg))  # the warning it becomes
            self.noted.add(msg)
            self.notes.append(msg)
        if security:
            self.flagged.add(msg)

    def hold(self, why: str) -> None:
        """Import the connection disabled: as it is here it would be more open than in
        ser2net (every reason is reported)."""
        if why not in self.holds:
            self.holds.append(why)

    def skip(self, why: str) -> None:
        """Note why the connection is not imported (then the only notes shown for it)."""
        msg = _("skipped: {0}").format(why)
        if msg not in self.skipped:
            self.skipped.append(msg)
        self.note(msg)

    def drop(self, option: str) -> None:
        self.dropped[option] = None

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


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _shown(value: str) -> str:
    """A value as a warning shows it: control characters (a CR/LF in a banner) escaped."""
    return _CONTROL.sub(lambda m: {"\r": "\\r", "\n": "\\n", "\t": "\\t"}.get(
        m.group(), f"\\x{ord(m.group()):02x}"), value)


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
        c.skip(_("the accepter has no port"))
        return False
    host, port_s = ("", fields[0]) if len(fields) == 1 else (fields[0], fields[1])
    if len(fields) > 2:
        c.note(_("only the first listen address is imported: {0}").format(",".join(fields[:2])))
    port = _port_number(port_s)
    if port is None:
        c.skip(_("listen port '{0}' is not a port number between 1 and 65535").format(port_s))
        return False
    c.network["port"] = port
    c.network["bind_ip"] = _bind_ip(c, host, family)
    if c.network["bind_ip"] == "::":
        # ipv6n4: an IPv6 socket that takes IPv4 connections too (gensio.5), so IPv4 and
        # IPv6 here; else IPv6 only, also for UDP where IPv6 sockets take IPv4 by default
        # (MappingConfig.from_dict reads a UDP mapping on :: without the key as an older
        # release ran it)
        c.network["dual_stack"] = family == "ipv6n4"
    c.any_family = not family and not host.strip("[]")
    return True


def _bind_ip(c: _Conn, host: str, family: str) -> str:
    """The bind IP for a listen host. Never wider than ser2net listened: a loopback
    stays loopback (::1 too), and an address that cannot be used here (a host name) gives
    0.0.0.0 with the mapping imported disabled until its bind IP is set."""
    h = host.strip("[]")
    if not h:  # (ipv6n4: IPv6 that takes IPv4 too, see _listen_address)
        return "::" if family in ("ipv6", "ipv6n4") else "0.0.0.0"
    if h.lower() == "localhost":
        return "127.0.0.1"
    if not bind_ip_problem(h):
        return canonical_ip(h)
    c.hold(_("listen address '{0}' cannot be used as a bind IP here: the mapping listens on "
             "0.0.0.0 and is imported disabled; set its bind IP, then enable it").format(host))
    return "0.0.0.0"


# gensio types that end a stack (the rest of the string is their address/device)
_NET_TERMINALS = ("tcp", "udp", "sctp")
_OTHER_TERMINALS = ("unix", "unixdgram", "stdio", "pty", "file", "echo", "dummy", "conacc",
                    "ipmisol", "serialdev", "sdev", "dev", "sound", "mdns", "net", "telnet",
                    "ssl", "certauth", "mux")


def _accepter(c: _Conn, spec: str, ssl_defaults: Optional[dict[str, str]] = None,
              rfc2217: Optional[bool] = None) -> bool:
    """The listening side: telnet / telnet(rfc2217) over tcp|udp,[host,]port. `rfc2217`:
    the file's telnet default for it (None: not set), for a telnet layer that doesn't
    say."""
    layers = _split_gensio(spec)
    for idx, part in enumerate(layers):
        name, opts = _layer(part)
        low = name.lower()
        if low in _NET_TERMINALS:
            if low == "udp":
                c.network["mode"] = "udp"
            elif low == "sctp":
                c.note(_("SCTP is not supported here: the mapping listens on TCP instead"))
            return _listen_address(c, layers[idx + 1:])
        if low == "telnet":
            rfc = rfc2217
            for o in opts:
                key, eq, value = o.partition("=")
                if key.strip().lower() == "rfc2217":
                    rfc = bool(_bool(value)) if eq else True
            c.network["protocol"] = "rfc2217" if rfc else "telnet"
        elif low == "ssl":
            c.ssl = _ssl_layer(opts, ssl_defaults or {})  # resolved in _tls()
        elif low == "dtls":
            c.hold(_("{0} (encryption) is not available here for this connection: the mapping "
                     "is imported disabled rather than unencrypted").format(name))
        elif low == "certauth":
            c.hold(_("certauth (gtlssh login) is not available here: the mapping is imported "
                     "disabled rather than open to clients without a login; allow only known "
                     "client IPs on it before you enable it"))
        elif low == "mux":
            pass  # gtlssh's channel layer, comes with certauth
        elif low in _OTHER_TERMINALS:
            break
        else:
            c.note(_("accepter layer '{0}' is not imported").format(name))
    c.skip(_("accepter '{0}' is not a TCP/UDP port").format(spec))
    return False


# the files ser2net's ssl layer uses unless told otherwise (<sysconfdir>/ser2net/)
_SSL_FILES = {"key": "/etc/ser2net/ser2net.key", "cert": "/etc/ser2net/ser2net.crt"}


def _ssl_layer(opts: list[str], defaults: dict[str, str]) -> dict[str, Any]:
    """ssl(key=..,cert=..,CA=..,clientauth) -> {key, cert, ca, clientauth}; what the layer
    leaves out comes from the file's ssl defaults, then from ser2net's own files (no CA
    file of its own)."""
    found = {**_SSL_FILES, "ca": "", **defaults}
    auth = found.pop("clientauth", "").strip()
    clientauth = bool(auth) and _bool(auth) is not False  # a default without a value is off
    for o in opts:
        name, eq, value = o.partition("=")
        name = name.strip().lower()
        if name in ("key", "cert", "ca"):
            found[name] = value.strip()
        elif name == "clientauth":
            clientauth = not eq or _bool(value) is not False
    # an empty cert= means the certificate is in the key file
    return {"key": found["key"], "cert": found["cert"] or found["key"], "ca": found["ca"],
            "clientauth": clientauth}


def _tls(c: _Conn) -> None:
    """The accepter's ssl layer: TLS with the same certificate when its files are on this
    host, else the mapping comes in disabled rather than unencrypted. Client certificates
    (clientauth) are checked against the CA file it names (CA=, also an ssl default) when
    that file is on this host, else the mapping comes in disabled rather than open to
    clients without a certificate."""
    s = c.ssl
    if s is None:
        return
    if s["clientauth"]:
        ca = s["ca"]
        if ca and os.path.isabs(ca) and os.path.isfile(ca):
            c.network["tls_client_ca"] = ca
            c.note(_("TLS client certificates (ssl clientauth) are kept: clients need a "
                     "certificate from the CA file '{0}'").format(ca))
        else:
            c.hold(_("TLS client certificates (ssl clientauth) are checked here only against "
                     "a CA file on this host, and {0}: the mapping is imported disabled rather "
                     "than open to clients without a certificate; set its client CA file "
                     "before you enable it").format(
                _("the ssl layer names none (CA=)") if not ca else
                _("'{0}' is not a file on this host").format(ca)))
    if c.network["mode"] == "udp":
        c.hold(_("{0} (encryption) is not available here for this connection: the mapping "
                 "is imported disabled rather than unencrypted").format("ssl"))
        return
    cert, key = s["cert"], s["key"]
    if all(os.path.isabs(p) and os.path.isfile(p) for p in (cert, key)):
        c.network.update(tls=True, tls_cert=cert, tls_key=key)
        c.note(_("TLS (ssl) is kept: the mapping uses the certificate '{0}' and the key '{1}'")
               .format(cert, key))
    else:
        c.hold(_("TLS (ssl): the certificate '{0}' or the key '{1}' is not on this host: the "
                 "mapping is imported disabled rather than unencrypted; set TLS on it with a "
                 "certificate and key, then enable it").format(cert, key))


_SPEED = re.compile(r"(?:speed=)?(\d+)([NEOMS])?([5-8])?([12])?", re.I)
_PARITY_WORDS = {"NONE": "N", "EVEN": "E", "ODD": "O", "MARK": "M", "SPACE": "S"}


def _serial_word(c: _Conn, tok: str) -> bool:
    """The serial framing written as words (gensio still takes them, ser2net 3.x wrote
    them so): 1STOPBIT / 2STOPBITS / 5DATABITS..8DATABITS in any case, the parity words
    NONE EVEN ODD MARK SPACE in capitals (gensio compares those exactly). False if `tok`
    is none of them."""
    word = tok.upper()
    if word in ("1STOPBIT", "2STOPBITS"):
        c.serial["stopbits"] = int(word[0])
    elif re.fullmatch(r"[5-8]DATABITS", word):
        c.serial["bytesize"] = int(word[0])
    elif tok in _PARITY_WORDS:
        c.serial["parity"] = _PARITY_WORDS[tok]
    else:
        return False
    return True


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
                c.skip(_("the connector names no device"))
                return False
            device = _windows_device(tokens[0])
            if url_port(device):  # a pyserial URL, not a device: never opened here
                break
            c.device = device
            if low == "dev":
                c.note(_("'dev' (a device without serial settings) is imported as a serial "
                         "port; it may not open if it is not a tty"))
            for tok in defaults + opts + tokens[1:]:
                _serial_option(c, tok)
            return True
        if low in _NET_TERMINALS or low in _OTHER_TERMINALS:
            break
        c.note(_("connector layer '{0}' is not imported").format(name))
    c.skip(_("connector '{0}' is not a local serial port").format(spec))
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
    if _serial_word(c, tok):
        return
    if tok.upper() in _PARITY_WORDS:
        c.note(_("serial option '{0}': ser2net 4.x takes the parity words in capitals only "
                 "({1}), so it did not accept this one; not imported").format(tok, tok.upper()))
        return
    name, eq, value = tok.partition("=")
    low = name.lower()
    flag: Optional[bool] = True
    if low.startswith("-"):
        low, flag = low[1:], False
    if low == "wronly":
        if flag and (not eq or _bool(value) is not False):
            _write_only(c, tok)
        return
    if low not in _SERIAL_FLAGS + ("dtr", "rts", "rs485"):
        if low not in ("custspeed", "readbuf"):  # nothing to do here
            c.drop(tok)
        return
    if eq and flag and low in _SERIAL_FLAGS:
        flag = _bool(value)
        if flag is None:
            c.note(_("serial option '{0}' has no true/false value, ignored").format(tok))
            return
    if low in ("xonxoff", "rtscts", "flock", "uucplock"):
        setattr(c, low, bool(flag))
    elif low in ("hangup-when-done", "hangup_when_done", "hupcl"):
        c.advanced["hangup_when_done"] = bool(flag)
    elif low == "nobreak":
        c.advanced["nobreak"] = bool(flag)
    elif low in ("dtr", "rts"):
        on = _bool(value or "on") if flag else False  # gensio reads it as a boolean
        if on is None:
            c.note(_("serial option '{0}' is not on/off, ignored").format(tok))
        else:
            c.serial[f"{low}_on_open"] = "on" if on else "off"
    elif low == "rs485":
        if value.strip().lower() not in ("", "off"):
            c.note(_("{0} is not imported: set RS-485 in the mapping's serial settings")
                   .format(tok))
    elif low == "rdonly":
        if flag:
            c.network["read_only"] = True
    elif low == "local" and not flag:
        c.note(_("local=false (watching the modem control lines) is not imported"))


_SERIAL_FLAGS = ("xonxoff", "rtscts", "hangup-when-done", "hangup_when_done", "hupcl",
                 "nobreak", "rdonly", "local", "flock", "uucplock")


def _write_only(c: _Conn, what: str) -> None:
    """ser2net only wrote to the device (wronly, 3.x rawlp): its output never reached the
    clients. A mapping here always passes it on, so it comes in disabled."""
    c.hold(_("{0} (a write-only device) is not available here: the device's output would be "
             "sent to clients, so the mapping is imported disabled").format(what))


# ---------------------------------------------------------------------------
# ser2net string escapes
# ---------------------------------------------------------------------------
_C_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "t": "\t", "v": "\v", "\\": "\\",
              "?": "?", "'": "'", '"': '"'}
_TIME_ESCAPES = "YyMmADeUHhiSqPT"
_ESCAPE = re.compile(r"\\(x[0-9a-fA-F]{1,2}|[0-7]{1,3}|.)", re.S)
_GONE = ""  # marks a dropped escape until it is removed


def _convert(c: _Conn, text: str, kind: str, what: str,
             high: Optional[list[str]] = None) -> str:
    """ser2net escapes -> this program. kind 'banner' keeps the escapes the banner
    renderer knows (\\d \\p \\N \\I \\r \\n); 'bytes' (openstr/closestr/closeon, sent
    as they are) and 'file' (a trace file name) get plain text. `high` gets the escapes
    that stand for a byte of 0x80 or more: ser2net sent that byte, a string here is sent
    as UTF-8 (two bytes for it)."""
    dropped: list[str] = []
    fixed: list[str] = []

    def byte(n: int, e: str) -> str:
        if n >= 0x80 and high is not None:
            high.append("\\" + e)
        return chr(n)

    def escape(m: re.Match[str]) -> str:
        e = m.group(1)
        ch = e[0]
        if ch == "x" and len(e) > 1:
            return byte(int(e[1:], 16), e)
        if ch in "01234567":
            return byte(int(e, 8) & 0xFF, e)
        if ch == "\\" and kind == "banner":
            return "\\\\"  # a backslash, written so the banner renderer keeps it
        if ch in _C_ESCAPES:
            return _C_ESCAPES[ch]
        if ch in "rn":
            return "\\" + ch if kind == "banner" else ("\r" if ch == "r" else "\n")
        if ch == "o" and not c.v3:  # ser2net 4.3 and later: the connection's name
            return "\\N" if kind == "banner" else c.name
        if ch == "o" and c.devname != c.device:  # 3.x: the DEVICE alias as the port names it
            name = c.devname.rsplit("/", 1)[-1] if kind == "file" else c.devname
            return name.replace("\\", "\\\\") if kind == "banner" else name
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

    grown = 0  # what the escapes add (\d is the whole device path, ...)

    def one(m: re.Match[str]) -> str:
        nonlocal grown
        rep = escape(m)
        grown += len(rep) - len(m.group(0))
        if len(text) + grown > _MAX_VALUE:
            raise _too_long(f"{c.label}: {what}")
        return rep
    if kind == "banner":  # real line breaks (from "..." YAML) become the banner's escapes
        text = text.replace("\r", "\\r").replace("\n", "\\n")
    c.budget.spend(len(text))
    out = _ESCAPE.sub(one, text)
    if len(out) > _MAX_VALUE:
        raise _too_long(f"{c.label}: {what}")
    c.budget.spend(len(out))
    # a dropped part of a file name takes its separator along: tr-\p-\Y-\M -> tr-2000
    out = re.sub(f"[-_.: ]?{_GONE}" if kind == "file" else _GONE, "", out)
    if fixed:
        c.note(_("{0}: {1} (serial parameters) is written as the fixed text {2}")
               .format(what, "/".join(dict.fromkeys(fixed)), c.serial_text()))
        if "\\s" in fixed and not c.v3:
            c.note(_("{0}: \\s stood for the serial parameters in ser2net 3.x and 4.0; ser2net "
                     "4.1 and later print a plain 's' instead").format(what))
    if dropped:
        parts = " ".join(dict.fromkeys(dropped))
        c.note(_("{0}: {1} in the file name left out (one trace file per mapping here, rotated "
                 "by size)").format(what, parts) if kind == "file" else
               _("{0}: {1} not supported here, left out").format(what, parts))
    return out


# ---------------------------------------------------------------------------
# Connection options (4.x `options:` keys; 3.x keywords map onto the same names)
# ---------------------------------------------------------------------------
_TRACE_MOD = re.compile(r"trace-(?:(both|read|write)-)?(hexdump|timestamp)")


def _flag(c: _Conn, key: str, value: str) -> Optional[bool]:
    b = _bool(value)
    if b is None:
        c.note(_("{0}: '{1}' is not true/false, ignored").format(key, value))
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
            c.note(_("max-connections '{0}' is not a number >= 1, ignored").format(value))
    elif k in ("banner", "openstr", "closestr", "closeon", "signature"):
        c.strings[k] = value
    elif k in ("trace-both", "trace-read", "trace-write"):
        if value.strip():
            c.traces[k[6:]] = value.strip()
    elif (m := _TRACE_MOD.fullmatch(k)) is not None:
        b = _flag(c, key, value)
        if b is not None:
            _trace_mod(c, m.group(1) or "all", m.group(2), b)
    elif k == "remaddr":
        _remaddr(c, value)
    elif k == "no-acc-to-con":  # clients' data is not passed to the device = read-only
        if _flag(c, key, value):
            c.network["read_only"] = True
    elif k == "no-con-to-acc":  # the device's data is not passed to the clients
        if _bool(value) is not False:
            c.hold(_("no-con-to-acc (the device's output is not sent to clients) is not "
                     "available here: the mapping is imported disabled rather than sending it "
                     "to them"))
    elif k == "telnet-brk-on-sync":  # only matters on telnet ports
        if _bool(value) and c.network["protocol"] != "raw":
            c.drop(key)
    elif k == "chardelay":
        pass  # batching of serial reads; data is forwarded as it arrives here
    else:
        c.drop(key)


def _trace_mod(c: _Conn, direction: str, mod: str, on: bool) -> None:
    """trace-hexdump / trace-timestamp (direction 'all') or one trace file's own, read in
    the order of the file as ser2net reads them: the general one sets every trace file
    (over a file's own one before it), a file's own one only that file."""
    for d in ("both", "read", "write") if direction == "all" else (direction,):
        c.trace_mods[(d, mod)] = on


def _idle_timeout(c: _Conn, value: str) -> None:
    v = value.strip()
    if v.isdigit():
        c.options["idle_timeout_s"] = min(int(v), MAX_TIMER_S)
        if int(v) > MAX_TIMER_S:
            c.note(_("timeout {0} s is longer than the maximum here: set to {1} s")
                   .format(v, MAX_TIMER_S))
    elif v:
        c.note(_("timeout '{0}' is not a number of seconds, ignored").format(value))


def _remaddr(c: _Conn, value: str) -> None:
    """remaddr=<addr>[;<addr>...] (allowed clients) -> allowed_client_ips. Only IP
    addresses and subnets can be represented; source ports are not checked here. An
    address that can't be imported still limits the clients in ser2net (access_given):
    without any other the mapping comes in disabled. No address = no limit, as there."""
    allowed = c.network.setdefault("allowed_client_ips", [])
    for entry in re.split(r"[;\s]+", value.strip()):
        if not entry:
            continue
        c.access_given = True
        if entry.startswith("!"):
            c.note(_("remaddr {0}: connect-back addresses are not imported").format(entry))
            continue
        fields = [f.strip() for f in entry.split(",") if f.strip()]
        while fields and fields[0].lower() in ("ipv4", "ipv6", "ipv6n4", "tcp", "udp", "sctp"):
            fields.pop(0)
        host = fields[0] if fields else ""
        try:
            ipaddress.ip_network(host, strict=False)
        except ValueError:
            c.note(_("remaddr {0}: only IP addresses and subnets can be imported").format(entry))
            continue
        if len(fields) > 1 and fields[1] != "0":
            c.note(_("remaddr {0}: only the client address is checked here, not its port")
                   .format(entry), security=True)
        if host not in allowed:
            allowed.append(host)


def _default_remaddr(c: _Conn, value: str) -> None:
    """The default remaddr, for a connection that gives no client address itself."""
    if value.strip() and not c.access_given:
        c.budget.spend(len(value))
        _remaddr(c, value)
        if c.access_given:
            c.note(_("remaddr {0} comes from the defaults (the connection sets none)")
                   .format(value.strip()))


def _finish(c: _Conn) -> None:
    """Resolve what depends on the whole connection; collect the warnings."""
    net = c.network
    if c.rtscts and c.xonxoff:
        c.note(_("both RTS/CTS and XON/XOFF are on: only RTS/CTS is imported (one flow control "
                 "per port here)"))
    c.serial["flowcontrol"] = "rtscts" if c.rtscts else "xonxoff" if c.xonxoff else "none"
    # keeping other programs off the port, as ser2net did: 4.x (gensio) flock = flock plus
    # TIOCEXCL, uucplock = UUCP lock files, both on unless turned off; 3.x used UUCP lock
    # files only (unless started with -u, which the file doesn't say)
    if c.v3:
        c.advanced["respect_uucp_locks"] = True
    else:
        c.serial["exclusive"] = c.flock
        c.advanced["tiocexcl"] = c.flock
        c.advanced["respect_uucp_locks"] = c.uucplock
    if net["mode"] == "udp" and net["protocol"] != "raw":
        c.note(_("{0} over UDP is not supported here: imported as raw UDP").format(net['protocol']))
        net["protocol"] = "raw"
    if net["protocol"] == "rfc2217" and net.get("max_connections", 1) > 1:
        c.note(_("RFC2217 allows a single client here: max-connections set to 1"))
        net["max_connections"] = 1
    for key in ("banner", "openstr", "closestr", "closeon"):
        if key in c.strings:
            high: list[str] = []
            c.options[key] = _convert(c, c.strings[key], "banner" if key == "banner" else "bytes",
                                      key, high)
            if high:
                _high_bytes(c, key, " ".join(dict.fromkeys(high)))
    if "signature" in c.strings and net["protocol"] == "rfc2217":  # (only RFC 2217 sends it)
        sig = _convert(c, c.strings["signature"], "bytes", "signature")
        if len(sig) > RFC2217_SIGNATURE_MAX or re.search(r"[\x00-\x1f\x7f]", sig):
            c.note(_("the RFC 2217 signature is not imported: here it is text of at most {0} "
                     "characters, without control characters").format(RFC2217_SIGNATURE_MAX))
        else:
            c.options["rfc2217_signature"] = sig
    for flag, name in (("hangup_when_done", "hangup-when-done"), ("nobreak", "nobreak")):
        if c.advanced.get(flag):
            # kept (the form keeps it too) but not acted on: the port stays open between
            # connections, so it is never closed (DTR dropped) or opened (BREAK cleared)
            # for one
            c.note(_("{0} is kept in the mapping but has no effect here: the serial port "
                     "stays open between connections").format(name.upper().replace("-", "_")
                                                              if c.v3 else name))
    if c.options.get("openstr"):
        c.note(_("openstr is sent once, when the serial port opens (at the mapping's start, "
                 "and when it is opened again after the device was lost), not each time a "
                 "client connects"))
    if (start := _privileged_below(net)) is not None:
        c.note(_("port {0} is below {1}: as ser2net runs now, it can't listen there (Linux "
                 "needs the CAP_NET_BIND_SERVICE capability: README, Ports below 1024); give "
                 "it the capability or change the port before enabling the mapping")
               .format(net.get("port"), start))
    labels = ({"both": "tb", "read": "tr", "write": "tw"} if c.v3 else
              {k: f"trace-{k}" for k in ("both", "read", "write")})
    kind = next((k for k in ("both", "read", "write") if k in c.traces), "")
    if kind == "read" and "write" not in c.traces:
        # only the device's output was traced; a trace here records the clients' input too
        c.note(_("{0} is not imported: it traced only the device's output, and a trace here "
                 "also records what clients send (passwords too); set a trace file on the "
                 "mapping if you want that").format(labels["read"]))
        kind = ""
    if kind:
        path = _convert(c, c.traces[kind], "file", labels[kind])
        name = trace_file_name(path)
        if name:
            c.options["trace_both"] = path
            if kind != "both":
                c.note(_("{0}: both directions are traced into one file here").format(labels[kind]))
            if name != path:
                c.note(_("trace file: written as traces/<mapping id>-{0} in the data directory "
                         "({1} is not used)").format(name, path))
        for other in c.traces:
            if other != kind:
                c.note(_("{0} is not imported: one trace file per mapping").format(labels[other]))
        for mod in ("hexdump", "timestamp"):
            b = c.trace_mods.get((kind, mod))
            if b is not None:
                c.options[f"trace_{mod}"] = b
    _tls(c)
    if c.access_given and not net.get("allowed_client_ips"):
        c.hold(_("remaddr could not be imported: the mapping is imported disabled rather "
                 "than open to every client"))
    if c.holds:
        c.enabled = False
        for why in c.holds:
            c.note(why, security=True)
    if c.dropped:
        c.note(_("not imported (no equivalent here): {0}").format(", ".join(c.dropped)))


def _privileged_below(net: dict, platform: Optional[str] = None) -> Optional[int]:
    """The first port anyone may listen on (bindhint.unprivileged_port_start), when the
    listening mapping `net` wants a port below it that this process may not use (Linux,
    not root, without CAP_NET_BIND_SERVICE: the systemd unit's account); else None."""
    platform = sys.platform if platform is None else platform
    port = net.get("port")
    if (not platform.startswith("linux") or net.get("mode") not in ("server", "udp")
            or not isinstance(port, int)):
        return None
    start = bindhint.unprivileged_port_start()
    if not 0 < port < start or bindhint.may_bind_low_ports():
        return None
    return start


def _high_bytes(c: _Conn, key: str, escapes: str) -> None:
    """An escape for a byte of 0x80 or more: a string here is sent as UTF-8, so that byte
    goes out as two. The device would get other bytes from an open or close string: the
    mapping comes in disabled; a close-on string then never matches (or a banner shows
    otherwise): a note."""
    if key in ("openstr", "closestr"):
        c.hold(_("{0}: {1} is a byte of 0x80 or more, which would be sent here as two bytes "
                 "(strings are sent as UTF-8): the mapping is imported disabled rather than "
                 "sending the device other bytes; change the string, then enable it")
               .format(key, escapes))
    elif key == "closeon":
        c.note(_("closeon: {0} is a byte of 0x80 or more, which is matched here as two bytes "
                 "(strings are sent as UTF-8), so this close-on string won't match what the "
                 "device sends; change it on the mapping").format(escapes))
    else:
        c.note(_("banner: {0} is a byte of 0x80 or more, which is sent here as two bytes "
                 "(strings are sent as UTF-8)").format(escapes))


class Note(str):
    """A warning of the import, as parse_ser2net_config returns them (a str). `mapping`: the
    index of the mapping it is about in the list returned with it (None: about the file,
    or about a connection that is not imported); `detail`: the text without the name of
    the connection in front; `security`: it says why the mapping comes in disabled (it
    would be more open here than in ser2net) or which limit on its clients is lost."""
    mapping: Optional[int] = None
    detail: str = ""
    security: bool = False
    conn: Optional[_Conn] = None  # until the mappings are known (_point_notes)


def _note(c: _Conn, detail: str, security: bool = False) -> Note:
    n = Note(f"{c.label}: {detail}")
    n.detail, n.security, n.conn = detail, security, c
    return n


def _point_notes(warnings: list[str], kept: list[_Conn]) -> None:
    """Point each connection's note at its mapping (Note.mapping), by position in `kept`."""
    index = {id(c): i for i, c in enumerate(kept)}
    for w in warnings:
        if isinstance(w, Note) and w.conn is not None:
            w.mapping, w.conn = index.get(id(w.conn)), None


def _notes(c: _Conn, imported: bool) -> list[str]:
    """A connection's warnings; for a skipped one only why it was skipped."""
    if not imported:
        return [f"{c.label}: {n}" for n in c.skipped]
    return [_note(c, n, n in c.flagged) for n in c.notes]


# ---------------------------------------------------------------------------
# ser2net 4.x (ser2net.yaml)
# ---------------------------------------------------------------------------
_V1_KEYS = {"connection", "define", "default", "delete_default", "rotator", "led", "admin"}
_V2_KEYS = {"connections", "defines", "defaults", "delete_defaults", "rotators", "leds",
            "admins"}
_TOP_KEY = re.compile(r"^(?:connections?|defines?|defaults?|delete_defaults?|rotators?|leds?|"
                      r"admins?|include)[ \t]*:(?:[ \t]|$)", re.I | re.M)
_SERIAL_DEFAULTS = {"speed", "nobreak", "rtscts", "xonxoff", "local", "hangup-when-done",
                    "dtr", "rts", "custspeed", "rs485", "uucplock", "flock", "wronly"}
_OPTION_DEFAULTS = {"kickolduser", "max-connections", "remaddr", "telnet-brk-on-sync",
                    "chardelay", "trace-hexdump", "trace-timestamp", "trace-both-hexdump",
                    "trace-both-timestamp", "signature", "banner", "openstr", "closestr",
                    "closeon"}
# (sent as they are: their spaces and line breaks too; the RFC 2217 signature is stripped)
_STRING_OPTIONS = ("banner", "openstr", "closestr", "closeon")
_SSL_DEFAULTS = {"key", "cert", "ca", "clientauth"}
# the gensio class that reads each default used here (it reads the base value, set
# without a class, too)
_DEFAULT_CLASS = {**dict.fromkeys(_SERIAL_DEFAULTS, "serialdev"),
                  **dict.fromkeys(_OPTION_DEFAULTS, "ser2net"),
                  **dict.fromkeys(_SSL_DEFAULTS, "ssl"), "rfc2217": "telnet"}


def _gensio_class(cls: str) -> str:
    c = cls.strip().lower()
    return "serialdev" if c == "sdev" else c


_REFERENCE = {act: re.compile("[" + re.escape(act) + "]") for act in ("*", "=", "*=")}


def _substitute(s: str, aliases: dict[str, str], act: str, note: Callable[[str], None],
                what: str) -> str:
    """ser2net's references inside strings: *(name) in version 1 files, =(name) in
    version 2 ('**' / '*(*' for the literal characters); *{file} / ={file} (a file on
    the ser2net host) cannot be read here. Nothing else is replaced ($(name) neither, as
    in ser2net). One pass over s (each bracket is searched for once); a value that grows
    past _MAX_VALUE refuses the file."""
    out: list[str] = []
    grown, i = 0, 0
    found = {")": -1, "}": -1}  # where that bracket was found last (len(s): none is left)
    special = _REFERENCE[act]
    while i < len(s):
        m = special.search(s, i)
        if m is None:
            out.append(s[i:])
            break
        if m.start() > i:
            out.append(s[i:m.start()])
            i = m.start()
        ch, nxt = s[i], s[i + 1:i + 2]
        if nxt == ch:
            out.append(ch)
            i += 2
            continue
        if nxt in ("(", "{"):
            if s[i + 2:i + 3] == ch:
                out.append(ch + nxt)
                i += 3
                continue
            closing = ")" if nxt == "(" else "}"
            if found[closing] < i + 2:
                at = s.find(closing, i + 2)
                found[closing] = len(s) if at < 0 else at
            close = found[closing]
            if close < len(s):
                ref = s[i + 2:close]
                if nxt == "{":
                    note(_("{0}{{{1}}} (a file on the ser2net host) is left out")
                         .format(ch, ref))
                    i = close + 1
                    continue
                if ref in aliases:
                    grown += len(aliases[ref])
                    if grown > _MAX_VALUE:
                        raise _too_long(what)
                    out.append(aliases[ref])
                    i = close + 1
                    continue
                # the name runs to the first ')', as ser2net reads it
                note(_("unknown reference {0}({1}) left as it is").format(ch, ref))
                out.append(s[i:close + 1])
                i = close + 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


class _Yaml:
    def __init__(self, act: str, budget: _Budget) -> None:
        self.act = act                       # reference character(s): * (v1), = (v2)
        self.budget = budget
        self.aliases: dict[str, str] = {}
        self.warnings: list[str] = []
        self.conns: list[_Conn] = []
        self.connections = 0                 # connection entries read (MAX_CONNECTIONS)
        self.defaults: dict[tuple[str, str], str] = {}  # (name, class) -> value, so far
        # what each gensio class gets from them for the connections from here on
        self.serial_defaults: dict[str, str] = {}
        self.option_defaults: dict[str, str] = {}
        self.ssl_defaults: dict[str, str] = {}   # key / cert / ca / clientauth of the ssl layer
        self.telnet_defaults: dict[str, str] = {}  # rfc2217 of the telnet layer
        self._views = {"serialdev": self.serial_defaults, "ser2net": self.option_defaults,
                       "ssl": self.ssl_defaults, "telnet": self.telnet_defaults}

    def text(self, node: Any, note: Callable[[str], None], what: str) -> str:
        if isinstance(node, (_YMap, list)):
            note(_("{0} is not a single value, ignored").format(what))
            return ""
        s = str(node)
        self.budget.spend(len(s))
        out = _substitute(s, self.aliases, self.act, note, what)
        if len(out) > _MAX_VALUE:
            raise _too_long(what)
        self.budget.spend(len(out))
        return out

    def gtext(self, node: Any, what: str) -> str:
        """text() outside a connection: notes become warnings about `what`."""
        def note(msg: str) -> None:
            self.budget.spend(len(what) + len(msg))
            self.warnings.append(f"{what}: {msg}")
        return self.text(node, note, what)

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
            self.delete_default(*(self.gtext(value.get(f, ""), "delete_default")
                                  for f in ("name", "class")))
        elif k == "delete_defaults" and isinstance(value, _YMap):
            for name, classes, _a in value.entries:  # <name>: [<class>, ...]
                for cls in classes if isinstance(classes, list) else [classes]:
                    self.delete_default(name, self.gtext(cls, f"delete_defaults {name}"))
        elif k in ("rotator", "rotators"):
            names = ([anchor] if k == "rotator" else
                     [n for n, _v, _a in value.entries] if isinstance(value, _YMap) else [])
            for name in names or ["(unnamed)"]:
                self.warnings.append(_("rotator {0}: not imported (rotators are not supported "
                                       "here)").format(name))
        elif k in ("admin", "admins"):
            self.warnings.append(_("the ser2net admin interface is not imported (the web UI "
                                   "and REST API take its place)"))
        elif k == "include":
            self.warnings.append(_("include {0}: included files are not read; import each of "
                                   "them as well").format(self.gtext(value, 'include')))
        elif k not in ("led", "leds", "defines", "connections", "defaults", "delete_default",
                       "delete_defaults", "default"):
            self.warnings.append(_("unknown top-level entry '{0}' ignored").format(key))

    def default(self, name: str, value: str, cls: str) -> None:
        """A default, for the connections after it (ser2net sets each connection up as it
        reads it). gensio keeps a value per class besides the base value (set without a
        class), and a class's own value wins over the base value whatever their order."""
        n, cls = name.strip().lower(), _gensio_class(cls)
        if not n:
            return
        reader = _DEFAULT_CLASS.get(n)
        if reader is None or cls not in ("", reader):
            if cls in ("", "serialdev", "ser2net"):  # (another gensio's: does not apply here)
                self.warnings.append(_("default {0}: {1} is not imported (no equivalent here)")
                                     .format(name, _shown(value)))
            self.defaults[(n, cls)] = value.strip()  # (for a delete_default of it)
            return
        if reader == "telnet" and _bool(value) is None:
            self.warnings.append(_("{0}: '{1}' is not true/false, ignored")
                                 .format(f"default {name}", _shown(value)))
            return
        # (a banner or open string as it is: its spaces and line breaks are sent)
        self.defaults[(n, cls)] = value if n in _STRING_OPTIONS else value.strip()
        self._resolve(n)

    def _resolve(self, n: str) -> None:
        """What the connections from here on get for default `n`: the value of the class
        that reads it, else the base value."""
        cls = _DEFAULT_CLASS[n]
        value = self.defaults.get((n, cls), self.defaults.get((n, "")))
        if value is None:
            self._views[cls].pop(n, None)
        else:
            self._views[cls][n] = value

    def delete_default(self, name: str, cls: str) -> None:
        """The value set for a class goes, so that class gets the base value again. ser2net
        refuses a file whose delete_default gives no class (a base value can't be
        deleted) or names a value that is not set: reported here, nothing deleted."""
        n, cls = name.strip().lower(), _gensio_class(cls)
        if not n:
            return
        if not cls:
            self.warnings.append(_("delete_default {0}: no class is given, and ser2net refuses "
                                   "such a file (a default set without a class can't be "
                                   "deleted); nothing is deleted").format(name))
        elif self.defaults.pop((n, cls), None) is None:
            self.warnings.append(_("delete_default {0}: no default of class {1} is set, and "
                                   "ser2net refuses such a file; nothing is deleted")
                                 .format(name, cls))
        elif n in _DEFAULT_CLASS:
            self._resolve(n)

    def connection(self, name: str, body: Any) -> None:
        self.connections += 1
        if self.connections > MAX_CONNECTIONS:
            raise _too_many_connections()
        if name:
            self.aliases[name] = name  # ser2net: *(con0) is the connection's name
        c = _Conn(name=name, label=name or _("a connection without a name"), budget=self.budget)
        if not isinstance(body, _YMap):
            c.skip(_("not a mapping"))
        else:
            accepter = self.text(body.get("accepter", ""), c.note, "accepter").strip()
            connector = self.text(body.get("connector", ""), c.note, "connector").strip()
            if not accepter or not connector:
                c.skip(_("it needs both an accepter and a connector"))
            elif _accepter(c, accepter, self.ssl_defaults,
                           _bool(self.telnet_defaults.get("rfc2217", ""))):
                if not name:
                    c.name = c.label = f"ser2net-{c.network['port']}"
                defaults = [f"{k}={v}" for k, v in self.serial_defaults.items()]
                self.budget.spend(sum(map(len, defaults)))
                if _connector(c, connector, defaults):
                    self.settings(c, body)
                    _finish(c)
                    self.conns.append(c)
        self.warnings.extend(_notes(c, imported=bool(self.conns) and self.conns[-1] is c))

    def settings(self, c: _Conn, body: _YMap) -> None:
        enable = self.text(body.get("enable", "on"), c.note, "enable")
        b = _bool(enable)
        if b is None:
            c.note(_("enable '{0}' is not on/off: imported disabled").format(enable))
        elif not b:
            c.note(_("imported disabled: the file turns it off (enable: off)"))
        c.enabled = bool(b)
        for k, v in self.option_defaults.items():
            if k != "remaddr":  # see below
                self.budget.spend(len(v))
                _option(c, k, v)
        for key, value, _a in body.entries:
            k = key.lower()
            if k == "options":
                if isinstance(value, _YMap):
                    for okey, oval, _oa in value.entries:
                        _option(c, okey, self.text(oval, c.note, okey))
                elif isinstance(value, list) or str(value).strip():
                    c.note(_("options is not a mapping, ignored"))
            elif k not in ("accepter", "connector", "enable"):
                _option(c, key, self.text(value, c.note, key))  # timeout (and stray options)
        # ser2net takes the default remaddr only for a connection that gives no address
        # itself (an empty remaddr gives none); it never adds the two up
        _default_remaddr(c, self.option_defaults.get("remaddr", ""))


def _from_yaml(text: str, label: str, budget: _Budget) -> tuple[list[_Conn], list[str]]:
    try:
        docs = _YamlReader(text).documents()
    except _YamlError as e:
        raise ConfigError(f"{label}: {e}") from None
    except RecursionError:  # the reader limits the nesting (_MAX_DEPTH); this only backs it up
        raise ConfigError(_("{0}: nested too deeply to import").format(label)) from None
    entries = [e for d in docs if isinstance(d, _YMap) for e in d.entries]
    keys = {k.lower() for k, _v, _a in entries}
    v1, v2 = bool(keys & _V1_KEYS), bool(keys & _V2_KEYS)
    if not (v1 or v2):
        raise ConfigError(_("{0} is not a ser2net configuration: it has no connection, define "
                            "or default entries.").format(label))
    y = _Yaml("=" if v2 and not v1 else "*=" if v2 else "*", budget)
    if v1 and v2:
        y.warnings.append(_("the file mixes ser2net version 1 and version 2 YAML entries"))
    for key, value, anchor in entries:
        y.entry(key, value, anchor)
    return y.conns, y.warnings


# ---------------------------------------------------------------------------
# ser2net 3.x (ser2net.conf)
# ---------------------------------------------------------------------------
_V3_STATE = re.compile(r":(rawlp|raw|telnet|off):")
_V3_DIRECTIVE = re.compile(r"(BANNER|SIGNATURE|OPENSTR|CLOSESTR|CLOSEON|DEVICE|TRACEFILE|"
                           r"CONTROLPORT|RS485CONF|DEFAULT|ROTATOR|LED):")
_V3_PARITY = {**_PARITY_WORDS, **{k.lower(): v for k, v in _PARITY_WORDS.items()}}
_V3_TRACE = {"tr": "read", "tw": "write", "tb": "both"}
_V3_IGNORED_DEFAULTS = ("local", "chardelay", "telnet_brk_on_sync")


def _v3_lines(text: str) -> list[tuple[int, str]]:
    """Logical lines (line number, text): blank and '#' lines dropped, a trailing
    backslash continues the line on the next one (as ser2net 3.x reads them). A line
    longer than _MAX_VALUE refuses the file."""
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    size, start = 0, 0  # size: characters in buf
    for n, line in enumerate(text.split("\n"), 1):
        if not size:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            start, line, buf = n, line.lstrip(), []
        if size + len(line) > _MAX_VALUE:
            raise ConfigError(_("line {0} is longer than {1} KB, so the file is not imported.")
                              .format(start, _MAX_VALUE // 1024))
        if line.endswith("\\"):
            buf.append(line[:-1])
            size += len(line) - 1
            continue
        out.append((start, "".join(buf) + line))
        buf, size = [], 0
    if size:
        out.append((start, "".join(buf)))
    return out


class _V3:
    def __init__(self, budget: _Budget) -> None:
        self.budget = budget
        self.connections = 0                           # port lines read (MAX_CONNECTIONS)
        self.strings: dict[str, tuple[str, str]] = {}  # name -> (BANNER/OPENSTR/..., text)
        self.devices: dict[str, str] = {}
        self.tracefiles: dict[str, str] = {}
        self.defaults: dict[str, str] = {}
        self.warnings: list[str] = []
        self.conns: list[_Conn] = []

    def directive(self, n: int, kind: str, rest: str) -> None:
        name, _sep, value = rest.partition(":")
        if kind == "CONTROLPORT":
            self.warnings.append(_("line {0}: CONTROLPORT {1} is not imported (the web UI and "
                                   "REST API take its place)").format(n, rest.strip()))
        elif kind == "DEFAULT":
            if name in _V3_IGNORED_DEFAULTS or name in (
                    "speed", "databits", "stopbits", "parity", "xonxoff", "rtscts",
                    "hangup_when_done", "nobreak", "remctl", "kickolduser", "max-connections",
                    "remaddr"):
                self.defaults[name] = value.strip()
            else:
                self.warnings.append(_("line {0}: DEFAULT {1} is not imported").format(n, name))
        elif kind == "DEVICE":
            self.devices[name] = value.strip()
        elif kind == "TRACEFILE":
            self.tracefiles[name] = value.strip()
        elif kind in ("LED", "ROTATOR"):
            what = {"LED": _("LEDs"), "ROTATOR": _("rotators")}
            self.warnings.append(_("line {0}: {1} {2} is not imported ({3} are not supported "
                                   "here)").format(n, kind, name, what[kind]))
            self.strings[name] = (kind, value)
        else:  # BANNER OPENSTR CLOSESTR CLOSEON SIGNATURE RS485CONF
            self.strings[name] = (kind, value)

    def port(self, n: int, line: str, m: re.Match[str]) -> None:
        self.connections += 1
        if self.connections > MAX_CONNECTIONS:
            raise _too_many_connections()
        state = m.group(1)
        timeout, _sep, rest = line[m.end():].partition(":")
        device, _sep, options = rest.partition(":")
        written = device.strip()
        device = self.devices.get(written, written)
        self.budget.spend(len(device))  # a DEVICE: name repeats its path
        c = _Conn(name="", label=_("line {0}").format(n), budget=self.budget, v3=True,
                  devname=written)
        fields = []
        for f in line[: m.start()].split(","):
            if f.strip().lower() == "udp":
                c.network["mode"] = "udp"
            elif f.strip().lower() != "tcp":
                fields.append(f)
        if _listen_address(c, fields):
            port = c.network["port"]
            c.name = f"ser2net-udp-{port}" if c.network["mode"] == "udp" else f"ser2net-{port}"
            c.label = _("{0} (line {1})").format(c.name, n)
            if not device:
                c.skip(_("no device"))
            elif url_port(device):  # e.g. a DEVICE alias; a pyserial URL is never opened here
                c.skip(_("device {0} is a URL, not a local serial port").format(device))
            elif device.startswith("sol.") or " " in device:
                c.skip(_("IPMI serial-over-LAN connections are not imported"))
            else:
                self.settings(c, state, timeout, device, options)
                _finish(c)
                self.conns.append(c)
        self.warnings.extend(_notes(c, imported=bool(self.conns) and self.conns[-1] is c))

    def settings(self, c: _Conn, state: str, timeout: str, device: str, options: str) -> None:
        c.device = device
        c.enabled = state != "off"
        if not c.enabled:
            c.note(_("imported disabled: the file turns it off (state off)"))
        c.network["protocol"] = "telnet" if state == "telnet" else "raw"
        if state == "rawlp":  # a printer port: ser2net opens it write-only
            _write_only(c, "rawlp")
        _idle_timeout(c, timeout)
        for name, value in self.defaults.items():
            if name != "remaddr":  # see below
                self.budget.spend(len(value))
                self.default(c, name, value)
        for tok in options.split():
            self.token(c, tok)
        if not c.remaddr_set:  # ser2net 3.x: the default only for a port without remaddr=
            _default_remaddr(c, self.defaults.get("remaddr", ""))
        if c.remctl:
            if state == "telnet":
                c.network["protocol"] = "rfc2217"
            else:
                c.note(_("remctl (RFC2217) needs a telnet port: ignored"))

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
            if kind in ("BANNER", "OPENSTR", "CLOSESTR", "CLOSEON", "SIGNATURE"):
                c.strings[kind.lower()] = value
            elif kind == "RS485CONF":
                c.note(_("RS485CONF {0} is not imported: set RS-485 in the mapping's serial "
                         "settings").format(tok))
            return  # LED: reported where it is defined
        if not self.serial(c, tok.upper()):
            c.note(_("unknown option '{0}' ignored").format(tok))

    @staticmethod
    def serial(c: _Conn, tok: str) -> bool:
        """The serial keywords (case as ser2net 3.x spells them)."""
        base, on = tok.lstrip("-"), not tok.startswith("-")
        if tok.isdigit():
            c.serial["baudrate"] = int(tok)
        elif _serial_word(c, tok):
            pass
        elif tok in _V3_PARITY:  # (in small letters too)
            c.serial["parity"] = _V3_PARITY[tok]
        elif base in ("XONXOFF", "RTSCTS"):
            setattr(c, base.lower(), on)
        elif base in ("HANGUP_WHEN_DONE", "NOBREAK"):
            c.advanced[base.lower()] = on
        elif base == "LOCAL":
            if not on:
                c.note(_("-LOCAL (watching the modem control lines) is not imported"))
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
                _trace_mod(c, _V3_TRACE.get(mod.group(1) or "", "all"), mod.group(2), on)
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
                c.note(_("{0}: there is no TRACEFILE named {1}").format(tok, value))
        elif name == "rs485":
            c.note(_("{0} is not imported: set RS-485 in the mapping's serial settings")
                   .format(tok))
        elif name == "max-connections":
            _option(c, name, value)
        elif name == "remaddr":
            c.remaddr_set = True
            _remaddr(c, value)
        elif name in ("chardelay-scale", "chardelay-min", "chardelay-max", "dev-to-net-bufsize",
                      "net-to-dev-bufsize", "dev-to-tcp-bufsize", "tcp-to-dev-bufsize"):
            c.drop(name)
        elif name not in ("led-rx", "led-tx"):  # LEDs: reported where they are defined
            return False
        return True


def _from_v3(lines: list[tuple[int, str]], budget: _Budget) -> tuple[list[_Conn], list[str]]:
    p = _V3(budget)
    for n, line in lines:
        d = _V3_DIRECTIVE.match(line)
        m = _V3_STATE.search(line)
        if d is not None:
            p.directive(n, d.group(1), line[d.end():])
        elif m is not None:
            p.port(n, line, m)
        else:
            p.warnings.append(_("line {0}: not a ser2net.conf line, ignored").format(n))
    return p.conns, p.warnings


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def _assemble(conns: list[_Conn], warnings: list[str], budget: _Budget) -> list[_Conn]:
    """Drop what does not validate or listens where an earlier one does, make the names
    unique and keep one active mapping per serial device: the connections to import."""
    def warn(msg: str) -> None:
        budget.spend(len(msg))
        warnings.append(msg)

    def warn_for(c: _Conn, msg: str) -> None:  # "<label>: ..." about one that is imported
        warn(_note(c, msg.removeprefix(f"{c.label}: ")))
    kept: list[_Conn] = []
    listening: dict[tuple[str, int], dict[str, str]] = {}  # (tcp|udp, port) -> bind IP -> name
    for c in conns:
        try:
            MappingConfig.from_dict(c.to_dict()).validate()
        except (TypeError, ValueError) as e:  # ConfigError is a ValueError
            warn(_("{0}: skipped: {1}").format(c.label, e))
            continue
        net = c.network
        proto = "udp" if net["mode"] == "udp" else "tcp"
        on_port = listening.setdefault((proto, net["port"]), {})
        other = next((name for ip, name in on_port.items()
                      if _ip_overlaps(ip, net["bind_ip"])), None)
        if other is not None:
            warn(_("{0}: skipped: {1} port {2} is already used by '{3}'")
                 .format(c.label, proto.upper(), net["port"], other))
            continue
        on_port[net["bind_ip"]] = c.name
        kept.append(c)
    used: set[str] = set()
    tried: dict[str, int] = {}  # name -> the last (k) tried for it: those before are taken
    owner: dict[str, str] = {}
    for c in kept:
        base = c.name
        k = tried.get(base, 1)
        while c.name in used:
            k += 1
            c.name = f"{base} ({k})"
        tried[base] = k
        used.add(c.name)
        if c.name != base:
            warn_for(c, _("{0}: renamed to '{1}' (the name is used twice)").format(c.label, c.name))
        key = canonical_device(c.device)  # (com3 and COM3 are one port)
        if c.enabled and key in owner:
            c.enabled = False
            warn_for(c, _("{0}: imported disabled: {1} is also used by '{2}', and a serial port can be "
                   "open in only one active mapping here")
                 .format(c.label, c.device, owner[key]))
        elif c.enabled:
            owner[key] = c.name
    return kept


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
    is neither, when the YAML is broken, or when the file is past one of the limits
    at the top of this module."""
    if len(text) > MAX_TEXT:
        raise ConfigError(_("The file is larger than {0} MB, too large for a ser2net "
                            "configuration.").format(MAX_TEXT // 2**20))
    budget = _Budget()
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    label = re.split(r"[\\/]", filename)[-1] if filename else ""
    if not text.strip():
        raise ConfigError(_("The file is empty."))
    if looks_yaml(text):
        conns, warnings = _from_yaml(text, label or "ser2net.yaml", budget)
    else:
        lines = _v3_lines(text)
        if not any(_V3_DIRECTIVE.match(ln) or _V3_STATE.search(ln) for _n, ln in lines):
            raise ConfigError(_("{0} is not a ser2net configuration: expected a ser2net.yaml "
                                "(ser2net 4.x) or ser2net.conf (ser2net 3.x) file.").format(
                                    label or _("The file")))
        conns, warnings = _from_v3(lines, budget)
    kept = _assemble(conns, warnings, budget)
    mappings = [c.to_dict() for c in kept]
    _point_notes(warnings, kept)
    if len(warnings) > _MAX_WARNINGS:
        more = len(warnings) - _MAX_WARNINGS
        del warnings[_MAX_WARNINGS:]
        warnings.append(_("… and {0} more warnings").format(more))
    if not mappings:
        warnings.append(_("No connection could be imported.") if conns else
                        _("The file has no ser2net connections to import."))
    if mappings:
        warnings.append(_("A mapping here opens its serial port when it starts and keeps it "
                          "open (ser2net opened it only while a client was connected), and "
                          "makes no UUCP lock files: programs that look for them, such as "
                          "minicom, don't see the port as in use"))
    wide = [c.name for c in kept if c.any_family]
    if wide:
        warnings.append(_("ser2net listened on IPv4 and IPv6 for an accepter that names no "
                          "address family or address ({0}): imported on 0.0.0.0, which takes "
                          "IPv4 clients only; pick “All interfaces (IPv4 + IPv6)” as the bind "
                          "IP to take IPv6 clients too").format(
            ", ".join(wide[:5]) + (", …" if len(wide) > 5 else "")))
    if any(_open_to_all(m) for m in mappings):
        warnings.append(_("ser2net may also have limited its clients with TCP wrappers "
                          "(/etc/hosts.allow, /etc/hosts.deny); those rules are not imported: a "
                          "mapping without allowed client IPs accepts any client"))
    return mappings, warnings


def _open_to_all(m: dict) -> bool:
    """An enabled mapping that listens beyond the loopback and accepts any client."""
    net = m["network"]
    return bool(m["enabled"] and not net.get("allowed_client_ips")
                and not ipaddress.ip_address(net["bind_ip"]).is_loopback)
