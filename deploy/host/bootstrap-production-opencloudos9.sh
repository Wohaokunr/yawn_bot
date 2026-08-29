#!/usr/bin/env bash
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"
YAWNBOT_ROOT="${YAWNBOT_ROOT:-/opt/yawnbot}"
YAWNBOT_RUNTIME_UID="${YAWNBOT_RUNTIME_UID:-10001}"
GITHUB_DEPLOY_PUBLIC_KEY="${GITHUB_DEPLOY_PUBLIC_KEY:-}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "error: run this script as root from the Tencent Cloud console/VNC session" >&2
  exit 1
fi

if [[ -z "$GITHUB_DEPLOY_PUBLIC_KEY" ]]; then
  cat >&2 <<'MSG'
error: GITHUB_DEPLOY_PUBLIC_KEY is required for production CD.

Pass the public key to the bootstrap process itself, for example:
  GITHUB_DEPLOY_PUBLIC_KEY='ssh-ed25519 AAAA... github-actions-yawnbot' \
    bash deploy/host/bootstrap-production-opencloudos9.sh

Or export it before invoking bash:
  export GITHUB_DEPLOY_PUBLIC_KEY='ssh-ed25519 AAAA... github-actions-yawnbot'
  bash deploy/host/bootstrap-production-opencloudos9.sh

Assigning GITHUB_DEPLOY_PUBLIC_KEY on a previous line without `export` does not make it
available to the child bash process. The bootstrap refuses to report success without the
GitHub Actions deploy key being authorized.
MSG
  exit 1
fi

if [[ ! "$GITHUB_DEPLOY_PUBLIC_KEY" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com)[[:space:]]+[A-Za-z0-9+/=]+([[:space:]].*)?$ ]]; then
  echo "error: GITHUB_DEPLOY_PUBLIC_KEY does not look like a supported OpenSSH public key" >&2
  exit 1
fi

if [[ "$YAWNBOT_ROOT" != "/opt/yawnbot" ]]; then
  echo "error: production forced-command policy currently requires YAWNBOT_ROOT=/opt/yawnbot" >&2
  exit 1
fi
if [[ ! "$YAWNBOT_RUNTIME_UID" =~ ^[0-9]+$ ]] || [[ "$YAWNBOT_RUNTIME_UID" -eq 0 ]]; then
  echo "error: YAWNBOT_RUNTIME_UID must be a non-zero numeric UID" >&2
  exit 1
fi

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
  os_id="${ID:-}"
  os_version="${VERSION_ID:-}"
  if [[ "${os_id,,}" != *opencloudos* ]] || [[ "$os_version" != 9* ]]; then
    echo "error: this bootstrap is intentionally limited to OpenCloudOS 9" >&2
    exit 1
  fi
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/../.." && pwd)"
production_dir="$repo_root/deploy/production"

for source_file in \
  "$production_dir/compose.yaml" \
  "$production_dir/deploy-release.sh" \
  "$production_dir/deploy-ssh-command" \
  "$production_dir/sync-control-plane.sh" \
  "$production_dir/write-deployment-record.py" \
  "$repo_root/deploy/host/bootstrap-napcat-opencloudos9.sh"; do
  if [[ ! -f "$source_file" ]]; then
    echo "error: missing repository file: $source_file" >&2
    exit 1
  fi
done

if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  echo "error: deploy user '$DEPLOY_USER' does not exist; run bootstrap-ssh-opencloudos9.sh first" >&2
  exit 1
fi

for command_name in docker curl flock install python3 sed ssh-keygen timeout; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "error: required command is missing: $command_name" >&2
    exit 1
  fi
done
if ! docker compose version >/dev/null 2>&1; then
  echo "error: Docker Compose v2 is required" >&2
  exit 1
fi

if getent group docker >/dev/null 2>&1; then
  usermod -aG docker "$DEPLOY_USER"
else
  echo "error: docker group is missing" >&2
  exit 1
fi

install -d -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$YAWNBOT_ROOT"
install -d -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
  "$YAWNBOT_ROOT/bin" \
  "$YAWNBOT_ROOT/deployments"

