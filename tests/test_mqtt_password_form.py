"""The mapping form never puts the stored MQTT password into the page.

  * the edit form says a password is stored ("•••••• (unchanged)", a remove box) but
    holds no value; a new mapping's form has neither;
  * saved from that form (it posts mqtt_password_write_only), a blank field keeps the
    stored password, a typed one replaces it, the remove box clears it, and the REST
    API's mask "********" keeps it;
  * a form without the marker (an older node's remote-edit form, an open tab from before
    the upgrade, a script) sets the password it posts, as before;
  * a form shown again after an error has what was typed, never the stored password;
  * a REST API PUT that sends back the mask a viewer token reads keeps the stored
    password (operator tokens still read and write the real one);
  * the export, which holds the MQTT passwords, says so next to its button.
The remote edit of a peer's mapping (the controlling node sends the password to store)
is in tests/test_cluster.py.

Run: python3 tests/test_mqtt_password_form.py
"""
import json
import re
import urllib.request

from _server import Browser, Server, free_port

from ser2netweb.web.auth import hash_password

PW = "mqtt-form-pass-1"
SECRET = "broker-secret-1"
MASK = "********"


def form(port, **kw):
    fields = {"id": "m1", "name": "plc", "kind": "net", "serial_port": "/dev/ttyS-mqtt-test",
              "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "raw",
              "network_bind_ip": "127.0.0.1", "network_port": str(port), "mqtt_host": "broker",
              "mqtt_port": "1883", "mqtt_username": "plc", "mqtt_password_write_only": "1",
              "mqtt_password": ""}
    fields.update(kw)
    return {k: v for k, v in fields.items() if v is not None}


def main():
    port = free_port()
    cfg = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                      "pwd_version": 1}],
           "mappings": [{"id": "m1", "name": "plc", "enabled": False,
                         "serial": {"port": "/dev/ttyS-mqtt-test"},
                         "network": {"bind_ip": "127.0.0.1", "port": port},
                         "mqtt": {"host": "broker", "username": "plc", "password": SECRET}}]}
    with Server(cfg) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)

        def stored():
            return srv.saved_config()["mappings"][0]["mqtt"]["password"]

        def save(**kw):
            st, _, body = b.post("/api/mappings/save", form(port, **kw), htmx=True)
            return st, body

        st, _, page = b.get("/api/mappings/m1/form")
        assert st == 200 and SECRET not in page, "the stored password is in the page"
        field = re.search(r'<input type="password" name="mqtt_password"[^>]*>', page).group(0)
        assert 'value=""' in field and "•••••• (unchanged)" in field, field
        assert 'name="mqtt_password_clear"' in page and 'name="mqtt_password_write_only"' in page
        st, _, page = b.get("/api/mappings/form")
        assert st == 200 and 'name="mqtt_password_clear"' not in page
        assert "•••••• (unchanged)" not in re.search(
            r'<input type="password" name="mqtt_password"[^>]*>', page).group(0)
        print("edit form: says a password is stored, holds none; new form: neither  OK")

        assert save() == (200, "") and stored() == SECRET, stored()
        assert save(mqtt_password=MASK)[0] == 200 and stored() == SECRET, stored()
        assert save(mqtt_password="typed-new-2")[0] == 200 and stored() == "typed-new-2"
        assert save(mqtt_password_clear="on")[0] == 200 and stored() == "", stored()
        assert save(mqtt_password=SECRET, mqtt_password_clear="on")[0] == 200, "typed wins"
        assert stored() == SECRET, stored()
        print("write-only form: blank keeps, typed replaces, remove clears, the mask keeps  OK")

        # an older form (no marker) sets what it posts, a blank field included
        assert save(mqtt_password_write_only=None, mqtt_password="older-form-3")[0] == 200
        assert stored() == "older-form-3", stored()
        assert save(mqtt_password_write_only=None)[0] == 200 and stored() == "", stored()
        assert save(mqtt_password_write_only=None, mqtt_password=SECRET)[0] == 200
        print("a form without the marker (older node, older tab, script) works as before  OK")

        # shown again after an error: what was typed, never the stored password
        st, body = save(network_port="not-a-port", mqtt_password="typed-in-error")
        assert st == 400 and SECRET not in body, (st, body[:300])
        assert 'value="typed-in-error"' in body, body[:300]
        st, body = save(network_port="not-a-port", mqtt_password_clear="on")
        assert st == 400 and SECRET not in body and "•••••• (unchanged)" in body
        assert re.search(r'name="mqtt_password_clear"\s+checked', body), "the box stays ticked"
        assert stored() == SECRET, stored()
        print("a form shown again after an error has what was typed, not the stored one  OK")

        # REST API: a PUT of what a viewer token reads keeps the stored password
        st, _, page = b.post("/settings/api-token", {"api_token_role": "operator"})
        token = re.search(r"s2n_[A-Za-z0-9_-]+", page).group(0)

        def api(method, path, doc=None):
            req = urllib.request.Request(srv.base + path, method=method,
                                         data=json.dumps(doc).encode() if doc else None,
                                         headers={"Authorization": f"Bearer {token}",
                                                  "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())

        doc = api("GET", "/api/v1/mappings/m1")
        assert doc["mqtt"]["password"] == SECRET, "an operator token reads the real password"
        doc.pop("status")
        doc["mqtt"]["password"] = MASK
        assert api("PUT", "/api/v1/mappings/m1", doc)["mqtt"]["password"] == SECRET
        assert stored() == SECRET, stored()
        doc["mqtt"]["password"] = "api-new-4"
        api("PUT", "/api/v1/mappings/m1", doc)
        assert stored() == "api-new-4", stored()
        print("REST API: a PUT with the viewer mask keeps the stored password  OK")

        st, _, page = b.get("/settings")
        assert st == 200 and ("The export holds each mapping’s MQTT broker password in plain "
                              "text: keep the file as safe as a password.") in page
        st, _, export = b.get("/settings/config/export")
        assert st == 200 and '"password": "api-new-4"' in export, "the export carries it"
        print("the export, which holds the MQTT passwords, says so  OK")
    print("\nPASS: the MQTT password is write-only in the mapping form")


if __name__ == "__main__":
    main()
