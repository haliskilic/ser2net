"""What to tell the user about a mapping's serial port.

``Note`` is one such message, kept in the runner's status: the dashboard shows it in the
web UI's language, the REST API, the log and the notifications in English (text()). Kept
as a kind plus its values rather than as text, because the text is made where it is shown:
a task a web request started inherits that request's language.

``explain(error, device)`` is the hint for an open that failed (the status's ``hint``),
next to pyserial's own text (``last_error``, kept as it is for scripts and webhooks that
match on it):
  - no access (EACCES / EPERM): the group the device belongs to, read from the device
    itself (dialout on Debian and Fedora, uucp on Arch, ...), and what is missing: this
    process is not in it (usermod / SupplementaryGroups / --group-add), its user was added
    after ser2net started (restart it), or the group may not open it (its mode);
  - in use (EBUSY, EAGAIN from flock; Windows "Access is denied"): which of this process's
    mappings has the port open (``opened`` / ``holder``, by device number), else what kind
    of lock another program holds.
It reads the device's owner and group and the account and group databases, which may ask
a directory service: run it in a thread.
"""
from __future__ import annotations

import errno
import os
import stat
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Optional

# an open that failed because there is no such device (unplugged, not enumerated yet)
_MISSING_ERRNOS = frozenset({errno.ENOENT, errno.ENXIO, errno.ENODEV})


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


def missing(e: BaseException) -> bool:
    """Whether opening a port failed because there is no such device: what the state
    device-missing stands for. pyserial keeps the errno of a failed open on POSIX; on
    Windows its message names the error (could not open port 'COM9':
    FileNotFoundError(2, 'The system cannot find the file specified.', None, 2))."""
    if getattr(e, "errno", None) in _MISSING_ERRNOS:
        return True
    return "FileNotFoundError(" in str(e)


# which mapping of this process has a port open: pyserial object -> (mapping, device key)
_OPEN: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _device_key(device: str) -> Any:
    """What two names of the same port have in common: POSIX its device number (by-id
    links, /dev/serial0 and the real name alike), Windows its COM name."""
    if os.name == "nt":
        name = device.upper()
        return name[4:] if name.startswith("\\\\.\\") else name
    try:
        return os.stat(device).st_rdev or None
    except (OSError, ValueError):
        return None


def opened(ser: Any, owner: str, device: str) -> None:
    """Remember that mapping ``owner`` has ``device`` open as ``ser`` (forgotten once
    ``ser`` is closed or gone)."""
    if os.name == "nt":
        key = _device_key(device)
    else:
        try:
            key = os.fstat(ser.fileno()).st_rdev or None
        except (AttributeError, OSError, ValueError, TypeError):
            key = None
    if key is not None:
        try:
            _OPEN[ser] = (owner, key)
        except TypeError:  # (a stand-in that can't be weakly referenced)
            pass


def holder(device: str) -> str:
    """The mapping of this process that has ``device`` open under any of its names, ""
    if none."""
    key = _device_key(device)
    if key is None:
        return ""
    for ser, (owner, k) in list(_OPEN.items()):
        if k == key and getattr(ser, "is_open", False):
            return owner
    return ""


def explain(e: BaseException, device: str) -> Optional[Note]:
    """Why opening ``device`` failed with ``e`` and what to do about it, when that can be
    told (None otherwise). Blocking (see the module docstring)."""
    code, text = getattr(e, "errno", None), str(e)
    if os.name == "nt":
        if "PermissionError(" in text or "Access is denied" in text:
            who = holder(device)
            return Note("in_use_by", (who,)) if who else Note("in_use_windows")
        return None
    if code in (errno.EACCES, errno.EPERM):
        return _no_access(device)
    if code in (errno.EBUSY, errno.EAGAIN, errno.EWOULDBLOCK):
        who = holder(device)
        if who:
            return Note("in_use_by", (who,))
        return Note("exclusive" if code == errno.EBUSY else "flock")
    return None


def _no_access(device: str) -> Optional[Note]:
    try:
        st = os.stat(device)
    except OSError:
        return None
    import grp
    import pwd

    gid = st.st_gid
    try:
        group = grp.getgrgid(gid).gr_name
    except KeyError:  # a group only the host knows (a container), or none
        group = str(gid)
    uid = os.geteuid()
    try:
        account = pwd.getpwuid(uid)
        user, base = account.pw_name, account.pw_gid
    except KeyError:
        user, base = str(uid), os.getegid()
    if gid == os.getegid() or gid in os.getgroups() or uid == st.st_uid:
        mode = stat.filemode(st.st_mode)
        return Note("mode", (device, mode, group))
    if gid == 0:
        return Note("root_group", (device,))
    try:
        listed = gid in os.getgrouplist(user, base)
    except (KeyError, OSError):
        listed = False
    if listed:
        return Note("restart", (user, group))
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return Note("container_group", (device, group, gid))
    return Note("group", (device, group, gid, user))


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
    # why an open failed (explain)
    "group": lambda _, device, group, gid, user: _(
        "{0} belongs to group {1} (gid {2}), and user {3} is not in it: add the user to "
        "the group (usermod -aG {1} {3}; the systemd unit: SupplementaryGroups={1}), then "
        "restart ser2net").format(device, group, gid, user),
    "container_group": lambda _, device, group, gid: _(
        "{0} belongs to group {1} (gid {2}), which this container doesn't have: start it "
        "with --group-add {2} (compose: group_add)").format(device, group, gid),
    "restart": lambda _, user, group: _(
        "user {0} was added to group {1} after ser2net started: restart ser2net (or sign in "
        "again) to use it").format(user, group),
    "mode": lambda _, device, mode, group: _(
        "{0} is {1} (group {2}): this process is in that group, but the device's permissions "
        "don't let the group open it").format(device, mode, group),
    "root_group": lambda _, device: _(
        "only root may open {0} (its group is root): a udev rule can give it the serial "
        "group (dialout, or uucp on Arch)").format(device),
    "in_use_by": lambda _, mapping: _(
        "mapping '{0}' has this port open").format(mapping),
    "in_use_windows": lambda _: _(
        "another program has this port open (Windows lets one program at a time open a COM "
        "port)"),
    "flock": lambda _: _(
        "another program has locked this port (flock): another ser2net, or a terminal "
        "program such as picocom, tio or miniterm"),
    "exclusive": lambda _: _(
        "another program keeps other programs out of this port (TIOCEXCL), for example "
        "ModemManager or a classic ser2net"),
}


def texts(notes: Any, translate: Callable[[str], str] = _english) -> list[str]:
    return [n.text(translate) for n in notes or ()]
