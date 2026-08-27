# ruff: noqa: T201
"""Verify that a fresh checkout contains only repository-approved content."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    dirty = _git(root, "status", "--porcelain=v1", "--untracked-files=all").strip()
    if dirty:
        print("Fresh-checkout content test requires a clean worktree:")
        print(dirty)
        return 1

    guard = subprocess.run(
        (sys.executable, str(root / "tools" / "repo_guard.py")),
        cwd=root,
        check=False,
    )
    if guard.returncode != 0:
        return guard.returncode

    tracked_count = len(_git(root, "ls-files").splitlines())
    print(
        "Repository content OK: "
        f"{tracked_count} tracked files and no runtime/generated/private "
        "checkout state."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
