"""TLS files uploaded in the browser (Settings → Admin TLS, a mapping's form), for an
installation whose admin has no shell on the host to put them there.

An upload is a certificate with its key, and for a mapping a CA file as well. Before
anything is kept, the files are written aside in the data folder's tls/ (private: the
folder 0700 or, on Windows, owner-only like the data folder; each file 0600) and loaded as
ser2netweb will load them (engine/tls.py): the key must be the certificate's and decrypt
with the password given, a CA file must hold a certificate, and a mapping's pair must not
be the admin UI's (nor the admin UI's a mapping's): browsers send a site's cookies to
every port of it. Only then they get their names,
`uploaded-<16 hex digits of the file's SHA-256>-cert.pem` / `-key.pem` / `-ca.pem`: the
same files uploaded again replace themselves, renewed ones stand next to the old (which
stay until someone deletes them: a configuration backup may name them), and a key never
replaces another one (a pair already in use keeps its key and its password).

The upload only stores files and answers their paths. They go into the same settings a
typed path goes into (admin_ui.tls_cert, network.tls_cert, ...), which keep working as
before.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
from typing import Optional

from ..config import data_path, lock_down_dir
from ..engine import tls
from ..i18n import _

MAX_FILE_BYTES = 256 * 1024      # one uploaded file (a certificate chain is a few KB)
MAX_BODY_BYTES = 1024 * 1024     # the whole upload request
TLS_DIR = "tls"
_NAME = "uploaded-{0}-{1}.pem"
_PEM_BLOCK = re.compile(rb"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.S)


class UploadError(Exception):
    """An upload that is not kept; str(): why, in the page's language."""


def _blocks(data: bytes, label: bytes) -> list[bytes]:
    """The PEM blocks of `data` whose type ends with `label`, without their white space
    (the same block compares equal however its lines are broken)."""
    return [b"".join(body.split()) for kind, body in _PEM_BLOCK.findall(data)
            if kind.endswith(label)]


def _check_shape(cert: Optional[bytes], key: Optional[bytes], cas: dict[str, bytes]) -> None:
    if cert is None and key is None and not cas:
        raise UploadError(_("Choose the files to upload."))
    if (cert is None) != (key is None):
        raise UploadError(_("Upload the certificate and its key together."))
    for data in (cert, key, *cas.values()):
        if data is not None and len(data) > MAX_FILE_BYTES:
            raise UploadError(_("An uploaded TLS file can have at most {0} KB.").format(
                MAX_FILE_BYTES // 1024))
    if cert is not None and not _blocks(cert, b"CERTIFICATE"):
        raise UploadError(_("The uploaded certificate is not a PEM certificate (a text file "
                            "that begins with -----BEGIN CERTIFICATE-----)."))
    if key is not None and not _blocks(key, b"PRIVATE KEY"):
        raise UploadError(_("The uploaded key is not a PEM private key (a text file that "
                            "begins with -----BEGIN PRIVATE KEY----- or the like)."))
    for data in cas.values():
        if not data.strip():
            raise UploadError(_("The uploaded CA file is empty."))


def _same_pair(cert: bytes, key: bytes, other_cert: str, other_key: str) -> bool:
    """Is the uploaded pair the one in the files `other_cert` / `other_key` (paths from
    the configuration; a file that can't be read is not the same)?"""
    for mine, path, label in ((key, other_key, b"PRIVATE KEY"), (cert, other_cert, b"CERTIFICATE")):
        if not path:
            continue
        try:
            with open(data_path(path), "rb") as fh:
                theirs = _blocks(fh.read(MAX_FILE_BYTES + 1), label)
        except OSError:
            continue
        ours = _blocks(mine, label)
        if ours and theirs and ours[0] == theirs[0]:
            return True
    return False


def _problem(e: tls.TlsFileError) -> str:
    """The text for a pair or CA file that did not load (the paths in the error are the
    temporary files': not shown)."""
    if e.kind == "password":
        return _("The uploaded key is password-protected: enter its password.")
    if e.kind == "unusable":
        return _("The uploaded certificate can't be used with the uploaded key ({0}). Is "
                 "the key password right, and is it the certificate's key?").format(e.detail)
    if e.kind == "ca_empty":
        return _("The uploaded CA file holds no certificate (PEM or DER).")
    return _("The uploaded TLS files could not be loaded: {0}").format(e.detail or e.kind)


def store(data_dir: str, *, cert: Optional[bytes] = None, key: Optional[bytes] = None,
          password: str = "", cas: Optional[dict[str, Optional[bytes]]] = None,
          others: tuple = (), reuse_message: str = "") -> dict[str, str]:
    """Check an upload and keep it in <data_dir>/tls: `cert` with `key` (decrypted with
    `password`), and the CA files `cas` ({"ca": ..., "client_ca": ...}). `others`:
    (certificate path, key path) pairs it must not be the same as (UploadError with
    `reuse_message`). Returns {"cert": path, "key": path, <ca name>: path} for what was
    uploaded (absolute paths). Raises UploadError; OSError when the folder can't be
    written. Blocking: call via asyncio.to_thread."""
    given = {name: data for name, data in (cas or {}).items() if data is not None}
    _check_shape(cert, key, given)
    if cert is not None and key is not None:
        for other_cert, other_key in others:
            if _same_pair(cert, key, other_cert, other_key):
                raise UploadError(reuse_message)
    folder = os.path.join(os.path.abspath(data_dir), TLS_DIR)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    lock_down_dir(folder)  # (also one made before, by Generate or by hand)
    files: dict[str, tuple[str, bytes]] = {}   # what -> (its path, its bytes)
    if cert is not None and key is not None:
        files["cert"] = (_path(folder, cert, "cert"), cert)
        files["key"] = (_path(folder, key, "key"), key)
    for name, data in given.items():
        files[name] = (_path(folder, data, "ca"), data)
    aside: dict[str, str] = {}
    try:
        for what, (_dest, data) in files.items():
            aside[what] = _write_aside(folder, data)
        try:  # as ser2netweb will load them
            if "cert" in aside:
                tls.tls_context(server_side=True, cert=aside["cert"], key=aside["key"],
                                password=password)
            for name in given:
                tls.tls_context(server_side=False, ca=aside[name], verify=True)
        except tls.TlsFileError as e:
            raise UploadError(_problem(e)) from None
        for what, (path, _data) in files.items():
            os.replace(aside[what], path)
    finally:
        for tmp in aside.values():
            with contextlib.suppress(OSError):
                os.remove(tmp)
    return {what: path for what, (path, _data) in files.items()}


def _path(folder: str, data: bytes, what: str) -> str:
    """Where a file is kept: named by its own hash, so a key never replaces another."""
    return os.path.join(folder, _NAME.format(hashlib.sha256(data).hexdigest()[:16], what))


def _write_aside(folder: str, data: bytes) -> str:
    """Write `data` to a new private file of its own in `folder` (0600; another upload
    at the same time has another); returns its path."""
    fd, path = tempfile.mkstemp(prefix=".upload-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(path)
        raise
    return path


async def read_file(form, name: str) -> Optional[bytes]:
    """The bytes of the file a multipart form has as `name`: None when there is none (no
    part, a text field, or the browser's empty part of a file input left blank). Reads at
    most MAX_FILE_BYTES + 1 bytes (store() refuses a longer one)."""
    part = form.get(name)
    if not hasattr(part, "read") or not getattr(part, "filename", ""):
        return None
    return bytes(await part.read(MAX_FILE_BYTES + 1))