# The production image runs as the non-root `yawnbot` user (UID 10001 by
# default). A bind mount keeps host ownership, so data owned only by the SSH
# deploy user makes the container entrypoint fail before ORM migrations start.
# Keep deploy as the group for host-side maintenance while making the runtime
# UID the owner. Re-running bootstrap also repairs an existing data tree.
install -d -m 2770 -o "$YAWNBOT_RUNTIME_UID" -g "$DEPLOY_USER" \
  "$YAWNBOT_ROOT/data" \
  "$YAWNBOT_ROOT/data/backups"
chown -R --no-dereference "$YAWNBOT_RUNTIME_UID:$DEPLOY_USER" "$YAWNBOT_ROOT/data"
chmod -R u+rwX,g+rwX,o-rwx "$YAWNBOT_ROOT/data"
find "$YAWNBOT_ROOT/data" -xdev -type d -exec chmod g+s {} +

install -m 0644 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
  "$production_dir/compose.yaml" "$YAWNBOT_ROOT/compose.yaml"
install -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
  "$production_dir/deploy-release.sh" "$YAWNBOT_ROOT/bin/deploy-release"
install -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
  "$production_dir/deploy-ssh-command" "$YAWNBOT_ROOT/bin/deploy-ssh-command"
install -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
  "$production_dir/sync-control-plane.sh" "$YAWNBOT_ROOT/bin/sync-control-plane"
install -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
  "$production_dir/write-deployment-record.py" "$YAWNBOT_ROOT/bin/write-deployment-record.py"

# A bootstrap bundle may have been produced from a Windows checkout with
# core.autocrlf=true. Strip CR from installed shell entrypoints so Linux never
# interprets a shebang such as /bin/sh\r as the interpreter path.
for installed_script in \
  "$YAWNBOT_ROOT/bin/deploy-release" \
  "$YAWNBOT_ROOT/bin/deploy-ssh-command" \
  "$YAWNBOT_ROOT/bin/sync-control-plane"; do
  sed -i 's/\r$//' "$installed_script"
  sh -n "$installed_script"
done
python3 - "$YAWNBOT_ROOT/bin/write-deployment-record.py" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY

if [[ ! -e "$YAWNBOT_ROOT/.env" ]]; then
  cat > "$YAWNBOT_ROOT/.env" <<'EOF'
ENVIRONMENT=prod
LOCALSTORE_USE_CWD=true
RPG_AI_ENABLED=false
WW_AI_ENABLED=false
YAWNBOT_AUTO_MIGRATE=false
EOF
fi
chown "$DEPLOY_USER:$DEPLOY_USER" "$YAWNBOT_ROOT/.env"
chmod 0600 "$YAWNBOT_ROOT/.env"

# Keep the OneBot credential in a dedicated env file so YawnBot and NapCat can
# share exactly one source of truth without exposing unrelated AI/WebUI secrets
# to the NapCat container. Existing .env values are migrated once; otherwise a
# high-entropy token is generated automatically.
if [[ ! -e "$YAWNBOT_ROOT/onebot.env" ]]; then
  onebot_token="$(python3 - "$YAWNBOT_ROOT/.env" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    if key.strip() != "ONEBOT_V11_ACCESS_TOKEN":
        continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    print(value)
    break
PY
)"
  if [[ -z "$onebot_token" ]]; then
    onebot_token="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
  fi
  printf 'ONEBOT_V11_ACCESS_TOKEN=%s\n' "$onebot_token" > "$YAWNBOT_ROOT/onebot.env"
  unset onebot_token
fi
chown "$DEPLOY_USER:$DEPLOY_USER" "$YAWNBOT_ROOT/onebot.env"
chmod 0600 "$YAWNBOT_ROOT/onebot.env"

if [[ ! -e "$YAWNBOT_ROOT/image.env" ]]; then
  install -m 0600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /dev/null "$YAWNBOT_ROOT/image.env"
else
  chown "$DEPLOY_USER:$DEPLOY_USER" "$YAWNBOT_ROOT/image.env"
  chmod 0600 "$YAWNBOT_ROOT/image.env"
fi

if ! docker network inspect yawnbot-internal >/dev/null 2>&1; then
  docker network create yawnbot-internal >/dev/null
fi

case "${YAWNBOT_BOOTSTRAP_NAPCAT:-true}" in
  1|true|TRUE|yes|YES|on|ON)
    YAWNBOT_ROOT="$YAWNBOT_ROOT" \
      bash "$repo_root/deploy/host/bootstrap-napcat-opencloudos9.sh"
    ;;
