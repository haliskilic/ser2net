"""Account recovery and the first admin account, without the web UI.

    ser2net --set-password NAME   a new password for local account NAME, whose sessions
                                  end (NAME is created as an admin if it doesn't exist)
    ser2net --sign-out-all        a new session key: every web UI session ends
    SER2NET_ADMIN_PASSWORD_FILE   (+ SER2NET_ADMIN_USER, default "admin") the first admin
                                  of an install without accounts, e.g. a Docker secret

The commands work whether ser2net is running or not. The data directory's lock
(app/datalock.py, <data>/ser2net.lock) tells which: while an instance of the same data
directory holds it, the change is handed to that instance: a request file in
<data>/admin-requests/ that it applies under its config lock, so its next save can't
undo the change and sessions end at once (this also reaches an instance in another
container or network namespace that shares the data directory). Otherwise the command
takes the lock itself — a ser2net that starts meanwhile waits for it — and changes
config.json here, through ConfigStore, which keeps the version it replaces in backups/.
Where the lock can't be used (a read-only data directory, a network share without
locks), and for a ser2net from before the lock, the web UI's /healthz tells whether one
runs. Either way the change is written to audit.log. The password is asked twice on
the terminal, or read from the first line of stdin when that is not a terminal; only
its scrypt hash leaves this process, and nothing secret is printed or logged. Run as
root on a data directory that belongs to another user (docker exec, sudo), the command
first becomes that user, so the files it writes keep the owner the running ser2net
needs.
"""
from __future__ import annotations

import contextlib
import getpass
import json
import os
import re
import secrets
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import datalock, healthcheck
from .config import ConfigStore, User, host_port, new_pwd_version
from .web import auth

REQUEST_DIR = "admin-requests"     # in the data directory
POLL_S = 1.0                       # how often a running instance looks for requests
REQUEST_MAX_AGE_S = 120.0          # an older request is answered "expired", not applied
WAIT_RUNNING_S = 20.0              # an instance runs: wait this long for it to take it
WAIT_STOPPED_S = 3.0               # no lock, no /healthz: still wait a moment (another container?)
WAIT_ANSWER_S = 30.0               # taken: wait this long for its answer
ACTOR = "(command line)"           # the audit log's user column
_REQUEST_ID = re.compile(r"^[0-9a-f]{16}$")


class AccountError(Exception):
    """A problem for the person at the command line (printed; exit code 1)."""


@dataclass
class Change:
    """What apply() changed: the reply, the audit record, and how to undo it in memory
    if saving fails."""
    message: str
    action: str
    detail: str
    undo: Callable[[], None]
    first_admin: bool = False


# --------------------------------------------------------------------------
# the changes (shared by the command line and a running instance)
# --------------------------------------------------------------------------
def apply(config, request: dict) -> Change:
    """Make one requested change to `config`, in memory. Raises AccountError."""
    action = request.get("action")
    if action == "set_password":
        return _set_password(config, str(request.get("username") or ""),
                             str(request.get("password_hash") or ""))
    if action == "sign_out_all":
        old = config.secret_key
        config.secret_key = secrets.token_hex(32)

        def undo() -> None:
            config.secret_key = old
        return Change("every web UI session was signed out (new session key)",
                      "sessions_revoke_all", "", undo)
    raise AccountError(f"unknown request {action!r}")


def _set_password(config, username: str, password_hash: str) -> Change:
    from .web.routes.forms import _username_problem

    if problem := _username_problem(username):
        raise AccountError(problem)
    if not auth.is_password_hash(password_hash):
        raise AccountError("the request carries no valid password hash")
    user = config.get_user(username)
    if user is None and (taken := config.named(username)):
        # names that differ only in letter case are one name for LDAP, and easily mistaken
        other = taken[0]
        if other.source == "local":
            raise AccountError(f"there is a local account '{other.username}' (another letter "
                               f"case); run the command with that name")
        raise AccountError(f"'{other.username}' is an {other.source.upper()} account (letter "
                           "case aside); give a new local admin account another name")
    if user is None:
        first = not config.users
        new = User(username=username, password_hash=password_hash, role="admin",
                   pwd_version=new_pwd_version())
        config.users.append(new)

        def remove() -> None:
            config.users.remove(new)
        return Change(f"local admin account '{username}' created",
                      "first_run_setup" if first else "user_create",
                      username if first else f"{username}:admin", remove, first_admin=first)
    if user.source != "local":
        raise AccountError(f"'{username}' is an {user.source.upper()} account, whose password "
                           "is managed there; give a new local admin account another name")
    old = (user.password_hash, user.pwd_version)
    user.password_hash, user.pwd_version = password_hash, user.pwd_version + 1

    def restore() -> None:
        user.password_hash, user.pwd_version = old
    return Change(f"new password set for '{username}' ({user.role}); its sessions were "
                  "signed out", "user_password_set", username, restore)


