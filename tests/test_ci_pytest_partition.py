from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PROFILED_TEST_JOBS = 4


def test_default_pytest_scope_excludes_expensive_tool_suites() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        config = tomllib.load(file)

    pytest_config = config["tool"]["pytest"]["ini_options"]
    assert pytest_config["testpaths"] == ["src/plugins", "tests"]
    assert "tools/rpg_module_editor" not in pytest_config["testpaths"]
    assert "tools/rpg_playtest" not in pytest_config["testpaths"]

    markers = pytest_config["markers"]
    assert any(marker.startswith("slow:") for marker in markers)
    assert any(marker.startswith("tool_ui:") for marker in markers)
    assert any(marker.startswith("playtest_e2e:") for marker in markers)


def test_ci_partitions_python_tests_and_records_slow_durations() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    for job_id in (
        "python-fast:",
        "rpg-tool-core:",
        "rpg-tool-ui:",
        "rpg-playtest-e2e:",
    ):
        assert job_id in workflow

    assert "python-tests:" not in workflow
    assert "python_fast: ${{ steps.scope.outputs.python_fast }}" in workflow
    assert "rpg_tool_core: ${{ steps.scope.outputs.rpg_tool_core }}" in workflow
    assert "rpg_tool_ui: ${{ steps.scope.outputs.rpg_tool_ui }}" in workflow
    assert "rpg_playtest: ${{ steps.scope.outputs.rpg_playtest }}" in workflow

    assert (
        workflow.count("--durations=30 --durations-min=0.05")
        >= EXPECTED_PROFILED_TEST_JOBS
    )
    assert "tools/rpg_module_editor/tests/test_validate_lint.py" in workflow
    assert "tools/rpg_module_editor/tests/test_yaml_io.py" in workflow
    assert "tools/rpg_module_editor/tests/test_app_smoke.py" in workflow
    assert "tools/rpg_module_editor/tests/test_responsive_references.py" in workflow
    assert "tools/rpg_playtest/tests" in workflow


def test_ci_routes_shared_rpg_schema_changes_to_all_rpg_suites() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    shared_case = (
        "src/plugins/yawn_core/yawn_rpg/charsheet.py|"
        "src/plugins/yawn_core/yawn_rpg/dice.py|"
        "src/plugins/yawn_core/yawn_rpg/module_schema.py|"
        "src/plugins/yawn_core/yawn_rpg/modules/*)"
    )
    assert shared_case in workflow

    shared_block = workflow.split(shared_case, maxsplit=1)[1].split(";;", maxsplit=1)[0]
    for output in (
        "python_fast=true",
        "rpg_tool_core=true",
        "rpg_tool_ui=true",
        "rpg_playtest=true",
    ):
        assert output in shared_block
