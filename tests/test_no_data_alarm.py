"""The no-data alarm end to end, over a socat PTY pair (no hardware needed).

A running mapping whose device sends nothing past its alarm is logged and notified
(no_data), and data_back once the device sends again; in reply-only mode, only a request
sent to the device that gets no answer counts, and the answer ends it.

Run: python3 tests/test_no_data_alarm.py
"""
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _server import free_port  # noqa: E402

from app import notify  # noqa: E402
from app.config import AppConfig, MappingConfig, NotificationSettings  # noqa: E402
from app.engine.supervisor import Supervisor  # noqa: E402


async def start_socat():
    proc = await asyncio.create_subprocess_exec(
        "socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    devs = []
    while len(devs) < 2:
        line = await proc.stderr.readline()
        if not line:
            raise RuntimeError("socat did not report PTYs")
        if m := re.search(rb"PTY is (\S+)", line):
            devs.append(m.group(1).decode())
    return proc, devs[0], devs[1]


class State:
    def __init__(self, mapping, supervisor):
        self.config = AppConfig.from_dict({})
        self.config.mappings = [mapping]
        self.config.notifications = NotificationSettings.from_dict({
            "enabled": True, "events": ["no_data", "data_back"], "repeat_s": 0,
            "webhook_url": "https://example.invalid/hook"})
        self.supervisor = supervisor
        self.logs = []
        self.cluster = None

    def log(self, msg):
        self.logs.append(msg)

    def make_mapping_logger(self, m):
        return lambda msg: self.log(f"[{m.name}] {msg}")


async def wait_for(cond, seconds):
    for _ in range(int(seconds / 0.1)):
        if cond():
            return True
        await asyncio.sleep(0.1)
    return cond()


async def main():
    import serial
    notify.POLL_S = 0.3
    proc, dev_a, dev_b = await start_socat()
    sup = Supervisor(logger=lambda m: None)
    port = free_port()
    m = MappingConfig.from_dict({"id": "nd1", "name": "meter", "serial": {"port": dev_a},
                                 "network": {"protocol": "raw", "bind_ip": "127.0.0.1",
                                             "port": port}})
    m.options.no_data_alarm_s = 2  # (an edit takes 10 s at least; the engine has no minimum)
    state = State(m, sup)
    n = notify.Notifier(state)
    sent = []

    async def deliver(event, mapping_name, detail, settings=None, warnings=None):
        sent.append((event, detail))
        return []
    n.deliver = deliver
    device = None
    try:
        ok, msg = await sup.apply_mapping(m)
        assert ok, msg
        device = serial.Serial(dev_b, timeout=0)
        assert await wait_for(lambda: sup.status("nd1").get("state") == "running", 5)
        await n.start()
        assert await wait_for(lambda: sent, 6), "no no_data from a silent device"
        assert sent[0][0] == "no_data" and sent[0][1].startswith(f"no data from {dev_a} for "), sent
        assert any("(no-data alarm: 2 s)" in ln for ln in state.logs), state.logs
        await asyncio.to_thread(device.write, b"reading 42\r\n")
        assert await wait_for(lambda: len(sent) >= 2, 4), sent
        assert sent[1] == ("data_back", dev_a), sent
        last_rx = sup.status("nd1")["last_rx"]
        assert last_rx and sup.status("nd1")["rx_idle_s"] < 2
        print(f"silent device: no_data after its alarm, data_back when it sends ({sent[:2]})  OK")

        # reply-only: silence alone is fine; a request without an answer is not
        m.options.no_data_reply_only = True
        await asyncio.sleep(3)
        assert len(sent) == 2, "reply-only: a device that wasn't asked may be silent"
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x01\x03\x00\x00\x00\x01\x84\x0a")  # a request ...
        await writer.drain()
        assert await wait_for(lambda: len(sent) >= 3, 6), sent  # ... that gets no answer
        assert sent[2][0] == "no_data" and sent[2][1].startswith(f"no answer from {dev_a} for"), sent
        await asyncio.to_thread(device.read, 64)
        await asyncio.to_thread(device.write, b"\x01\x03\x02\x00\x2a\x39\x9b")  # the answer
        assert await wait_for(lambda: len(sent) >= 4, 4), sent
        assert sent[3] == ("data_back", dev_a), sent
        writer.close()
        print("reply-only: a request without an answer is no_data, the answer data_back  OK")
    finally:
        await n.stop()
        await sup.stop_all()
        if device is not None:
            device.close()
        proc.terminate()
        await proc.wait()
    print("\nPASS: no-data alarm over a PTY")


if __name__ == "__main__":
    asyncio.run(main())
