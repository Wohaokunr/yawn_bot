#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9A-Za-z.-]+)?$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
TIMESTAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write YawnBot production deployment metadata")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--previous-image", default="")
    parser.add_argument("--current-image", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--release-version", required=True)
    parser.add_argument("--db-backup", default="")
    parser.add_argument("--migration-before", default="")
    parser.add_argument("--migration-after", default="")
    parser.add_argument("--migration-heads", default="")
    parser.add_argument("--deployed-at", required=True)
    parser.add_argument("--status", default="healthy", choices=("healthy",))
    return parser


def _validate(args: argparse.Namespace) -> None:
    if not VERSION_RE.fullmatch(args.release_version):
        raise SystemExit(f"invalid release version: {args.release_version}")
    if not COMMIT_RE.fullmatch(args.commit_sha):
        raise SystemExit("invalid commit SHA")
    if not TIMESTAMP_RE.fullmatch(args.deployed_at):
        raise SystemExit(f"invalid deployment timestamp: {args.deployed_at}")
    if "\n" in args.current_image or "\r" in args.current_image:
        raise SystemExit("invalid current image reference")


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def main() -> int:
    args = _parser().parse_args()
    _validate(args)

    root = args.root
    data = {
        "schema_version": 1,
        "previous_image": args.previous_image,
        "current_image": args.current_image,
        "commit_sha": args.commit_sha,
        "release_version": args.release_version,
        "db_backup": args.db_backup,
        "migration_before": args.migration_before.splitlines(),
        "migration_after": args.migration_after.splitlines(),
        "migration_heads": args.migration_heads.splitlines(),
        "deployed_at": args.deployed_at,
        "status": args.status,
    }
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    record = root / f"deploy-{args.release_version}-{args.deployed_at}.json"

    _atomic_write(record, payload)
    _atomic_write(root / "current.json", payload)
    print(record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