esac

home_dir="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
if [[ -z "$home_dir" || ! -d "$home_dir" ]]; then
  echo "error: cannot resolve home directory for $DEPLOY_USER" >&2
  exit 1
fi

read -r key_type key_blob _ <<< "$GITHUB_DEPLOY_PUBLIC_KEY"
install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$home_dir/.ssh"
touch "$home_dir/.ssh/authorized_keys"
chown "$DEPLOY_USER:$DEPLOY_USER" "$home_dir/.ssh/authorized_keys"
chmod 0600 "$home_dir/.ssh/authorized_keys"

legacy_forced_line="restrict,command=\"/opt/yawnbot/bin/deploy-ssh-command\" $GITHUB_DEPLOY_PUBLIC_KEY"
forced_line="restrict,command=\"/bin/sh /opt/yawnbot/bin/deploy-ssh-command\" $GITHUB_DEPLOY_PUBLIC_KEY"
if grep -Fqx "$forced_line" "$home_dir/.ssh/authorized_keys"; then
  :
elif grep -Fqx "$legacy_forced_line" "$home_dir/.ssh/authorized_keys"; then
  authorized_tmp="$(mktemp "$home_dir/.ssh/authorized_keys.XXXXXX")"
  {
    grep -Fvx "$legacy_forced_line" "$home_dir/.ssh/authorized_keys" || true
    printf '%s\n' "$forced_line"
  } > "$authorized_tmp"
  chown "$DEPLOY_USER:$DEPLOY_USER" "$authorized_tmp"
  chmod 0600 "$authorized_tmp"
  mv -f "$authorized_tmp" "$home_dir/.ssh/authorized_keys"
elif grep -Eq "(^|[[:space:]])${key_type}[[:space:]]+${key_blob}([[:space:]]|$)" "$home_dir/.ssh/authorized_keys"; then
  echo "error: the GitHub deploy key is already authorized with different SSH options; refusing to create an ambiguous unrestricted entry" >&2
  exit 1
else
  printf '%s\n' "$forced_line" >> "$home_dir/.ssh/authorized_keys"
fi

if command -v restorecon >/dev/null 2>&1; then
  restorecon -RF "$home_dir/.ssh" "$YAWNBOT_ROOT" || true
fi

if ! grep -Fqx "$forced_line" "$home_dir/.ssh/authorized_keys"; then
  echo "error: GitHub deploy key authorization verification failed" >&2
  exit 1
fi

deploy_key_fingerprint="$(printf '%s\n' "$GITHUB_DEPLOY_PUBLIC_KEY" | ssh-keygen -lf - | awk '{print $2}')"

if ! runuser -u "$DEPLOY_USER" -- docker info >/dev/null 2>&1; then
  echo "error: deploy user still cannot access Docker; check docker group membership and daemon socket permissions" >&2
  exit 1
fi

cat <<MSG
Production host bootstrap complete.

Installed:
  $YAWNBOT_ROOT/compose.yaml
  $YAWNBOT_ROOT/bin/deploy-release
  $YAWNBOT_ROOT/bin/deploy-ssh-command
  $YAWNBOT_ROOT/bin/sync-control-plane
  $YAWNBOT_ROOT/bin/write-deployment-record.py
  $YAWNBOT_ROOT/.env
  $YAWNBOT_ROOT/onebot.env
  $YAWNBOT_ROOT/data/backups/ (owner UID $YAWNBOT_RUNTIME_UID, group $DEPLOY_USER)
  $YAWNBOT_ROOT/deployments/

GitHub Actions deploy key authorized for $DEPLOY_USER:
  $deploy_key_fingerprint

The release workflow passes a short-lived GitHub Actions package token over encrypted SSH stdin for each deploy, so no long-lived GHCR PAT is stored on this host.

Next checks:
  sudo -u $DEPLOY_USER docker info
  sudo -u $DEPLOY_USER test -r $YAWNBOT_ROOT/.env
  sudo -u $DEPLOY_USER test -x $YAWNBOT_ROOT/bin/deploy-release
  sudo -u $DEPLOY_USER test -r $YAWNBOT_ROOT/bin/write-deployment-record.py

Edit $YAWNBOT_ROOT/.env with the real production OneBot/WebUI/AI credentials before relying on the bot for production traffic.
MSG
