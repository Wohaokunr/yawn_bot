from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy" / "production"
WRITER = DEPLOY_DIR / "write-deployment-record.py"
DEPLOY_RELEASE = DEPLOY_DIR / "deploy-release.sh"


def _writer_args(root: Path) -> list[str]:
    return [
        sys.executable,
        str(WRITER),
        "--root",
        str(root),
        "--previous-image",
        "sgccr.ccs.tencentyun.com/yawn_bot/yawn_bot@sha256:" + "1" * 64,
        "--current-image",
        "sgccr.ccs.tencentyun.com/yawn_bot/yawn_bot@sha256:" + "2" * 64,
        "--commit-sha",
        "a" * 40,
        "--release-version",
        "v0.1.0-rc.8",
        "--db-backup",
        "/opt/yawnbot/data/backups/pre-deploy.sqlite3",
        "--migration-before",
        "old-revision\nold-head",
        "--migration-after",
        "new-revision",
        "--migration-heads",
        "new-revision (head)",
        "--deployed-at",
        "20260829T080000Z",
        "--status",
        "healthy",
    ]


def test_deployment_record_writer_round_trip(tmp_path: Path) -> None:
    deployments = tmp_path / "deployments"
    result = subprocess.run(
        _writer_args(deployments),
        check=True,
        capture_output=True,
        text=True,
    )

    record = deployments / "deploy-v0.1.0-rc.8-20260829T080000Z.json"
    current = deployments / "current.json"
    assert result.stdout.strip() == str(record)
    assert record.is_file()
    assert current.is_file()

    payload = json.loads(record.read_text(encoding="utf-8"))
    assert json.loads(current.read_text(encoding="utf-8")) == payload
    assert payload["schema_version"] == 1
    assert payload["status"] == "healthy"
    assert payload["release_version"] == "v0.1.0-rc.8"
    assert payload["commit_sha"] == "a" * 40
    assert payload["migration_before"] == ["old-revision", "old-head"]
    assert payload["migration_after"] == ["new-revision"]
    assert payload["migration_heads"] == ["new-revision (head)"]


def test_deployment_record_writer_rejects_unsafe_version(tmp_path: Path) -> None:
    args = _writer_args(tmp_path / "deployments")
    version_index = args.index("--release-version") + 1
    args[version_index] = "../../escape"
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode != 0
    assert "invalid release version" in result.stderr
    assert not (tmp_path / "escape").exists()


def test_production_shell_entrypoints_parse() -> None:
    for path in (
        DEPLOY_RELEASE,
        DEPLOY_DIR / "deploy-ssh-command",
        DEPLOY_DIR / "sync-control-plane.sh",
    ):
        subprocess.run(["sh", "-n", str(path)], check=True)


def test_deploy_release_smoke_writes_healthy_record(tmp_path: Path) -> None:
    root = tmp_path / "yawnbot"
    (root / "data" / "backups").mkdir(parents=True)
    (root / "bin").mkdir(parents=True)
    shutil.copy2(WRITER, root / "bin" / "write-deployment-record.py")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
set -eu
case "${1:-}" in
  image)
    exit 0
    ;;
  ps)
    exit 0
    ;;
  compose)
    case "$*" in
      *"nb orm current"*) printf '%s\\n' 'revision-after' ;;
      *"nb orm heads"*) printf '%s\\n' 'revision-after (head)' ;;
    esac
    exit 0
    ;;
  stop|login|inspect|exec|run|pull)
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    curl = fake_bin / "curl"
    curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    curl.chmod(0o755)

    image = "sgccr.ccs.tencentyun.com/yawn_bot/yawn_bot@sha256:" + "2" * 64
    env = os.environ.copy()
    env["YAWNBOT_ROOT"] = str(root)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        [
            "sh",
            str(DEPLOY_RELEASE),
            image,
            "v0.1.0-rc.8",
            "a" * 40,
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    current = json.loads((root / "deployments" / "current.json").read_text(encoding="utf-8"))
    assert current["current_image"] == image
    assert current["release_version"] == "v0.1.0-rc.8"
    assert current["migration_after"] == ["revision-after"]
    assert current["migration_heads"] == ["revision-after (head)"]
    assert current["status"] == "healthy"
    assert "[deploy:record] success" in result.stdout
