#!/usr/bin/env python3
"""ser2netweb — expose local serial ports over the network, managed from a web UI.

Start script of a source install (ser2netweb/cli.py has the command line):

    python3 ser2netweb.py                 # normal start (first run asks bind IP on console)
    python3 ser2netweb.py --reconfigure   # re-pick the admin UI bind IP/port on the console
    python3 ser2netweb.py --about         # versions of the bundled components (for bug reports)
    python3 ser2netweb.py --set-password admin   # lost the password? (also while it runs)
    python3 ser2netweb.py --reload        # the running ser2netweb reads config.json again
    python3 ser2netweb.py --help

ser2net.py, its name up to 2.9.x, runs the same.
"""
# No `from __future__ import annotations` and nothing newer than Python 3.6 syntax in this
# file: an old python3 (RHEL 8's 3.6, RHEL 9's 3.9) gets the message below instead of a
# SyntaxError or a TypeError. ser2netweb/ is imported only after it.
import sys

if sys.version_info < (3, 10):
    sys.exit("ser2netweb needs Python 3.10 or newer; this is Python {} ({}). Run it with a "
             "newer one (e.g. python3.11 ser2netweb.py; RHEL / AlmaLinux / Rocky: sudo dnf "
             "install python3.11), or use the standalone binary or the Docker image "
             "(README.md > Install & run).".format(sys.version.split()[0], sys.executable))

# The script's own directory is on sys.path, so the ser2netweb package (the folder next to
# this file, not this script) imports here, and PyInstaller bundles it in frozen builds.
from ser2netweb.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
