"""State directories must be owner-private on both platforms (H5).

config.json holds the session secret_key and the admin password hash; the logs
dir can hold raw serial traffic. Previously only POSIX got chmod 0700 — on
Windows the data dir kept its inherited ACLs, so other local users could read
the secrets (and the README's "0600" claim was false there). This verifies
lock_down_dir() restricts a directory to its owner.

Run: python3 tests/test_dir_permissions.py
"""
import os
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


def test_windows_owner_only_acl():
    with tempfile.TemporaryDirectory() as d:
        lock_down_dir(d)
        out = subprocess.run(["icacls", d], capture_output=True, text=True, timeout=15).stdout
        user = os.environ.get("USERNAME", "")
        # broad principals must be gone after /inheritance:r + explicit grants
        for broad in ("Everyone", "Authenticated Users", "\\Users:"):
            assert broad not in out, f"broad ACL principal still present: {broad!r}\n{out}"
        assert user and user.lower() in out.lower(), f"current user not granted access\n{out}"
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


def main():
    if os.name == "posix":
        test_posix_mode_0700()
    elif os.name == "nt":
        test_windows_owner_only_acl()
    else:
        print(f"skipped: unsupported os.name={os.name!r}")
        return
    print("\nPASS: state directory permissions (H5)")


if __name__ == "__main__":
    main()
