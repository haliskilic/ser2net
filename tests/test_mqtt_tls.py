"""MQTT over TLS: a CA file, a client certificate (mutual TLS) and the host name check.

  * tls_context(): the system's CAs by default (certificate and host name checked); a CA
    file (PEM or DER) instead; the host name check can be turned off (the chain is still
    checked); a client certificate with its key (in its own file or the same one, an
    encrypted key with its password); what can't be used is a TlsFileError of a known kind
    (unreadable / no certificate CA file, a key without a certificate, a wrong password,
    a key that is not the certificate's, a missing key file) — never a prompt;
  * edits: the mapping form and the REST API refuse unusable files (in the UI's language),
    while validate() / validate_input() don't look at them, so a stored config whose files
    are gone still loads and saves; an import takes the mapping with a warning; only for a
    net mapping with MQTT and TLS on;
  * the form: the fields are read when posted and kept when not (a form from an older
    cluster peer), the host name check is on unless unticked; a viewer's REST API token
    doesn't get the key password;
  * with paho-mqtt, against a TLS test broker on 127.0.0.1: the CA file makes the
    connection work (without it the certificate check fails, logged); a certificate that
    doesn't name the host fails unless the host name check is off; a broker that asks for
    a client certificate takes the encrypted-key one and refuses a client without; a CA
    file that can't be read at start keeps MQTT off (logged, status off) and nothing is
    sent in clear.

Needs cryptography (test certificates); the broker parts need paho-mqtt.

Run: python3 tests/test_mqtt_tls.py
"""
import asyncio
import os
import shutil
import ssl
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _mqtt_broker import MqttBroker
from _optional import need

from app import i18n
from app.config import AppConfig, ConfigError, MappingConfig, MqttSettings
from app.engine.mqtt_pub import MqttPublisher, TlsFileError, tls_context


def settings(**kw):
    return MqttSettings(**dict({"enabled": True, "host": "127.0.0.1", "base_topic": "plant/a",
                                "tls": True}, **kw))


