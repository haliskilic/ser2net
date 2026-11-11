"""State directories must be owner-private on both platforms (H5).

config.json holds the session secret_key and the admin password hash; the logs
dir can hold raw serial traffic. Previously only POSIX got chmod 0700 — on
Windows the data dir kept its inherited ACLs, so other local users could read
the secrets (and the README's "0600" claim was false there). This verifies
lock_down_dir() restricts a directory to its owner. Windows: the account running it
gets its own entry (also on a folder an elevated administrator made private to itself,
which Windows Server has Administrators own), but a service's data directory as
--install-service leaves it, and what inherits from it, are left as they are.

Run: python3 tests/test_dir_permissions.py
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import lock_down_dir


def test_posix_mode_0700():
    with tempfile.TemporaryDirectory() as d:
        lock_down_dir(d)
        mode = stat.S_IMODE(os.stat(d).st_mode)
        assert mode == 0o700, f"expected 0700, got {oct(mode)}"
    print("POSIX: state dir locked to 0700  OK")


def icacls(*args: str) -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    p = subprocess.run([os.path.join(root, "System32", "icacls.exe"), *args],
                       capture_output=True, text=True, timeout=60)
    return p.stdout + p.stderr


def has_entry(path: str, sid: str) -> bool:
    """An entry that allows `sid` something on `path` itself."""
    from app import winservice

    sec = winservice.read_security(path)
    return sec is not None and any(
        s == sid and allow and not flags & winservice.INHERIT_ONLY_ACE
        for s, _mask, allow, flags in sec[1])


def test_windows_owner_only_acl():
    from app import winservice

    me = winservice._current_user_sid()
    with tempfile.TemporaryDirectory() as d:
        lock_down_dir(d)
        out = icacls(d)
        # broad principals must be gone after /inheritance:r + explicit grants
        for broad in ("Everyone", "Authenticated Users", "\\Users:"):
            assert broad not in out, f"broad ACL principal still present: {broad!r}\n{out}"
        assert me and has_entry(d, me), f"current user not granted access\n{out}"
    print("Windows: state dir restricted to owner/SYSTEM/Administrators  OK")

    # icacls must be run by its full System32 path: an unqualified name is searched
    # in the program's own folder and the current directory BEFORE System32, so an
    # icacls.exe planted next to ser2net.exe (e.g. in Downloads) would run instead
    calls, real_run = [], subprocess.run
    subprocess.run = lambda cmd, *a, **kw: calls.append(cmd) or real_run(cmd, *a, **kw)
    try:
        with tempfile.TemporaryDirectory() as d:
            lock_down_dir(d)
    finally:
        subprocess.run = real_run
    exe = calls[0][0] if calls else ""
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32").lower()
    assert os.path.isabs(exe) and os.path.dirname(exe).lower() == system32, \
        f"icacls not run by its full System32 path: {exe!r}"
    print("Windows: icacls run by its full System32 path (no binary planting)  OK")


def test_windows_admin_owned_folders():
    """A service's data directory as --install-service leaves it (SYSTEM and
    Administrators only, owned by Administrators) and a folder created in it keep their
    permissions; a folder an elevated administrator made private to itself (Python's
    mkdir(0o700) on Windows Server: SYSTEM, Administrators, OWNER RIGHTS, owned by
    Administrators) gets that account's entry, so it can still use it unelevated."""
    from app import winservice as w

    me = w._current_user_sid()
    base = tempfile.mkdtemp(prefix="s2n_perm_")
    try:
        service = os.path.join(base, "service")
        w._mkdir_admin_only(service)
        w._harden(service, users_read=False)  # as install() leaves the data directory
        sec = w.read_security(service)
        if not me or sec is None or sec[0] != w.ADMINS_SID:
            print("SKIP admin-owned folders: making Administrators the owner needs an "
                  "elevated prompt")
            return
        lock_down_dir(service)
        assert not has_entry(service, me), icacls(service)
        backups = os.path.join(service, "backups")  # inherits it (the service makes it)
        os.makedirs(backups)
        icacls(backups, "/setowner", f"*{w.ADMINS_SID}")
        lock_down_dir(backups)
        assert not has_entry(backups, me), icacls(backups)
        sec = w.read_security(service)
        assert sec is not None and w.service_folder(*sec), icacls(service)

        private = os.path.join(base, "private")
        os.mkdir(private)
        icacls(private, "/inheritance:r", "/grant:r", f"*{w.SYSTEM_SID}:(OI)(CI)F",
               "/grant:r", f"*{w.ADMINS_SID}:(OI)(CI)F",
               "/grant:r", f"*{w.OWNER_RIGHTS_SID}:(OI)(CI)F")
        icacls(private, "/setowner", f"*{w.ADMINS_SID}")
        sec = w.read_security(private)
        assert sec is not None and w.admin_only(*sec) and not has_entry(private, me), \
            icacls(private)
        lock_down_dir(private)
        assert has_entry(private, me), f"current user not granted access\n{icacls(private)}"
    finally:
        icacls(base, "/reset", "/T", "/C", "/Q")
        shutil.rmtree(base, ignore_errors=True)
    print("Windows: a service's data directory (and a folder inside) kept as it is; a "
          "folder an elevated administrator made private gets that account's entry  OK")


def main():
    if os.name == "posix":
        test_posix_mode_0700()
    elif os.name == "nt":
        test_windows_owner_only_acl()
        test_windows_admin_owned_folders()
    else:
        print(f"skipped: unsupported os.name={os.name!r}")
        return
    print("\nPASS: state directory permissions (H5)")


if __name__ == "__main__":
    main()
