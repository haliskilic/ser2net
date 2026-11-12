"""Text written into a line of its own: an all.log / stdout line, a mapping's log line, an
audit record, a notification's subject.

A value in such a line (a mapping name, a user name, a path, a message from a device or
an identity provider) may hold a line break or another control character. Written as
it is, it would start a line of its own (one that looks like any other record), cut a
record short where it is read back, make an e-mail header invalid, or reach a terminal
showing the log as an escape sequence. ``one_line`` turns each such character into a
space; everything else - letters of any script, bidi marks - stays as it is.
"""
from __future__ import annotations

import re

# The C0 controls (tab, line feed, carriage return, escape ...), DEL, the C1 controls (NEL,
# CSI ...) and the Unicode line and paragraph separators: every character str.splitlines()
# ends a line at, and every one a terminal acts on.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def one_line(text) -> str:
    """*text* with each control character (CONTROL) replaced by a space."""
    return CONTROL.sub(" ", str(text))
