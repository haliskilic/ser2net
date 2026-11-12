"""Readers for .github/workflows/*.yml, shared by the CI and release pipeline tests.

The tests run where only the standard library (and ser2net's requirements) is installed,
so there is no YAML parser: the workflows keep a plain block layout (two-space indents,
job ids at two spaces, their keys at four), which these helpers rely on.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")


def names() -> list[str]:
    return sorted(n for n in os.listdir(WORKFLOWS) if n.endswith(".yml"))


def read(name: str) -> str:
    with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as fh:
        return fh.read()


def jobs(text: str) -> dict[str, str]:
    """Job id -> its block, for the top-level jobs: mapping of a workflow file."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"(?m)^  ([A-Za-z0-9_-]+):[ \t]*\n", body)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def key(block: str, name: str) -> str | None:
    """The value of a job-level `name: value` line (comments stripped), or None."""
    m = re.search(rf"(?m)^    {re.escape(name)}: (.*?)(?:\s+#.*)?$", block)
    return m.group(1) if m else None


def needs(block: str) -> list[str]:
    value = key(block, "needs")
    if value is None:
        return []
    return [n.strip() for n in value.strip("[]").split(",") if n.strip()]


def transitive_needs(all_jobs: dict[str, str], job: str) -> set[str]:
    seen: set[str] = set()
    todo = needs(all_jobs[job])
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo += needs(all_jobs[n])
    return seen


def permissions(block: str) -> dict[str, str]:
    """A job's own permissions: block (scope -> read/write); {} when it has none."""
    m = re.search(r"(?m)^    permissions:\n((?:      .*\n)+)", block)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        scope, _, level = line.split("#", 1)[0].strip().partition(":")
        out[scope.strip()] = level.strip()
    return out


def uses(text: str) -> list[tuple[int, str, str]]:
    """Every `uses:` of a file: (line number, reference, the rest of the line)."""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r"\s*(?:- )?uses: (\S+)(.*)$", line)
        if m:
            out.append((i, m.group(1), m.group(2)))
    return out
