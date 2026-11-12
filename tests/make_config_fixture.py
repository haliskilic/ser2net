"""Make tests/fixtures/config-<version>.json with the code of that release.

    python tests/make_config_fixture.py v2.8.0

Run by hand once per minor release, after its tag (CONTRIBUTING.md, Releases), from a git
checkout with the release tags; not in CI. It unpacks `git archive <tag> app` into a
temporary folder and, with that release's code, loads the previous fixture (an install
upgraded from the release before), switches on what the release added (FEATURES below:
every new setting with a value other than its default, dummy secrets), adds an account
made by that release's password hashing, validates the result and saves it with the
release's own ConfigStore, which writes config.json exactly as that release does.

tests/test_upgrade_from_<X_Y>.py then loads the frozen file with the current code: a later
rename, a dropped key or a stricter rule that would reset or refuse a setting of that
release fails there. For a new minor release: add its entry to FEATURES (its new settings,
from its CHANGELOG section), run this with its tag and add a test for the new fixture like
tests/test_upgrade_from_2_8.py.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")


def _features_2_8_0(d: dict) -> None:
    """What 2.8.0 added to config.json, each set to a value other than its default."""
    d["admin_ui"]["language"] = "tr"
    d["notifications"] = {
        "enabled": True,
        "events": ["device_lost", "device_back", "start_failed", "client_connected",
                   "client_disconnected", "login_failed"],
        "repeat_s": 900,
        "webhook_url": "https://hooks.example.invalid/services/T0000/B0000/dummy-webhook-token",
        "webhook_format": "slack",
        "telegram_chat_id": "-1001234567890",
        "email_to": "ops@example.invalid, oncall@example.invalid",
        "email_from": "ser2net@example.invalid",
        "smtp_host": "smtp.example.invalid",
        "smtp_port": 465,
        "smtp_security": "ssl",
        "smtp_username": "ser2net-mailer",
        "smtp_password": "dummy-smtp-pass-2.8.0",
    }
    by_name = {m["name"]: m for m in d["mappings"]}
    # traffic traces: size and number of rotated files
    for name, mb, keep in (("PLC-1 raw", 2.5, 5), ("Telnet console", 50.0, 0),
                           ("Modbus gateway", 0.5, 1)):
        by_name[name]["options"].update(trace_max_mb=mb, trace_keep=keep)
    gw = by_name["Modbus gateway"]
    # Sparkplug B edge node, and the new point kinds: coils / discrete inputs (bool), a
    # writable coil and a writable holding register
    gw["mqtt"].update(payload_format="sparkplug_b", sparkplug_group_id="plant-2",
                      sparkplug_node_id="gw-line-2")
    gw["modbus_poll"]["points"][1]["writable"] = True     # count: fn 3, uint32
    gw["modbus_poll"]["points"] += [
        {"name": "pump_on", "unit": 1, "fn": 1, "address": 7, "dtype": "bool", "scale": 1.0,
         "writable": True},
        {"name": "door_open", "unit": 3, "fn": 2, "address": 0, "dtype": "bool",
         "scale": 1.0, "writable": False},
        {"name": "setpoint", "unit": 1, "fn": 3, "address": 40, "dtype": "int16",
         "scale": 0.5, "writable": True},
    ]


# version -> (the fixture it builds on, what the release added, an account made by it)
FEATURES = {
    "2.8.0": ("2.7.1", _features_2_8_0, ("op28", "operator", "new-in-2.8.0-pass-1")),
}


def _inside(root: str, version: str, previous_path: str, out_path: str) -> None:
    """Runs in a child process with the release's code first on sys.path."""
    sys.path.insert(0, root)
    import app
    from app.config import AppConfig, ConfigStore, User
    from app.web.auth import hash_password

    assert app.__version__ == version, (app.__version__, version)
    _prev, features, (user, role, password) = FEATURES[version]
    with open(previous_path, encoding="utf-8") as fh:
        cfg = AppConfig.from_dict(json.load(fh))  # the upgrade, as that release loads it
    d = json.loads(json.dumps(cfg.to_dict()))
    features(d)
    cfg = AppConfig.from_dict(d)
    again = json.loads(json.dumps(cfg.to_dict()))
    assert again == d, "the release dropped or changed a feature setting on load"
    cfg.users.append(User.from_dict({"username": user, "password_hash": hash_password(password),
                                     "role": role, "pwd_version": 1}))
    ConfigStore(out_path).save(cfg)  # validates, then writes as the release does


def main(argv) -> int:
    if argv[:1] == ["--inside"]:
        _inside(*argv[1:5])
        return 0
    for tag in argv:
        version = tag.lstrip("v")
        if version not in FEATURES:
            sys.stderr.write(f"{tag}: add the release's settings to FEATURES first\n")
            return 1
        previous = os.path.join(FIXTURES, f"config-{FEATURES[version][0]}.json")
        tmp = tempfile.mkdtemp(prefix="s2n_fixture_")
        try:
            archive = subprocess.run(["git", "archive", tag, "app"], cwd=ROOT, check=True,
                                     capture_output=True).stdout
            subprocess.run(["tar", "-x", "-C", tmp], input=archive, check=True)
            out = os.path.join(tmp, "data", "config.json")
            res = subprocess.run([sys.executable, os.path.abspath(__file__), "--inside", tmp,
                                  version, previous, out], cwd=tmp, capture_output=True,
                                 text=True, encoding="utf-8")
            if res.returncode:
                sys.stderr.write(res.stderr)
                return 1
            name = os.path.join(FIXTURES, f"config-{version}.json")
            shutil.copyfile(out, name)
            print(f"wrote {os.path.relpath(name, ROOT)}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
