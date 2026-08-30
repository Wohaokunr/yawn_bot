#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_RELEASE_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9A-Za-z.-]+)?$")
_TIMESTAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def _atomic_write(path: Path, payload: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    release_version = _required_env("RELEASE_VERSION")
    deployed_at = _required_env("DEPLOYED_AT")
    if not _RELEASE_RE.fullmatch(release_version):
        raise SystemExit(f"invalid release version: {release_version}")
    if not _TIMESTAMP_RE.fullmatch(deployed_at):
        raise SystemExit(f"invalid deployment timestamp: {deployed_at}")

    root = Path(os.environ.get("DEPLOYMENT_ROOT", "/deployments"))
    root.mkdir(parents=True, exist_ok=True)

    data = {
        "previous_image": os.environ.get("PREVIOUS_IMAGE", ""),
        "current_image": _required_env("CURRENT_IMAGE"),
        "previous_browser_image": os.environ.get("PREVIOUS_BROWSER_IMAGE", ""),
        "current_browser_image": os.environ.get("CURRENT_BROWSER_IMAGE", ""),
        "commit_sha": _required_env("COMMIT_SHA"),
        "release_version": release_version,
        "db_backup": os.environ.get("DB_BACKUP", ""),
        "migration_before": os.environ.get("MIGRATION_BEFORE", "").splitlines(),
        "migration_after": os.environ.get("MIGRATION_AFTER", "").splitlines(),
        "migration_heads": os.environ.get("MIGRATION_HEADS", "").splitlines(),
        "deployed_at": deployed_at,
        "status": "healthy",
    }
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    record = root / f"deploy-{release_version}-{deployed_at}.json"
    _atomic_write(record, payload)
    _atomic_write(root / "current.json", payload)


if __name__ == "__main__":
    main()