def provision_requested(environ) -> bool:
    """SER2NET_ADMIN_PASSWORD_FILE / SER2NET_ADMIN_USER is set: the first admin comes
    from the environment, never from the setup page."""
    return bool((environ.get("SER2NET_ADMIN_PASSWORD_FILE") or "").strip()
                or (environ.get("SER2NET_ADMIN_USER") or "").strip())


def provision_from_env(config, environ) -> tuple[Optional[str], Optional[str]]:
    """SER2NET_ADMIN_PASSWORD_FILE (+ SER2NET_ADMIN_USER): add the first admin to an
    install that has no account yet. Returns (the name created, a line for the log);
    (None, None) when nothing was asked for."""
    path = (environ.get("SER2NET_ADMIN_PASSWORD_FILE") or "").strip()
    name = (environ.get("SER2NET_ADMIN_USER") or "").strip()
    if config.users:
        if path:
            return None, ("SER2NET_ADMIN_PASSWORD_FILE is ignored: accounts exist already "
                          "(ser2net --set-password changes a password)")
        return None, None
    if not path:
        if name:
            return None, ("ERROR: SER2NET_ADMIN_USER is set but SER2NET_ADMIN_PASSWORD_FILE is "
                          "not: no admin account was created")
        return None, None
    from .web.routes.forms import _password_problem, _username_problem

    name = name or "admin"
    if problem := _username_problem(name):
        return None, f"ERROR: SER2NET_ADMIN_USER: {problem}"
    try:
        with open(path, encoding="utf-8") as fh:
            password = fh.read(4096)
    except (OSError, UnicodeDecodeError) as e:
        return None, f"ERROR: SER2NET_ADMIN_PASSWORD_FILE: cannot read {path}: {e}"
    password = password.rstrip("\r\n")  # a file written with echo / a secret ends in one
    if problem := _password_problem(password, password):
        return None, f"ERROR: SER2NET_ADMIN_PASSWORD_FILE: {problem}"
    config.users.append(User(username=name, password_hash=auth.hash_password(password),
                             role="admin", pwd_version=new_pwd_version()))
    return name, f"admin account '{name}' created from SER2NET_ADMIN_PASSWORD_FILE"


# --------------------------------------------------------------------------
# hand-over to a running instance: request files in <data>/admin-requests/
#   <id>.req   written by the command (atomically)
#   <id>.work  the instance took it (rename: either it or the command's withdrawal wins)
#   <id>.res   the instance's answer {"ok": bool, "message": str}
# --------------------------------------------------------------------------
def _request_dir(data_dir: str) -> str:
    return os.path.join(data_dir, REQUEST_DIR)


