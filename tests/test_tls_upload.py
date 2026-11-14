"""TLS files uploaded in the browser (ser2netweb/web/tlsupload.py).

Checks:
  * store(): a certificate with its key (and CA files) is kept in <data>/tls with private
    permissions (also a tls/ folder made before with others' access) under names made
    from each file's own hash (a key never replaces another pair's), only after it loaded
    as ser2netweb will load it: a key that is another certificate's, an encrypted key
    without or with a wrong password, a file that is not PEM, a file over the size limit,
    a certificate without its key and a CA file without a certificate are refused and
    leave nothing behind; uploads at the same time don't get in each other's way; the
    admin UI's pair is refused for a mapping and a mapping's for the admin UI, also as
    another certificate for the same key written another way (public key compared);
  * the mapping form (admins): POST /api/mappings/tls-upload stores the files and answers
    their paths for the form's fields (HX-Trigger: tlsUploaded), audited; operators 403,
    the CSRF header is required, a bad pair is 400 with the reason; a mapping saved with
    the uploaded paths serves that certificate; the typed-path fields keep working;
  * Settings → Admin TLS (admins): POST /settings/tls/upload stores the pair, sets it as
    the admin UI's and serves it for new connections at once, audited; operators 403, a
    bad pair changes nothing; a body over the limit is refused.

Needs the optional cryptography package (to make certificates); skipped without it.

Run: python3 tests/test_tls_upload.py
"""
import concurrent.futures
import datetime
import json
import os
import socket
import ssl
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request

from _pki import TestCA
from _server import Browser, Server, free_port

from ser2netweb.web import selfsigned, tlsupload
from ser2netweb.web.auth import hash_password, hash_token

PW = "tls-upload-pass-1"
TOKEN = "s2n_tls-upload-token"
MID = "ccccccccccc1"


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


def make_pair(folder, name):
    cert, key = os.path.join(folder, f"{name}.pem"), os.path.join(folder, f"{name}.key")
    selfsigned.generate(cert, key, common_name=name, days=30)
    return read(cert), read(key)


