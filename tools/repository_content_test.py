# ruff: noqa: T201
"""Verify that a fresh checkout contains only repository-approved content."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REQUIRED_OPEN_SOURCE_FILES = (
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    ".github/CODEOWNERS",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/deployment_help.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
)


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

    missing = [
        path
        for path in _REQUIRED_OPEN_SOURCE_FILES
        if not (root / path).is_file()
    ]
    if missing:
        print("Required open-source community files are missing:")
        for path in missing:
            print(f"  - {path}")
        return 1

    tracked_count = len(_git(root, "ls-files").splitlines())
    print(
        "Repository content OK: "
        f"{tracked_count} tracked files, required community files present, and no "
        "runtime/generated/private checkout state."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
