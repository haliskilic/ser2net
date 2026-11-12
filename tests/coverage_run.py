"""Run the whole test suite under coverage.py — including the ser2net servers the
tests start as subprocesses — and combine the data.

    pip install coverage
    python tests/coverage_run.py [-v]      # then: coverage report / coverage html

Every Python process the suite starts imports a temporary ``sitecustomize`` that
calls ``coverage.process_startup()`` (COVERAGE_PROCESS_START points at .coveragerc),
so code that only runs inside a spawned server is measured as well. Linux: also
runs the socat data-path tests when socat is installed.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main(argv) -> int:
    env = dict(os.environ)
    env["COVERAGE_PROCESS_START"] = os.path.join(ROOT, ".coveragerc")
    env["SER2NETWEB_COV_DATA"] = os.path.join(ROOT, ".coverage")
    with tempfile.TemporaryDirectory() as hook:
        with open(os.path.join(hook, "sitecustomize.py"), "w", encoding="utf-8") as fh:
            fh.write("import coverage\ncoverage.process_startup()\n")
        env["PYTHONPATH"] = os.pathsep.join(p for p in (hook, env.get("PYTHONPATH")) if p)
        subprocess.run([sys.executable, "-m", "coverage", "erase"], cwd=ROOT, env=env, check=True)
        args = [sys.executable, os.path.join(HERE, "run_all.py"), *argv]
        if shutil.which("socat") and "--socat" not in args:
            args.append("--socat")
        result = subprocess.run(args, cwd=ROOT, env=env)
    subprocess.run([sys.executable, "-m", "coverage", "combine", "-q"], cwd=ROOT, env=env, check=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
