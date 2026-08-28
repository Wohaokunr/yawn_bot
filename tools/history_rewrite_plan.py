#!/usr/bin/env python3
"""Build exact path and secret-redaction plans for public-history rewriting.

The plan reuses the same classifiers as history_secret_audit.py. It never prints
file contents or secret values. Secret replacement values are written only to an
explicit caller-provided file, intended to live in an ephemeral private temp dir.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path

from history_secret_audit import (
    DEFAULT_MAX_BLOB_SIZE,
    _ASSIGNMENT_PATTERN,
    _PLACEHOLDER_MARKERS,
    _SECRET_PATTERNS,
    _git as _git_bytes,
    _object_meta,
    _reachable_objects,
    _sensitive_path_reason,
)


def _git(*args: str) -> str:
    return subprocess.run(
        ("git", *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _is_placeholder(value: bytes) -> bool:
    normalized = value.strip().strip(b"\"'").lower()
    if not normalized:
        return True
    if normalized.startswith((b"${", b"$")):
        return True
    return any(marker in normalized for marker in _PLACEHOLDER_MARKERS)


def build_plan() -> tuple[list[str], dict[str, int], list[bytes]]:
    reasons: Counter[str] = Counter()
    paths: set[str] = set()
    replacements: set[bytes] = set()
    paths_by_oid = _reachable_objects()
    metadata = _object_meta(list(paths_by_oid))

    # rev-list --objects includes commit/tree objects as well as blobs. Feeding a
    # directory-tree path such as ``data/nonebot_plugin_orm`` to git-filter-repo
    # would remove the whole subtree and could destroy legitimate migrations.
    # Only blob paths are exact file-removal candidates.
    for oid, observed_paths in paths_by_oid.items():
        object_type, size = metadata.get(oid, ("", 0))
        if object_type != "blob":
            continue

        for path in observed_paths:
            reason = _sensitive_path_reason(path)
            if reason is None:
                continue
            paths.add(path)
            reasons[reason] += 1

        if size > DEFAULT_MAX_BLOB_SIZE:
            continue
        data = _git_bytes("cat-file", "blob", oid)

        # Exact token matches are safer than a broad regex replacement: the
        # current tree remains unchanged and placeholder examples stay readable.
        for label, pattern in _SECRET_PATTERNS:
            if label == "private key":
                # A BEGIN marker alone is not enough to safely remove an entire
                # embedded private-key block. The audit remains the hard gate for
                # this case; a future hit must be handled explicitly.
                continue
            for match in pattern.finditer(data):
                candidate = match.group(1) if match.lastindex else match.group(0)
                if not _is_placeholder(candidate):
                    replacements.add(candidate)

        for match in _ASSIGNMENT_PATTERN.finditer(data):
            candidate = match.group(1)
            if not _is_placeholder(candidate):
                replacements.add(candidate)

    return sorted(paths), dict(sorted(reasons.items())), sorted(replacements)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paths-out",
        type=Path,
        default=Path("history-rewrite-paths.txt"),
        help="exact paths file for git-filter-repo",
    )
    parser.add_argument(
        "--replacements-out",
        type=Path,
        default=Path("history-rewrite-replacements.txt"),
        help="private exact-value replacement file for git-filter-repo",
    )
    parser.add_argument(
        "--manifest-out",
        type=Path,
        default=Path("history-rewrite-manifest.json"),
        help="non-secret summary of the rewrite plan",
    )
    args = parser.parse_args()

    paths, reasons, replacements = build_plan()
    current_paths = set(_git("ls-files").splitlines())
    current_blockers = sorted(current_paths.intersection(paths))
    if current_blockers:
        print("Refusing to prepare rewrite: sensitive paths still exist in HEAD:")
        for path in current_blockers:
            print(f"  - {path}")
        return 2
    if not paths and not replacements:
        print("No history blockers found; no rewrite plan needed.")
        return 0

    args.paths_out.write_text("".join(f"{path}\n" for path in paths), encoding="utf-8")
    with args.replacements_out.open("wb") as handle:
        for value in replacements:
            # Scanner token/value alphabets do not contain newlines or the `==>`
            # delimiter. Never print this file: it contains historical credentials.
            handle.write(b"literal:" + value + b"==>REMOVED-HISTORICAL-SECRET\n")

    manifest = {
        "path_count": len(paths),
        "replacement_count": len(replacements),
        "reason_counts": reasons,
        "head": _git("rev-parse", "HEAD").strip(),
        "head_tree": _git("rev-parse", "HEAD^{tree}").strip(),
    }
    args.manifest_out.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "Prepared rewrite plan for "
        f"{len(paths)} historical paths and {len(replacements)} exact secret values."
    )
    for reason, count in reasons.items():
        print(f"  - {reason}: {count}")
    print(f"Paths: {args.paths_out}")
    print(f"Private replacements file: {args.replacements_out} (contents suppressed)")
    print(f"Manifest: {args.manifest_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
