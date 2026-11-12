"""The browser viewers of a mapping's traffic — the console and the hex view — from the
feed the bridges fill to the WebSocket's close (no socat, no serial device).

  - a feed's end is a flag outside its queue: a viewer that fell behind until its feed is
    full still learns that the mapping stopped (its sender sends what is left, then ends),
    on every kind of runner, the stand-in for a mapping that can't be built included.

Run: python3 tests/test_console_feed.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig  # noqa: E402
from app.engine.bridge import FROM_DEVICE, MappingRunner  # noqa: E402
from app.engine.modbus_gateway import ModbusGatewayRunner  # noqa: E402
from app.engine.supervisor import _Unbuildable  # noqa: E402
from app.web.routes.console import _Monitor, _TrafficMonitor  # noqa: E402


def test_end_of_a_full_feed():
    async def run():
        con, hexv = _Monitor(), _TrafficMonitor(to_device=True)
        for mon, feed in ((con, con.feed), (hexv, lambda b: hexv.feed_traffic(FROM_DEVICE, b))):
            for i in range(mon.MAX_ITEMS + 10):  # a viewer that fell behind: full
                feed(b"%d" % i)
            mon.close()
            feed(b"late")
            items, ended = await asyncio.wait_for(mon.take(), 2)
            assert ended, "the end of a full feed was lost"
            assert len(items) == mon.MAX_ITEMS, len(items)   # the oldest went first
            last = items[-1] if isinstance(mon, _Monitor) else items[-1][2]
            assert last == b"%d" % (mon.MAX_ITEMS + 9), last  # nothing after the end
    asyncio.run(run())
    print("a full console / hex-view feed still delivers its end, after what is left  OK")


def test_runners_end_their_viewers():
    async def run():
        net = MappingConfig.from_dict({"name": "n", "kind": "net", "serial": {"port": "/dev/none"}})
        modbus = MappingConfig.from_dict({"name": "m", "kind": "net", "serial": {"port": "/dev/none"},
                                          "network": {"protocol": "modbus"}})
        for runner in (MappingRunner(net, logger=None), ModbusGatewayRunner(modbus, logger=None),
                       _Unbuildable(ValueError("bad value"))):
            con, hexv = _Monitor(), _TrafficMonitor(to_device=True)
            runner.add_monitor(con)
            runner.add_traffic_monitor(hexv)
            for _ in range(con.MAX_ITEMS):
                con.feed(b"x")
            await runner.stop()
            for mon in (con, hexv):
                _items, ended = await asyncio.wait_for(mon.take(), 2)
                assert ended, f"{type(runner).__name__}.stop() left a viewer open"
    asyncio.run(run())
    print("stopping a bridge, a Modbus gateway or an unbuildable mapping's stand-in ends "
          "its consoles and hex views  OK")


def main():
    test_end_of_a_full_feed()
    test_runners_end_their_viewers()
    print("\nPASS: console and hex-view feeds")


if __name__ == "__main__":
    main()
