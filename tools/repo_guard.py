# ruff: noqa: C901,PLR0911,T201
"""Fail CI when generated/runtime/private files enter the Git index.

This guard intentionally checks Git-tracked files instead of the whole working tree, so
local databases, caches and IDE state may exist without making development painful.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

DEFAULT_MAX_FILE_SIZE = 5 * 1024 * 1024
SECRET_SCAN_LIMIT = 2 * 1024 * 1024

_PRIVATE_PARTS = frozenset(
    {
        ".claude",
        ".idea",
        ".mimosa",
        ".qoder",
        ".vs",
        ".vscode",
        ".zcode",
        "__pycache__",
        "node_modules",
    }
)
_CACHE_PARTS = frozenset(
    {
        ".hypothesis",
        ".mypy_cache",
        ".nox",
        ".playwright",
        ".pytest_cache",
        ".pytype",
        ".ruff_cache",
        ".tox",
        ".vite",
        "htmlcov",
        "ms-playwright",
        "playwright-report",
        "release-out",
        "release-stage",
        "test-results",
    }
)
_BROWSER_RUNTIME_PARTS = frozenset(
    {
        "browser-data",
        "browser-profile",
        "browser_data",
        "browser_profile",
        "chrome-profile",
        "chrome_profile",
        "chromium-profile",
        "chromium_profile",
        "playwright-browsers",
        "playwright_browsers",
        "user-data-dir",
        "user_data_dir",
    }
)
_RUNTIME_MEDIA_PARTS = frozenset(
    {
        "download-cache",
        "download_cache",
        "downloads",
        "media-cache",
        "media_cache",
        "runtime-media",
        "runtime_media",
    }
)
_SYSTEM_NAMES = frozenset({".ds_store", "desktop.ini", "ehthumbs.db", "thumbs.db"})
_RUNTIME_SUFFIXES = (
    ".bak",
    ".db",
    ".db-journal",
    ".log",
    ".old",
    ".pid",
    ".sqlite",
    ".sqlite3",
    ".sqlite3-journal",
    ".swp",
    ".swo",
    ".temp",
    ".tmp",
    ".tsbuildinfo",
)
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("private key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("OpenAI-style API key", re.compile(rb"\bsk-[A-Za-z0-9_.-]{20,}\b")),
    ("tp-style API token", re.compile(rb"\btp-[A-Za-z0-9_-]{24,}\b")),
    ("GitHub fine-grained token", re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("GitHub classic token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("AWS access key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
)
_ASSIGNMENT_PATTERN = re.compile(
    rb"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|"
    rb"webui_admin_token|onebot_v11_access_token|deploy_ssh_private_key)\b"
    rb"[ \t]*[:=][ \t]*[\"']?([A-Za-z0-9_./+=-]{24,})"
)
_PLACEHOLDER_MARKERS = (
    b"change-me",
    b"changeme",
    b"dummy",
    b"example",
    b"placeholder",
    b"replace-me",
    b"replace_me",
    b"test-token",
    b"your-",
    b"your_",
)


def _run_git(*args: str) -> bytes:
    completed = subprocess.run(
        ("git", *args),
        check=True,
        capture_output=True,
    )
    return completed.stdout


def tracked_files() -> list[str]:
    """Return tracked file paths using Git's NUL-safe output."""

    raw = _run_git("ls-files", "-z")
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in raw.split(b"\0")
        if item
    ]


def path_violation(path: str) -> str | None:
    """Return a reason when a tracked path violates repository hygiene."""

    normalized = path.replace("\\", "/")
    lower = normalized.lower()
    parts = tuple(part.lower() for part in normalized.split("/"))
    name = parts[-1]

    if lower.startswith("%systemdrive%/"):
        return "Windows system cache tree"
    if lower.startswith("data/") and not lower.startswith(
        "data/nonebot_plugin_orm/migrations/"
    ):
        return "runtime data (only ORM migrations may be tracked under data/)"
    if lower == "migrations" or lower.startswith("migrations/"):
        return (
            "legacy duplicate migration tree; use "
            "data/nonebot_plugin_orm/migrations/"
        )
    if lower == "webui/dist" or lower.startswith("webui/dist/"):
        return "generated WebUI build output"
    if lower in {".env", ".env.dev", ".env.prod"} or (
        name.startswith(".env.") and name != ".env.example"
    ):
        return "local environment file"
    if any(part in _PRIVATE_PARTS for part in parts):
        return "developer-tool private state"
    if any(part in _CACHE_PARTS for part in parts):
        return "generated cache/test output"
    if any(part in _BROWSER_RUNTIME_PARTS for part in parts):
        return "browser profile/runtime state"
    if any(part in _RUNTIME_MEDIA_PARTS for part in parts):
        return "runtime media/download cache"
    if name in _SYSTEM_NAMES:
        return "operating-system metadata"
    if name.endswith(("-wal", "-shm")) or name.endswith(_RUNTIME_SUFFIXES):
        return "runtime/generated file"
    return None


def _secret_findings(data: bytes) -> list[str]:
    findings = [label for label, pattern in _SECRET_PATTERNS if pattern.search(data)]
    for match in _ASSIGNMENT_PATTERN.finditer(data):
        value = match.group(1).lower()
        if not any(marker in value for marker in _PLACEHOLDER_MARKERS):
            findings.append("high-entropy secret assignment")
            break
    return findings


def inspect_repository(root: Path, max_file_size: int) -> list[str]:
    """Inspect tracked files and return human-readable violations."""

    violations: list[str] = []
    for relative in tracked_files():
        reason = path_violation(relative)
        if reason is not None:
            violations.append(f"{relative}: {reason}")
            continue

        file_path = root / relative
        if not file_path.is_file():
            continue
        size = file_path.stat().st_size
        if size > max_file_size:
            violations.append(
                f"{relative}: tracked file is {size / (1024 * 1024):.1f} MiB "
                f"(limit {max_file_size / (1024 * 1024):.1f} MiB)"
            )
            continue
        if size > SECRET_SCAN_LIMIT:
            continue

        try:
            data = file_path.read_bytes()
        except OSError as exc:
            violations.append(f"{relative}: cannot read tracked file ({exc})")
            continue
        violations.extend(
            f"{relative}: possible {finding}" for finding in _secret_findings(data)
        )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-file-size-mib",
        type=float,
        default=DEFAULT_MAX_FILE_SIZE / (1024 * 1024),
        help="maximum size of one tracked file before CI rejects it (default: 5)",
    )
    args = parser.parse_args()
    if args.max_file_size_mib <= 0:
        parser.error("--max-file-size-mib must be positive")

    root = Path(_run_git("rev-parse", "--show-toplevel").decode().strip())
    violations = inspect_repository(
        root, max_file_size=int(args.max_file_size_mib * 1024 * 1024)
    )
    if violations:
        print("Repository hygiene violations detected:")
        for violation in violations:
            print(f"  - {violation}")
        print(
            "\nKeep runtime/generated/private state local; commit source inputs only."
        )
        return 1

    print(f"Repository hygiene OK: {len(tracked_files())} tracked files checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
