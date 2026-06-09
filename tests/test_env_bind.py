"""SER2NET_BIND_IP / SER2NET_PORT env override for headless/Docker (Phase 1).

In a container the interactive console picker can't run, so it would default the
admin UI to loopback and leave it unreachable behind a published port. The env
override lets compose/run set the bind address. Run: python3 tests/test_env_bind.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import AdminUI
from app.runtime import _env_bind_override


def _clear():
    os.environ.pop("SER2NET_BIND_IP", None)
    os.environ.pop("SER2NET_PORT", None)


def test_no_env_is_noop():
    _clear()
    a = AdminUI()
    assert _env_bind_override(a) is False
    assert a.bind_ip == "127.0.0.1" and a.port == 8080
    print("no env: no override, loopback default kept  OK")


def test_env_applies_ip_and_port():
    _clear()
    os.environ["SER2NET_BIND_IP"] = "0.0.0.0"
    os.environ["SER2NET_PORT"] = "9000"
    try:
        a = AdminUI()
        assert _env_bind_override(a) is True
        assert a.bind_ip == "0.0.0.0" and a.port == 9000
    finally:
        _clear()
    print("env: bind IP + port applied  OK")


def test_bad_port_keeps_default():
    _clear()
    os.environ["SER2NET_BIND_IP"] = "0.0.0.0"
    os.environ["SER2NET_PORT"] = "not-a-number"
    try:
        a = AdminUI()
        assert _env_bind_override(a) is True
        assert a.bind_ip == "0.0.0.0" and a.port == 8080  # invalid port ignored
    finally:
        _clear()
    print("env: invalid port ignored, default kept  OK")


def main():
    test_no_env_is_noop()
    test_env_applies_ip_and_port()
    test_bad_port_keeps_default()
    print("\nPASS: env bind override (Docker/headless)")


if __name__ == "__main__":
    main()
