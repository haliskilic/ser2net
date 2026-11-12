"""A mapping's TLS files (app/engine/tls.py): its listener and its connect-out link.

  - key passwords: an encrypted key is decrypted with network.tls_key_password; without
    one, or with a wrong one, loading it fails at once with an error that says so (never a
    prompt on the terminal), at start and when the mapping is saved;
  - edits (the mapping form, the REST API) load the files and refuse what does not load;
    an import takes such a mapping with a warning; the form never shows the stored
    password: a blank field keeps it, the remove box clears it; a viewer API token gets it
    masked, and a PUT that sends the mask back keeps the stored one; a mapping can't be
    saved with the admin UI's own key file (a stored one keeps working);
  - connect-out: the server certificate's SHA-256 fingerprint is logged at every connect;
    with network.tls_ca it must chain to a certificate in that file (PEM or DER) and name
    remote_host, with network.tls_fingerprint be one it lists (else the link is not used);
    tls_cert/tls_key are optional, and presented to a server that asks for a client
    certificate;
  - listeners (the bridge and the Modbus gateway) with network.tls_client_ca: a client
    without a certificate from that CA file gets no connection (its handshake fails, which
    is logged), one with such a certificate does, and the log names its subject;
  - the admin UI's key the same way (admin_ui.tls_key_password, Settings → Admin TLS): it
    serves HTTPS with its password, and without one the start ends at once (exit 1, the
    reason in all.log), before any mapping starts.
Test certificates are made here (_pki.py, needs cryptography: SKIP without). Listens on
127.0.0.1 only, on OS-assigned ports.

Run: python3 tests/test_tls_mapping.py
"""
import asyncio
import contextlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import types
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _optional import need
from _server import ROOT, Browser, Server, free_port

from app import importer
from app.config import ConfigError, MappingConfig
from app.engine import serial_io, tls
from app.engine.bridge import MappingRunner
from app.engine.supervisor import make_runner
from app.web.auth import hash_password, hash_token

MISSING_PORT = "COM250" if sys.platform == "win32" else "/dev/ser2net-test-missing"
PASSWORD = "key-pass-1"
TOKEN = "s2n_tls-mapping-test"
USERS = [{"username": "admin", "password_hash": hash_password("admin-pass-1"),
          "role": "admin", "pwd_version": 1, "source": "local"}]


class Pki:
    """A test CA, the certificates it issues (with plain and encrypted keys) in one folder."""

    def __init__(self, folder: str):
        from _pki import TestCA
        self.folder = folder
        self.ca = TestCA(folder)
        self.cert, self.key = self.ca.issue(folder, "srv", ["srv.test", "127.0.0.1"])
        self.locked = self.encrypted(self.key, PASSWORD)
        self.ui_cert, self.ui_key = self.ca.issue(folder, "ui", ["ui.test", "127.0.0.1"])

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


def _unverified() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


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


def test_context_options(pki: Pki):
    """tls_context for other TLS clients: the system's CAs, no host name check, no check."""
    ctx = tls.tls_context(server_side=False)  # a mapping without a CA file: encryption only
    assert ctx.verify_mode == ssl.CERT_NONE and not ctx.check_hostname
    ctx = tls.tls_context(server_side=False, ca=pki.ca.path)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    ctx = tls.tls_context(server_side=False, verify=True)  # the system's CAs
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    ctx = tls.tls_context(server_side=False, ca=pki.ca.path, check_hostname=False)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and not ctx.check_hostname
    ctx = tls.tls_context(server_side=False, ca=pki.ca.path, verify=False)
    assert ctx.verify_mode == ssl.CERT_NONE and not ctx.cert_store_stats()["x509"]
    ctx = tls.tls_context(server_side=True, cert=pki.cert, key=pki.key, ca=pki.ca.path)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    print("tls_context: checks with a CA file or the system's CAs, with or without the host "
          "name, or none  OK")


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
    _r, w = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port, ssl=_unverified()), 10)
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


