"""SER2NETWEB_BIND_IP / SER2NETWEB_PORT env override for headless/Docker (Phase 1).

In a container the interactive console picker can't run, so it would default the
admin UI to loopback and leave it unreachable behind a published port. The env
override lets compose/run set the bind address. Run: python3 tests/test_env_bind.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ser2netweb.config import AdminUI, ConfigError
from ser2netweb.runtime import _env_bind_override


def _clear():
    for name in ("SER2NETWEB_BIND_IP", "SER2NETWEB_PORT", "SER2NET_BIND_IP", "SER2NET_PORT"):
        os.environ.pop(name, None)


def test_no_env_is_noop():
    _clear()
    a = AdminUI()
    assert _env_bind_override(a) is False
    assert a.bind_ip == "127.0.0.1" and a.port == 8080
    print("no env: no override, loopback default kept  OK")


def test_env_applies_ip_and_port():
    _clear()
    os.environ["SER2NETWEB_BIND_IP"] = "0.0.0.0"
    os.environ["SER2NETWEB_PORT"] = "9000"
    try:
        a = AdminUI()
        assert _env_bind_override(a) is True
        assert a.bind_ip == "0.0.0.0" and a.port == 9000
    finally:
        _clear()
    print("env: bind IP + port applied  OK")


def test_bad_port_keeps_default():
    _clear()
    os.environ["SER2NETWEB_BIND_IP"] = "0.0.0.0"
    os.environ["SER2NETWEB_PORT"] = "not-a-number"
    try:
        a = AdminUI()
        notes = []
        assert _env_bind_override(a, notes) is True
        assert a.bind_ip == "0.0.0.0" and a.port == 8080  # invalid port ignored
        assert notes == ["WARNING: SER2NETWEB_PORT 'not-a-number' is not a number and is "
                         "ignored — the admin UI uses port 8080"], notes
    finally:
        _clear()
    print("env: invalid port ignored (with a warning), default kept  OK")


def test_unusable_values_rejected():
    """The UI could not start on these: a clear error (start-up turns it into one line)."""
    for ip, port, expect in (("eth0", "", "SER2NETWEB_BIND_IP must be an IP address"),
                             ("192.168.1.300", "", "SER2NETWEB_BIND_IP must be an IP address"),
                             ("0.0.0.0", "0", "SER2NETWEB_PORT must be a port number"),
                             ("0.0.0.0", "65536", "SER2NETWEB_PORT must be a port number")):
        _clear()
        os.environ["SER2NETWEB_BIND_IP"] = ip
        os.environ["SER2NETWEB_PORT"] = port
        a = AdminUI()
        try:
            _env_bind_override(a)
        except ConfigError as e:
            assert expect in str(e), str(e)
        else:
            raise AssertionError(f"{ip!r}:{port!r} was accepted")
        finally:
            _clear()
        assert (a.bind_ip, a.port) == ("127.0.0.1", 8080), "nothing applied"
    for ip in ("::", "fe80::1", " 10.0.0.5 "):
        os.environ["SER2NETWEB_BIND_IP"] = ip
        try:
            a = AdminUI()
            assert _env_bind_override(a) is True and a.bind_ip == ip.strip()
        finally:
            _clear()
    print("env: a bind IP that is not an address / a port outside 1-65535 rejected; IPv6 ok  OK")


def main():
    test_no_env_is_noop()
    test_env_applies_ip_and_port()
    test_bad_port_keeps_default()
    test_unusable_values_rejected()
    print("\nPASS: env bind override (Docker/headless)")


if __name__ == "__main__":
    main()
