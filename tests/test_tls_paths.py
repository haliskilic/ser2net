"""TLS file paths when the data directory moves (config.data_path, moved_tls_file).

  - a relative TLS path (admin UI, mapping certificate / key / CA files) is a file in the
    data directory, whatever the working directory (Docker /app, a service's folder,
    System32 for the Windows service); one found only from the working directory, as
    releases before 2.9 looked it up, still works; absolute paths are used as they are;
  - absolute paths of a data directory that moved or was restored elsewhere: a file that
    is not there and was in a folder named "tls" (where Settings → Admin TLS generates its
    certificate) is taken from this data directory's tls folder, at start-up (with a log
    line, and config.json saved with the new paths), in a backup restore and in an import;
    a pair only when both files are found; a Windows path is read on Linux too;
  - not found there: the start ends with one line that names the file and the fix.
Listens on 127.0.0.1 only, on OS-assigned ports. Needs cryptography for the
certificates (SKIP without).

Run: python3 tests/test_tls_paths.py
"""
import contextlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _optional import need
from _server import ROOT, Browser, Server, free_port

from ser2netweb import config as cfgmod
from ser2netweb import importer, runtime
from ser2netweb.config import AppConfig, MappingConfig, data_path, moved_tls_file
from ser2netweb.web.auth import hash_password

USERS = [{"username": "admin", "password_hash": hash_password("admin-pass-1"),
          "role": "admin", "pwd_version": 1, "source": "local"}]


