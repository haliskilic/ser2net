"""Form posts take text fields only; a file part is refused, not a 500.

Starlette parses a multipart body into text fields and uploads (UploadFile). The web
UI's forms are text, but a crafted multipart post could send a file part under a
field's name, and handlers that expect a string then failed (AttributeError /
TypeError: HTTP 500). Every form read now bounds its file parts: the UI's forms take
none (routes.common.read_form: 400 while the body is read), the mappings import one.

  1. static: every request.form() call in app/web says how many files it takes
  2. a real server: a file part named like a field gets 400 on mapping save, the
     access and notification settings and a cluster action; nothing is saved and the
     server keeps running; the same fields as multipart text are still accepted

Run: python3 tests/test_form_uploads.py
"""
from __future__ import annotations

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from _server import Browser, Server

sys.path.insert(0, ROOT)
from app.web.auth import hash_password

PASSWORD = "uploads-pass-1"


def _limits_with_max_files(tree: ast.Module) -> set[str]:
    """Module-level names bound to a dict literal with a "max_files" key."""
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
            if "max_files" in keys:
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def test_every_form_read_bounds_files():
    unbounded, seen = [], 0
    for folder, _dirs, files in os.walk(os.path.join(ROOT, "app", "web")):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), path)
            limits = _limits_with_max_files(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "form" and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "request"):
                    continue
                seen += 1
                ok = any(k.arg == "max_files" or (k.arg is None and isinstance(k.value, ast.Name)
                                                  and k.value.id in limits)
                         for k in node.keywords)
                if not ok:
                    unbounded.append(f"{os.path.relpath(path, ROOT)}:{node.lineno}")
    assert seen >= 3, seen  # read_form, the import, the sign-in forms
    assert not unbounded, ("request.form() without max_files (use routes.common.read_form):\n  "
                           + "\n  ".join(unbounded))
    print(f"every request.form() in app/web bounds its file parts ({seen} calls)  OK")


def test_file_parts_refused():
    config = {"users": [{"username": "admin", "password_hash": hash_password(PASSWORD),
                         "role": "admin", "pwd_version": 1}]}
    with Server(config) as srv:
        b = Browser(srv.base)
        b.login("admin", PASSWORD)
        before = srv.saved_config()
        cases = [
            ("/api/mappings/save", {"listen_port": "4001"}, "name"),
            ("/settings/access", {"require_login": "on"}, "session_minutes"),
            ("/settings/notifications", {"notify_enabled": "on"}, "notify_webhook_url"),
            ("/api/cluster/control-peer", {"action": "start"}, "peer"),
        ]
        for path, fields, as_file in cases:
            st, _h, body = b.post(path, fields, files={as_file: ("x.txt", b"30")}, htmx=True)
            assert st == 400, (path, st, body[:300])
            assert "Traceback" not in srv.output(), srv.output()[-2000:]
        assert srv.proc.poll() is None, "the server must keep running"
        assert srv.saved_config() == before, "nothing may be saved"
        # the same form as multipart text fields still goes through (a text part has no
        # filename): the session lifetime is saved
        st, _h, body = _post_multipart_text(b, "/settings/access",
                                            {"require_login": "on", "session_minutes": "45"})
        assert st in (200, 303), (st, body[:300])
        assert srv.saved_config()["session_timeout_s"] == 45 * 60, srv.saved_config()
    print("a file part named like a form field: 400, nothing saved, server up; multipart "
          "text fields still accepted  OK")


def _post_multipart_text(b: Browser, path: str, fields: dict):
    """A multipart/form-data post with text parts only (what curl -F name=value sends)."""
    import urllib.request

    fields = dict(fields, _csrf=b.csrf())
    boundary = "s2ntextboundary"
    data = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
                    .encode() for k, v in fields.items()) + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(b.base + path, data=data, method="POST", headers={
        "X-CSRF-Token": b.csrf(), "Content-Type": f"multipart/form-data; boundary={boundary}"})
    st, h, body = b._open(req)
    return st, h, body.decode("utf-8", "replace")


def main():
    test_every_form_read_bounds_files()
    test_file_parts_refused()
    print("\nPASS: form posts take text fields only (a file part is refused)")


if __name__ == "__main__":
    main()