def other_cert_same_key(key_pem, name, password):
    """Another certificate for the key in `key_pem`, and that key written another way
    (traditional OpenSSL PEM, encrypted with `password`)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID

    key = serialization.load_pem_private_key(key_pem, None)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.TraditionalOpenSSL,
                              serialization.BestAvailableEncryption(password.encode())))


def refused(text, **kwargs):
    try:
        tlsupload.store(**kwargs)
    except tlsupload.UploadError as e:
        assert text in str(e), (text, str(e))
    else:
        raise AssertionError(f"accepted; expected: {text}")


def test_store(src):
    cert_a, key_a = make_pair(src, "a")
    cert_b, key_b = make_pair(src, "b")
    ca = TestCA(src)
    enc_cert, enc_key = (read(p) for p in ca.issue(src, "enc", ["enc.example"], password=b"s3cret"))
    with tempfile.TemporaryDirectory() as data:
        tdir = os.path.join(data, "tls")
        os.makedirs(tdir)
        if os.name != "nt":  # (made before: by Generate, or by hand, with others' access)
            os.chmod(tdir, 0o755)
        got = tlsupload.store(data, cert=cert_a, key=key_a, cas={"ca": read(ca.path),
                                                               "client_ca": None})
        assert set(got) == {"cert", "key", "ca"}, got
        for what, path in got.items():
            assert os.path.dirname(path) == tdir and os.path.isabs(path), path
            name = os.path.basename(path)
            assert name.startswith("uploaded-") and name.endswith(
                {"cert": "-cert.pem", "key": "-key.pem", "ca": "-ca.pem"}[what]), name
            if os.name != "nt":
                assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, oct(os.stat(path).st_mode)
        assert read(got["cert"]) == cert_a and read(got["key"]) == key_a
        if os.name != "nt":
            assert stat.S_IMODE(os.stat(tdir).st_mode) == 0o700
        kept = sorted(os.listdir(tdir))
        assert tlsupload.store(data, cert=cert_a, key=key_a) == {
            "cert": got["cert"], "key": got["key"]}, "the same files again replace themselves"
        assert sorted(os.listdir(tdir)) == kept
        # the same certificate with its key written another way (encrypted): a key file of
        # its own, the one in use stays as it is
        again_cert, again_key = other_cert_same_key(key_a, "a", "pw")
        other = tlsupload.store(data, cert=cert_a, key=again_key, password="pw")
        assert other["cert"] == got["cert"] and other["key"] != got["key"], other
        assert read(got["key"]) == key_a and read(other["key"]) == again_key
        os.remove(other["key"])
        # uploads at the same time each have their own files aside
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _i: tlsupload.store(data, cert=cert_a, key=key_a),
                                    range(16)))
        assert all(r == results[0] for r in results) and sorted(os.listdir(tdir)) == kept
        print("store: kept in tls/ with private permissions, each file named by its own "
              "hash; side by side uploads  OK")

        refused("can't be used with the uploaded key", data_dir=data, cert=cert_a, key=key_b)
        refused("password-protected", data_dir=data, cert=enc_cert, key=enc_key)
        refused("can't be used with the uploaded key", data_dir=data, cert=enc_cert,
                key=enc_key, password="wrong")
        refused("together", data_dir=data, cert=cert_a)
        refused("together", data_dir=data, key=key_a)
        refused("Choose the files", data_dir=data)
        refused("not a PEM certificate", data_dir=data, cert=b"\x30\x82 der bytes", key=key_a)
        refused("not a PEM certificate", data_dir=data, cert=key_a, key=key_a)
        refused("not a PEM private key", data_dir=data, cert=cert_a, key=cert_a)
        refused("at most 256 KB", data_dir=data, cert=cert_a + b"\n" * tlsupload.MAX_FILE_BYTES,
                key=key_a)
        refused("holds no certificate", data_dir=data, cas={"ca": b"just some text\n"})
        refused("is empty", data_dir=data, cas={"client_ca": b"  \n"})
        refused("holds no certificate", data_dir=data, cert=cert_b, key=key_b,
                cas={"ca": key_b})
        assert sorted(os.listdir(tdir)) == kept, \
            f"a refused upload leaves nothing behind: {sorted(os.listdir(tdir))}"
        ok = tlsupload.store(data, cert=enc_cert, key=enc_key, password="s3cret")
        assert read(ok["key"]) == enc_key, "an encrypted key is kept as it is"
        with open(ca.der_path, "rb") as fh:
            assert "client_ca" in tlsupload.store(data, cas={"client_ca": fh.read()}), "DER CA"
        print("store: wrong key, missing or wrong password, not PEM, too large, a CA file "
              "without a certificate: refused, nothing left behind  OK")

        # the admin UI's pair for a mapping (and the other way round)
        other = os.path.join(src, "a.pem"), os.path.join(src, "a.key")
        refused("the admin UI's own", data_dir=data, cert=cert_a, key=key_a, others=(other,),
                reuse_message="the admin UI's own")
        refused("reused", data_dir=data, cert=cert_a.replace(b"\n", b"\r\n"),
                key=key_a.replace(b"\n", b"\r\n"), others=(other,), reuse_message="reused")
        refused("reused", data_dir=data, cert=cert_a, key=key_a,
                others=(("", ""), ("/no/such/cert", other[1])), reuse_message="reused")
        # its key with another certificate, written another way (encrypted, PKCS#1)
        refused("reused", data_dir=data, cert=again_cert, key=again_key, password="pw",
                others=(other,), reuse_message="reused")
        assert tlsupload.public_key(again_cert) == tlsupload.public_key(cert_a)
        assert tlsupload.public_key(cert_b) != tlsupload.public_key(cert_a)
        assert tlsupload.public_key(key_a) is None and tlsupload.public_key(b"x") is None
        assert tlsupload.store(data, cert=cert_b, key=key_b, others=(other,),
                               reuse_message="reused")
        print("store: the admin UI's pair is not a mapping's (line ends aside, its key "
              "with another certificate, written another way)  OK")


def served_cert(port):
    return ssl.get_server_certificate(("127.0.0.1", port), timeout=10).encode()


def pem_body(pem):
    return b"".join(pem.split())


def test_live(src):
    cert_a, key_a = make_pair(src, "map")
    cert_b, key_b = make_pair(src, "other")
    cert_ui, key_ui = make_pair(src, "ui")
    port = free_port()
    users = [{"username": u, "password_hash": hash_password(PW), "role": r, "pwd_version": 1}
             for u, r in (("admin", "admin"), ("op", "operator"))]
    srv = Server({"users": users, "api_token_hash": hash_token(TOKEN), "api_token_role": "admin",
                  "mappings": [{"id": MID, "name": "tls-map", "enabled": True, "kind": "net",
                                "serial": {"port": "/dev/ser2net-tls-upload"},
                                "network": {"mode": "server", "protocol": "raw",
                                            "bind_ip": "127.0.0.1", "port": port}}]})

    def audit(action):
        with open(os.path.join(srv.data, "audit.log"), encoding="utf-8") as fh:
            return [ln.split("\t") for ln in fh.read().splitlines() if f"\t{action}\t" in ln]

    def uploaded():
        tdir = os.path.join(srv.data, "tls")
        return sorted(os.listdir(tdir)) if os.path.isdir(tdir) else []

    with srv:
        admin, op = Browser(srv.base), Browser(srv.base)
        admin.login("admin", PW)
        op.login("op", PW)
        # ---- the mapping form ----
        _st, _h, form = admin.get(f"/api/mappings/{MID}/form")
        assert 'id="tls-upload"' in form and 'name="cert" form="tls-upload"' in form
        assert f'name="mid" value="{MID}"' in form
        _st, _h, form = op.get(f"/api/mappings/{MID}/form")
        assert 'form="tls-upload"' not in form and 'id="tls-upload"' not in form, \
            "uploading is for admins"
        assert 'name="network_tls_cert"' in form, "the path fields stay for operators"
        files = {"cert": ("map.pem", cert_a), "key": ("map.key", key_a)}
        st, _h, body = op.post("/api/mappings/tls-upload", {"mid": MID}, files=files, htmx=True)
        assert st == 403 and "requires the admin role" in body, (st, body[:200])
        st, _h, body = admin.post("/api/mappings/tls-upload", {"mid": MID},
                                  files={"cert": ("c.pem", cert_a), "key": ("k.key", key_b)},
                                  htmx=True)
        assert st == 400 and "can't be used with the uploaded key" in body, (st, body[:300])
        st, _h, body = admin.post("/api/mappings/tls-upload", {"mid": MID},
                                  files={"cert": ("c.pem", cert_a)}, htmx=True)
        assert st == 400 and "together" in body, (st, body[:300])
        st, _h, body = admin.post("/api/mappings/tls-upload", {"mid": MID},
                                  files={"cert": ("", b""), "key": ("", b"")}, htmx=True)
        assert st == 400 and "Choose the files" in body, "file inputs left blank"
        big = cert_a + b"\n" * (tlsupload.MAX_BODY_BYTES + 10)
        st, _h, body = admin.post("/api/mappings/tls-upload", {"mid": MID},
                                  files={"cert": ("c.pem", big), "key": ("k", key_a)}, htmx=True)
        assert st == 413, st
        assert uploaded() == [] and not audit("tls_upload"), uploaded()
        # without the CSRF header: refused by the gate
        req = urllib.request.Request(srv.base + "/api/mappings/tls-upload", data=b"x", method="POST",
                                     headers={"Content-Type": "multipart/form-data; boundary=x",
                                              "Cookie": "; ".join(f"{c.name}={c.value}"
                                                                  for c in admin.jar)})
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("an upload without the CSRF header was taken")
        except urllib.error.HTTPError as e:
            assert e.code == 403, e.code

        st, h, body = admin.post("/api/mappings/tls-upload", {"mid": MID}, files=files, htmx=True)
        assert st == 204, (st, body[:300])
        fields = json.loads(h["HX-Trigger"])["tlsUploaded"]
        assert set(fields) == {"network_tls_cert", "network_tls_key"}, fields
        assert h["HX-Trigger"].isascii() and "X-Notice" in h
        assert read(fields["network_tls_cert"]) == cert_a and len(uploaded()) == 2
        record = audit("tls_upload")
        assert len(record) == 1 and record[0][4] == "admin" and record[0][3].startswith(
            "tls-map: uploaded-"), record
        assert srv.saved_config()["mappings"][0]["network"]["tls_cert"] == "", \
            "an upload changes no setting: the form is saved with the paths"
        # the mapping saved with the uploaded paths serves that certificate
        patch = {"network": {"tls": True, "tls_cert": fields["network_tls_cert"],
                             "tls_key": fields["network_tls_key"]}}
        req = urllib.request.Request(
            f"{srv.base}/api/v1/mappings/{MID}", data=json.dumps(patch).encode(), method="PATCH",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            assert r.status == 200
        end = time.time() + 10
        while True:
            try:
                assert pem_body(served_cert(port)) == pem_body(cert_a)
                break
            except (OSError, ssl.SSLError):
                assert time.time() < end, "the mapping does not serve the uploaded certificate"
                time.sleep(0.2)
        # a CA file alone, for the field it stands next to; a new mapping (no id yet)
        st, h, _b = admin.post("/api/mappings/tls-upload", {"mid": ""},
                               files={"client_ca": ("ca.pem", cert_b)}, htmx=True)
        assert st == 204 and set(json.loads(h["HX-Trigger"])["tlsUploaded"]) == {
            "network_tls_client_ca"}, h["HX-Trigger"]
        assert audit("tls_upload")[-1][3].startswith("(new): uploaded-")
        print("mapping form: admins upload a pair or a CA file, checked and audited, the "
              "paths answered for the form; operators 403; the mapping serves it  OK")

        # ---- Settings → Admin TLS ----
        _st, _h, page = admin.get("/settings")
        assert 'action="/settings/tls/upload"' in page and 'name="cert_file"' in page
        ui_files = {"cert_file": ("ui.pem", cert_ui), "key_file": ("ui.key", key_ui)}
        st, _h, body = op.post("/settings/tls/upload", files=ui_files)
        assert st == 403, st
        before = len(uploaded())
        st, _h, page = admin.post("/settings/tls/upload", files={
            "cert_file": ("ui.pem", cert_ui), "key_file": ("ui.key", key_b)})
        assert st == 400 and "be used with the uploaded key" in page, (st, page[:300])
        st, _h, page = admin.post("/settings/tls/upload", files={"cert_file": ("ui.pem", cert_ui)})
        assert st == 400 and "Choose a certificate file and its key file." in page
        st, _h, page = admin.post("/settings/tls/upload", files={
            "cert_file": ("m.pem", cert_a), "key_file": ("m.key", key_a)})
        assert st == 400 and "are a mapping" in page, "a mapping's pair is not the admin UI's"
        assert len(uploaded()) == before and srv.saved_config()["admin_ui"]["tls_cert"] == ""
        st, _h, page = admin.post("/settings/tls/upload", files=ui_files)
        assert st == 200 and "Certificate and key uploaded and set" in page, (st, page[:400])
        assert "use HTTPS now" in page
        saved = srv.saved_config()["admin_ui"]
        assert os.path.basename(saved["tls_cert"]).startswith("uploaded-") and \
            read(saved["tls_cert"]) == cert_ui and read(saved["tls_key"]) == key_ui, saved
        assert pem_body(served_cert(srv.port)) == pem_body(cert_ui), \
            "new connections to the UI get the uploaded certificate at once"
        record = audit("admin_tls_upload")
        assert len(record) == 1 and record[0][4] == "admin", record
        with socket.create_connection(("127.0.0.1", srv.port), timeout=5):
            pass
    print("Settings: an admin uploads the admin UI's pair, checked, set and served at "
          "once, audited; operators 403; a bad or a mapping's pair changes nothing  OK")


def main():
    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("SKIP: needs the cryptography package to make certificates")
        return
    if sys.platform == "win32":
        print("(Windows: file permissions are the data folder's ACL; not checked here)")
    with tempfile.TemporaryDirectory() as src:
        test_store(src)
        test_live(src)
    print("\nPASS: TLS files uploaded in the browser")


if __name__ == "__main__":
    main()
