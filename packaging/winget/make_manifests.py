"""winget's manifests for a release of ser2netweb, in the layout of microsoft/winget-pkgs:

    python packaging/winget/make_manifests.py vX.Y.Z [--sums SHA256SUMS] [--out DIR]

writes the version, installer and locale manifests of haliskilic.ser2netweb X.Y.Z to
packaging/winget/manifests/h/haliskilic/ser2netweb/X.Y.Z (or straight into DIR): the release's
portable ser2netweb-windows-x64.exe (x64; the command ser2netweb) with its SHA-256 from the
release's SHA256SUMS (or the downloaded file --sums), the en-US locale (the default) and
tr-TR. .github/workflows/winget.yml makes and submits them for each release;
`python tools/windows_channels.py validate` checks them against winget's schemas. Standard
library only; the YAML is written here, in the plain style winget-pkgs uses.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Union

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools"))

import windows_channels as wc

Value = Union[str, list[str], list[dict[str, str]]]
Manifest = dict[str, Value]

OWNER = "Halis KILIÇ"  # (NOTICE)
GENERATOR = "packaging/winget/make_manifests.py"


def manifests(tag: str, sha256: str) -> dict[str, Manifest]:
    """file name -> manifest (its fields in winget-pkgs' order) for the release `tag`."""
    version = wc.version_of(tag)
    ident = {"PackageIdentifier": wc.WINGET_ID, "PackageVersion": version}
    legal = f"{wc.HOMEPAGE}/blob/{tag}"
    service = ("As a Windows service: run 'ser2netweb --install-service' from an elevated "
               "prompt, and again after each upgrade (the service runs its own copy, in "
               "C:\\Program Files\\ser2netweb).")
    en: Manifest = {
        **ident,
        "PackageLocale": "en-US",
        "Publisher": OWNER,
        "PublisherUrl": "https://github.com/haliskilic",
        "PublisherSupportUrl": f"{wc.HOMEPAGE}/issues",
        "PrivacyUrl": f"{legal}/docs/PRIVACY.md",
        "Author": OWNER,
        "PackageName": "ser2netweb",
        "PackageUrl": wc.HOMEPAGE,
        "License": wc.LICENSE,
        "LicenseUrl": f"{legal}/LICENSE",
        "Copyright": f"Copyright (C) 2026 {OWNER}",
        "CopyrightUrl": f"{legal}/NOTICE",
        "ShortDescription": wc.SUMMARY,
        "Description": (
            "ser2netweb shares local serial ports (RS-232 / RS-485, USB adapters) over the "
            "network as raw TCP, telnet, RFC 2217 or UDP, runs a Modbus RTU/TCP gateway and "
            "publishes serial data and Modbus points to MQTT and Sparkplug B, all set up and "
            "watched in a browser, with accounts, roles, an audit log, TLS and LDAP / OpenID "
            "Connect sign-in. It runs in a console window or as a Windows service.\n\n"
            + wc.DISAMBIGUATION),
        "Moniker": "ser2netweb",
        "Tags": ["serial", "serial-port", "com-port", "rs232", "rs485", "usb-serial", "tcp",
                 "telnet", "rfc2217", "udp", "modbus", "mqtt", "sparkplug", "iot",
                 "serial-to-ethernet", "web-ui"],
        "ReleaseNotesUrl": f"{wc.HOMEPAGE}/releases/tag/{tag}",
        "InstallationNotes": (
            "Start it with: ser2netweb (the first run asks for the web UI's address). "
            + service + f" Where it keeps its data: {wc.WINDOWS_GUIDE}"),
        "Documentations": [{"DocumentLabel": "Windows guide", "DocumentUrl": wc.WINDOWS_GUIDE},
                           {"DocumentLabel": "README", "DocumentUrl": f"{wc.HOMEPAGE}#readme"}],
        "ManifestType": "defaultLocale",
        "ManifestVersion": wc.WINGET_MANIFEST_VERSION,
    }
    tr: Manifest = {
        **ident,
        "PackageLocale": "tr-TR",
        "Publisher": OWNER,
        "PackageName": "ser2netweb",
        "License": wc.LICENSE,
        "ShortDescription": ("Web arayüzlü seri port - ağ köprüsü: raw TCP, telnet, RFC 2217, "
                             "UDP, Modbus geçidi, MQTT"),
        "Description": (
            "ser2netweb yerel seri portları (RS-232 / RS-485, USB dönüştürücüler) ağ üzerinden "
            "raw TCP, telnet, RFC 2217 ya da UDP olarak paylaşır, Modbus RTU/TCP geçidi "
            "çalıştırır, seri verileri ve Modbus noktalarını MQTT'ye ve Sparkplug B'ye yayınlar. "
            "Hepsi tarayıcıdan kurulur ve izlenir: hesaplar, roller, denetim kaydı, TLS, LDAP / "
            "OpenID Connect ile giriş. Konsol penceresinde ya da Windows servisi olarak "
            "çalışır.\n\n"
            "ser2netweb bağımsız bir projedir; Corey Minyard'ın ser2net'iyle bir bağlantısı "
            "yoktur."),
        "InstallationNotes": (
            "Başlatmak için: ser2netweb (ilk çalıştırma web arayüzünün adresini sorar). Windows "
            "servisi olarak: yönetici olarak açılmış bir komut isteminde 'ser2netweb "
            "--install-service' komutunu çalıştırın, her yükseltmeden sonra yeniden (servis "
            "kendi kopyasını, C:\\Program Files\\ser2netweb içindekini çalıştırır). Verisini "
            f"nerede tuttuğu: {wc.WINDOWS_GUIDE}"),
        "Documentations": [{"DocumentLabel": "Windows kılavuzu (İngilizce)",
                            "DocumentUrl": wc.WINDOWS_GUIDE}],
        "ManifestType": "locale",
        "ManifestVersion": wc.WINGET_MANIFEST_VERSION,
    }
    installer: Manifest = {
        **ident,
        "InstallerType": "portable",
        "Commands": [wc.COMMAND],
        "Installers": [{"Architecture": "x64", "InstallerUrl": wc.release_url(tag),
                        "InstallerSha256": sha256.upper()}],
        "ManifestType": "installer",
        "ManifestVersion": wc.WINGET_MANIFEST_VERSION,
    }
    version_manifest: Manifest = {**ident, "DefaultLocale": "en-US", "ManifestType": "version",
                                  "ManifestVersion": wc.WINGET_MANIFEST_VERSION}
    base = wc.WINGET_ID
    return {f"{base}.yaml": version_manifest, f"{base}.installer.yaml": installer,
            f"{base}.locale.en-US.yaml": en, f"{base}.locale.tr-TR.yaml": tr}


# ---------------------------------------------------------------- YAML (the subset used)
# a plain scalar: starts with a letter, digit or '(' and holds nothing YAML reads otherwise
_PLAIN = re.compile(r"[\w(][\w .,/:@()+'#-]*", re.UNICODE)
_SPECIAL = re.compile(r"(?i)y|n|yes|no|true|false|on|off|null|~")
_VERSION = re.compile(r"\d+(\.\d+){2,}")  # X.Y.Z: a string in YAML (unlike X.Y, a float)
# an upper-case SHA-256 with a letter other than E: never a number either
_DIGEST = re.compile(r"(?=.*[A-DF])[0-9A-F]{64}")


def scalar(value: str) -> str:
    """`value` as a YAML scalar: plain when that reads back as the same string, else in single
    quotes."""
    plain = (_PLAIN.fullmatch(value) and not _SPECIAL.fullmatch(value)
             and not value.endswith((":", " ")) and ": " not in value and " #" not in value
             and (not value[0].isdigit() or _VERSION.fullmatch(value)
                  or _DIGEST.fullmatch(value)))
    return value if plain else "'" + value.replace("'", "''") + "'"


def dump(manifest: Manifest, schema: str) -> str:
    """A manifest as winget-pkgs keeps it: the comments naming the tool and the schema, then
    the fields; lists at the indentation of their key, a text with line breaks as a block."""
    lines = [f"# Created with {GENERATOR} ({wc.HOMEPAGE})",
             f"# yaml-language-server: $schema=https://aka.ms/winget-manifest.{schema}."
             f"{wc.WINGET_MANIFEST_VERSION}.schema.json", ""]
    for key, value in manifest.items():
        if isinstance(value, str) and "\n" in value:
            lines.append(f"{key}: |-")
            lines += [f"  {line}" if line else "" for line in value.split("\n")]
        elif isinstance(value, str):
            lines.append(f"{key}: {scalar(value)}")
        else:
            lines.append(f"{key}:")
            for item in value:
                if isinstance(item, str):
                    lines.append(f"- {scalar(item)}")
                    continue
                for i, (k, v) in enumerate(item.items()):
                    lines.append(f"{'- ' if i == 0 else '  '}{k}: {scalar(v)}")
    return "\n".join(lines) + "\n"


def files(tag: str, sha256: str) -> dict[str, str]:
    """file name -> its text."""
    return {name: dump(m, str(m["ManifestType"])) for name, m in manifests(tag, sha256).items()}


def folder(tag: str) -> str:
    """The release's folder in winget-pkgs' layout, under packaging/winget/manifests."""
    publisher, package = wc.WINGET_ID.split(".", 1)
    return os.path.join(wc.WINGET_DIR, publisher[0].lower(), publisher, package,
                        wc.version_of(tag))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="make_manifests.py", description=__doc__.split("\n\n")[0])
    p.add_argument("tag", help="the release, vX.Y.Z")
    p.add_argument("--sums", help="a downloaded SHA256SUMS of that release")
    p.add_argument("--out", help="write the manifests into this folder")
    args = p.parse_args(argv)
    try:
        out = args.out or folder(args.tag)
        texts = files(args.tag, wc.exe_sha256(args.tag, args.sums))
        os.makedirs(out, exist_ok=True)
        for name, text in texts.items():
            with open(os.path.join(out, name), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
    except (OSError, ValueError) as e:
        print(f"make_manifests.py: {e}", file=sys.stderr)
        return 1
    print(f"wrote {len(texts)} manifests to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
