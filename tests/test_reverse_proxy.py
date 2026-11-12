"""The web UI behind a reverse proxy, end to end against a running server:

  * the console and hex-view WebSockets take the same rule as the UI's forms
    (auth.cross_site): accepted when the browser says same-origin (Sec-Fetch-Site), or
    when its Origin is one of the hosts the request claims - Host, X-Forwarded-Host,
    Forwarded host= - port included (default ports normalised), so a proxy that
    rewrites Host but passes the browser's address on works; refused for another port,
    another host, Origin null, Sec-Fetch-Site cross-site / same-site; a session is
    still needed; a refusal is logged (at most once a minute) with what to set on the
    proxy;
  * `also`: an address the UI is published under is accepted as an Origin too.

Run: python3 tests/test_reverse_proxy.py
"""
import socket
import time

from _server import Browser, Server, free_port

from app.web import auth
from app.web.auth import hash_password

PW = "proxy-pass-1"
MID = "m1"


def ws_handshake(port: int, path: str, headers: dict) -> int:
    """The HTTP status the server answers a WebSocket handshake with (101 accepted),
    every header as given - Host included, which a WebSocket library would set itself."""
    lines = [f"GET {path} HTTP/1.1", "Upgrade: websocket", "Connection: Upgrade",
             "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version: 13"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        data = b""
        while b"\r\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    return int(data.split(b" ", 2)[1])


def test_matcher() -> None:
    def headers(**h):
        return {k.replace("_", "-").lower(): v for k, v in h.items()}

    # the hosts the request claims, port included, default ports normalised
    assert auth.cross_site(headers(origin="https://s2n.example.com", host="s2n.example.com")) == ""
    assert auth.cross_site(headers(origin="https://s2n.example.com",
                                   host="s2n.example.com:443")) == ""
    assert auth.cross_site(headers(origin="http://[::1]:8080", host="[::1]:8080")) == ""
    assert auth.cross_site(headers(origin="https://s2n.example.com", host="ser2net:8080",
                                   x_forwarded_host="s2n.example.com")) == ""
    assert auth.cross_site(headers(origin="https://gw.example.com:8443", host="127.0.0.1:8080",
                                   forwarded='for=10.0.0.9;host="gw.example.com:8443"')) == ""
    assert auth.cross_site(headers(origin="https://gw.example.com:8443", host="gw.example.com"))
    # the browser's word: same-origin (also behind a proxy that rewrites Host) / none
    assert auth.cross_site(headers(sec_fetch_site="same-origin", origin="https://a.example",
                                   host="ser2net:8080")) == ""
    assert auth.cross_site(headers(sec_fetch_site="cross-site", origin="http://h:1", host="h:1"))
    assert auth.cross_site(headers(sec_fetch_site="same-site", origin="http://h:1", host="h:1"))
    # an address the UI is published under (also=): that origin only
    published = ("https://s2n.example.com",)
    rewritten = headers(origin="https://s2n.example.com", host="127.0.0.1:8080")
    assert auth.cross_site(rewritten)
    assert auth.cross_site(rewritten, also=published) == ""
    assert auth.cross_site(headers(origin="https://s2n.example.com:443", host="127.0.0.1:8080"),
                           also=published) == ""
    for other in ("http://s2n.example.com", "https://s2n.example.com:8443",
                  "https://evil.example", "null"):
        assert auth.cross_site(headers(origin=other, host="127.0.0.1:8080"), also=published), other
    assert auth.cross_site(headers(origin="https://s2n.example.com", host="127.0.0.1:8080"),
                           also=("not an origin", "https://s2n.example.com/path")), "a path"
    print("origin check: claimed hosts with ports, Sec-Fetch-Site, published origins  OK")


def test_websockets(srv) -> None:
    b = Browser(srv.base)
    b.login("admin", PW)
    session = b.cookie("ser2net_session").value
    own = f"127.0.0.1:{srv.port}"

    def status(path, **h):
        hdr = {"Host": own, "Cookie": f"ser2net_session={session}"}
        hdr.update({k.replace("_", "-"): v for k, v in h.items() if v is not None})
        return ws_handshake(srv.port, path, hdr)

    for kind in ("console", "traffic"):
        path = f"/api/mappings/{MID}/{kind}"
        accepted = (
            ("same origin", {"Origin": "http://" + own}),
            ("no Origin (a script)", {}),
            ("browser says same-origin, Host rewritten",
             {"Host": "ser2net:8080", "Origin": "https://s2n.example.com",
              "Sec-Fetch-Site": "same-origin"}),
            ("Host rewritten, X-Forwarded-Host",
             {"Host": "ser2net:8080", "Origin": "https://s2n.example.com",
              "X-Forwarded-Host": "s2n.example.com", "X-Forwarded-Proto": "https"}),
            ("Host rewritten, Forwarded host=",
             {"Host": "127.0.0.1:8080", "Origin": "https://gw.example.com:8443",
              "Forwarded": "for=10.0.0.9;host=gw.example.com:8443;proto=https"}),
            ("Host with the default port", {"Host": "s2n.example.com:443",
                                            "Origin": "https://s2n.example.com"}),
        )
        for name, h in accepted:
            assert status(path, **h) == 101, (kind, name)
        refused = (
            ("another port", {"Origin": f"http://127.0.0.1:{srv.port + 1}"}),
            ("another host", {"Origin": "http://attacker.example"}),
            ("Origin null", {"Origin": "null"}),
            ("Host rewritten, no X-Forwarded-Host", {"Host": "ser2net:8080",
                                                    "Origin": "https://s2n.example.com"}),
            ("X-Forwarded-Host without the public port",
             {"Host": "ser2net:8080", "Origin": "https://gw.example.com:8443",
              "X-Forwarded-Host": "gw.example.com"}),
            ("Sec-Fetch-Site cross-site", {"Origin": "http://" + own,
                                           "Sec-Fetch-Site": "cross-site"}),
            ("Sec-Fetch-Site same-site", {"Origin": "http://" + own,
                                          "Sec-Fetch-Site": "same-site"}),
        )
        for name, h in refused:
            assert status(path, **h) == 403, (kind, name)
        # a matching Origin doesn't stand in for the session
        assert ws_handshake(srv.port, path, {"Host": own, "Origin": "http://" + own}) == 403
    time.sleep(0.3)
    lines = [ln for ln in srv.output().splitlines() if "opened by a page of another site" in ln]
    assert len(lines) == 1, lines   # at most once a minute
    assert (f"refused WebSocket /api/mappings/{MID}/console from 127.0.0.1" in lines[0]
            and f"Origin 'http://127.0.0.1:{srv.port + 1}'" in lines[0]
            and "proxy_set_header Host $http_host" in lines[0]
            and "ProxyPreserveHost On" in lines[0] and "X-Forwarded-Host" in lines[0]), lines[0]
    print("WebSockets: same origin, a proxy passing the browser's address (X-Forwarded-Host, "
          "Forwarded, Sec-Fetch-Site same-origin) accepted; other sites refused, logged once "
          "with the proxy settings  OK")


def main():
    test_matcher()
    cfg = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                      "pwd_version": 1}],
           "mappings": [{"id": MID, "name": "proxied", "enabled": True,
                         "serial": {"port": "/dev/ser2net-proxy-test"},
                         "network": {"mode": "server", "protocol": "raw",
                                     "bind_ip": "127.0.0.1", "port": free_port()}}]}
    with Server(cfg) as srv:
        test_websockets(srv)
    print("\nPASS: reverse proxy")


if __name__ == "__main__":
    main()
