from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RECONNECT_INTERVAL_MS = 5000


def test_playwright_browser_runtime_is_version_scoped_sidecar() -> None:
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    app_dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    browser_dockerfile = (
        REPO_ROOT / "deploy" / "docker" / "playwright-server.Dockerfile"
    ).read_text(encoding="utf-8")
    release = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    pinned_version = (
        REPO_ROOT / "deploy" / "docker" / "playwright-version.txt"
    ).read_text(encoding="utf-8").strip()

    match = re.search(
        r'name = "playwright"\s+version = "([^"]+)"',
        lock,
        flags=re.MULTILINE,
    )
    assert match is not None
    assert pinned_version == match.group(1)

    assert "playwright install --with-deps chromium" not in app_dockerfile
    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" not in app_dockerfile
    assert "AS browser-runtime" not in app_dockerfile
    assert "FROM python:3.12-slim-trixie AS runtime" in app_dockerfile

    assert "COPY deploy/docker/playwright-version.txt" in browser_dockerfile
    assert (
        'npm install --global "playwright@${playwright_version}"'
        in browser_dockerfile
    )
    assert (
        "playwright install --with-deps --only-shell chromium"
        in browser_dockerfile
    )
    assert '["playwright", "run-server", "--port", "3000"' in browser_dockerfile

    assert "runtime_hash=" in release
    assert 'tag="browser-pw-${playwright_version}-${runtime_hash}"' in release
    assert "Reusing stable Playwright Chromium runtime" in release
    assert "production_browser_digest=" in release


def test_release_exports_persistent_registry_build_cache() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "cache-from: type=registry" in workflow
    assert "ref=${{ steps.release_meta.outputs.image }}:buildcache" in workflow
    assert "cache-to: type=registry" in workflow
    assert "mode=max" in workflow
    assert "browser-buildcache" in workflow


def test_production_pull_reuses_local_immutable_image() -> None:
    script = (
        REPO_ROOT / "deploy" / "production" / "deploy-release.sh"
    ).read_text(encoding="utf-8")

    local_check = 'docker image inspect "$pull_ref"'
    remote_pull = 'docker pull "$pull_ref"'
    assert local_check in script
    assert remote_pull in script
    assert script.index(local_check) < script.index(remote_pull)
    assert 'pull_image "$image" "application"' in script
    assert 'pull_image "$browser_image" "browser"' in script


def test_napcat_is_pinned_and_outside_yawnbot_release_lifecycle() -> None:
    compose = (REPO_ROOT / "deploy" / "napcat" / "compose.yaml").read_text(
        encoding="utf-8"
    )
    release = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "mlikiowa/napcat-docker:latest" not in compose
    assert "mlikiowa/napcat-docker:v4.18.19" in compose
    assert "pull_policy: missing" in compose
    assert "MODE: yawnbot" in compose
    assert "yawnbot-onebot.json:/app/templates/yawnbot.json:ro" in compose
    assert "napcat-docker" not in release


def test_napcat_template_targets_yawnbot_reverse_websocket() -> None:
    template_path = (
        REPO_ROOT / "deploy" / "napcat" / "yawnbot-onebot.template.json"
    )
    config = json.loads(template_path.read_text(encoding="utf-8"))
    clients = config["network"]["websocketClients"]

    assert len(clients) == 1
    client = clients[0]
    assert client["enable"] is True
    assert client["name"] == "yawnbot-rws"
    assert client["url"] == "ws://yawnbot:8080/onebot/v11/ws"
    assert client["token"] == ""
    assert client["reconnectInterval"] == EXPECTED_RECONNECT_INTERVAL_MS


def test_napcat_renderer_injects_shared_token(tmp_path: Path) -> None:
    env_file = tmp_path / "onebot.env"
    output = tmp_path / "yawnbot-onebot.json"
    env_file.write_text("ONEBOT_V11_ACCESS_TOKEN=test-token-123\n", encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "deploy" / "napcat" / "render-yawnbot-config.py"),
            "--env-file",
            str(env_file),
            "--output",
            str(output),
        ],
        check=True,
    )

    config = json.loads(output.read_text(encoding="utf-8"))
    client = config["network"]["websocketClients"][0]
    assert client["url"] == "ws://yawnbot:8080/onebot/v11/ws"
    assert client["token"] == "test-token-123"


