"""vendor/wheels/ must match requirements.txt (the offline install set).

For every pinned requirement and every Python 3.10–3.14 × Linux/Windows combination
its markers select, a compatible wheel of exactly that version must be bundled; and
no wheel may be left over from an old pin. Fix with: python tools/refresh_wheels.py

Run: python3 tests/test_vendor_wheels.py   (stdlib only)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import refresh_wheels as rw  # noqa: E402


def test_markers():
    assert rw.marker_applies('python_version < "3.11"', "3.10", "Linux")
    assert not rw.marker_applies('python_version < "3.11"', "3.11", "Linux")
    assert rw.marker_applies('python_version >= "3.11"', "3.14", "Windows")
    assert rw.marker_applies('platform_system == "Windows"', "3.12", "Windows")
    assert not rw.marker_applies('platform_system == "Windows"', "3.12", "Linux")
    try:
        rw.marker_applies('sys_platform == "linux"', "3.12", "Linux")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown marker must not be silently ignored")


def test_wheel_tags():
    fits = rw.wheel_fits
    assert fits("starlette-1.10.0-py3-none-any.whl", "3.13", "Windows")
    assert fits("pyserial-3.5-py2.py3-none-any.whl", "3.10", "Linux")
    assert fits("psutil-7.2.2-cp36-abi3-manylinux2010_x86_64.manylinux_2_12_x86_64.manylinux_2_28_x86_64.whl",
                "3.14", "Linux")
    assert not fits("psutil-7.2.2-cp37-abi3-win_amd64.whl", "3.12", "Linux")
    assert fits("markupsafe-3.0.4-cp311-cp311-win_amd64.whl", "3.11", "Windows")
    assert not fits("markupsafe-3.0.4-cp311-cp311-win_amd64.whl", "3.12", "Windows")
    assert not fits("websockets-17.4-cp311-cp311-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl",
                    "3.11", "Windows")


def test_vendor_matches_requirements():
    missing, stale = rw.problems()
    msg = []
    if missing:
        msg.append("missing wheels:\n  " + "\n  ".join(missing))
    if stale:
        msg.append("stale wheels (no requirement uses them):\n  " + "\n  ".join(stale))
    assert not msg, "\n".join(msg) + "\nrun: python tools/refresh_wheels.py"


if __name__ == "__main__":
    test_markers()
    test_wheel_tags()
    test_vendor_matches_requirements()
    print("PASS: vendor/wheels matches requirements.txt for Python 3.10-3.14 on Linux + Windows")