def _write_atomic(path: str, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def post_request(data_dir: str, request: dict) -> str:
    """Leave a request for a running instance; returns its id."""
    request_id = secrets.token_hex(8)
    d = _request_dir(data_dir)
    os.makedirs(d, mode=0o700, exist_ok=True)
    _write_atomic(os.path.join(d, request_id + ".req"), json.dumps({**request, "time": time.time()}))
    return request_id


def take_requests(data_dir: str) -> list[tuple[str, Optional[dict]]]:
    """The running instance's side: take every waiting request — (id, request), or
    (id, None) for one that is unreadable or too old — and tidy up leftovers."""
    d = _request_dir(data_dir)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    now, taken = time.time(), []
    for name in names:
        path = os.path.join(d, name)
        request_id, ext = os.path.splitext(name)
        if ext != ".req" or not _REQUEST_ID.match(request_id):
            with contextlib.suppress(OSError):  # an answer nobody read, a stray temp file
                if now - os.path.getmtime(path) > 600:
                    os.remove(path)
            continue
        work = os.path.join(d, request_id + ".work")
        try:
            os.replace(path, work)
        except OSError:
            continue  # withdrawn by its command in the meantime
        try:
            with open(work, encoding="utf-8") as fh:
                request = json.load(fh)
            age = now - float(request["time"])
        except (OSError, ValueError, TypeError, KeyError):
            request, age = None, 0.0
        with contextlib.suppress(OSError):
            os.remove(work)
        if not isinstance(request, dict) or not 0 <= age <= REQUEST_MAX_AGE_S:
            request = None
        taken.append((request_id, request))
    return taken


def answer(data_dir: str, request_id: str, ok: bool, message: str) -> None:
    _write_atomic(os.path.join(_request_dir(data_dir), request_id + ".res"),
                  json.dumps({"ok": ok, "message": message}))


def _read_answer(path: str) -> Optional[tuple[bool, str]]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None  # not there (yet)
    with contextlib.suppress(OSError):
        os.remove(path)
    return bool(doc.get("ok")), str(doc.get("message", ""))


def wait_answer(data_dir: str, request_id: str, take_s: float, answer_s: float = WAIT_ANSWER_S,
                sleep=time.sleep, gone: Optional[Callable[[], bool]] = None
                ) -> Optional[tuple[bool, str]]:
    """The command's side: a running instance's (ok, message), or None when no instance
    took the request within take_s seconds, or before gone() said it has stopped (it is
    withdrawn then)."""
    d = _request_dir(data_dir)
    request_path, answer_path = (os.path.join(d, request_id + ext) for ext in (".req", ".res"))
    end = time.monotonic() + take_s
    while (result := _read_answer(answer_path)) is None and time.monotonic() < end:
        if gone is not None and gone():
            break
        sleep(0.2)
    if result is not None:
        return result
    try:
        os.remove(request_path)
        return None  # nobody took it
    except FileNotFoundError:
        pass  # taken just now: its answer is on the way
    end = time.monotonic() + answer_s
    while (result := _read_answer(answer_path)) is None and time.monotonic() < end:
        sleep(0.2)
    if result is None:
        raise AccountError("the running ser2net took the request but did not answer; "
                           "see its log (all.log)")
    return result


# --------------------------------------------------------------------------
# the command line
# --------------------------------------------------------------------------
def read_password(stdin) -> str:
    from .web.routes.forms import _password_problem

    if stdin is not None and stdin.isatty():
        password = getpass.getpass("New password (at least 8 characters): ")
        again = getpass.getpass("The same again: ")
    else:
        password = again = (stdin.readline() if stdin is not None else "").rstrip("\r\n")
    if problem := _password_problem(password, again):
        raise AccountError(problem)
    return password


def become_owner(data_dir: str, platform: str = sys.platform, os_=os) -> str:
    """Run as root on a data directory that belongs to another account (docker exec,
    sudo): become that account before touching anything, so whatever is written keeps
    the owner the running ser2net needs. Returns a note for the output. (`platform`
    and `os_` are injection points for tests.)"""
    if platform == "win32" or os_.geteuid() != 0:
        return ""
    try:
        st = os_.stat(data_dir)
    except FileNotFoundError:
        raise AccountError(f"{data_dir} does not exist; run this as the user ser2net runs as, "
                           "so the new data directory belongs to it") from None
    if st.st_uid == 0:
        return ""
    try:
        os_.setgroups([])
        os_.setgid(st.st_gid)
        os_.setuid(st.st_uid)
    except OSError as e:
        raise AccountError(f"cannot switch to uid {st.st_uid}, the data directory's owner: "
                           f"{e}") from None
    return f" (as uid {st.st_uid}, the data directory's owner)"


def apply_here(request: dict, config_path: str) -> str:
    """No instance runs: change config.json directly (ConfigStore keeps a backup)."""
    from .state import write_audit_record

    store = ConfigStore(config_path)
    created = not store.exists()
    try:
        config = store.load()
    except (OSError, ValueError) as e:
        raise AccountError(f"cannot read {config_path}: {e}") from None
    change = apply(config, request)
    try:
        store.save(config, validate=False)  # an unrelated bad setting must not block this
    except OSError as e:
        raise AccountError(f"cannot write {config_path}: {e}") from None
    write_audit_record(os.path.join(os.path.dirname(store.path), "audit.log"), "local",
                       change.action, change.detail, ACTOR)
    if created:
        admin = config.admin_ui
        return (f"{change.message}; {config_path} was created — the web UI will listen on "
                f"{host_port(admin.bind_ip, admin.port)} (change it with --reconfigure or "
                "SER2NET_BIND_IP)")
    return change.message


def submit(request: dict, config_path: str) -> str:
    """Hand the change to the running instance of this data directory, or make it here
    while holding the data directory's lock."""
    data_dir = os.path.dirname(os.path.abspath(config_path))
    with contextlib.suppress(OSError):  # if it can't be created, the change itself says why
        os.makedirs(data_dir, exist_ok=True)
    try:
        return _submit(request, config_path, data_dir)
    finally:
        datalock.release(data_dir)  # if this command took it


def _done(result: tuple[bool, str]) -> str:
    ok, message = result
    if not ok:
        raise AccountError(message)
    return f"{message} (done by the running ser2net)"


def _submit(request: dict, config_path: str, data_dir: str) -> str:
    while True:
        try:
            note = datalock.acquire(data_dir, brief=True, wait=datalock.BRIEF_WAIT_S)
        except datalock.Held as e:
            if e.brief:  # another of these commands, still busy
                raise AccountError(f"{e}; run this again in a moment") from None
            if (message := _hand_over(request, data_dir, e.pid)) is not None:
                return message
            continue  # it stopped before taking the request: the lock is this command's now
        break
    if not os.path.isfile(config_path):
        return apply_here(request, config_path)  # nothing can be running on it
    # The lock is this command's, or can't be used here. A ser2net that runs without it
    # (one from before the lock, or on a data directory that can't be locked) still
    # answers /healthz; where the lock can't be used, also give one the healthcheck can't
    # reach (another container) a moment to take the request.
    running = healthcheck.check(config_path, timeout=3)[0]
    if running or note is not None:
        request_id = post_request(data_dir, request)
        result = wait_answer(data_dir, request_id, WAIT_RUNNING_S if running else WAIT_STOPPED_S)
        if result is not None:
            return _done(result)
        if running:
            raise AccountError("ser2net is running on this data directory but did not take the "
                               "request (an older version?). Stop it, run this command again, "
                               "then start it.")
    return apply_here(request, config_path)


def _hand_over(request: dict, data_dir: str, pid: Optional[int]) -> Optional[str]:
    """The instance holding the lock applies the request. None when it stopped before
    taking it: then this command holds the lock and makes the change itself."""
    def stopped() -> bool:
        try:
            return datalock.acquire(data_dir, brief=True) is None
        except datalock.Held:
            return False

    request_id = post_request(data_dir, request)
    result = wait_answer(data_dir, request_id, WAIT_RUNNING_S, gone=stopped)
    if result is not None:
        return _done(result)
    if datalock.holds(data_dir):
        return None
    process = f" (process {pid})" if pid else ""
    raise AccountError(f"the ser2net running with {data_dir}{process} did not take the request "
                       f"within {WAIT_RUNNING_S:.0f} s. If it is still starting, run this "
                       "command again once its web UI answers; otherwise see its log (all.log)")


def main(args, config_path: str, stdin=None) -> int:
    """`ser2net --set-password NAME` / `ser2net --sign-out-all`."""
    stdin = sys.stdin if stdin is None else stdin
    try:
        if args.set_password is not None:
            from .web.routes.forms import _username_problem

            username = args.set_password.strip()
            if problem := _username_problem(username):
                raise AccountError(problem)
            request = {"action": "set_password", "username": username,
                       "password_hash": auth.hash_password(read_password(stdin))}
        else:
            request = {"action": "sign_out_all"}
        data_dir = os.path.dirname(os.path.abspath(config_path))
        note = become_owner(data_dir)
        if os.path.isdir(data_dir) and not os.access(data_dir, os.R_OK | os.W_OK | os.X_OK):
            raise AccountError(f"no access to {data_dir}: run this as the user ser2net runs as "
                               "(or as root)")
        print(f"ser2net: {submit(request, config_path)}{note}")
    except AccountError as e:
        print(f"ser2net: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"ser2net: {e}", file=sys.stderr)
        return 1
    return 0
