"""What keeps working under the names of 2.9.x and older, when ser2net became ser2netweb:

  * ser2net.py, the start script's old name: the same command line as ser2netweb.py
    (--version, --about, a server start), so systemd units, Windows services, copies of
    start.sh / start.bat and scripts that name it keep working; a server started with it
    says so once in its start-up log, other commands print nothing extra.

Run: python3 tests/test_old_names.py
"""
import shutil
import subprocess
import sys
import tempfile

from _server import ROOT, Server

from ser2netweb import __version__, cli, standalone


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, script, "--no-bootstrap", *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)


def test_old_script_runs_the_same():
    new, old = run("ser2netweb.py", "--version"), run("ser2net.py", "--version")
    assert new.returncode == old.returncode == 0, (new, old)
    assert new.stdout == old.stdout and new.stdout.split()[-1] == __version__, (new, old)
    new, old = run("ser2netweb.py", "--about"), run("ser2net.py", "--about")
    assert new.returncode == old.returncode == 0 and old.stdout == new.stdout, (new, old)
    assert old.stderr == "", old.stderr
    print("ser2net.py: --version / --about as ser2netweb.py, nothing extra printed  OK")


def test_old_script_note():
    note = cli.OLD_SCRIPT_NOTE.format(script="ser2net.py")
    assert "ser2net.py" in note and "ser2netweb.py" in note, note
    with Server({"mappings": []}, script="ser2net.py") as srv:
        out = srv.output()
    assert out.count(note) == 1, out[-3000:]
    with Server({"mappings": []}) as srv:
        out = srv.output()
    assert note not in out, out[-3000:]
    # in-process: the note is for the start-up log only, given by the old script alone
    standalone.notes.clear()
    d = tempfile.mkdtemp(prefix="s2n_old_")
    try:
        cli.main(["--about", "--no-bootstrap", "--data-dir", d], started_as="ser2net.py")
        assert standalone.notes == [note], standalone.notes
    finally:
        standalone.notes.clear()
        shutil.rmtree(d, ignore_errors=True)
    print("ser2net.py: a server it starts logs the old-name note once; ser2netweb.py none  OK")


def main():
    test_old_script_runs_the_same()
    test_old_script_note()
    print("\nPASS: the names of 2.9.x keep working")


if __name__ == "__main__":
    main()
