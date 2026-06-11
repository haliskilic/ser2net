"""Per-mapping TLS data bridge: a TLS-wrapped TCP server mapping <-> serial, e2e.

Brings up a real bridge with network.tls=True (self-signed cert), connects a TLS
client, and verifies the channel is actually encrypted and bytes flow both ways
between the TLS socket and the serial device. Needs socat + openssl (skips if absent).

Run: python3 tests/test_tls_bridge.py
"""
import asyncio
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine.supervisor import Supervisor


async def socat():
    p = await asyncio.create_subprocess_exec(
        "socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    devs = []
    while len(devs) < 2:
        m = re.search(rb"PTY is (\S+)", await p.stderr.readline())
        if m:
            devs.append(m.group(1).decode())
    return p, devs[0], devs[1]


def gen_cert(tmp):
    cert, key = os.path.join(tmp, "cert.pem"), os.path.join(tmp, "key.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=localhost"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert, key


async def main():
    if not shutil.which("socat") or not shutil.which("openssl"):
        print("SKIP: socat/openssl not available")
        return
    import serial

    tmp = tempfile.mkdtemp(prefix="ser2net_tls_")
    cert, key = gen_cert(tmp)
    p, dev_a, dev_b = await socat()
    sup = Supervisor(logger=lambda s: None)
    port = 47360
    m = MappingConfig.from_dict({
        "name": "tls", "enabled": True, "serial": {"port": dev_a, "baudrate": 115200},
        "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                    "port": port, "tls": True, "tls_cert": cert, "tls_key": key}})
    try:
        ok, msg = await sup.apply_mapping(m)
        assert ok, msg
        await asyncio.sleep(0.5)
        device = serial.Serial(dev_b, 115200, timeout=1)

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # self-signed; we only assert encryption here
        r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)

        sslobj = w.get_extra_info("ssl_object")
        assert sslobj is not None, "the data connection is not TLS"

        # a plaintext client must NOT be able to talk to the TLS listener
        try:
            pr, pw = await asyncio.open_connection("127.0.0.1", port)
            pw.write(b"plain"); await pw.drain()
            assert await asyncio.wait_for(pr.read(16), timeout=2) == b"", "plaintext accepted on TLS port"
            pw.close()
        except (ssl.SSLError, ConnectionError, asyncio.TimeoutError, OSError):
            pass  # rejected, as expected

        # serial -> TLS client
        await asyncio.to_thread(device.write, b"hello-tls"); await asyncio.to_thread(device.flush)
        got = await asyncio.wait_for(r.read(32), timeout=3)
        assert b"hello-tls" in got, got
        # TLS client -> serial
        w.write(b"to-serial"); await w.drain()
        await asyncio.sleep(0.3)
        back = await asyncio.to_thread(device.read, 32)
        assert back == b"to-serial", back
        print(f"per-mapping TLS bridge: encrypted ({sslobj.version()}) bidirectional  OK")

        w.close(); device.close()
        print("\nPASS: per-mapping TLS data bridge")
    finally:
        await sup.stop_all()
        p.terminate(); await p.wait()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