class TlsServer:
    """A TLS server on 127.0.0.1 (OS-assigned port) that asks for a client certificate from
    ``client_ca`` (if given) and records each client's certificate subject."""

    def __init__(self, cert: str, key: str, client_ca: str = ""):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        if client_ca:
            self.ctx.verify_mode = ssl.CERT_REQUIRED
            self.ctx.load_verify_locations(client_ca)
            # TLS 1.2: a client without a certificate fails in its handshake (with TLS 1.3
            # its handshake is over first, and the server's alert ends the link after)
            self.ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        self.subjects: list[str] = []

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0, ssl=self.ctx)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def _client(self, reader, writer):
        cert = writer.get_extra_info("peercert") or {}
        self.subjects.append(", ".join(f"{k}={v}" for rdn in cert.get("subject", ())
                                       for k, v in rdn) or "-")
        with contextlib.suppress(OSError):  # (a client that drops the link: a reset)
            await reader.read()  # until the link closes
        writer.close()


def client_mapping(port: int, **network) -> MappingConfig:
    net = {"mode": "client", "protocol": "raw", "remote_host": "127.0.0.1",
           "remote_port": port, "connect_timeout_s": 3, "tls": True}
    net.update(network)
    return MappingConfig.from_dict({"name": "uplink", "serial": {"port": MISSING_PORT},
                                    "network": net})


async def _port_open(settings):
    await asyncio.Event().wait()  # the serial port "opens" and stays open


async def _until(check, timeout=10.0):
    end = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < end, "timed out"
        await asyncio.sleep(0.05)


async def _link(m: MappingConfig, logs: list) -> dict:
    """Start a connect-out runner until its link is up or down: its status."""
    runner = MappingRunner(m, logger=logs.append)
    with mock.patch.object(serial_io, "open_serial", _port_open):
        await runner.start()
        try:
            await _until(lambda: runner.status.link in ("up", "down"))
            return runner.status.as_dict()
        finally:
            await runner.stop()


async def _connect_out(pki: Pki):
    der = ssl.PEM_cert_to_DER_cert(open(pki.cert, encoding="ascii").read())
    seen = tls.fingerprint(der)
    client_cert, client_key = pki.ca.issue(pki.folder, "client", ["client-7"])
    async with TlsServer(pki.cert, pki.key) as srv:
        logs: list[str] = []
        st = await _link(client_mapping(srv.port), logs)
        assert st["link"] == "up", st
        line = next(x for x in logs if x.startswith("connected out"))
        assert f"server certificate SHA-256 {seen}, not checked" in line, line
        for pin in (seen, seen.replace(":", "").lower(), f"{'00' * 32}, {seen}"):
            logs = []
            st = await _link(client_mapping(srv.port, tls_fingerprint=pin), logs)
            assert st["link"] == "up", (pin, st)
            assert any(f"SHA-256 {seen}, pinned" in x for x in logs), logs
        logs = []
        st = await _link(client_mapping(srv.port, tls_fingerprint="ab" * 32), logs)
        assert st["link"] == "down" and "not the pinned one" in st["link_error"], st
        assert seen in st["link_error"], st
        # the CA file: the chain and the host name are checked
        from _pki import TestCA
        os.makedirs(os.path.join(pki.folder, "other"))
        other = TestCA(os.path.join(pki.folder, "other"), "other CA")
        for ca in (pki.ca.path, pki.ca.der_path, pki.cert):  # (PEM, DER, the server's own)
            logs = []
            st = await _link(client_mapping(srv.port, tls_ca=ca), logs)
            assert st["link"] == "up", (ca, st)
            assert any(f"SHA-256 {seen}, CN=srv.test checked against the CA file" in x
                       for x in logs), logs
        st = await _link(client_mapping(srv.port, tls_ca=other.path), [])
        assert st["link"] == "down" and "certificate verify failed" in st["link_error"], st
        logs = []
        st = await _link(client_mapping(srv.port, tls_ca=pki.ca.path, tls_fingerprint=seen), logs)
        assert st["link"] == "up" and any("CA file, pinned" in x for x in logs), (st, logs)
        empty = os.path.join(pki.folder, "empty.pem")
        open(empty, "w").close()
        assert "holds no certificate" in refused(client_mapping(srv.port, tls_ca=empty))
        assert "The CA file" in refused(client_mapping(srv.port, tls_ca=empty + ".gone"))
    named_cert, named_key = pki.ca.issue(pki.folder, "named", ["named.test"])
    async with TlsServer(named_cert, named_key) as srv:  # (a certificate without 127.0.0.1)
        st = await _link(client_mapping(srv.port, tls_ca=pki.ca.path), [])
        assert st["link"] == "down" and "mismatch" in st["link_error"], st
    async with TlsServer(pki.cert, pki.key, client_ca=pki.ca.path) as srv:
        st = await _link(client_mapping(srv.port), [])
        assert st["link"] == "down", st  # it asks for a client certificate
        st = await _link(client_mapping(srv.port, tls_cert=client_cert, tls_key=client_key), [])
        assert st["link"] == "up", st
        await _until(lambda: srv.subjects)
        assert srv.subjects == ["commonName=client-7"], srv.subjects
        locked = pki.encrypted(client_key, PASSWORD)
        st = await _link(client_mapping(srv.port, tls_cert=client_cert, tls_key=locked,
                                        tls_key_password=PASSWORD), [])
        assert st["link"] == "up", st
        st = await _link(client_mapping(srv.port, tls_cert=client_cert, tls_key=locked), [])
        assert st["link"] == "down" and "password-protected" in st["link_error"], st
    print("connect-out: the server certificate's fingerprint is logged; a CA file checks its "
          "chain and host name, a pin its fingerprint, and a failed check keeps the link "
          "down; a client certificate is presented when set (its key decrypted with its "
          "password)  OK")


