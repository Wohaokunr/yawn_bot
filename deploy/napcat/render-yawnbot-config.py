from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MIN_QUOTED_VALUE_LENGTH = 2
EXPECTED_WS_CLIENTS = 1
CONFIG_ERROR_EXIT_CODE = 2


def read_env_value(path: Path, key: str) -> str:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        current_key, value = line.split("=", 1)
        if current_key.strip() != key:
            continue
        value = value.strip()
        if (
            len(value) >= MIN_QUOTED_VALUE_LENGTH
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        return value
    return ""


def render(template_path: Path, output_path: Path, env_path: Path) -> None:
    token = read_env_value(env_path, "ONEBOT_V11_ACCESS_TOKEN")
    if not token:
        sys.stderr.write(f"ONEBOT_V11_ACCESS_TOKEN is empty in {env_path}\n")
        raise SystemExit(CONFIG_ERROR_EXIT_CODE)

    config = json.loads(template_path.read_text(encoding="utf-8"))
    clients = config["network"]["websocketClients"]
    if len(clients) != EXPECTED_WS_CLIENTS:
        sys.stderr.write("expected exactly one YawnBot websocket client template\n")
        raise SystemExit(CONFIG_ERROR_EXIT_CODE)

    client = clients[0]
    client["enable"] = True
    client["name"] = "yawnbot-rws"
    client["url"] = "ws://yawnbot:8080/onebot/v11/ws"
    client["token"] = token
    output_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument(
        "--template",
        type=Path,
        default=Path(__file__).with_name("yawnbot-onebot.template.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("yawnbot-onebot.json"),
    )
    args = parser.parse_args()
    render(args.template, args.output, args.env_file)


if __name__ == "__main__":
    main()
