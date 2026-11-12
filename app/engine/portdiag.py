"""What to tell the user about a mapping's serial port.

``Note`` is one such message, kept in the runner's status: the dashboard shows it in the
web UI's language, the REST API, the log and the notifications in English (text()). Kept
as a kind plus its values rather than as text, because the text is made where it is shown:
a task a web request started inherits that request's language.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


def _english(message: str) -> str:
    return message


@dataclass(frozen=True)
class Note:
    kind: str
    args: tuple = ()

    def text(self, translate: Callable[[str], str] = _english) -> str:
        """The message; ``translate`` is i18n.gettext for the web UI (English without)."""
        make = _TEXTS.get(self.kind)
        return make(translate, *self.args) if make is not None else self.kind


def reason(e: BaseException) -> str:
    return str(e) or type(e).__name__


# what each kind of Note says; `_` is the translate function (test_i18n finds the texts)
_TEXTS: dict[str, Callable[..., str]] = {
    # a port that did not take a setting asked for at open (serial_io.open_serial)
    "line": lambda _, line, on, why: (
        _("{0} could not be switched on: {1}") if on else
        _("{0} could not be switched off: {1}")).format(line, why),
    "rs485": lambda _, why: _(
        "RS-485 mode is off: the port did not take it ({0})").format(why),
    # one of a serial-to-serial bridge's ports ("A" / "B") and a Note about it
    "side": lambda _, side, note: _("port {0}: {1}").format(side, note.text(_)),
}


def texts(notes: Any, translate: Callable[[str], str] = _english) -> list[str]:
    return [n.text(translate) for n in notes or ()]