class Pki:
    def __init__(self, folder):
        from _pki import TestCA

        self.folder = folder
        self.ca = TestCA(folder)
        self.server = self.ca.issue(folder, "broker", ["127.0.0.1", "localhost"])
        self.named = self.ca.issue(folder, "named", ["broker.test"])  # not 127.0.0.1
        self.client = self.ca.issue(folder, "client", ["gateway"])
        self.locked = self.ca.issue(folder, "locked", ["gateway"], password=b"s3cret")
        self.combined = os.path.join(folder, "combined.pem")
        with open(self.combined, "wb") as out:
            for p in self.client:
                with open(p, "rb") as fh:
                    out.write(fh.read())

    def file(self, name, data):
        path = os.path.join(self.folder, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path


def expect(kind, **kw):
    try:
        tls_context(settings(**kw))
    except TlsFileError as e:
        assert e.kind == kind, (kind, e.kind, str(e))
        return str(e)
    raise AssertionError(f"{kw} should be refused ({kind})")


def test_context(pki):
    ctx = tls_context(settings())
    assert ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED
    for path in (pki.ca.path, pki.ca.der_path):
        ctx = tls_context(settings(ca_file=path))
        assert ctx.cert_store_stats()["x509"] == 1, path
        assert ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED
    ctx = tls_context(settings(ca_file=pki.ca.path, tls_check_hostname=False))
    assert not ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED
    missing = os.path.join(pki.folder, "missing.pem")
    text = expect("ca_unreadable", ca_file=missing)
    assert text.startswith(f"the CA file {missing!r} can't be read: "), text
    for name, data in (("empty.pem", b""), ("junk.pem", b"not a certificate\n"),
                       ("bad.pem", b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")):
        expect("ca_empty", ca_file=pki.file(name, data))
    print("CA: the system's by default, a PEM or DER file instead, host name check optional; "
          "unreadable / empty files refused  OK")

    cert, key = pki.client
    tls_context(settings(client_cert=cert, client_key=key))
    tls_context(settings(client_cert=pki.combined))  # the key in the certificate's file
    tls_context(settings(client_cert=pki.locked[0], client_key=pki.locked[1],
                         client_key_password="s3cret"))
    for password in ("", "wrong"):  # never a prompt on the terminal
        expect("cert_unusable", client_cert=pki.locked[0], client_key=pki.locked[1],
               client_key_password=password)
    expect("cert_unusable", client_cert=cert, client_key=pki.server[1])  # not its key
    expect("cert_unusable", client_cert=cert)                            # no key at all
    expect("key_without_cert", client_key=key)
    expect("cert_unreadable", client_cert=cert, client_key=missing)
    print("client certificate: own key file, same file, encrypted key with its password; "
          "a wrong / missing password, another key, a missing file refused  OK")


def test_edits(pki):
    missing = os.path.join(pki.folder, "gone", "ca.pem")
    m = MappingConfig.from_dict({"name": "m", "serial": {"port": "COM3"},
                                 "mqtt": {"enabled": True, "host": "b", "base_topic": "t",
                                          "tls": True, "ca_file": missing}})
    m.validate()
    m.validate_input()
    AppConfig.from_dict({"mappings": [m.to_dict()]}).validate()  # a stored one loads
    try:
        m.check_mqtt_tls_files()
        raise AssertionError("an edit took a CA file that can't be read")
    except ConfigError as e:
        assert str(e).startswith(f"The MQTT CA file {missing} can't be read: "), e
    token = i18n.set_language("tr")
    try:
        m.check_mqtt_tls_files()
    except ConfigError as e:
        assert str(e).startswith(f"MQTT CA dosyası {missing} okunamıyor: "), e
    finally:
        i18n.reset(token)
    for change in ({"tls": False}, {"enabled": False}):
        off = MappingConfig.from_dict(dict(m.to_dict(), mqtt=dict(m.to_dict()["mqtt"], **change)))
        off.check_mqtt_tls_files()
    m.mqtt.ca_file = pki.ca.path
    m.mqtt.client_cert, m.mqtt.client_key = pki.locked
    m.mqtt.client_key_password = "s3cret"
    m.check_mqtt_tls_files()
    m.mqtt.client_key_password = "nope"
    try:
        m.check_mqtt_tls_files()
        raise AssertionError("an edit took a wrong key password")
    except ConfigError as e:
        assert "Is the key password right" in str(e), e
    print("edits refuse unusable TLS files (translated); a stored config with them still "
          "loads; MQTT or TLS off: not checked  OK")

    import json

    from app import importer
    doc = {"mappings": [dict(m.to_dict(), name="imported", id="imp1",
                             mqtt=dict(m.to_dict()["mqtt"], ca_file=missing))]}
    plan = importer.parse_mappings_file(json.dumps(doc).encode())
    assert [x.name for x in plan.mappings] == ["imported"], plan.mappings
    assert any("Mapping #1 (imported): The MQTT CA file" in w and "runs without MQTT" in w
               for w in plan.warnings), plan.warnings
    print("import: a mapping whose TLS files are not here is taken, with a warning  OK")


def test_form_and_api():
    from app.web.api import REDACTED, _mapping_view
    from app.web.routes.forms import _build_mapping_dict, _preserve_unmanaged_fields

    base = {"name": "m", "kind": "net", "serial_port": "COM3", "network_port": "4001",
            "mqtt_enabled": "on", "mqtt_host": "b", "mqtt_base_topic": "t", "mqtt_tls": "on"}
    stored = MappingConfig.from_dict({"id": "m1", "name": "m", "serial": {"port": "COM3"},
                                      "mqtt": {"enabled": True, "host": "b", "base_topic": "t",
                                               "tls": True, "ca_file": "/data/ca.pem",
                                               "client_cert": "/data/gw.pem",
                                               "client_key": "/data/gw.key",
                                               "client_key_password": "pw",
                                               "tls_check_hostname": False}})

    def parsed(form, existing=None):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing, form)
        return m.mqtt

    mq = parsed(dict(base, mqtt_ca_file=" /etc/ca.pem ", mqtt_client_cert="/etc/c.pem",
                     mqtt_client_key="", mqtt_client_key_password=" p w ",
                     mqtt_tls_check_hostname="on"))
    assert (mq.ca_file, mq.client_cert, mq.client_key, mq.client_key_password,
            mq.tls_check_hostname) == ("/etc/ca.pem", "/etc/c.pem", "", " p w ", True), mq
    mq = parsed(dict(base, mqtt_ca_file=""), stored)  # shown, the check unticked
    assert mq.ca_file == "" and mq.tls_check_hostname is False and mq.client_cert == ""
    mq = parsed(dict(base, id="m1"), stored)          # an older peer's form: all kept
    assert (mq.ca_file, mq.client_cert, mq.client_key, mq.client_key_password,
            mq.tls_check_hostname) == ("/data/ca.pem", "/data/gw.pem", "/data/gw.key", "pw",
                                       False), mq
    mq = parsed(dict(base))                           # new, without the fields: defaults
    assert mq.ca_file == "" and mq.tls_check_hostname is True
    assert MqttSettings().tls_check_hostname is True and MqttSettings().ca_file == ""
    print("form: TLS fields read when posted, kept when not (older peers), check on by "
          "default  OK")

    class State:
        class config:
            api_token_role = "viewer"

        class supervisor:
            @staticmethod
            def status(_mid):
                return {}
    view = _mapping_view(stored, State)
    assert view["mqtt"]["client_key_password"] == REDACTED, view["mqtt"]
    assert view["mqtt"]["client_cert"] == "/data/gw.pem"
    State.config.api_token_role = "operator"
    assert _mapping_view(stored, State)["mqtt"]["client_key_password"] == "pw"
    print("REST API: a viewer token doesn't get the client key password  OK")


async def until(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            return False
        await asyncio.sleep(0.05)
    return True


def server_context(cert_key, client_ca=None):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(*cert_key)
    if client_ca:
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(client_ca)
    return ctx


async def broker_checks(pki):
    logs = []

    async def attempt(broker, **kw):
        """A publisher to the broker: whether it connected (and its birth arrived)."""
        logs.clear()
        pub = MqttPublisher(settings(port=broker.port, **kw), logger=logs.append)
        assert pub.connect() is True, logs
        pub._client.reconnect_delay_set(min_delay=0.2, max_delay=0.2)
        try:
            ok = await until(lambda: pub.state["state"] == "connected", 3)
            if ok:
                assert await until(lambda: broker.topics("plant/a/status")), broker.messages
            return ok, pub.state["error"]
        finally:
            pub.close()

    with MqttBroker(ssl=server_context(pki.server)) as broker:
        ok, _ = await attempt(broker, ca_file=pki.ca.path)
        assert ok, logs
        assert logs[0] == f"MQTT: connecting to 127.0.0.1:{broker.port} (TLS), topic 'plant/a'"
        ok, error = await attempt(broker)  # the system's CAs don't know the test CA
        assert not ok and "CERTIFICATE_VERIFY_FAILED" in error, (error, logs)
        assert sum("can't connect" in m for m in logs) == 1, logs
    print("paho over TLS: the CA file makes it connect; without it the certificate check "
          "fails (logged)  OK")

    with MqttBroker(ssl=server_context(pki.named)) as broker:  # names broker.test only
        ok, error = await attempt(broker, ca_file=pki.ca.path)
        assert not ok and "mismatch" in error, (error, logs)
        ok, _ = await attempt(broker, ca_file=pki.ca.path, tls_check_hostname=False)
        assert ok, logs
    print("paho over TLS: a certificate that doesn't name the host fails unless the host "
          "name check is off  OK")

    with MqttBroker(ssl=server_context(pki.server, client_ca=pki.ca.path)) as broker:
        ok, _ = await attempt(broker, ca_file=pki.ca.path, client_cert=pki.locked[0],
                              client_key=pki.locked[1], client_key_password="s3cret")
        assert ok, logs
        assert broker.connects, broker.connects
        before = len(broker.connects)
        ok, _ = await attempt(broker, ca_file=pki.ca.path)
        assert not ok and len(broker.connects) == before and broker.tls_errors, \
            (broker.tls_errors, logs)
    print("paho over mutual TLS: the client certificate (encrypted key) is taken, a client "
          "without one is refused  OK")

    with MqttBroker() as plain:  # a CA file gone at start: MQTT off, nothing in clear
        logs.clear()
        missing = os.path.join(pki.folder, "gone.pem")
        pub = MqttPublisher(settings(port=plain.port, ca_file=missing, username="u",
                                     password="secret"), logger=logs.append)
        assert pub.connect() is False
        assert logs == [f"MQTT publishing disabled: TLS can't be set up: the CA file "
                        f"{missing!r} can't be read: No such file or directory"], logs
        assert pub.state["state"] == "off" and pub.state["error"].startswith("TLS: "), pub.state
        await asyncio.sleep(0.5)
        assert plain.connects == [], "a plaintext CONNECT went out"
        pub.feed(b"line\n")
        pub.close()
    print("a CA file that can't be read at start: MQTT off (logged, status), no plaintext "
          "connection  OK")


def main():
    if not need("cryptography", what="MQTT TLS options (test certificates)"):
        test_form_and_api()
        print("\nPASS: MQTT TLS options (without cryptography: form and API only)")
        return
    folder = tempfile.mkdtemp(prefix="s2n_mqtt_tls_")
    try:
        pki = Pki(folder)
        test_context(pki)
        test_edits(pki)
        test_form_and_api()
        if need("paho.mqtt", what="MQTT over TLS against a test broker"):
            asyncio.run(broker_checks(pki))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("\nPASS: MQTT TLS options (CA file, client certificate, host name check)")


if __name__ == "__main__":
    main()