@contextlib.contextmanager
def env(**values):
    old = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def cwd(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def make_tls(data: str, names=("cert.pem", "key.pem")) -> tuple[str, str]:
    """A certificate and key in <data>/tls/ (the names Settings → Admin TLS generates)."""
    from _pki import TestCA
    folder = os.path.join(data, "tls")
    os.makedirs(folder, exist_ok=True)
    ca_dir = os.path.join(data, "ca")
    os.makedirs(ca_dir, exist_ok=True)
    cert, key = TestCA(ca_dir).issue(ca_dir, "ui", ["127.0.0.1"])
    out = []
    for src, name in zip((cert, key), names):
        dst = os.path.join(folder, name)
        shutil.copyfile(src, dst)
        out.append(dst)
    return out[0], out[1]


def test_data_path():
    data = tempfile.mkdtemp(prefix="s2n_tlspath_")
    work = tempfile.mkdtemp(prefix="s2n_tlscwd_")
    try:
        os.makedirs(os.path.join(data, "tls"))
        open(os.path.join(data, "tls", "a.pem"), "w").close()
        open(os.path.join(work, "legacy.pem"), "w").close()
        with cwd(work):
            assert data_path("tls/a.pem", data) == os.path.join(data, "tls", "a.pem")
            assert data_path("legacy.pem", data) == "legacy.pem"  # (found as before 2.9)
            assert data_path("gone.pem", data) == os.path.join(data, "gone.pem")
            absolute = os.path.join(work, "legacy.pem")
            assert data_path(absolute, data) == absolute
            assert data_path("", data) == ""
            assert data_path(r"C:\certs\a.pem", data) == r"C:\certs\a.pem"
            cfgmod.ConfigStore(os.path.join(data, "config.json"))  # sets the data directory
            assert data_path("tls/a.pem") == os.path.join(data, "tls", "a.pem")
            m = MappingConfig.from_dict({"name": "rel", "serial": {"port": "/dev/x"},
                                         "network": {"tls": True, "tls_cert": "tls/a.pem",
                                                     "tls_key": "tls/a.pem"}})
            m.validate()  # the existence check looks in the data directory too
    finally:
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
    print("data_path: a relative TLS path is a file in the data directory (one found only "
          "from the working directory still is), an absolute one is kept  OK")


def test_moved_tls_file():
    data = tempfile.mkdtemp(prefix="s2n_moved_")
    try:
        os.makedirs(os.path.join(data, "tls"))
        for name in ("cert.pem", "key.pem"):
            open(os.path.join(data, "tls", name), "w").close()
        here = os.path.join(data, "tls", "cert.pem")
        for old in ("/home/me/ser2net/data/tls/cert.pem", r"C:\ser2net\data\tls\cert.pem",
                    r"D:\Data\TLS\cert.pem"):
            assert moved_tls_file(old, data) == here, old
        for old in ("/home/me/ser2net/data/certs/cert.pem",  # not a tls folder
                    "/home/me/ser2net/data/tls/other.pem",   # no such file here
                    "tls/cert.pem",                          # relative: data_path's job
                    here, ""):                               # there: nothing to do
            assert moved_tls_file(old, data) == "", old
        cfg = AppConfig.from_dict({"admin_ui": {"tls_cert": "/old/data/tls/cert.pem",
                                                "tls_key": r"C:\old\data\tls\key.pem"},
                                   "mappings": [
            {"name": "pair", "serial": {"port": "/dev/a"},
             "network": {"tls": True, "tls_cert": "/old/data/tls/cert.pem",
                         "tls_key": "/old/data/tls/gone.pem"}},
            {"name": "ca", "serial": {"port": "/dev/b"},
             "network": {"mode": "client", "remote_host": "h", "remote_port": 1, "tls": True,
                         "tls_ca": "/old/data/tls/cert.pem"}}]})
        notes = cfg.find_moved_tls_files(data)
        assert cfg.admin_ui.tls_cert == here, cfg.admin_ui
        assert cfg.admin_ui.tls_key == os.path.join(data, "tls", "key.pem"), cfg.admin_ui
        assert cfg.mappings[0].network.tls_cert == "/old/data/tls/cert.pem", "half a pair moved"
        assert cfg.mappings[1].network.tls_ca == here
        assert len(notes) == 3 and all("is not there" in n for n in notes), notes
    finally:
        shutil.rmtree(data, ignore_errors=True)
    print("moved_tls_file: a missing file of a tls folder (POSIX or Windows path) is taken "
          "from this data directory's tls folder; a pair only as a whole  OK")


def test_start_after_a_move():
    data = tempfile.mkdtemp(prefix="s2n_move_start_")
    try:
        cert, key = make_tls(data)
        path = os.path.join(data, "config.json")
        old = {"tls_cert": r"C:\ProgramData\ser2net\tls\cert.pem",
               "tls_key": r"C:\ProgramData\ser2net\tls\key.pem"}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"users": USERS, "admin_ui": old}, fh)
        with env(SER2NET_BIND_IP=None, SER2NET_PORT=None):
            _store, cfg = runtime.prepare_config(path)
        runtime.require_admin_tls(cfg.admin_ui)  # (would exit with the old paths)
        assert (cfg.admin_ui.tls_cert, cfg.admin_ui.tls_key) == (cert, key), cfg.admin_ui
        assert any("is not there" in n for n in cfg.load_notes), cfg.load_notes
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)["admin_ui"]
        assert (saved["tls_cert"], saved["tls_key"]) == (cert, key), saved

        # through the real start: HTTPS with the files of this data directory
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"users": USERS, "admin_ui": old}, fh)
        with Server(data_dir=data) as srv:
            assert b"200 OK" in https_get(srv.port, "/healthz")
            assert "of this data directory is used" in srv.output(), srv.output()[-2000:]

        # not there either: one line that names the file and the fix, exit 1
        os.remove(key)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"users": USERS, "admin_ui": old}, fh)
        p = subprocess.run([sys.executable, "ser2netweb.py", "--no-bootstrap", "--data-dir", data],
                           cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True,
                           env=dict(os.environ, SER2NET_BIND_IP="127.0.0.1",
                                    SER2NET_PORT=str(free_port())),
                           encoding="utf-8", errors="replace", timeout=60)
        out = p.stdout + p.stderr
        assert p.returncode == 1 and "Traceback" not in out, (p.returncode, out[-2000:])
        assert r"C:\ProgramData\ser2net\tls\cert.pem" in out and "tls folder" in out, out[-2000:]
    finally:
        shutil.rmtree(data, ignore_errors=True)
    print("start-up after a move: the admin UI's certificate is found in this data "
          "directory's tls folder (logged, saved); not there: one line, exit 1  OK")


