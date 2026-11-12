"""A mapping's TLS files (app/engine/tls.py): its listener and its connect-out link.

  - key passwords: an encrypted key is decrypted with network.tls_key_password; without
    one, or with a wrong one, loading it fails at once with an error that says so (never a
    prompt on the terminal), at start and when the mapping is saved;
  - edits (the mapping form, the REST API) load the files and refuse what does not load;
    an import takes such a mapping with a warning; the form never shows the stored
    password: a blank field keeps it, the remove box clears it; a viewer API token gets it
    masked, and a PUT that sends the mask back keeps the stored one.
Test certificates are made here (_pki.py, needs cryptography: SKIP without). Listens on
127.0.0.1 only, on OS-assigned ports.

Run: python3 tests/test_tls_mapping.py
"""
import asyncio
import json
import os
import shutil
import ssl
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _optional import need
from _server import Browser, Server, free_port

from app import importer
from app.config import ConfigError, MappingConfig
from app.engine import tls
from app.engine.supervisor import make_runner
from app.web.auth import hash_password, hash_token

MISSING_PORT = "COM250" if sys.platform == "win32" else "/dev/ser2net-test-missing"
PASSWORD = "key-pass-1"
TOKEN = "s2n_tls-mapping-test"


class Pki:
    """A test CA, the certificates it issues (with plain and encrypted keys) in one folder."""

    def __init__(self, folder: str):
        from _pki import TestCA
        self.folder = folder
        self.ca = TestCA(folder)
        self.cert, self.key = self.ca.issue(folder, "srv", ["srv.test", "127.0.0.1"])
        self.locked = self.encrypted(self.key, PASSWORD)

    def encrypted(self, key_path: str, password: str) -> str:
        """A copy of the key at ``key_path``, encrypted with ``password``."""
        from cryptography.hazmat.primitives import serialization
        with open(key_path, "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), None)
        path = key_path[:-4] + "-locked.key"
        with open(path, "wb") as fh:
            fh.write(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.BestAvailableEncryption(password.encode())))
        return path


def mapping(**network) -> MappingConfig:
    net = {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1", "port": 4001,
           "tls": True}
    net.update(network)
    return MappingConfig.from_dict({"name": "tls", "serial": {"port": MISSING_PORT},
                                    "network": net})


def refused(m: MappingConfig) -> str:
    try:
        m.check_tls_files()
    except ConfigError as e:
        return str(e)
    raise AssertionError("check_tls_files accepted it")


def test_key_passwords(pki: Pki):
    started = time.monotonic()
    ctx = tls.tls_context(server_side=True, cert=pki.cert, key=pki.locked, password=PASSWORD)
    assert isinstance(ctx, ssl.SSLContext)
    tls.tls_context(server_side=True, cert=pki.cert, key=pki.key, password="not used")
    for password, kind in (("", "password"), ("wrong", "unusable")):
        try:
            tls.tls_context(server_side=True, cert=pki.cert, key=pki.locked, password=password)
        except tls.TlsFileError as e:
            assert e.kind == kind, (password, e.kind, e)
        else:
            raise AssertionError(f"loaded with password {password!r}")
    assert time.monotonic() - started < 5, "loading waited (for a terminal prompt?)"
    other_cert, _other_key = pki.ca.issue(pki.folder, "other", ["other.test"])
    for cert, key, kind in ((pki.cert, os.path.join(pki.folder, "gone.key"), "unreadable"),
                            (other_cert, pki.key, "unusable")):
        try:
            tls.tls_context(server_side=True, cert=cert, key=key)
        except tls.TlsFileError as e:
            assert e.kind == kind, (e.kind, e)
        else:
            raise AssertionError("loaded")

    assert "password-protected" in refused(mapping(tls_cert=pki.cert, tls_key=pki.locked))
    assert "Is the key password right" in refused(
        mapping(tls_cert=pki.cert, tls_key=pki.locked, tls_key_password="wrong"))
    assert "can't be read" in refused(mapping(tls_cert=pki.cert,
                                              tls_key=os.path.join(pki.folder, "gone.key")))
    mapping(tls_cert=pki.cert, tls_key=pki.locked, tls_key_password=PASSWORD).check_tls_files()
    mapping(tls=False, tls_cert="", tls_key="").check_tls_files()
    print("key passwords: decrypted with tls_key_password; none / a wrong one / a missing "
          "file fail at once with a clear error  OK")


def test_import_warns(pki: Pki):
    item = mapping(tls_cert=pki.cert, tls_key=pki.locked).to_dict()
    plan = importer.parse_mappings_file(json.dumps({"mappings": [item]}).encode())
    assert len(plan.mappings) == 1, plan
    assert any("password-protected" in w and "can't start until" in w for w in plan.warnings), \
        plan.warnings
    item["network"]["tls_key_password"] = PASSWORD
    plan = importer.parse_mappings_file(json.dumps({"mappings": [item]}).encode())
    assert not plan.warnings, plan.warnings
    print("import: a TLS mapping whose key does not load is taken, with a warning  OK")


def test_api_masks_the_password():
    from app.web import api
    m = mapping(tls_cert="/c.pem", tls_key="/k.pem", tls_key_password="secret")
    for role, shown in (("viewer", api.REDACTED), ("operator", "secret"), ("admin", "secret")):
        state = types.SimpleNamespace(config=types.SimpleNamespace(api_token_role=role),
                                      supervisor=types.SimpleNamespace(status=lambda _mid: {}))
        assert api._mapping_view(m, state)["network"]["tls_key_password"] == shown, role
    print("REST API: a viewer token gets the TLS key password masked  OK")


