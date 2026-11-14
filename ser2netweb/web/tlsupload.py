"""TLS files uploaded in the browser (Settings → Admin TLS, a mapping's form), for an
installation whose admin has no shell on the host to put them there.

An upload is a certificate with its key, and for a mapping a CA file as well. Before
anything is kept, the files are written aside in the data folder's tls/ (private: the
folder 0700 or, on Windows, owner-only like the data folder; each file 0600) and loaded as
ser2netweb will load them (engine/tls.py): the key must be the certificate's and decrypt
with the password given, a CA file must hold a certificate, and a mapping's pair must not
be the admin UI's (nor the admin UI's a mapping's), compared by the certificates' public
key, so also as another copy of the key or with another certificate: browsers send a
site's cookies to every port of it. Only then they get their names,
`uploaded-<16 hex digits of the file's SHA-256>-cert.pem` / `-key.pem` / `-ca.pem`: the
same files uploaded again replace themselves, renewed ones stand next to the old (which
stay until someone deletes them: a configuration backup may name them), and a key never
replaces another one (a pair already in use keeps its key and its password).

The upload only stores files and answers their paths. They go into the same settings a
typed path goes into (admin_ui.tls_cert, network.tls_cert, ...), which keep working as
before.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import os
import re
import tempfile
from typing import Optional

from ..config import data_path, lock_down_dir, replace_when_free
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


def _der_element(der: bytes, at: int) -> tuple[int, int, int]:
    """(tag, where its value begins, where it ends) of the DER element at `at`. Raises
    ValueError for one that runs past the end."""
    tag, length = der[at], der[at + 1]
    at += 2
    if length & 0x80:
        size = length & 0x7F
        if not 0 < size <= 4:
            raise ValueError("DER length")
        length, at = int.from_bytes(der[at:at + size], "big"), at + size
    if at + length > len(der):
        raise ValueError("DER length")
    return tag, at, at + length


def public_key(pem: bytes) -> Optional[bytes]:
    """The subjectPublicKeyInfo (DER) of the first certificate in `pem`: the same for
    every certificate of a key, however the key file is written (PKCS#1 or #8, encrypted
    or not). None when there is none or it can't be read."""
    body = next((b for kind, b in _PEM_BLOCK.findall(pem) if kind == b"CERTIFICATE"), None)
    if body is None:
        return None
    try:
        der = base64.b64decode(b"".join(body.split()), validate=True)
        _tag, at, _end = _der_element(der, 0)       # Certificate
        tag, at, end = _der_element(der, at)        # tbsCertificate
        fields: list[tuple[int, int, int]] = []
        while at < end and len(fields) < 8:
            field = _der_element(der, at)
            fields.append((field[0], at, field[2]))
            at = field[2]
        if fields and fields[0][0] == 0xA0:         # [0] version
            fields = fields[1:]
        # serialNumber, signature, issuer, validity, subject, subjectPublicKeyInfo
        spki_tag, start, stop = fields[5]
    except (ValueError, IndexError, binascii.Error):
        return None
    return der[start:stop] if tag == 0x30 and spki_tag == 0x30 else None


def _read(path: str) -> Optional[bytes]:
    """A file of the configuration (a relative path is one in the data directory); None
    when it can't be read."""
    if not path:
        return None
    try:
        with open(data_path(path), "rb") as fh:
            return fh.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None


def _same_pair(cert: bytes, key: bytes, other_cert: str, other_key: str) -> bool:
    """Is the uploaded pair the one in the files `other_cert` / `other_key` (paths from
    the configuration; a file that can't be read is not the same)? The same public key
    in the certificates, or the same key or certificate block."""
    theirs_cert, theirs_key = _read(other_cert), _read(other_key)
    mine = public_key(cert)
    if mine is not None and theirs_cert is not None and public_key(theirs_cert) == mine:
        return True
    for ours, theirs, label in ((key, theirs_key, b"PRIVATE KEY"),
                                (cert, theirs_cert, b"CERTIFICATE")):
        a, b = _blocks(ours, label), _blocks(theirs or b"", label)
        if a and b and a[0] == b[0]:
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
        for what, (path, data) in files.items():
            _keep(aside[what], path, data)
    finally:
        for tmp in aside.values():
            with contextlib.suppress(OSError):
                os.remove(tmp)
    return {what: path for what, (path, _data) in files.items()}


def _keep(tmp: str, path: str, data: bytes) -> None:
    """Move the checked file `tmp` to `path`. A file is named by its hash, so one that is
    there with these bytes already (the same file uploaded before, or at the same time)
    is kept as it is when Windows refuses the rename (config.replace_when_free)."""
    replace_when_free(tmp, path, done=lambda: _holds(path, data))


def _holds(path: str, data: bytes) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(data) + 1) == data
    except OSError:
        return False


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
