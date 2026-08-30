# ruff: noqa: T201
"""Verify that a fresh checkout contains only repository-approved content."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

_REQUIRED_OPEN_SOURCE_FILES = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    ".github/CODEOWNERS",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/deployment_help.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/workflows/license-compliance.yml",
    ".github/workflows/playwright-runtime.yml",
    "deploy/docker/compose.release.yaml",
    "deploy/docker/playwright-server.Dockerfile",
    "deploy/docker/playwright-version.txt",
    "docs/public-docker-deployment.md",
    "third_party_licenses/README.md",
    "third_party_licenses/ZCOOL-KuaiLe-OFL-1.1.txt",
    "third_party_licenses/nonebot-plugin-htmlkit-MIT.txt",
    "third_party_licenses/litehtml-BSD-3-Clause.txt",
    "third_party_licenses/nonebot-plugin-htmlkit-native-provenance.md",
)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _expected_public_browser_image(root: Path) -> str:
    version_path = root / "deploy" / "docker" / "playwright-version.txt"
    dockerfile_path = root / "deploy" / "docker" / "playwright-server.Dockerfile"
    version = version_path.read_text(encoding="utf-8").strip()
    runtime_hash = hashlib.sha256(
        version_path.read_bytes() + dockerfile_path.read_bytes()
    ).hexdigest()[:16]
    return (
        "ghcr.io/wohaokunr/yawn_bot:"
        f"browser-pw-{version}-{runtime_hash}"
    )


def _validate_public_release_compose(root: Path) -> list[str]:
    path = root / "deploy" / "docker" / "compose.release.yaml"
    if not path.is_file():
        return []

    content = path.read_text(encoding="utf-8")
    expected_browser_image = _expected_public_browser_image(root)
    required_fragments = (
        'image: "${YAWNBOT_IMAGE:?',
        "../../.env",
        "yawnbot-data:/app/data",
        "name: yawnbot-internal",
        "/healthz",
        "fanqie-browser",
        "FANQIE_BROWSER_WS_ENDPOINT",
        expected_browser_image,
    )
    violations = [
        (
            "deploy/docker/compose.release.yaml: missing required fragment "
            f"{fragment!r}"
        )
        for fragment in required_fragments
        if fragment not in content
    ]
    if "build:" in content:
        violations.append(
            "deploy/docker/compose.release.yaml: public release path must pull a "
            "published image, not build source"
        )
    return violations


def _validate_production_action_logging(root: Path) -> list[str]:
    violations: list[str] = []
    workflow_paths = (
        ".github/workflows/release.yml",
        ".github/workflows/deploy-existing.yml",
    )
    forbidden_fragments = (
        "ssh -vv",
        "GitHub deployment key fingerprint:",
        "Production SSH user:",
    )
    for relative_path in workflow_paths:
        path = root / relative_path
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        violations.extend(
            f"{relative_path}: public Actions logs must not contain/use {fragment!r}"
            for fragment in forbidden_fragments
            if fragment in content
        )
    return violations


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
        print("Required open-source/public-deployment files are missing:")
        for path in missing:
            print(f"  - {path}")
        return 1

    public_compose_violations = _validate_public_release_compose(root)
    if public_compose_violations:
        print("Public release Compose contract violations detected:")
        for violation in public_compose_violations:
            print(f"  - {violation}")
        return 1

    production_log_violations = _validate_production_action_logging(root)
    if production_log_violations:
        print("Production Actions log-safety violations detected:")
        for violation in production_log_violations:
            print(f"  - {violation}")
        return 1

    tracked_count = len(_git(root, "ls-files").splitlines())
    print(
        "Repository content OK: "
        f"{tracked_count} tracked files, required community/public-deployment files "
        "present, production Actions logging is hardened, and no runtime/generated/"
        "private checkout state is tracked."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
