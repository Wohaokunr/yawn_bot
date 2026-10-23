"""容器启动时把面板管理的配置文件加载进进程环境，再执行原命令。

compose 的 ``env_file`` 只在创建容器时读取，面板改完文件后重启容器也拿不到新值，
而且注入的键在面板看来全是“外部环境覆盖”。这里改为每次进程启动都重新读取
``YAWNBOT_ENV_FILE``：已经存在于进程环境的键（compose ``environment``、
``onebot.env``）保持优先，文件提供的键记入 ``YAWNBOT_DOTENV_KEYS`` 供面板区分来源。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values


def main() -> None:
    command = sys.argv[1:]
    if not command:
        sys.exit("usage: yawnbot-load-env COMMAND [ARGS...]")
    path = os.environ.get("YAWNBOT_ENV_FILE", "")
    loaded: list[str] = []
    if path and Path(path).is_file():
        for key, value in dotenv_values(path).items():
            if value is None or key in os.environ:
                continue
            os.environ[key] = value
            loaded.append(key)
        sys.stderr.write(f"[config] loaded {len(loaded)} keys from {path}\n")
    elif path:
        sys.stderr.write(f"[config] {path} is missing; using process env only\n")
    os.environ["YAWNBOT_DOTENV_KEYS"] = ",".join(loaded)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
