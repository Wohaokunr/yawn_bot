from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy" / "production"
WRITER = DEPLOY_DIR / "write-deployment-record.py"
DEPLOY_SCRIPT = DEPLOY_DIR / "deploy-release.sh"
SYNC_SCRIPT = DEPLOY_DIR / "sync-control-plane.sh"
DOCKERFILE = REPO_ROOT / "Dockerfile"
PRODUCTION_COMPOSE = DEPLOY_DIR / "compose.yaml"
FORCED_COMMAND = DEPLOY_DIR / "deploy-ssh-command"


class ProductionDeploymentControlPlaneTests(unittest.TestCase):
    def test_deployment_record_writer_writes_versioned_and_current_records(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = os.environ.copy()
            env.update(
                {
                    "DEPLOYMENT_ROOT": str(root),
                    "PREVIOUS_IMAGE": "old@example",
                    "CURRENT_IMAGE": "new@example",
                    "PREVIOUS_BROWSER_IMAGE": "old-browser@example",
                    "CURRENT_BROWSER_IMAGE": "new-browser@example",
                    "COMMIT_SHA": "a" * 40,
                    "RELEASE_VERSION": "v1.2.3-rc.4",
                    "DB_BACKUP": "/opt/yawnbot/data/backups/pre.sqlite3",
                    "MIGRATION_BEFORE": "old-head\n",
                    "MIGRATION_AFTER": "new-head\n",
                    "MIGRATION_HEADS": "new-head (head)\n",
                    "DEPLOYED_AT": "20260829T080000Z",
                }
            )
            subprocess.run([sys.executable, str(WRITER)], env=env, check=True)

            record = root / "deploy-v1.2.3-rc.4-20260829T080000Z.json"
            current = root / "current.json"
            self.assertTrue(record.is_file())
            self.assertEqual(
                record.read_text(encoding="utf-8"),
                current.read_text(encoding="utf-8"),
            )
            payload = json.loads(current.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "healthy")
            self.assertEqual(payload["release_version"], "v1.2.3-rc.4")
            self.assertEqual(payload["current_browser_image"], "new-browser@example")
            self.assertEqual(payload["previous_browser_image"], "old-browser@example")
            self.assertEqual(payload["migration_before"], ["old-head"])
            self.assertEqual(payload["migration_after"], ["new-head"])
            self.assertEqual(payload["migration_heads"], ["new-head (head)"])

    def test_deploy_release_reaches_record_stage_and_writes_metadata(self) -> None:
        result, root = self._run_fake_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[deploy:pull] success", result.stdout)
        self.assertIn("[deploy:migrate] success", result.stdout)
        self.assertIn("[deploy:start] success", result.stdout)
        self.assertIn("[deploy:health] success", result.stdout)
        self.assertIn("[deploy:record] success", result.stdout)

        current = root / "deployments" / "current.json"
        payload = json.loads(current.read_text(encoding="utf-8"))
        self.assertEqual(payload["release_version"], "v1.2.3")
        self.assertEqual(payload["current_image"], self._image_ref())
        self.assertEqual(payload["current_browser_image"], "")
        self.assertEqual(payload["commit_sha"], "b" * 40)
        self.assertEqual(payload["migration_after"], ["migration-current"])
        self.assertEqual(payload["migration_heads"], ["migration-head (head)"])

    def test_deploy_release_starts_versioned_browser_sidecar(self) -> None:
        result, root = self._run_fake_deploy(browser=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[deploy:browser] start", result.stdout)
        self.assertIn("[deploy:browser] healthy", result.stdout)
        self.assertIn("browser runtime", result.stdout)

        image_env = (root / "image.env").read_text(encoding="utf-8")
        self.assertIn(f"YAWNBOT_BROWSER_IMAGE={self._browser_image_ref()}", image_env)
        self.assertIn(
            "FANQIE_BROWSER_WS_ENDPOINT=ws://playwright:3000/",
            image_env,
        )
        payload = json.loads(
            (root / "deployments" / "current.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["current_browser_image"], self._browser_image_ref())

    def test_metadata_failure_has_distinct_exit_code_after_health_success(
        self,
    ) -> None:
        result, _ = self._run_fake_deploy(writer_source="raise SystemExit(9)\n")
        self.assertEqual(result.returncode, 6)
        self.assertIn("[deploy:health] success", result.stdout)
        self.assertIn("[deploy:record] failed", result.stderr)
        self.assertIn("application is healthy", result.stderr)

    def test_shell_entrypoints_parse(self) -> None:
        for path in (DEPLOY_SCRIPT, SYNC_SCRIPT, FORCED_COMMAND):
            subprocess.run(["sh", "-n", str(path)], check=True)

    def test_runtime_dockerfile_does_not_rechown_large_mutable_tree(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        self.assertNotIn("chown -R yawnbot:yawnbot /app /opt/yawnbot", dockerfile)
        self.assertIn(
            "COPY --chown=10001:10001 --from=python-builder /app/.venv /app/.venv",
            dockerfile,
        )
        self.assertIn("COPY --chown=10001:10001 src ./src", dockerfile)
        self.assertIn(
            "COPY --chown=10001:10001 data/nonebot_plugin_orm/migrations "
            "/opt/yawnbot/migrations",
            dockerfile,
        )
        self.assertIn(
            "COPY --chown=10001:10001 --from=webui-builder /build/webui/dist "
            "./webui/dist",
            dockerfile,
        )

        source_copy = dockerfile.index("COPY --chown=10001:10001 src ./src")
        self.assertNotIn("\nRUN ", dockerfile[source_copy:])

    def test_browser_sidecar_is_profiled_and_not_publicly_exposed(self) -> None:
        compose = PRODUCTION_COMPOSE.read_text(encoding="utf-8")
        self.assertIn("playwright:", compose)
        self.assertIn("fanqie-browser", compose)
        self.assertIn('shm_size: "512m"', compose)
        self.assertIn('FANQIE_BROWSER_WS_ENDPOINT: "${FANQIE_BROWSER_WS_ENDPOINT:-}"', compose)
        playwright_section = compose.split("  playwright:\n", 1)[1]
        self.assertNotIn("ports:", playwright_section)

    def test_image_pull_policy_is_bounded_and_emits_safe_diagnostics(self) -> None:
        deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("YAWNBOT_PULL_TIMEOUT_SECONDS:-1200", deploy)
        self.assertNotIn("YAWNBOT_PULL_TIMEOUT_SECONDS:-2400", deploy)
        self.assertIn("YAWNBOT_PULL_DIAGNOSTIC_INTERVAL_SECONDS:-120", deploy)
        self.assertIn("[deploy:pull:diag] reason=", deploy)
        self.assertIn("registry_dns_addresses=", deploy)
        self.assertIn("established_https_connections=", deploy)
        self.assertIn("docker system df", deploy)
        self.assertIn("duration ${pull_elapsed}s", deploy)
        self.assertNotIn("[deploy:pull:diag] registry_username=", deploy)
        self.assertNotIn("[deploy:pull:diag] registry_password=", deploy)
        self.assertIn('pull_image "$image" "application"', deploy)
        self.assertIn('pull_image "$browser_image" "browser"', deploy)

    def test_backup_and_stop_tolerate_a_crash_looping_container(self) -> None:
        deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("backup_result=$(docker exec", deploy)
        self.assertIn("backup_result=$(docker run --rm -i --entrypoint python", deploy)
        self.assertIn(
            'docker stop "$container" >/dev/null 2>&1 || true',
            deploy,
        )

    def test_sync_control_plane_accepts_legacy_then_current_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "yawnbot"
            stage = Path(tmp) / "stage"
            fake_bin = Path(tmp) / "fake-bin"
            (root / "bin").mkdir(parents=True)
            stage.mkdir()
            fake_bin.mkdir()
            (root / ".env").write_text("ENVIRONMENT=prod\n", encoding="utf-8")
            (root / "onebot.env").write_text(
                "ONEBOT_V11_ACCESS_TOKEN=test\n", encoding="utf-8"
            )

            docker = fake_bin / "docker"
            docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            docker.chmod(0o755)

            (stage / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
            shutil.copyfile(DEPLOY_SCRIPT, stage / "deploy-release")
            shutil.copyfile(FORCED_COMMAND, stage / "deploy-ssh-command")
            shutil.copyfile(SYNC_SCRIPT, stage / "sync-control-plane")
            shutil.copyfile(WRITER, stage / "write-deployment-record.py")

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            env["YAWNBOT_ROOT"] = str(root)

            legacy = Path(tmp) / "legacy.tar.gz"
            self._make_bundle(
                legacy,
                stage,
                (
                    "compose.yaml",
                    "deploy-release",
                    "deploy-ssh-command",
                    "sync-control-plane",
                ),
            )
            first = self._run_sync(SYNC_SCRIPT, legacy, env)
            self.assertEqual(first.returncode, 0, first.stderr.decode())
            self.assertIn("bootstrap synchronized", first.stdout.decode())
            self.assertFalse((root / "bin" / "write-deployment-record.py").exists())

            current = Path(tmp) / "current.tar.gz"
            self._make_bundle(
                current,
                stage,
                (
                    "compose.yaml",
                    "deploy-release",
                    "deploy-ssh-command",
                    "sync-control-plane",
                    "write-deployment-record.py",
                ),
            )
            installed_sync = root / "bin" / "sync-control-plane"
            second = self._run_sync(installed_sync, current, env)
            self.assertEqual(second.returncode, 0, second.stderr.decode())
            self.assertIn("control plane synchronized", second.stdout.decode())
            self.assertTrue((root / "bin" / "write-deployment-record.py").is_file())

    def test_control_plane_distribution_includes_record_writer(self) -> None:
        release = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        deploy_existing = (
            REPO_ROOT / ".github" / "workflows" / "deploy-existing.yml"
        ).read_text(encoding="utf-8")
        bootstrap = (
            REPO_ROOT / "deploy" / "host" / "bootstrap-production-opencloudos9.sh"
        ).read_text(encoding="utf-8")
        sync = SYNC_SCRIPT.read_text(encoding="utf-8")

        writer_copy = (
            'cp deploy/production/write-deployment-record.py '
            '"$stage/write-deployment-record.py"'
        )
        for workflow in (release, deploy_existing):
            self.assertIn(writer_copy, workflow)
            self.assertIn("bootstrap_bundle=", workflow)
            self.assertIn("CONTROL_PLANE_BOOTSTRAP_BUNDLE", workflow)
            self.assertLess(
                workflow.index("CONTROL_PLANE_BOOTSTRAP_BUNDLE"),
                workflow.index("CONTROL_PLANE_BUNDLE"),
            )
        self.assertIn('"$production_dir/write-deployment-record.py"', bootstrap)
        self.assertIn('"$YAWNBOT_ROOT/bin/write-deployment-record.py"', bootstrap)
        self.assertIn("write-deployment-record.py", sync)
        self.assertIn("legacy_files=", sync)
        self.assertIn("current_files=", sync)

    def _run_fake_deploy(
        self,
        writer_source: str | None = None,
        *,
        browser: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "bin").mkdir()
        (root / "data" / "backups").mkdir(parents=True)
        (root / "deployments").mkdir()
        (root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        (root / "image.env").write_text("", encoding="utf-8")

        writer_target = root / "bin" / "write-deployment-record.py"
        if writer_source is None:
            shutil.copyfile(WRITER, writer_target)
        else:
            writer_target.write_text(writer_source, encoding="utf-8")

        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        docker = fake_bin / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            'case "${1:-}" in\n'
            "  ps) exit 0 ;;\n"
            "  image) exit 0 ;;\n"
            "  inspect)\n"
            '    case "$*" in\n'
            "      *fake-playwright*) echo healthy ;;\n"
            "    esac\n"
            "    exit 0\n"
            "    ;;\n"
            "  compose)\n"
            '    case "$*" in\n'
            "      *'ps -q playwright'*) echo fake-playwright ;;\n"
            "      *'nb orm current'*) echo migration-current ;;\n"
            "      *'nb orm heads'*) echo 'migration-head (head)' ;;\n"
            "    esac\n"
            "    exit 0\n"
            "    ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        docker.chmod(0o755)
        curl = fake_bin / "curl"
        curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        curl.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        env["YAWNBOT_ROOT"] = str(root)
        args = [
            "sh",
            str(DEPLOY_SCRIPT),
            self._image_ref(),
            "v1.2.3",
            "b" * 40,
        ]
        if browser:
            args.append(self._browser_image_ref())
        result = subprocess.run(
            args,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        return result, root

    @staticmethod
    def _make_bundle(
        output: Path, stage: Path, names: tuple[str, ...]
    ) -> None:
        with tarfile.open(output, "w:gz") as archive:
            for name in names:
                archive.add(stage / name, arcname=name)

    @staticmethod
    def _run_sync(
        script: Path, bundle: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        with bundle.open("rb") as stdin:
            return subprocess.run(
                ["sh", str(script), digest],
                env=env,
                stdin=stdin,
                text=False,
                capture_output=True,
                check=False,
            )

    @staticmethod
    def _image_ref() -> str:
        return (
            "sgccr.ccs.tencentyun.com/yawn_bot/yawn_bot@sha256:" + "a" * 64
        )

    @staticmethod
    def _browser_image_ref() -> str:
        return (
            "sgccr.ccs.tencentyun.com/yawn_bot/yawn_bot@sha256:" + "c" * 64
        )


if __name__ == "__main__":
    unittest.main()
