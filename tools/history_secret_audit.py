#!/usr/bin/env python3
# ruff: noqa: T201
"""Audit every Git-reachable blob before making the repository public.

This is intentionally stricter than tools/repo_guard.py. repo_guard protects the
current index; this tool checks every branch/tag reachable through ``--all`` so a
secret deleted from HEAD is still reported before repository visibility changes.

The scanner never prints matched secret values.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from collections import defaultdict
from pathlib import PurePosixPath

DEFAULT_MAX_BLOB_SIZE = 2 * 1024 * 1024

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("private key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("OpenAI-style API key", re.compile(rb"\bsk-[A-Za-z0-9_.-]{20,}\b")),
    ("tp-style API token", re.compile(rb"\btp-[A-Za-z0-9_-]{24,}\b")),
    ("GitHub fine-grained token", re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("GitHub classic token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("AWS access key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    (
        "Cloudflare-style API token assignment",
        re.compile(
            rb"(?i)\b(?:cloudflare|cf)[_-]?(?:api[_-]?)?(?:token|key)\b"
            rb"[ \t]*[:=][ \t]*[\"']?([A-Za-z0-9_./+=-]{24,})"
        ),
    ),
)

# Keep this deliberately aligned with repo_guard.py. Horizontal whitespace is
# intentional: using \s here would let an empty KEY= line consume the next line
# and incorrectly classify the following variable name as a credential value.
_ASSIGNMENT_PATTERN = re.compile(
    rb"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|"
    rb"webui_admin_token|onebot_v11_access_token|deploy_ssh_private_key)\b"
    rb"[ \t]*[:=][ \t]*[\"']?([A-Za-z0-9_./+=-]{24,})"
)

_ENV_LINE_PATTERN = re.compile(
    rb"(?m)^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$"
)
_ENV_SECRET_KEY_PATTERN = re.compile(
    rb"(?i)(?:^|_)(?:api_?key|key|token|secret|password|cookie|dsn)$"
)

_PLACEHOLDER_MARKERS = (
    b"change-me",
    b"changeme",
    b"dummy",
    b"example",
    b"placeholder",
    b"replace-with",
    b"replace-me",
    b"replace_me",
    b"test-token",
    b"your-",
    b"your_",
)

_SENSITIVE_SUFFIXES = (
    ".db",
    ".db-journal",
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".sqlite",
    ".sqlite3",
    ".sqlite3-journal",
)

_PRIVATE_PATH_PARTS = frozenset(
    {
        ".claude",
        ".idea",
        ".mimosa",
        ".qoder",
        ".vs",
        ".vscode",
        ".zcode",
    }
)


def _git(*args: str, input_data: bytes | None = None) -> bytes:
    return subprocess.run(
        ("git", *args),
        input=input_data,
        check=True,
        capture_output=True,
    ).stdout


def _reachable_objects() -> dict[str, set[str]]:
    """Return reachable object ids and every path observed for each object."""

    paths_by_oid: dict[str, set[str]] = defaultdict(set)
    for raw_line in _git("rev-list", "--objects", "--all").splitlines():
        oid, separator, raw_path = raw_line.partition(b" ")
        oid_text = oid.decode("ascii")
        if separator:
            paths_by_oid[oid_text].add(
                raw_path.decode("utf-8", errors="surrogateescape").replace("\\", "/")
            )
        else:
            paths_by_oid.setdefault(oid_text, set())
    return paths_by_oid


def _object_meta(oids: list[str]) -> dict[str, tuple[str, int]]:
    if not oids:
        return {}
    payload = ("\n".join(oids) + "\n").encode()
    output = _git(
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        input_data=payload,
    )
    result: dict[str, tuple[str, int]] = {}
    for line in output.decode("ascii").splitlines():
        oid, object_type, size = line.split()
        result[oid] = (object_type, int(size))
    return result


def _is_environment_path(path: str) -> bool:
    normalized = path.lower()
    name = PurePosixPath(normalized).name
    return normalized == ".env" or (
        name.startswith(".env.") and name != ".env.example"
    )


def _sensitive_path_reason(path: str) -> str | None:
    normalized = path.lower()
    path_parts = tuple(part.lower() for part in PurePosixPath(normalized).parts)
    name = PurePosixPath(normalized).name

    if normalized.startswith("%systemdrive%/"):
        return "Windows system cache tree existed in reachable history"
    if any(part in _PRIVATE_PATH_PARTS for part in path_parts):
        return "developer-tool private state existed in reachable history"
    if _is_environment_path(path):
        return "environment file existed in reachable history"
    if name.endswith(("-wal", "-shm")) or name.endswith(_SENSITIVE_SUFFIXES):
        return "database/key-like file existed in reachable history"
    if normalized.startswith("deploy/napcat/data/"):
        return "NapCat runtime/login data existed in reachable history"
    if any(
        marker in f"/{normalized}/"
        for marker in (
            "/browser-profile/",
            "/browser_profile/",
            "/chrome-profile/",
            "/chrome_profile/",
            "/user-data-dir/",
            "/user_data_dir/",
        )
    ):
        return "browser profile existed in reachable history"
    if normalized.startswith("data/") and not normalized.startswith(
        "data/nonebot_plugin_orm/migrations/"
    ):
        return "runtime data existed in reachable history"
    return None


def _looks_like_placeholder(value: bytes) -> bool:
    normalized = value.strip().strip(b"\"'").lower()
    if not normalized:
        return True
    if normalized.startswith((b"${", b"$")):
        return True
    return any(marker in normalized for marker in _PLACEHOLDER_MARKERS)


def _environment_secret_keys(data: bytes) -> list[str]:
    """Return only sensitive variable names; never return their values."""

    keys: list[str] = []
    for match in _ENV_LINE_PATTERN.finditer(data):
        key = match.group(1)
        value = match.group(2)
        if not _ENV_SECRET_KEY_PATTERN.search(key):
            continue
        if _looks_like_placeholder(value):
            continue
        keys.append(key.decode("ascii", errors="replace"))
    return sorted(set(keys))


def _secret_findings(data: bytes) -> list[str]:
    findings = [label for label, pattern in _SECRET_PATTERNS if pattern.search(data)]
    for match in _ASSIGNMENT_PATTERN.finditer(data):
        value = match.group(1).lower()
        if not any(marker in value for marker in _PLACEHOLDER_MARKERS):
            findings.append("high-entropy secret assignment")
            break
    return findings


def audit(max_blob_size: int) -> list[str]:
    paths_by_oid = _reachable_objects()
    metadata = _object_meta(list(paths_by_oid))
    findings: list[str] = []

    for oid, paths in sorted(paths_by_oid.items()):
        object_type, size = metadata.get(oid, ("", 0))
        if object_type != "blob":
            continue

        for path in sorted(paths):
            reason = _sensitive_path_reason(path)
            if reason is not None:
                findings.append(f"{oid[:12]} {path}: {reason}")

        if size > max_blob_size:
            continue
        data = _git("cat-file", "blob", oid)

        if any(_is_environment_path(path) for path in paths):
            for key in _environment_secret_keys(data):
                findings.append(
                    f"{oid[:12]} historical environment file: "
                    f"non-placeholder value for sensitive key {key}"
                )

        secret_labels = _secret_findings(data)
        if not secret_labels:
            continue

        shown_paths = ", ".join(sorted(paths)[:3]) or "<path unavailable>"
        if len(paths) > 3:
            shown_paths += f", +{len(paths) - 3} more"
        for label in secret_labels:
            findings.append(f"{oid[:12]} {shown_paths}: possible {label}")

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-blob-size-mib",
        type=float,
        default=DEFAULT_MAX_BLOB_SIZE / (1024 * 1024),
        help="largest blob whose contents are scanned (default: 2 MiB)",
    )
    args = parser.parse_args()
    if args.max_blob_size_mib <= 0:
        parser.error("--max-blob-size-mib must be positive")

    _git("rev-parse", "--is-inside-work-tree")
    findings = audit(int(args.max_blob_size_mib * 1024 * 1024))
    if findings:
        print("Open-source history audit found blocking items:")
        for finding in findings:
            print(f"  - {finding}")
        print(
            "\nDo not make the repository public yet. For real credentials: revoke/rotate "
            "first, then remove the historical objects (for example with git-filter-repo), "
            "force-update affected refs, and re-run this audit. Runtime/private files may "
            "also contain personal data and should be removed from public history."
        )
        return 1

    print("Open-source history audit passed: no blocking reachable blobs detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
