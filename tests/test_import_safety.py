"""Importing safely: a classic ser2net file never comes in more open than it was in
ser2net, and an import file can't tie up the service.

Checks:
  * a classic ser2net.yaml whose connection relies on a gtlssh login (certauth) shows
    the reason in the preview, and applying the import keeps that mapping disabled
    while the plain one next to it is added as usual;
  * the preview shows each mapping as it will be after the import (enabled or disabled,
    TLS, any client or how many allowed, read-only), its own warnings in its row (what
    it lost of its protection marked), and only the file's warnings as banners; applying
    it (from a Turkish page too) writes the warnings to all.log and a summary to the
    audit log, in English;
  * a classic file posted without the preview step (a 2.7 script's one-step import) is
    refused with a pointer to the preview, and nothing changes;
  * an import file with too many mappings, too large, or JSON nested too deeply is
    refused with a clear message;
  * an upload over the size limit is refused (413) before it is read; the configuration
    is unchanged and the server keeps working.

Run: python3 tests/test_import_safety.py
"""
import http.client
import json
import os
import re
import urllib.parse

from _server import Browser, Server, free_port

from app.config import ConfigError
from app.importer import MAX_FILE_BYTES, MAX_MAPPINGS, parse_mappings_file
from app.web.auth import hash_password

PW = "import-pass-1"


def mapping(i: int) -> dict:
    return {"id": f"{i:012x}", "name": f"m{i}", "enabled": False, "kind": "net",
            "serial": {"port": f"/dev/ser2net-test-{i}"},
            "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                        "port": 1 + i}}


def test_file_limits():
    many = json.dumps({"mappings": [mapping(i) for i in range(MAX_MAPPINGS + 1)]}).encode()
    for raw, want in ((many, f"at most {MAX_MAPPINGS}"),
                      (b'{"mappings": ' + b"[" * 100000 + b"]" * 100000 + b"}", "not valid JSON"),
                      (b" " * (MAX_FILE_BYTES + 1), "larger than 16 MB")):
        try:
            parse_mappings_file(raw)
        except ConfigError as e:
            assert want in str(e), (want, str(e)[:200])
        else:
            raise AssertionError(f"accepted ({want})")
    print("import files: too many mappings, too large or nested too deeply -> a clear error  OK")


def preview_rows(page: str) -> dict:
    """The import preview's rows: name -> {after: its 'After import' cell, notes: the
    <li> of its warnings row}."""
    body = page[page.index('class="grid import-preview'):]
    body = body[body.index("<tbody>"):body.index("</tbody>")]
    rows: dict = {}
    last = ""
    for tr in re.findall(r"<tr\b.*?</tr>", body, re.S):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", tr, re.S)
        if 'class="notes-row"' in tr:
            rows[last]["notes"] = re.findall(r"<li\b.*?</li>", cells[0], re.S)
        else:
            last = re.sub(r"<[^>]+>", "", cells[0]).strip()
            rows[last] = {"after": cells[2], "notes": []}
    return rows


def post_announcing(browser: Browser, path: str, size: int) -> int:
    """POST only the headers of a multipart upload of `size` bytes: a body over the
    route's limit must be refused (413) before any of it is read."""
    url = urllib.parse.urlsplit(browser.base)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=20)
    conn.putrequest("POST", path)
    conn.putheader("Cookie", "; ".join(f"{c.name}={c.value}" for c in browser.jar))
    conn.putheader("Content-Type", "multipart/form-data; boundary=s2nbig")
    conn.putheader("Content-Length", str(size))
    conn.endheaders()
    try:
        return conn.getresponse().status
    finally:
        conn.close()


