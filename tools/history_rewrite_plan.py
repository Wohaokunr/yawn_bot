#!/usr/bin/env python3
"""Build an exact path-removal plan for public-history rewriting.

The plan reuses the same path classification as history_secret_audit.py. It never
prints file contents or secret values. The generated path list is suitable for
``git filter-repo --invert-paths --paths-from-file``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path

from history_secret_audit import _reachable_objects, _sensitive_path_reason


def _git(*args: str) -> str:
    return subprocess.run(
        ("git", *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def build_plan() -> tuple[list[str], dict[str, int]]:
    reasons: Counter[str] = Counter()
    paths: set[str] = set()
    for observed_paths in _reachable_objects().values():
        for path in observed_paths:
            reason = _sensitive_path_reason(path)
            if reason is None:
                continue
            paths.add(path)
            reasons[reason] += 1
    return sorted(paths), dict(sorted(reasons.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paths-out",
        type=Path,
        default=Path("history-rewrite-paths.txt"),
        help="exact paths file for git-filter-repo",
    )
    parser.add_argument(
        "--manifest-out",
        type=Path,
        default=Path("history-rewrite-manifest.json"),
        help="non-secret summary of the rewrite plan",
    )
    args = parser.parse_args()

    paths, reasons = build_plan()
    current_paths = set(_git("ls-files").splitlines())
    current_blockers = sorted(current_paths.intersection(paths))
    if current_blockers:
        print("Refusing to prepare rewrite: sensitive paths still exist in HEAD:")
        for path in current_blockers:
            print(f"  - {path}")
        return 2
    if not paths:
        print("No path-based history blockers found; no rewrite path list needed.")
        return 0

    args.paths_out.write_text("".join(f"{path}\n" for path in paths), encoding="utf-8")
    manifest = {
        "path_count": len(paths),
        "reason_counts": reasons,
        "head": _git("rev-parse", "HEAD").strip(),
        "head_tree": _git("rev-parse", "HEAD^{tree}").strip(),
    }
    args.manifest_out.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Prepared exact rewrite plan for {len(paths)} historical paths.")
    for reason, count in reasons.items():
        print(f"  - {reason}: {count}")
    print(f"Paths: {args.paths_out}")
    print(f"Manifest: {args.manifest_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
