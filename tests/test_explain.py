"""What the common OS / network errors mean, in the page's language (app/explain.py).

Checks:
  * the raw texts the engine leaves in a mapping's status (pyserial on Linux and Windows,
    asyncio's bind and connect errors, the resolver) and exceptions (OSError, TimeoutError,
    urllib's wrapped ones) are told apart: serial device missing / no permission (dialout)
    / busy / gone, port in use, no such bind address, privileged port, connection refused,
    no answer, no route, unknown host name, certificate refused; anything else is None;
  * the sentences are Turkish in a Turkish request;
  * the dashboard row shows the sentence with the raw error as its tooltip, and an error
    it doesn't know as it is; a backup or an import that can't be read says where the
    JSON is wrong instead of the parser's English words.

Run: python3 tests/test_explain.py
"""
import json
import os

from _server import ROOT

from app import i18n, importer
from app.config import ConfigError, MappingConfig
from app.explain import explain
from app.web.routes.settings import _unreadable
from app.web.server import make_templates

TR = json.load(open(os.path.join(ROOT, "app", "locale", "tr.json"), encoding="utf-8"))
MISSING = "The serial device is not there: unplugged, or it has another name now."
DIALOUT = ("No permission to open the serial device: on Linux, add the user ser2net runs as "
           "to the dialout group; on Windows, another program may have the port open.")
IN_USE = "The port is already in use by another program (or another mapping)."
CASES = [
    ("[Errno 2] could not open port /dev/ttyUSB9: [Errno 2] No such file or directory: "
     "'/dev/ttyUSB9'", MISSING),
    ("could not open port 'COM3': FileNotFoundError(2, 'The system cannot find the file "
     "specified.', None, 2)", MISSING),
    ("could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)", DIALOUT),
    ("[Errno 13] could not open port /dev/ttyS0: [Errno 13] Permission denied: '/dev/ttyS0'",
     DIALOUT),
    ("[Errno 16] could not open port /dev/ttyUSB0: [Errno 16] Device or resource busy: "
     "'/dev/ttyUSB0'", "The serial device is busy: another program has it open."),
    ("Could not exclusively lock port /dev/ttyUSB0: [Errno 11] Resource temporarily unavailable",
     "The serial device is busy: another program has it open."),
    ("device reports readiness to read but returned no data (device disconnected or multiple "
     "access on port?)",
     "The serial device stopped answering: unplugged, or another program uses it too."),
    ("[Errno 98] error while attempting to bind on address ('0.0.0.0', 4001): address already "
     "in use", IN_USE),
    ("[Errno 10048] error while attempting to bind on address ('0.0.0.0', 4001): only one "
     "usage of each socket address (protocol/network address/port) is normally permitted", IN_USE),
    ("UDP discovery port 5000 unavailable: [Errno 98] Address already in use", IN_USE),
    ("[Errno 13] error while attempting to bind on address ('0.0.0.0', 80): permission denied",
     "No permission to listen on this port: ports below 1024 need extra rights."),
    ("[Errno 99] error while attempting to bind on address ('10.9.9.9', 4001): cannot assign "
     "requested address", "This machine has no such address to listen on: check the bind IP."),
    ("Multiple exceptions: [Errno 111] Connect call failed ('::1', 4001), [Errno 111] Connect "
     "call failed ('127.0.0.1', 4001)",
     "The connection was refused: nothing listens at that address and port."),
    ("<urlopen error [Errno 111] Connection refused>",
     "The connection was refused: nothing listens at that address and port."),
    ("[Errno -2] Name or service not known", "The host name could not be resolved (DNS)."),
    ("<urlopen error [Errno 11001] getaddrinfo failed>", "The host name could not be resolved (DNS)."),
    ("[Errno 113] Connect call failed ('10.1.1.1', 4001)",
     "The address can't be reached from here (no network route to it)."),
    ("timed out", "No answer in time."),
    ("<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get "
     "local issuer certificate (_ssl.c:1000)>", "The server's certificate was not accepted."),
    (TimeoutError(), "No answer in time."),
    (FileNotFoundError(2, "No such file or directory"), "The file or folder does not exist."),
    (PermissionError(13, "Permission denied"), "Permission denied."),
    ("could not open port /dev/serial/by-id/usb-FTDI", None),
    ("HTTP 500 Internal Server Error", None),
    ("", None),
]


def test_classes():
    bad = [(e, explain(e), want) for e, want in CASES if explain(e) != want]
    assert not bad, "\n".join(map(repr, bad))
    assert explain(FileNotFoundError(2, "x"), "serial") == MISSING
    tok = i18n.set_language("tr")
    try:
        assert explain(CASES[0][0]) == TR[MISSING] and explain(CASES[7][0]) == TR[IN_USE]
    finally:
        i18n.reset(tok)
    print(f"explain: {len(CASES)} raw errors told apart (Linux, Windows, resolver, TLS), "
          "Turkish in a Turkish request  OK")


def test_shown():
    tpl = make_templates().env.get_template("_mappings_body.html")
    maps = [MappingConfig.from_dict({"id": f"e{i}", "name": f"E{i}", "serial": {"port": "COM3"}})
            for i in (1, 2)]
    raw = CASES[7][0]
    statuses = {"e1": {"state": "error", "last_error": raw},
                "e2": {"state": "device-missing", "last_error": "something odd"}}
    tok = i18n.set_language("tr")
    try:
        out = tpl.render(mappings=maps, statuses=statuses, can_edit=False)
    finally:
        i18n.reset(tok)
    shown = f'title="{raw.replace(chr(39), "&#39;")}">{TR[IN_USE]}'   # (softbreak: <wbr> after a .)
    assert shown in out, out[out.find("err-small"):][:400]
    assert '>something odd</div>' in out, "an error it doesn't know: as it is"

    assert _unreadable(json.JSONDecodeError("Expecting ',' delimiter", '{"a": 1 "b"}', 9)) == \
        "it is not valid JSON (line 1, column 10)"
    assert _unreadable(AttributeError("'list' object has no attribute 'get'")) == \
        "it is not a ser2net configuration"
    assert _unreadable(FileNotFoundError(2, "No such file")) == "The file or folder does not exist."
    assert _unreadable(ConfigError("No such backup.")) == "No such backup."
    try:
        importer.parse_mappings_file(b'{"mappings": [\n  {"name": "x"}\n  {"name": "y"}]}')
    except ConfigError as e:
        assert str(e) == "The file is not valid JSON (line 3, column 3).", str(e)
    else:
        raise AssertionError("bad JSON accepted")
    print("shown: the dashboard row's sentence with the raw error as tooltip; backups and "
          "imports say where the JSON is wrong  OK")


def main():
    test_classes()
    test_shown()
    print("\nPASS: OS / network errors explained in the page's language")


if __name__ == "__main__":
    main()