async def _client_certificates(pki: Pki):
    client_cert, client_key = pki.ca.issue(pki.folder, "master", ["master-3"])
    stranger_ca = os.path.join(pki.folder, "stranger")
    os.makedirs(stranger_ca)
    from _pki import TestCA
    stranger_cert, stranger_key = TestCA(stranger_ca, "stranger CA").issue(
        stranger_ca, "stranger", ["stranger"])
    for protocol, connected in (("raw", "client connected"), ("modbus", "modbus master connected")):
        port = free_port()
        logs: list[str] = []
        runner = make_runner(mapping(port=port, protocol=protocol, tls_cert=pki.cert,
                                     tls_key=pki.key, tls_client_ca=pki.ca.path), logs.append)
        await runner.start()
        try:
            for cert, key in ((None, None), (stranger_cert, stranger_key)):
                ctx = _unverified()
                if cert:
                    ctx.load_cert_chain(cert, key)
                try:  # TLS 1.3: the client's handshake ends first, the server's alert after
                    r, w = await asyncio.wait_for(
                        asyncio.open_connection("127.0.0.1", port, ssl=ctx), 10)
                    assert await asyncio.wait_for(r.read(), 10) == b"", "served"
                    w.close()
                except (ssl.SSLError, ConnectionError):
                    pass
            # (one line a minute: a second failure is counted in the next one)
            await _until(lambda: any("TLS handshake with" in x for x in logs))
            await asyncio.sleep(0.3)
            assert not any(connected in x for x in logs), logs
            ctx = _unverified()
            ctx.load_cert_chain(client_cert, client_key)
            _r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port, ssl=ctx), 10)
            await _until(lambda: any(connected in x for x in logs))
            line = next(x for x in logs if connected in x)
            assert "[certificate CN=master-3]" in line, line
            w.close()
        finally:
            await runner.stop()
    print("listeners with a client CA file (bridge, Modbus gateway): a client without a "
          "certificate from it gets no connection, one with gets in and is logged by its "
          "subject  OK")


