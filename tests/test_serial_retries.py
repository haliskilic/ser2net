"""How a mapping keeps trying a serial port it can't open (POSIX PTYs, no socat).

  - serial<->serial bridge: while one port is missing, the other one is not opened and
    closed again at every retry (each open pulses DTR/RTS: a board that resets on DTR
    would reset every few seconds). The side that failed is opened first next time, so
    with B missing A is opened once, and again only once B is there; with A missing, B is
    never opened.
The retries are made fast (Backoff.pause patched); ports are PTYs, a missing one a path
that does not exist yet (a symlink to a PTY "plugs it in").

Run: python3 tests/test_serial_retries.py
"""
import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine import serial_io, serial_link
from app.engine.bridge import MappingRunner


class Pty:
    def __init__(self):
        self.master, slave = os.openpty()
        self.path = os.ttyname(slave)
        os.close(slave)

    def close(self):
        os.close(self.master)


def _fast_pause(self):
    self.attempt += 1
    return 0.05


@contextlib.contextmanager
def fast_retries():
    with mock.patch.object(serial_link.Backoff, "pause", _fast_pause):
        yield


@contextlib.contextmanager
def counted_opens(opens):
    real = serial_io.open_serial

    async def counting(settings):
        opens.append(settings.port)
        return await real(settings)

    with mock.patch.object(serial_io, "open_serial", counting):
        yield


async def until(check, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not check():
        if loop.time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


async def _bridge_side_missing(folder):
    a, b = Pty(), Pty()
    missing = os.path.join(folder, "b-port")
    logs, opens = [], []
    m = MappingConfig.from_dict({"name": "sb", "kind": "serialbridge",
                                 "serial": {"port": a.path}, "serial_b": {"port": missing}})
    runner = MappingRunner(m, logger=logs.append)
    try:
        with fast_retries(), counted_opens(opens):
            await runner.start()
            await until(lambda: opens.count(missing) >= 6)
            assert opens.count(a.path) == 1, opens
            assert runner.status.state in ("reconnecting", "device-missing"), runner.status
            os.symlink(b.path, missing)  # B is plugged in
            await until(lambda: runner.status.state == "running")
            assert opens.count(a.path) == 2 and opens[-2:] == [missing, a.path], opens
            await runner.stop()

        # the other way round: with A missing, B is never opened
        opens.clear()
        m = MappingConfig.from_dict({"name": "sb2", "kind": "serialbridge",
                                     "serial": {"port": os.path.join(folder, "a-port")},
                                     "serial_b": {"port": b.path}})
        runner = MappingRunner(m, logger=logs.append)
        with fast_retries(), counted_opens(opens):
            await runner.start()
            await until(lambda: len(opens) >= 6)
            assert b.path not in opens, opens
    finally:
        await runner.stop()
        a.close()
        b.close()


def test_bridge_opens_the_missing_side_first():
    if os.name != "posix":
        print("SKIP (part): the PTY checks are POSIX only")
        return
    folder = tempfile.mkdtemp(prefix="s2n_retries_")
    try:
        asyncio.run(_bridge_side_missing(folder))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("serial<->serial bridge: with B missing A is opened once (then again only once B "
          "is there), with A missing B is never opened  OK")


if __name__ == "__main__":
    test_bridge_opens_the_missing_side_first()
    print("\nPASS: how a mapping keeps trying a serial port it can't open")
