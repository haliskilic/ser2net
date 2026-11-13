"""python -m ser2netweb: the same as the ser2netweb command a pip / pipx install has (and
as python3 ser2netweb.py in a source checkout)."""
from ser2netweb.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
