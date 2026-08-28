#!/usr/bin/env bash
set -euo pipefail

NAPCAT_ROOT="${NAPCAT_ROOT:-/opt/napcat}"
YAWNBOT_ROOT="${YAWNBOT_ROOT:-/opt/yawnbot}"
NAPCAT_UID="${NAPCAT_UID:-1000}"
NAPCAT_GID="${NAPCAT_GID:-1000}"
NAPCAT_IMAGE="${NAPCAT_IMAGE:-mlikiowa/napcat-docker:v4.18.19}"
NAPCAT_WEBUI_PORT="${NAPCAT_WEBUI_PORT:-6099}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "error: run this script as root" >&2
  exit 1
fi

for value_name in NAPCAT_UID NAPCAT_GID NAPCAT_WEBUI_PORT; do
  value="${!value_name}"
  if [[ ! "$value" =~ ^[0-9]+$ ]]; then
    echo "error: $value_name must be numeric" >&2
    exit 1
  fi
done

case "$NAPCAT_IMAGE" in
  *:latest|*:latest-*)
    echo "error: NAPCAT_IMAGE must be version-pinned, not latest: $NAPCAT_IMAGE" >&2
    exit 1
    ;;
esac

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/../.." && pwd)"
napcat_source="$repo_root/deploy/napcat"

for source_file in \
  "$napcat_source/compose.yaml" \
  "$napcat_source/render-yawnbot-config.py" \
  "$napcat_source/yawnbot-onebot.template.json"; do
  [[ -f "$source_file" ]] || {
    echo "error: missing repository file: $source_file" >&2
    exit 1
  }
done

for command_name in cmp docker install python3; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "error: required command is missing: $command_name" >&2
    exit 1
  }
done

docker compose version >/dev/null 2>&1 || {
  echo "error: Docker Compose v2 is required" >&2
  exit 1
}

onebot_env="$YAWNBOT_ROOT/onebot.env"
[[ -r "$onebot_env" ]] || {
  echo "error: $onebot_env is missing; run bootstrap-production-opencloudos9.sh first" >&2
  exit 1
}

existing_container="$(docker ps -aq \
  --filter label=com.docker.compose.project=napcat \
  --filter label=com.docker.compose.service=napcat | head -n 1)"

mount_source() {
  local destination="$1"
  local result=""
  if [[ -n "$existing_container" ]]; then
    result="$(docker inspect --format \
      '{{range .Mounts}}{{if eq .Destination "'"$destination"'"}}{{.Source}}{{end}}{{end}}' \
      "$existing_container" 2>/dev/null || true)"
  fi
  printf '%s' "$result"
}

napcat_qq_dir="$(mount_source /app/.config/QQ)"
napcat_config_dir="$(mount_source /app/napcat/config)"
napcat_plugin_dir="$(mount_source /app/napcat/plugins)"

: "${napcat_qq_dir:=$NAPCAT_ROOT/data/QQ}"
: "${napcat_config_dir:=$NAPCAT_ROOT/data/config}"
: "${napcat_plugin_dir:=$NAPCAT_ROOT/data/plugins}"

install -d -m 0750 "$NAPCAT_ROOT"
install -d -m 0770 -o "$NAPCAT_UID" -g "$NAPCAT_GID" \
  "$napcat_qq_dir" "$napcat_config_dir" "$napcat_plugin_dir"
install -m 0644 "$napcat_source/compose.yaml" "$NAPCAT_ROOT/compose.yaml"

webui_token=""
if [[ -r "$NAPCAT_ROOT/.env" ]]; then
  webui_token="$(python3 - "$NAPCAT_ROOT/.env" <<'PY'
from pathlib import Path
import sys

for raw_line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if raw_line.startswith("NAPCAT_WEBUI_TOKEN="):
        print(raw_line.split("=", 1)[1].strip())
        break
PY
)"
fi
if [[ -z "$webui_token" && -n "$existing_container" ]]; then
  webui_token="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' \
    "$existing_container" 2>/dev/null \
    | sed -n 's/^WEBUI_TOKEN=//p' \
    | head -n 1)"
fi
if [[ -z "$webui_token" ]]; then
  webui_token="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
fi

cat > "$NAPCAT_ROOT/.env.tmp" <<EOF
NAPCAT_UID=$NAPCAT_UID
NAPCAT_GID=$NAPCAT_GID
NAPCAT_IMAGE=$NAPCAT_IMAGE
NAPCAT_WEBUI_PORT=$NAPCAT_WEBUI_PORT
NAPCAT_WEBUI_TOKEN=$webui_token
NAPCAT_QQ_DIR=$napcat_qq_dir
NAPCAT_CONFIG_DIR=$napcat_config_dir
NAPCAT_PLUGIN_DIR=$napcat_plugin_dir
EOF
chmod 0600 "$NAPCAT_ROOT/.env.tmp"
mv -f "$NAPCAT_ROOT/.env.tmp" "$NAPCAT_ROOT/.env"

template_tmp="$NAPCAT_ROOT/yawnbot-onebot.json.tmp"
python3 "$napcat_source/render-yawnbot-config.py" \
  --env-file "$onebot_env" \
  --template "$napcat_source/yawnbot-onebot.template.json" \
  --output "$template_tmp"
chmod 0600 "$template_tmp"

template_changed=true
if [[ -f "$NAPCAT_ROOT/yawnbot-onebot.json" ]] && cmp -s "$template_tmp" "$NAPCAT_ROOT/yawnbot-onebot.json"; then
  template_changed=false
fi
mv -f "$template_tmp" "$NAPCAT_ROOT/yawnbot-onebot.json"
chown "$NAPCAT_UID:$NAPCAT_GID" "$NAPCAT_ROOT/yawnbot-onebot.json"

if ! docker network inspect yawnbot-internal >/dev/null 2>&1; then
  docker network create yawnbot-internal >/dev/null
fi

compose=(docker compose --env-file "$NAPCAT_ROOT/.env" -f "$NAPCAT_ROOT/compose.yaml")
container_before="$existing_container"
"${compose[@]}" up -d --no-build --pull missing napcat
container_after="$(docker ps -aq \
  --filter label=com.docker.compose.project=napcat \
  --filter label=com.docker.compose.service=napcat | head -n 1)"

# The upstream NapCat entrypoint copies /app/templates/$MODE.json only at
# process start. If only the rendered token/template changed and Compose did
# not recreate the container, restart once so the new reverse-WS config loads.
if [[ "$template_changed" == true && -n "$container_before" && "$container_before" == "$container_after" ]]; then
  "${compose[@]}" restart napcat
fi

cat <<MSG
NapCat bootstrap complete.

Image (pinned): $NAPCAT_IMAGE
Config root: $NAPCAT_ROOT
Persistent QQ data: $napcat_qq_dir
Persistent NapCat config: $napcat_config_dir
Reverse WebSocket: ws://yawnbot:8080/onebot/v11/ws
OneBot token source: $onebot_env
WebUI: http://127.0.0.1:$NAPCAT_WEBUI_PORT/webui (use an SSH tunnel)

NapCat/NTQQ is intentionally outside the YawnBot release lifecycle. Routine
YawnBot releases do not pull or restart this container.
MSG
