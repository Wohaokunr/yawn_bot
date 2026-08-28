from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "Dockerfile"
BOOTSTRAP = REPO_ROOT / "deploy" / "host" / "bootstrap-production-opencloudos9.sh"


def _required_match(pattern: str, text: str, source: Path) -> re.Match[str]:
    match = re.search(pattern, text, flags=re.MULTILINE)
    assert match is not None, f"expected pattern not found in {source}: {pattern}"
    return match


def test_production_data_owner_matches_container_runtime_uid() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")

    image_uid = _required_match(
        r"useradd\b[^\n]*--uid\s+(\d+)\b[^\n]*\byawnbot\b",
        dockerfile,
        DOCKERFILE,
    ).group(1)
    bootstrap_uid = _required_match(
        r'^YAWNBOT_RUNTIME_UID="\$\{YAWNBOT_RUNTIME_UID:-(\d+)\}"$',
        bootstrap,
        BOOTSTRAP,
    ).group(1)

    assert bootstrap_uid == image_uid


def test_production_bootstrap_repairs_bind_mount_permissions() -> None:
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")

    assert (
        'install -d -m 2770 -o "$YAWNBOT_RUNTIME_UID" -g "$DEPLOY_USER"'
        in bootstrap
    )
    assert (
        'chown -R --no-dereference "$YAWNBOT_RUNTIME_UID:$DEPLOY_USER" '
        '"$YAWNBOT_ROOT/data"'
        in bootstrap
    )
    assert 'chmod -R u+rwX,g+rwX,o-rwx "$YAWNBOT_ROOT/data"' in bootstrap
    assert (
        'find "$YAWNBOT_ROOT/data" -xdev -type d -exec chmod g+s {} +' in bootstrap
    )
