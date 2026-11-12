"""The release pipeline publishes only what every check passed, with pinned actions.

* every action in .github/workflows is pinned to a full commit SHA with its version in a
  comment (Dependabot updates both): a moved or replaced tag can't change what a job
  runs, and the repository's "require full-length SHA pins" policy can be switched on.

Run: python3 tests/test_release_pipeline.py   (stdlib only)
"""
import re

from _workflows import names, read, uses

_PINNED = re.compile(r"[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}")


def test_actions_pinned_by_sha():
    count = 0
    for name in names():
        for line, ref, rest in uses(read(name)):
            if ref.startswith("./.github/workflows/"):
                continue  # a reusable workflow of this repository, same commit
            assert _PINNED.fullmatch(ref), f"{name}:{line}: {ref} is not pinned to a commit SHA"
            assert re.match(r"\s+# v\d", rest), (
                f"{name}:{line}: {ref} needs its version as a '# vX.Y.Z' comment")
            count += 1
    assert count > 40, count
    print(f"all {count} action references are pinned to a commit SHA  OK")


if __name__ == "__main__":
    test_actions_pinned_by_sha()
    print("\nPASS: release pipeline")
