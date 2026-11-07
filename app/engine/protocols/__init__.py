"""Protocol session factory."""
from __future__ import annotations

from typing import Callable, Optional

from .base import ProtocolSession
from .raw import RawSession
from .telnet import TelnetSession
from .rfc2217 import Rfc2217Session


def make_session(protocol: str, serial_instance, poll_interval: float = 1.0,
                 read_only: bool = False,
                 log: Optional[Callable[[str], None]] = None) -> ProtocolSession:
    """``log``: where a session reports what the client sent that it refused or ignored."""
    if protocol == "raw":
        return RawSession()
    if protocol == "telnet":
        return TelnetSession()
    if protocol == "rfc2217":
        return Rfc2217Session(serial_instance, poll_interval=poll_interval, read_only=read_only,
                              log=log)
    raise ValueError(f"Unknown protocol: {protocol!r}")


__all__ = ["ProtocolSession", "make_session"]