def https_get(port: int, path: str) -> bytes:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw, \
            ctx.wrap_socket(raw) as s:
        s.sendall(f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode())
        out = b""
        while chunk := s.recv(4096):
            out += chunk
    return out


def test_relative_paths_at_start():
    data = tempfile.mkdtemp(prefix="s2n_rel_start_")
    try:
        make_tls(data)
        port = free_port()
        config = {"users": USERS,
                  "admin_ui": {"tls_cert": "tls/cert.pem", "tls_key": "tls/key.pem"},
                  "mappings": [{"name": "rel", "serial": {"port": "/dev/ser2net-test-missing"},
                                "network": {"mode": "server", "bind_ip": "127.0.0.1",
                                            "port": port, "tls": True,
                                            "tls_cert": "tls/cert.pem",
                                            "tls_key": "tls/key.pem"}}]}
        with open(os.path.join(data, "config.json"), "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        with Server(data_dir=data) as srv:  # (its working directory is the source folder)
            assert b"200 OK" in https_get(srv.port, "/healthz"), srv.output()[-2000:]
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with socket.create_connection(("127.0.0.1", port), timeout=10) as raw, \
                    ctx.wrap_socket(raw) as s:
                assert s.version(), "no TLS on the mapping"
            assert "Traceback" not in srv.output()
    finally:
        shutil.rmtree(data, ignore_errors=True)
    print("relative TLS paths (admin UI, mapping) are files in the data directory, "
          "whatever the working directory  OK")


def test_restore_and_import_after_a_move():
    data = tempfile.mkdtemp(prefix="s2n_move_restore_")
    try:
        cert, key = make_tls(data)
        old_cert, old_key = "/srv/old-host/data/tls/cert.pem", "/srv/old-host/data/tls/key.pem"
        backup = {"users": USERS, "admin_ui": {"tls_cert": old_cert, "tls_key": old_key},
                  "mappings": [{"id": "m00000000001", "name": "moved",
                                "serial": {"port": "/dev/ser2net-test-missing"},
                                "network": {"mode": "server", "bind_ip": "127.0.0.1",
                                            "port": free_port(), "tls": True,
                                            "tls_cert": old_cert, "tls_key": old_key}}]}
        with Server({"users": USERS}, data_dir=data) as srv:
            os.makedirs(os.path.join(data, "backups"), exist_ok=True)
            name = "config-20200101T000000Z.json"
            with open(os.path.join(data, "backups", name), "w", encoding="utf-8") as fh:
                json.dump(backup, fh)
            b = Browser(srv.base)
            b.login("admin", "admin-pass-1")
            st, _h, page = b.post("/settings/backups/restore", {"name": name})
            assert "Restored" in page and "tls folder" in page, page[-2000:]
            saved = srv.saved_config()
            assert (saved["admin_ui"]["tls_cert"], saved["admin_ui"]["tls_key"]) == (cert, key)
            net = saved["mappings"][0]["network"]
            assert (net["tls_cert"], net["tls_key"]) == (cert, key), net
        cfgmod.set_data_dir(data)  # (this process's: the server above is another one)
        plan = importer.parse_mappings_file(json.dumps(backup).encode())
        assert plan.mappings[0].network.tls_cert == cert, plan.mappings[0].network
        assert any("tls folder" in w for w in plan.warnings), plan.warnings
        assert not any("can't be read" in w for w in plan.warnings), plan.warnings
    finally:
        shutil.rmtree(data, ignore_errors=True)
    print("a backup (and an import) from before a move: its TLS files are taken from this "
          "data directory's tls folder  OK")


def main():
    test_moved_tls_file()
    test_data_path()
    if not need("cryptography", what="TLS paths with test certificates"):
        return
    test_start_after_a_move()
    test_relative_paths_at_start()
    test_restore_and_import_after_a_move()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
