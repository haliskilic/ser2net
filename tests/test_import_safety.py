"""Importing safely: a classic ser2net file never comes in more open than it was in
ser2net.

Checks:
  * a classic ser2net.yaml whose connection relies on a gtlssh login (certauth) shows
    the reason in the preview, and applying the import keeps that mapping disabled
    while the plain one next to it is added as usual.

Run: python3 tests/test_import_safety.py
"""
from _server import Browser, Server, free_port

from app.web.auth import hash_password

PW = "import-pass-1"


def main():
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
    print("\nPASS: a classic import never opens a connection wider than it was")


if __name__ == "__main__":
    main()