def test_connect_out_rules():
    client_mapping(1).validate()  # no certificate or key: fine for a connect-out link
    for net in ({"tls_cert": "/c.pem"}, {"tls_key": "/k.pem"}):
        try:
            client_mapping(1, **net).validate()
        except ConfigError as e:
            assert "or neither" in str(e), e
        else:
            raise AssertionError(net)
    try:
        mapping(tls_cert="", tls_key="").validate()
    except ConfigError as e:
        assert "both a certificate and a key" in str(e), e
    else:
        raise AssertionError("a TLS listener without a certificate")
    client_mapping(1, tls_fingerprint="AB:" * 31 + "AB").validate_input()
    for bad in ("AB:CD", "zz" * 32, "sha256:" + "ab" * 32):
        try:
            client_mapping(1, tls_fingerprint=bad).validate_input()
        except ConfigError as e:
            assert "is not a SHA-256 fingerprint" in str(e), e
        else:
            raise AssertionError(bad)
    print("rules: a connect-out link needs no certificate (both or neither), a listener "
          "does; a pin must be SHA-256 fingerprints  OK")


def test_older_forms_keep_the_fields():
    """A form without the new fields (an older cluster node's, or the block hidden for
    another mode) keeps what is stored."""
    from app.web.routes.forms import _mapping_from_form, _preserve_unmanaged_fields
    stored = client_mapping(1, tls_fingerprint="ab" * 32, tls_key_password="pw", tls_ca="/ca.pem",
                            tls_client_ca="/clients.pem")
    fields = {"name": "uplink", "kind": "net", "serial_port": MISSING_PORT,
              "network_mode": "client", "network_remote_host": "127.0.0.1",
              "network_remote_port": "1", "network_tls": "on"}
    m = MappingConfig.from_dict(_mapping_from_form(fields))
    _preserve_unmanaged_fields(m, stored, fields)
    assert m.network.tls_fingerprint == "ab" * 32 and m.network.tls_key_password == "pw", m.network
    assert m.network.tls_ca == "/ca.pem" and m.network.tls_client_ca == "/clients.pem", m.network
    m = MappingConfig.from_dict(_mapping_from_form(dict(fields, network_tls_fingerprint="")))
    _preserve_unmanaged_fields(m, stored, dict(fields, network_tls_fingerprint=""))
    assert m.network.tls_fingerprint == "", m.network
    print("forms: a form without the TLS fields keeps the stored ones  OK")


def form(port: int, pki: Pki, **fields) -> dict:
    out = {"name": "tls-form", "kind": "net", "enabled": "on", "serial_port": MISSING_PORT,
           "network_mode": "server", "network_protocol": "raw", "network_bind_ip": "127.0.0.1",
           "network_port": str(port), "network_tls": "on", "network_tls_cert": pki.cert,
           "network_tls_key": pki.locked}
    out.update(fields)
    return out


def stored(srv: Server) -> dict:
    return next(m for m in srv.saved_config()["mappings"] if m["name"] == "tls-form")


def https_browser(srv: Server) -> Browser:
    """A Browser for a server whose admin UI has TLS (its test certificate is not checked)."""
    import urllib.request

    from _server import _NoRedirect
    b = Browser(f"https://127.0.0.1:{srv.port}")
    b.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(b.jar), _NoRedirect(),
                                       urllib.request.HTTPSHandler(context=_unverified()))
    return b


def api(srv: Server, method: str, path: str, body=None):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"https://127.0.0.1:{srv.port}/api/v1" + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=_unverified()) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def test_form_and_api(pki: Pki):
    config = {"users": USERS, "api_token_hash": hash_token(TOKEN), "api_token_role": "operator",
              "admin_ui": {"tls_cert": pki.ui_cert, "tls_key": pki.ui_key}}
    with Server(config) as srv:
        b = https_browser(srv)
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
        # the admin UI's own key file (here: a hard link to it) is refused
        link = os.path.join(srv.tmp, "ui-key-link.pem")
        os.link(pki.ui_key, link)
        st, _h, body = b.post("/api/mappings/save", form(
            port, pki, id=m["id"], network_tls_cert=pki.ui_cert, network_tls_key=link))
        assert st == 400 and "own key. Give the mapping" in body, (st, body[-800:])
        body = {"name": "tls-api", "serial": {"port": MISSING_PORT + "-2"},
                "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": free_port(),
                            "tls": True, "tls_cert": pki.ui_cert, "tls_key": pki.ui_key}}
        st, out = api(srv, "PUT", f"/mappings/{created['id']}", body)
        assert st == 400 and "the admin UI's own key" in out["detail"], (st, out)
        assert "Traceback" not in srv.output()
    print("form and REST API: a key that does not load is refused; the form never shows the "
          "stored password, blank keeps it, the remove box clears it; a PUT with the mask "
          "keeps it; the admin UI's key file is refused  OK")


