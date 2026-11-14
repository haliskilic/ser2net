"""Connect-out failover (network.remote_targets, failover_return_s).

Targets are TCP servers on 127.0.0.1 (OS-assigned ports); the serial device is a stand-in.
  - the first target does not answer: the link goes to the next one in the list (the
    status names the target, the log says which one did not answer);
  - the link sticks to the target that works: after its connection closes it is tried
    first again, also when the first target is back by then;
  - when the target in use is gone, the list is gone through from there, round to the
    first;
  - failover_return_s: while the link is on another target the first one is tried
    again on that interval, and the link moves back once it answers;
  - no target answers: the link is down, the error names each target, and the round is
    made again after the backoff, from the first target;
  - one target (the default, every config written without the keys): as before;
  - edits check the entries (host:port, [IPv6]:port, at most 8) and the interval; a
    stored entry that is no target is left out and named in the log;
  - the form fields have a presence marker.

Run: python3 tests/test_connect_failover.py
"""
import asyncio
import contextlib
import os
import socket
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _device import FakeDevice, running, until

from ser2netweb.config import (
    ConfigError, MappingConfig, connect_targets, failover_return, remote_target,
)
from ser2netweb.engine import bridge
from ser2netweb.web.routes.forms import _build_mapping_dict, _preserve_unmanaged_fields


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Target:
    """A connect-out target: counts the connections it got, keeps what it was sent."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.connections = 0
        self.heard = bytearray()
        self.server = None
        self._writers = []

    async def start(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", self.port)
        return self

    async def _serve(self, reader, writer):
        self.connections += 1
        self._writers.append(writer)
        with contextlib.suppress(ConnectionError, OSError):
            while data := await reader.read(4096):
                self.heard += data
        with contextlib.suppress(ValueError):
            self._writers.remove(writer)
        writer.close()

    @property
    def linked(self) -> bool:
        return bool(self._writers)

    def drop(self):
        """Close the connections it has (it keeps listening)."""
        for w in list(self._writers):
            w.close()

    async def stop(self):
        if self.server is not None:
            self.server.close()
            self.server = None
        for w in list(self._writers):
            w.transport.abort()


def mapping(first: int, *others: int, back=0, **network):
    return MappingConfig.from_dict({
        "name": "out", "serial": {"port": "/dev/ttyFAKE-out"},
        "network": {"mode": "client", "protocol": "raw", "remote_host": "127.0.0.1",
                    "remote_port": first, "connect_timeout_s": 2,
                    "remote_targets": [f"127.0.0.1:{p}" for p in others],
                    "failover_return_s": back, **network}})


async def _failover():
    device, logs = await FakeDevice().start(), []
    a, b = Target(free_port()), await Target(free_port()).start()
    try:
        async with running(mapping(a.port, b.port), device, logs) as runner:
            st = runner.status
            assert any(f"connect-out to 127.0.0.1:{a.port} (then 127.0.0.1:{b.port})" in ln
                       for ln in logs), logs
            await until(lambda: b.linked and st.link == "up")
            assert st.as_dict()["link_target"] == f"127.0.0.1:{b.port}"
            assert st.connect_failures == 1, "the first target's failed attempt"
            assert any(f"connected out to 127.0.0.1:{b.port}" in ln and "target 2 of 2" in ln
                       and f"127.0.0.1:{a.port} did not answer" in ln for ln in logs), logs
            device.say(b"data")
            await until(lambda: bytes(b.heard) == b"data")
            # the first target comes back: without failover_return_s the link stays put,
            # also after its connection was closed
            await a.start()
            b.drop()
            await until(lambda: b.connections == 2 and b.linked)
            assert a.connections == 0 and st.link_target == f"127.0.0.1:{b.port}"
            # the target in use goes away: on through the list, round to the first
            await b.stop()
            await until(lambda: a.linked and st.link == "up")
            assert st.link_target == f"127.0.0.1:{a.port}"
            device.say(b"more")
            await until(lambda: bytes(a.heard) == b"more")
    finally:
        await a.stop()
        await b.stop()
        await device.close()


async def _return_to_first():
    device, logs = await FakeDevice().start(), []
    a, b = Target(free_port()), await Target(free_port()).start()
    try:
        with mock.patch.object(bridge, "failover_return", lambda net: 0.3):
            async with running(mapping(a.port, b.port, back=60), device, logs) as runner:
                st = runner.status
                await until(lambda: b.linked and st.link == "up")
                await asyncio.sleep(0.8)  # tried meanwhile, in vain: the link stays
                assert b.linked and b.connections == 1
                await a.start()
                await until(lambda: a.linked and not b.linked)
                await until(lambda: st.link == "up")
                assert st.link_target == f"127.0.0.1:{a.port}" and a.connections == 1
                assert any("closed after" in ln and "the first target" in ln
                           and "answers again" in ln for ln in logs), logs
                device.say(b"home")
                await until(lambda: bytes(a.heard) == b"home")
                # on the first target nothing is tried any more
                await asyncio.sleep(0.8)
                assert a.connections == 1 and b.connections == 1
    finally:
        await a.stop()
        await b.stop()
        await device.close()


async def _none_answers():
    device, logs = await FakeDevice().start(), []
    a, b = Target(free_port()), Target(free_port())
    try:
        async with running(mapping(a.port, b.port), device, logs) as runner:
            st = runner.status
            await until(lambda: st.link == "down")
            assert f"127.0.0.1:{a.port}: " in st.link_error and \
                f"127.0.0.1:{b.port}: " in st.link_error, st.link_error
            await until(lambda: st.connect_failures >= 4)  # (the rounds go on)
            assert any("connect-out failed" in ln for ln in logs), logs
            await b.start()
            await until(lambda: b.linked and st.link == "up")
            assert st.link_error == ""
    finally:
        await a.stop()
        await b.stop()
        await device.close()


async def _one_target():
    device, logs = await FakeDevice().start(), []
    a = await Target(free_port()).start()
    try:
        async with running(mapping(a.port), device, logs) as runner:
            await until(lambda: a.linked and runner.status.link == "up")
            assert any(ln.startswith(f"connect-out to 127.0.0.1:{a.port} (raw") for ln in logs)
            assert any(ln == f"connected out to 127.0.0.1:{a.port}" for ln in logs), logs
            await a.stop()
            await until(lambda: runner.status.link == "down")
            assert not runner.status.link_error.startswith("127.0.0.1:"), \
                "one target: the error as before, without the target's name in front"
    finally:
        await a.stop()
        await device.close()


def test_runner():
    asyncio.run(_failover())
    print("the next target when the first does not answer; the link sticks to the one that "
          "works; on through the list when it goes away  OK")
    asyncio.run(_return_to_first())
    print("failover_return_s: back to the first target once it answers  OK")
    asyncio.run(_none_answers())
    print("no target answers: link down, each target's error, tried again  OK")
    asyncio.run(_one_target())
    print("one target: as before  OK")


def test_settings():
    assert remote_target("plc.example:4001") == ("plc.example", 4001)
    assert remote_target(" 10.0.0.9:502 ") == ("10.0.0.9", 502)
    assert remote_target("[fd00::9]:2002") == ("fd00::9", 2002)
    for bad in ("plc.example", "plc:0", "plc:65536", "fd00::9:2002", "[plc]:1", ":4001",
                "a b:1", "plc:4001x", "", 7, "[10.0.0.1]:5"):
        try:
            remote_target(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} was read as a target")
    m = mapping(4001)
    assert connect_targets(m.network) == ([("127.0.0.1", 4001)], []) and \
        failover_return(m.network) == 0.0, "the defaults: one target"
    m = mapping(4001, 4002, 4001, 4002, back=30)
    assert connect_targets(m.network)[0] == [("127.0.0.1", 4001), ("127.0.0.1", 4002)], \
        "each target once"
    assert failover_return(m.network) == 30.0
    m.validate_input()
    # stored values an edit refuses: validate() takes them, the runner leaves them out
    stored = mapping(4001, back=3)
    stored.network.remote_targets = ["nonsense", "10.0.0.9:502"]
    stored.validate()
    assert connect_targets(stored.network) == (
        [("127.0.0.1", 4001), ("10.0.0.9", 502)], ["nonsense"])
    assert failover_return(stored.network) == 0.0
    for change in ({"remote_targets": ["nonsense"]}, {"failover_return_s": 3},
                   {"failover_return_s": 86401},
                   {"remote_targets": [f"10.0.0.{i}:1" for i in range(9)]}):
        bad = mapping(4001)
        for key, value in change.items():
            setattr(bad.network, key, value)
        try:
            bad.validate_input()
        except ConfigError:
            continue
        raise AssertionError(f"an edit took {change}")
    print("settings: host:port entries, each once, at most 8; the interval's range; stored "
          "bad values are left out  OK")


async def _bad_entry_logged():
    device, logs = await FakeDevice().start(), []
    a = await Target(free_port()).start()
    m = mapping(a.port)
    m.network.remote_targets = ["nonsense"]
    try:
        async with running(m, device, logs):
            await until(lambda: a.linked)
            assert any("'nonsense' is not a host and a port" in ln and "left out" in ln
                       for ln in logs), logs
    finally:
        await a.stop()
        await device.close()


def test_form():
    base = {"name": "out", "kind": "net", "enabled": "on", "serial_port": "COM7",
            "serial_baudrate": "9600", "network_mode": "client", "network_protocol": "raw",
            "network_remote_host": "10.0.0.1", "network_remote_port": "4001"}
    existing = mapping(4001, 4002, back=60)

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing if form.get("id") else None, form)
        return m.network.remote_targets, m.network.failover_return_s

    assert parsed(dict(base, id=existing.id)) == (["127.0.0.1:4002"], 60), "absent: kept"
    assert parsed(dict(base, id=existing.id, network_remote_targets="")) == ([], 0)
    assert parsed(dict(base, network_remote_targets="b.example:1, [fd00::2]:2",
                       network_failover_return_s="30")) == (["b.example:1", "[fd00::2]:2"], 30)
    assert parsed(base) == ([], 0)
    print("form: the other targets and the return time are kept when the form leaves them "
          "out, set when shown  OK")


def main():
    test_settings()
    test_form()
    asyncio.run(_bad_entry_logged())
    print("a stored entry that is no target is left out and named in the log  OK")
    test_runner()
    print("\nPASS: connect-out failover (remote_targets, failover_return_s)")


if __name__ == "__main__":
    main()
