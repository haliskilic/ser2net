#!/usr/bin/env python3
"""ser2net.py: the name of ser2netweb's start script up to 2.9.x.

It runs the same as ser2netweb.py, so the systemd units, Windows services, copies of
start.sh / start.bat and scripts that name it keep working; a server started with it says
so once in the start-up log. New set-ups use ser2netweb.py.
"""
# Python 3.6 syntax only, as in ser2netweb.py: an old python3 gets the message below.
import sys

if sys.version_info < (3, 10):
    sys.exit("ser2netweb needs Python 3.10 or newer; this is Python {} ({}). Run it with a "
             "newer one (e.g. python3.11 ser2netweb.py; RHEL / AlmaLinux / Rocky: sudo dnf "
             "install python3.11), or use the standalone binary or the Docker image "
             "(README.md > Install & run).".format(sys.version.split()[0], sys.executable))

from ser2netweb.cli import main

if __name__ == "__main__":
    raise SystemExit(main(started_as="ser2net.py"))