async def _handshake(port: int) -> str:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    _r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port, ssl=ctx), 10)
    version = w.get_extra_info("ssl_object").version()
    w.close()
    return version


async def _listener_starts(pki: Pki):
    port = free_port()
    logs: list[str] = []
    runner = make_runner(mapping(port=port, tls_cert=pki.cert, tls_key=pki.locked,
                                 tls_key_password=PASSWORD), logs.append)
    await runner.start()
    try:
        assert (await _handshake(port)).startswith("TLS"), logs
    finally:
        await runner.stop()
    runner = make_runner(mapping(port=port, tls_cert=pki.cert, tls_key=pki.locked), logs.append)
    started = time.monotonic()
    try:
        await runner.start()
    except tls.TlsFileError as e:
        assert e.kind == "password" and "password-protected" in str(e), e
    else:
        await runner.stop()
        raise AssertionError("a listener started with a key it can't decrypt")
    assert time.monotonic() - started < 5
    print("a listener with an encrypted key: starts with its password, fails at once with "
          "a clear error without one  OK")


def form(port: int, pki: Pki, **fields) -> dict:
    out = {"name": "tls-form", "kind": "net", "enabled": "on", "serial_port": MISSING_PORT,
           "network_mode": "server", "network_protocol": "raw", "network_bind_ip": "127.0.0.1",
           "network_port": str(port), "network_tls": "on", "network_tls_cert": pki.cert,
           "network_tls_key": pki.locked}
    out.update(fields)
    return out


def stored(srv: Server) -> dict:
    return next(m for m in srv.saved_config()["mappings"] if m["name"] == "tls-form")


def api(srv: Server, method: str, path: str, body=None):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(srv.base + "/api/v1" + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def test_form_and_api(pki: Pki):
    config = {"users": [{"username": "admin", "password_hash": hash_password("admin-pass-1"),
                         "role": "admin", "pwd_version": 1, "source": "local"}],
              "api_token_hash": hash_token(TOKEN), "api_token_role": "operator"}
    with Server(config) as srv:
        b = Browser(srv.base)
        b.login("admin", "admin-pass-1")
        port = free_port()
        st, _h, body = b.post("/api/mappings/save", form(port, pki))
        assert st == 400 and "password-protected" in body, (st, body[-1500:])
        st, _h, body = b.post("/api/mappings/save", form(port, pki, network_tls_key_password="x"))
        assert st == 400 and "Is the key password right" in body, (st, body[-1500:])
        assert 'value="x"' in body, "the password typed is lost on the re-drawn form"
        st, _h, body = b.post("/api/mappings/save",
                              form(port, pki, network_tls_key_password=PASSWORD))
        assert st == 200, (st, body[-1500:])
        m = stored(srv)
        assert m["network"]["tls_key_password"] == PASSWORD, m["network"]
        st, _h, page = b.get(f"/api/mappings/{m['id']}/form")
        assert st == 200 and PASSWORD not in page, "the stored password is in the edit form"
        assert 'name="network_tls_key_password_clear"' in page
        # a blank field keeps it (also a form without the field: an older node's)
        st, _h, body = b.post("/api/mappings/save", form(port, pki, id=m["id"], name="tls-form",
                                                         network_tls_key_password=""))
        assert st == 200 and stored(srv)["network"]["tls_key_password"] == PASSWORD, body[-800:]
        fields = form(port, pki, id=m["id"])
        st, _h, body = b.post("/api/mappings/save", fields)
        assert st == 200 and stored(srv)["network"]["tls_key_password"] == PASSWORD, body[-800:]
        # the remove box: refused while the key needs it, done with a plain key
        st, _h, body = b.post("/api/mappings/save", form(
            port, pki, id=m["id"], network_tls_key_password_clear="on"))
        assert st == 400 and "password-protected" in body, (st, body[-800:])
        st, _h, body = b.post("/api/mappings/save", form(
            port, pki, id=m["id"], network_tls_key=pki.key, network_tls_key_password_clear="on"))
        assert st == 200 and stored(srv)["network"]["tls_key_password"] == "", body[-800:]

        # REST API: refused when the key does not load; a PUT with the mask keeps the password
        st, created = api(srv, "POST", "/mappings", {
            "name": "tls-api", "serial": {"port": MISSING_PORT + "-2"},
            "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": free_port(),
                        "tls": True, "tls_cert": pki.cert, "tls_key": pki.locked}})
        assert st == 400 and "password-protected" in created["detail"], (st, created)
        body = {"name": "tls-api", "serial": {"port": MISSING_PORT + "-2"},
                "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": free_port(),
                            "tls": True, "tls_cert": pki.cert, "tls_key": pki.locked,
                            "tls_key_password": PASSWORD}}
        st, created = api(srv, "POST", "/mappings", body)
        assert st == 201, (st, created)
        body["network"]["tls_key_password"] = "********"
        st, updated = api(srv, "PUT", f"/mappings/{created['id']}", body)
        assert st == 200 and updated["network"]["tls_key_password"] == PASSWORD, (st, updated)
        assert "Traceback" not in srv.output()
    print("form and REST API: a key that does not load is refused; the form never shows the "
          "stored password, blank keeps it, the remove box clears it; a PUT with the mask "
          "keeps it  OK")


def main():
    if not need("cryptography", what="per-mapping TLS files (test certificates)"):
        test_api_masks_the_password()
        return
    tmp = tempfile.mkdtemp(prefix="s2n_tlsmap_")
    try:
        pki = Pki(tmp)
        test_key_passwords(pki)
        test_import_warns(pki)
        test_api_masks_the_password()
        asyncio.run(_listener_starts(pki))
        test_form_and_api(pki)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