def test_production_and_napcat_share_dedicated_onebot_secret() -> None:
    compose = (
        REPO_ROOT / "deploy" / "production" / "compose.yaml"
    ).read_text(encoding="utf-8")
    bootstrap = (
        REPO_ROOT / "deploy" / "host" / "bootstrap-production-opencloudos9.sh"
    ).read_text(encoding="utf-8")
    napcat_bootstrap = (
        REPO_ROOT / "deploy" / "host" / "bootstrap-napcat-opencloudos9.sh"
    ).read_text(encoding="utf-8")
    renderer = (
        REPO_ROOT / "deploy" / "napcat" / "render-yawnbot-config.py"
    ).read_text(encoding="utf-8")

    assert "- onebot.env" in compose
    assert "ONEBOT_V11_ACCESS_TOKEN=%s\\n" in bootstrap
    assert "bootstrap-napcat-opencloudos9.sh" in bootstrap
    assert 'onebot_env="$YAWNBOT_ROOT/onebot.env"' in napcat_bootstrap
    assert 'client["token"] = token' in renderer


def test_release_syncs_restricted_control_plane_before_deploy() -> None:
    release = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    deploy_existing = (
        REPO_ROOT / ".github" / "workflows" / "deploy-existing.yml"
    ).read_text(encoding="utf-8")
    forced_command = (
        REPO_ROOT / "deploy" / "production" / "deploy-ssh-command"
    ).read_text(encoding="utf-8")
    bootstrap = (
        REPO_ROOT / "deploy" / "host" / "bootstrap-production-opencloudos9.sh"
    ).read_text(encoding="utf-8")

    for workflow in (release, deploy_existing):
        assert "Package production control plane" in workflow
        assert "Synchronize production control plane" in workflow
        assert "/opt/yawnbot/bin/sync-control-plane" in workflow
        assert workflow.index("Synchronize production control plane") < workflow.index(
            "Deploy immutable release image"
        )

    assert "/opt/yawnbot/bin/sync-control-plane" in forced_command
    assert "^[0-9a-f]{64}$" in forced_command
    assert "sync-control-plane.sh" in bootstrap


def test_production_ssh_entrypoints_are_crlf_safe() -> None:
    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    forced_command = (
        REPO_ROOT / "deploy" / "production" / "deploy-ssh-command"
    ).read_text(encoding="utf-8")
    bootstrap = (
        REPO_ROOT / "deploy" / "host" / "bootstrap-production-opencloudos9.sh"
    ).read_text(encoding="utf-8")

    assert "*.sh text eol=lf" in attributes
    assert "deploy/production/deploy-ssh-command text eol=lf" in attributes
    assert 'exec /bin/sh "$@"' in forced_command
    assert "sed -i 's/\\r$//'" in bootstrap
    assert "/bin/sh /opt/yawnbot/bin/deploy-ssh-command" in bootstrap
    assert "legacy_forced_line=" in bootstrap


def test_release_mirrors_production_image_to_tencent_tcr() -> None:
    release = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    deploy_existing = (
        REPO_ROOT / ".github" / "workflows" / "deploy-existing.yml"
    ).read_text(encoding="utf-8")
    deploy_script = (
        REPO_ROOT / "deploy" / "production" / "deploy-release.sh"
    ).read_text(encoding="utf-8")
    forced_command = (
        REPO_ROOT / "deploy" / "production" / "deploy-ssh-command"
    ).read_text(encoding="utf-8")

    tcr_image = "sgccr.ccs.tencentyun.com/yawn_bot/yawn_bot"
    assert f"TCR_IMAGE: {tcr_image}" in release
    assert f"TCR_IMAGE: {tcr_image}" in deploy_existing
    assert 'TCR_USERNAME: "100025310087"' in release
    assert "secrets.TCR_PASSWORD" in release
    assert "Log in to Tencent Container Registry" in release
    assert "production_image=" in release
    assert "production_digest=" in release
    assert "production_browser_image=" in release
    assert "production_browser_digest=" in release
    assert "Verify Tencent production mirror digest" in release
    assert "registry-token-stdin" in release
    assert "registry-token-stdin" in deploy_existing
    assert f"{tcr_image}@sha256:*" in deploy_script
    assert "ghcr.io/wohaokunr/yawn_bot@sha256:*" in deploy_script
    assert 'docker login "$registry_host"' in deploy_script
    assert "github-token-stdin|registry-token-stdin" in forced_command
    assert "browser-pw-" in release
    assert "production_browser_image" in deploy_existing


def test_generated_napcat_secret_config_is_gitignored() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/deploy/napcat/yawnbot-onebot.json" in gitignore
