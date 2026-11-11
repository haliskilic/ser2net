"""Importing safely: a classic ser2net file never comes in more open than it was in
ser2net, and an import file can't tie up the service.

Checks:
  * a classic ser2net.yaml whose connection relies on a gtlssh login (certauth) shows
    the reason in the preview, and applying the import keeps that mapping disabled
    while the plain one next to it is added as usual;
  * an import file with too many mappings, too large, or JSON nested too deeply is
    refused with a clear message;
  * an upload over the size limit is refused (413) before it is read; the configuration
    is unchanged and the server keeps working.

Run: python3 tests/test_import_safety.py
"""
import http.client
import json
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
        assert "console: certauth (gtlssh login) is not available here" in page
        token = page.split('name="token" value="', 1)[1].split('"', 1)[0]
        st, _, page = admin.post("/settings/config/import/apply", {"token": token, "mode": "merge"})
        assert "Imported: 2 added" in page, page[page.find("banner"):][:300]
        saved = {m["name"]: m for m in srv.saved_config()["mappings"]}
        assert saved["plc"]["enabled"] is True
        assert saved["console"]["enabled"] is False, "a connection whose login is lost must not start"
        print("classic import: the preview says why a connection is held, and applying it keeps "
              "that one disabled  OK")

        before = srv.saved_config()["mappings"]
        assert post_announcing(admin, "/settings/config/import", MAX_FILE_BYTES + 2**20) == 413
        assert srv.saved_config()["mappings"] == before and admin.get("/settings")[0] == 200
        print("an upload over the import size limit is refused (413) before it is read  OK")
    print("\nPASS: imports never open a connection wider, and oversized files are refused")


if __name__ == "__main__":
    main()