def _https_healthz(port: int) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw, \
            _unverified().wrap_socket(raw) as s:
        s.sendall(b"GET /healthz HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        out = b""
        while chunk := s.recv(4096):
            out += chunk
    return out


def test_admin_ui(pki: Pki):
    admin = {"tls_cert": pki.cert, "tls_key": pki.locked, "tls_key_password": PASSWORD}
    with Server({"users": USERS, "admin_ui": admin}) as srv:
        assert b"200 OK" in _https_healthz(srv.port), srv.output()[-2000:]
    data = tempfile.mkdtemp(prefix="s2n_tlsui_")
    try:
        cfg = {"users": USERS, "admin_ui": {"tls_cert": pki.cert, "tls_key": pki.locked},
               "mappings": [mapping(port=free_port(), tls=False).to_dict()]}
        with open(os.path.join(data, "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        env = dict(os.environ, SER2NET_BIND_IP="127.0.0.1", SER2NET_PORT=str(free_port()))
        p = subprocess.run([sys.executable, "ser2net.py", "--no-bootstrap", "--data-dir", data],
                           cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                           encoding="utf-8", errors="replace", timeout=60)
        with open(os.path.join(data, "all.log"), encoding="utf-8") as fh:
            log = fh.read()
        assert p.returncode == 1, (p.returncode, (p.stdout + p.stderr)[-2000:])
        assert "is password-protected and no password is set" in log, log[-2000:]
        assert "tls_key_password" in log and "[tls] " not in log, log[-2000:]
    finally:
        shutil.rmtree(data, ignore_errors=True)

    with Server({"users": USERS}) as srv:
        b = Browser(srv.base)
        b.login("admin", "admin-pass-1")
        fields = {"tls_cert": pki.cert, "tls_key": pki.locked}
        st, _h, page = b.post("/settings/tls", fields)
        assert "password-protected" in page and "could not be loaded" in page, page[-1500:]
        st, _h, page = b.post("/settings/tls", dict(fields, tls_key_password="wrong"))
        assert "Is the key password right" in page, page[-1500:]
        st, _h, page = b.post("/settings/tls", dict(fields, tls_key_password=PASSWORD))
        assert "Admin TLS saved" in page and PASSWORD not in page, page[-1500:]
        assert srv.saved_config()["admin_ui"]["tls_key_password"] == PASSWORD
        st, _h, page = b.post("/settings/tls", fields)  # blank: kept
        assert "Admin TLS saved" in page, page[-1500:]
        assert srv.saved_config()["admin_ui"]["tls_key_password"] == PASSWORD
        st, _h, page = b.post("/settings/tls", dict(fields, tls_key_password_clear="on"))
        assert "password-protected" in page, page[-1500:]
        st, _h, page = b.post("/settings/tls", dict(fields, tls_key=pki.key,
                                                    tls_key_password_clear="on"))
        assert "Admin TLS saved" in page, page[-1500:]
        assert srv.saved_config()["admin_ui"]["tls_key_password"] == ""
    print("admin UI: an encrypted key serves HTTPS with admin_ui.tls_key_password; without "
          "it the start ends at once (exit 1, all.log says why); Settings loads the pair "
          "with the password, never shows it, blank keeps it  OK")


def main():
    if not need("cryptography", what="per-mapping TLS files (test certificates)"):
        test_api_masks_the_password()
        return
    tmp = tempfile.mkdtemp(prefix="s2n_tlsmap_")
    try:
        pki = Pki(tmp)
        test_key_passwords(pki)
        test_context_options(pki)
        test_import_warns(pki)
        test_api_masks_the_password()
        asyncio.run(_listener_starts(pki))
        test_connect_out_rules()
        test_older_forms_keep_the_fields()
        asyncio.run(_connect_out(pki))
        asyncio.run(_client_certificates(pki))
        test_form_and_api(pki)
        test_admin_ui(pki)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