def main():
    test_file_limits()
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
              "pwd_version": 1}]
    with Server({"users": users, "mappings": []}) as srv:
        admin = Browser(srv.base)
        admin.login("admin", PW)
        classic = ("connection: &plc\n"
                   f"  accepter: telnet(rfc2217),tcp,127.0.0.1,{free_port()}\n"
                   "  connector: serialdev,/dev/ser2net-test-missing,19200n81,local\n"
                   "connection: &console\n"   # behind a gtlssh login in ser2net
                   f"  accepter: telnet,mux,certauth,ssl,tcp,127.0.0.1,{free_port()}\n"
                   "  connector: serialdev,/dev/ser2net-test-missing2,9600n81\n").encode()
        st, _, page = admin.post("/settings/config/import", {"step": "preview", "mode": "merge"},
                                 files={"file": ("ser2net.yaml", classic)})
        assert st == 200 and "Import preview" in page, page[:300]
        rows = preview_rows(page)
        assert "enabled" in rows["plc"]["after"] and "any client" in rows["plc"]["after"]
        assert "tag-warn" not in rows["plc"]["after"], "on the loopback: not open to the network"
        assert "disabled" in rows["console"]["after"]
        note = rows["console"]["notes"][0]
        assert note.startswith('<li class="security">⚠ certauth (gtlssh login) is not available'), note
        banners = re.findall(r'<div class="banner warn">(.*?)</div>', page)
        assert not any("certauth" in b for b in banners), banners   # in its row, not on top
        token = page.split('name="token" value="', 1)[1].split('"', 1)[0]
        st, _, page = admin.post("/settings/config/import/apply", {"token": token, "mode": "merge"})
        assert "Imported: 2 added" in page, page[page.find("banner"):][:300]
        saved = {m["name"]: m for m in srv.saved_config()["mappings"]}
        assert saved["plc"]["enabled"] is True
        assert saved["console"]["enabled"] is False, "a connection whose login is lost must not start"
        print("classic import: the preview says why a connection is held, and applying it keeps "
              "that one disabled  OK")

        # the preview of a Turkish page: as each mapping will be; applied: logged in English
        tr = Browser(srv.base)
        tr.login("admin", PW)
        tr.get("/lang/tr?next=/settings")
        open_file = ("connection: &lab\n"
                     f"  accepter: telnet,tcp,{free_port()}\n"
                     "  connector: serialdev,/dev/ser2net-test-lab,9600n81\n"
                     "  options:\n"
                     "    remaddr: 10.1.0.0/16,5000;10.2.0.1\n"
                     "    no-acc-to-con: true\n"
                     "connection: &bench\n"
                     f"  accepter: tcp,{free_port()}\n"
                     "  connector: serialdev,/dev/ser2net-test-bench\n"
                     "  enable: off\n"
                     "  options:\n"
                     "    kickolduser: \"yes\\nFORGED log line\"\n"
                     "rotator: &r\n"
                     "  accepter: tcp,4999\n"
                     "  connections: [ lab ]\n").encode()
        st, _, page = tr.post("/settings/config/import", {"step": "preview", "mode": "merge"},
                              files={"file": ("ser2net.yaml", open_file)})
        assert st == 200 and 'lang="tr"' in page
        rows = preview_rows(page)
        lab = rows["lab"]
        assert "açık" in lab["after"] and "2 izinli adres" in lab["after"] and "RO" in lab["after"]
        assert lab["notes"] and "security" in lab["notes"][0] and "10.1.0.0/16,5000" in lab["notes"][0]
        assert "yalnızca adresi denetlenir" in lab["notes"][0], lab["notes"]  # the page's language
        assert "kapalı" in rows["bench"]["after"] and "enable: off" in rows["bench"]["notes"][0]
        banners = re.findall(r'<div class="banner warn">(.*?)</div>', page)
        assert any("rotator r" in b for b in banners), banners
        token = page.split('name="token" value="', 1)[1].split('"', 1)[0]
        tr.post("/settings/config/import/apply", {"token": token, "mode": "merge"})
        with open(os.path.join(srv.data, "all.log"), encoding="utf-8") as fh:
            log = fh.read()
        assert "mappings imported (merge) from ser2net.yaml (ser2net 4.x): 2 added" in log, log[-800:]
        assert ("import warning: lab: remaddr 10.1.0.0/16,5000: only the client address is "
                "checked here, not its port") in log
        assert "import warning: bench: imported disabled: the file turns it off" in log
        assert "\nFORGED" not in log and "'yes\\nFORGED log line' is not true/false" in log
        with open(os.path.join(srv.data, "audit.log"), encoding="utf-8") as fh:
            audit = [ln.split("\t") for ln in fh.read().split("\n") if ln]
        detail = next(r[3] for r in reversed(audit) if r[2] == "config_import_merge")
        # the audit record counts the warnings the log has, one line each (the four above
        # and the file's note on the port locking)
        last = log[log.rindex("mappings imported (merge) from ser2net.yaml"):]
        warned = last.split(" AUDIT ", 1)[0].count("import warning: ")
        assert warned >= 4 and detail.startswith(
            f"2 mappings (ser2net.yaml (ser2net 4.x)); {warned} warnings: "), (warned, detail)
        assert "rotator r: not imported" in detail and len(detail) <= 500
        print("preview: each mapping as it will be, its warnings in its row (protection first); "
              "applied from a Turkish page: warnings in all.log and the audit log in English  OK")

        # a 2.7 script's one-step import only ever sent JSON: a classic file goes through
        # the preview
        before = srv.saved_config()["mappings"]
        st, _, page = admin.post("/settings/config/import", {"mode": "replace"},
                                 files={"file": ("ser2net.yaml", classic)})
        assert st == 400 and "imported through the preview" in page, (st, page[:300])
        assert srv.saved_config()["mappings"] == before
        print("one-step import of a classic file: refused, pointing to the preview  OK")

        before = srv.saved_config()["mappings"]
        assert post_announcing(admin, "/settings/config/import", MAX_FILE_BYTES + 2**20) == 413
        assert srv.saved_config()["mappings"] == before and admin.get("/settings")[0] == 200
        print("an upload over the import size limit is refused (413) before it is read  OK")
    print("\nPASS: imports never open a connection wider, and oversized files are refused")


if __name__ == "__main__":
    main()
