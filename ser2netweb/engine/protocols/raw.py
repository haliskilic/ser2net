"""Raw mode: byte-for-byte pass-through, no escaping, no negotiation.

Matches a plain TCP / pyserial ``socket://`` client. Critically, 0xFF is NOT
doubled here (that is telnet's job) — raw must deliver every byte verbatim.
"""
from __future__ import annotations

from .base import ProtocolSession


class RawSession(ProtocolSession):
    pass  # all base methods are already pure pass-through
