"""Protocol session factory."""
from __future__ import annotations

from .base import ProtocolSession
from .raw import RawSession
from .telnet import TelnetSession
from .rfc2217 import Rfc2217Session


def make_session(protocol: str, serial_instance, poll_interval: float = 1.0) -> ProtocolSession:
    if protocol == "raw":
        return RawSession()
    if protocol == "telnet":
        return TelnetSession()
    if protocol == "rfc2217":
        return Rfc2217Session(serial_instance, poll_interval=poll_interval)
    raise ValueError(f"Unknown protocol: {protocol!r}")


__all__ = ["ProtocolSession", "make_session"]
