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
    mappings has the port open (``opened`` / ``holder``, by device number), else the
    program that holds a UUCP lock file on it (``uucp_lock``), else what kind of lock
    another program holds.
It reads the device's owner and group and the account and group databases, which may ask
a directory service: run it in a thread.

``port_warnings(device)`` (Linux) is what else may use a port that no lock keeps out: the
kernel console (/sys/class/tty/console/active), a login prompt (a running serial-getty /
getty unit, /run/systemd/units), and for a USB CDC port (ttyACM) ModemManager, unless udev
tells it to leave the port alone (ID_MM_DEVICE_IGNORE / ID_MM_PORT_IGNORE). These sources
need no privileges and are there in a container too (the console list is the host's);
where one is missing nothing is said. The ports page shows them (portlist.snapshot), and
the open port's status (port_notes, through serial_io.open_serial).
"""
from __future__ import annotations

import asyncio
import errno
import os
import stat
import sys
import weakref
from dataclasses import dataclass
from typing import Any, Callable, NamedTuple, Optional

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
        lock = uucp_lock(device)
        if lock is not None:
            return Note("uucp", (lock.program or "?", lock.pid, lock.path))
        return Note("exclusive" if code == errno.EBUSY else "flock")
    return None


# where programs that use UUCP lock files (minicom, cu, lockdev) put them
LOCK_DIRS = ("/run/lock", "/var/lock", "/run/lock/lockdev", "/var/lock/lockdev")


class UucpLock(NamedTuple):
    path: str
    pid: int
    program: str  # its name (/proc/<pid>/comm), "" when this process can't see it

    def describe(self) -> str:
        who = f"{self.program} (PID {self.pid})" if self.program else f"PID {self.pid}"
        return f"{who} has locked it with a UUCP lock file ({self.path})"


def uucp_lock(device: str) -> Optional[UucpLock]:
    """The UUCP lock file a running program holds on ``device``, if any: LCK..<name> (of
    the name given and of the tty it leads to) or lockdev's LCK.<major>.<minor>, in
    LOCK_DIRS. Only read: a lock whose process is gone is skipped, never removed (other
    programs' lock directories and process numbers, in a container another PID namespace's,
    are not this process's to clean up). Blocking: a few small reads."""
    real = os.path.realpath(device)
    names = [f"LCK..{os.path.basename(device)}", f"LCK..{os.path.basename(real)}"]
    try:
        rdev = os.stat(real).st_rdev
        if rdev:
            names.append(f"LCK.{os.major(rdev):03d}.{os.minor(rdev):03d}")
    except OSError:
        pass
    seen = set()
    for folder in LOCK_DIRS:
        for name in dict.fromkeys(names):
            path = os.path.join(folder, name)
            key = os.path.realpath(path)
            if key in seen:
                continue  # (/var/lock is usually /run/lock)
            seen.add(key)
            pid = _lock_pid(path)
            if pid is not None and _alive(pid):
                return UucpLock(path, pid, _program(pid))
    return None


def _lock_pid(path: str) -> Optional[int]:
    """The process number in a lock file: HDB style (ASCII, as minicom and lockdev write
    it) or the old binary one (a 4-byte int)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(64)
    except OSError:
        return None
    text = data.strip()
    if text.isdigit():
        pid = int(text)
    elif len(data) == 4:
        pid = int.from_bytes(data, sys.byteorder)
    else:
        return None
    return pid if pid > 0 and pid != os.getpid() else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # another user's process
    except OSError:
        return False
    return True


def _program(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:  # not Linux, or hidden (systemd's ProtectProc=invisible)
        return ""


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


CONSOLE_ACTIVE = "/sys/class/tty/console/active"
SYSTEMD_UNITS = "/run/systemd/units"
UDEV_DATA = "/run/udev/data"
_LOGIN_UNITS = ("serial-getty@{0}.service", "getty@{0}.service")
_MM_IGNORE = ("E:ID_MM_DEVICE_IGNORE=1", "E:ID_MM_PORT_IGNORE=1")


def _port_checks(device: str) -> list[tuple[str, str]]:
    """(kind, detail) of what else may use ``device``: ("console", tty name),
    ("getty", the unit), ("modemmanager", tty name). Linux only ([] elsewhere)."""
    if not sys.platform.startswith("linux"):
        return []
    real = os.path.realpath(device)  # /dev/serial0, a by-id link: the tty itself
    name = os.path.basename(real)
    found = []
    try:
        with open(CONSOLE_ACTIVE, encoding="ascii", errors="replace") as fh:
            if name in fh.read().split():
                found.append(("console", name))
    except OSError:
        pass
    for unit in (u.format(name) for u in _LOGIN_UNITS):
        if os.path.lexists(os.path.join(SYSTEMD_UNITS, f"invocation:{unit}")):
            found.append(("getty", unit))
            break
    if (name.startswith("ttyACM")
            and os.path.lexists(os.path.join(SYSTEMD_UNITS, "invocation:ModemManager.service"))
            and not _mm_ignores(real)):
        found.append(("modemmanager", name))
    return found


def _mm_ignores(real: str) -> bool:
    """Whether udev tells ModemManager to leave the tty ``real`` alone."""
    try:
        rdev = os.stat(real).st_rdev
        path = os.path.join(UDEV_DATA, f"c{os.major(rdev)}:{os.minor(rdev)}")
        with open(path, encoding="utf-8", errors="replace") as fh:
            return any(line.strip() in _MM_IGNORE for line in fh)
    except OSError:
        return False


def port_warnings(device: str) -> list[str]:
    """What else may use ``device``, for the ports page: "console", "getty",
    "modemmanager" (Linux; blocking, a few small reads)."""
    return [kind for kind, _detail in _port_checks(device)]


def port_notes(device: str) -> list[Note]:
    """The same as Notes for the status of a mapping that has ``device`` open."""
    return [Note(kind, (detail,)) for kind, detail in _port_checks(device)]


async def about_port(device: str) -> list[Note]:
    """port_notes off the event loop (nothing to look at off Linux)."""
    if not sys.platform.startswith("linux"):
        return []
    return await asyncio.to_thread(port_notes, device)


def access(device: str) -> Optional[bool]:
    """Whether this process may open ``device`` (POSIX; None on Windows, or when it can't
    be told)."""
    if os.name != "posix":
        return None
    try:
        return os.access(device, os.R_OK | os.W_OK)
    except (OSError, ValueError):
        return None


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
    "uucp": lambda _, program, pid, path: _(
        "{0} (PID {1}) is using this port: it holds a UUCP lock file on it ({2})"
    ).format(program, pid, path),
    # what else may use an open port (port_notes), and what that did (SerialLink)
    "console": lambda _, name: _(
        "{0} is also the kernel console: the kernel writes its messages to it (remove "
        "console={0} from the kernel command line; Raspberry Pi: raspi-config, Interface "
        "Options, Serial Port)").format(name),
    "getty": lambda _, unit: _(
        "a login prompt runs on this port ({0}): it reads from the port and hangs it up when "
        "it restarts (systemctl disable --now {0}, or systemctl mask {0})").format(unit),
    "modemmanager": lambda _, name: _(
        "ModemManager may probe {0} (it sends AT commands to new ttyACM devices): a udev rule "
        "that sets ID_MM_DEVICE_IGNORE=1 for it keeps it away").format(name),
    "hangups": lambda _, n: _(
        "hung up {0} time(s) while open, and could be reopened at once: another program may "
        "be using this port (a login prompt on it?)").format(n),
}


def texts(notes: Any, translate: Callable[[str], str] = _english) -> list[str]:
    return [n.text(translate) for n in notes or ()]
