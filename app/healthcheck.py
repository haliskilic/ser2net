"""`ser2net --healthcheck`: is the web UI of this data directory's instance answering?

Used by the Docker image's HEALTHCHECK, also handy for systemd or monitoring scripts.
Reads the admin UI address from config.json (SER2NET_BIND_IP / SER2NET_PORT win, as at
start-up), asks /healthz and exits 0 when it answers "ok", else 1. A wildcard bind
(0.0.0.0 / ::, or every interface of both families: "" / SER2NET_BIND_IP=*) is probed on
loopback; TLS is accepted without a certificate check (the probe only asks whether the
server is up, and a self-signed certificate is normal); a TLS change not yet in effect (it
is, from the next start) is tried the other way. The
probe always connects directly: HTTP(S)_PROXY / ALL_PROXY, set so that webhooks or SSO
can reach the Internet, don't apply to it. Standard library only.
"""
from __future__ import annotations

import http.client
import json
import os
import ssl
import time
import urllib.error
import urllib.request


def target(config_path: str) -> str:
    try:
        with open(config_path, encoding="utf-8") as fh:
            admin = (json.load(fh) or {}).get("admin_ui") or {}
    except (OSError, ValueError, AttributeError):
        admin = {}
    ip = (os.environ.get("SER2NET_BIND_IP") or "").strip() or admin.get("bind_ip") or "127.0.0.1"
    try:
        port = int((os.environ.get("SER2NET_PORT") or "").strip())
    except ValueError:  # unset, or not a number (which start-up ignores as well)
        port = admin.get("port") or 8080
    host = {"0.0.0.0": "127.0.0.1", "::": "::1", "*": "127.0.0.1"}.get(ip, ip)
    if ":" in host:
        host = f"[{host}]"
    scheme = "https" if admin.get("tls_cert") and admin.get("tls_key") else "http"
    return f"{scheme}://{host}:{int(port)}/healthz"


def check(config_path: str, timeout: float = 5.0) -> tuple[bool, str]:
    """(healthy, what to print). A certificate set in Settings (or generated there) is
    used from the next start, while config.json names it at once: when the configured
    scheme gets no answer, the other one is asked in the time left, so an instance that
    still runs plain http (or still https) until its restart is not reported unhealthy."""
    url = target(config_path)
    deadline = time.monotonic() + timeout
    ok, msg = _probe(url, timeout)
    left = deadline - time.monotonic()
    if not ok and left > 0.5:
        other = ("http:" + url[6:]) if url.startswith("https:") else ("https:" + url[5:])
        if _probe(other, left)[0]:
            return True, f"ok: {other} (TLS changes take effect at the next start)"
    return ok, msg


def _probe(url: str, timeout: float) -> tuple[bool, str]:
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]  # direct
    if url.startswith("https:"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    try:
        with urllib.request.build_opener(*handlers).open(url, timeout=timeout) as r:
            body = r.read(64).strip()
    except (OSError, urllib.error.URLError, ValueError, http.client.HTTPException) as e:
        return False, f"unhealthy: {url}: {getattr(e, 'reason', e)}"
    if body != b"ok":
        return False, f"unhealthy: {url} answered {body[:20]!r}"
    return True, f"ok: {url}"
