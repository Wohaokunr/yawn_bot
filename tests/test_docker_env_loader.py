from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = REPO_ROOT / "deploy" / "docker-load-env.py"


# 加载器依赖 python-dotenv，所以不放进只用标准库运行的控制面测试
def test_env_loader_keeps_process_values_and_records_file_keys(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AI_MODEL=file\nHOST=file-host\n", encoding="utf-8")
    env = os.environ.copy()
    env.pop("YAWNBOT_DOTENV_KEYS", None)
    env.pop("AI_MODEL", None)
    env.update({"YAWNBOT_ENV_FILE": str(env_file), "HOST": "0.0.0.0"})
    result = subprocess.run(
        [
            sys.executable,
            str(LOADER),
            sys.executable,
            "-c",
            "import os; print(os.environ['AI_MODEL'], os.environ['HOST'],"
            " os.environ['YAWNBOT_DOTENV_KEYS'])",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == "file 0.0.0.0 AI_MODEL"
