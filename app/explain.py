"""What the common OS and network errors mean, in the page's language.

A serial port that can't be opened, a port already in use, a refused connection or a
missing file reach the web UI as the operating system's or a library's own text
("[Errno 98] error while attempting to bind on address ('0.0.0.0', 4001): address
already in use"), in English whatever the page's language. explain() turns the usual
ones into a short sentence that says what to do; the raw text stays where it is
(the logs, the REST API's last_error, a title= tooltip next to the sentence), so
nothing is lost for support. Anything it doesn't know gives None: show the text as it is.
"""
from __future__ import annotations

import errno
import re

from .i18n import _

# the same condition on Linux, macOS / BSD and Windows (WSA... codes)
_IN_USE = {errno.EADDRINUSE, 98, 48, 10048}
_NO_ADDRESS = {errno.EADDRNOTAVAIL, 99, 49, 10049}
_REFUSED = {errno.ECONNREFUSED, 111, 61, 10061}
_TIMED_OUT = {errno.ETIMEDOUT, 110, 60, 10060}
_UNREACHABLE = {errno.EHOSTUNREACH, errno.ENETUNREACH, 113, 101, 65, 51, 10065, 10051}
_NOT_FOUND = {errno.ENOENT, 3}            # 3: Windows ERROR_PATH_NOT_FOUND
_DENIED = {errno.EACCES, errno.EPERM, 5}  # 5: Windows ERROR_ACCESS_DENIED
_BUSY = {errno.EBUSY}

# a resolver error has numbers of its own (EAI_*: -2, 8, 11001 ...): told by its words
_NAME_WORDS = ("name or service not known", "nodename nor servname", "getaddrinfo failed",
               "name resolution", "no address associated with hostname")
# (kind, words in the text) when there is no errno to go by: the Windows texts, pyserial's
_WORDS = (
    ("in_use", ("address already in use", "only one usage of each socket address")),
    ("no_address", ("assign requested address", "not valid in its context")),
    ("refused", ("connection refused", "actively refused")),
    ("timed_out", ("timed out",)),
    ("unreachable", ("no route to host", "network is unreachable", "host is unreachable")),
    ("busy", ("device or resource busy", "exclusively lock port")),
    ("not_found", ("no such file or directory", "cannot find the file", "cannot find the path",
                   "filenotfounderror")),
    ("denied", ("permission denied", "access is denied", "operation not permitted",
                "permissionerror")),
    ("certificate", ("certificate verify failed",)),
    ("vanished", ("returned no data (device disconnected",)),
)
_ERRNO = re.compile(r"\[(?:Errno|WinError) (-?\d+)\]|\b(?:FileNotFound|Permission)Error\((\d+),")


def _kind(error) -> str:
    """The class of `error` (an exception or the text one left), or ""."""
    text = str(error)
    low = text.lower()
    if isinstance(error, OSError) and isinstance(getattr(error, "winerror", None), int):
        codes = [error.winerror]  # type: ignore[attr-defined]
    elif isinstance(error, OSError) and isinstance(error.errno, int):
        codes = [error.errno]
    else:
        codes = [int(a or b) for a, b in _ERRNO.findall(text)]
    if any(w in low for w in _NAME_WORDS):
        return "name"
    for code in reversed(codes):   # the innermost one (pyserial repeats the outer)
        for kind, known in (("in_use", _IN_USE), ("no_address", _NO_ADDRESS),
                            ("refused", _REFUSED), ("timed_out", _TIMED_OUT),
                            ("unreachable", _UNREACHABLE), ("not_found", _NOT_FOUND),
                            ("denied", _DENIED), ("busy", _BUSY)):
            if code in known:
                return kind
    if isinstance(error, TimeoutError):
        return "timed_out"
    return next((kind for kind, words in _WORDS if any(w in low for w in words)), "")


def explain(error, about: str = "") -> str | None:
    """A short sentence, in the page's language, for what `error` (an exception, or the
    text an error left in a status) means and what to do; None for an error it doesn't
    know. `about`: "serial" (a serial device), "file" (a file ser2net reads or writes),
    "net" (a connection out); "" decides by the text (a mapping's last error)."""
    if error is None or error == "":
        return None
    kind = _kind(error)
    text = str(error).lower()
    if not about:
        about = ("listen" if "bind" in text else
                 "serial" if "port" in text and ("open" in text or "serial" in text) else "")
    if kind == "in_use":
        return _("The port is already in use by another program (or another mapping).")
    if kind == "no_address":
        return _("This machine has no such address to listen on: check the bind IP.")
    if kind == "refused":
        return _("The connection was refused: nothing listens at that address and port.")
    if kind == "name":
        return _("The host name could not be resolved (DNS).")
    if kind == "timed_out":
        return _("No answer in time.")
    if kind == "unreachable":
        return _("The address can't be reached from here (no network route to it).")
    if kind == "busy":
        return _("The serial device is busy: another program has it open.")
    if kind == "vanished":
        return _("The serial device stopped answering: unplugged, or another program "
                 "uses it too.")
    if kind == "certificate":
        return _("The server's certificate was not accepted.")
    if kind == "not_found":
        if about == "serial":
            return _("The serial device is not there: unplugged, or it has another name now.")
        return _("The file or folder does not exist.")
    if kind == "denied":
        if about == "serial":
            return _("No permission to open the serial device: on Linux, add the user "
                     "ser2net runs as to the dialout group; on Windows, another program "
                     "may have the port open.")
        if about == "listen":
            return _("No permission to listen on this port: ports below 1024 need extra "
                     "rights.")
        return _("Permission denied.")
    return None
