#!/usr/bin/env bash
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"
YAWNBOT_ROOT="${YAWNBOT_ROOT:-/opt/yawnbot}"
GITHUB_DEPLOY_PUBLIC_KEY="${GITHUB_DEPLOY_PUBLIC_KEY:-}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "error: run this script as root from the Tencent Cloud console/VNC session" >&2
  exit 1
fi

if [[ "$YAWNBOT_ROOT" != "/opt/yawnbot" ]]; then
  echo "error: production forced-command policy currently requires YAWNBOT_ROOT=/opt/yawnbot" >&2
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
  "$production_dir/deploy-ssh-command"; do
  if [[ ! -f "$source_file" ]]; then
    echo "error: missing repository file: $source_file" >&2
    exit 1
  fi
done

if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  echo "error: deploy user '$DEPLOY_USER' does not exist; run bootstrap-ssh-opencloudos9.sh first" >&2
  exit 1
fi

for command_name in docker curl flock install; do
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
  "$YAWNBOT_ROOT/data" \
  "$YAWNBOT_ROOT/data/backups" \
  "$YAWNBOT_ROOT/deployments"

install -m 0644 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
  "$production_dir/compose.yaml" "$YAWNBOT_ROOT/compose.yaml"
install -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
  "$production_dir/deploy-release.sh" "$YAWNBOT_ROOT/bin/deploy-release"
install -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
  "$production_dir/deploy-ssh-command" "$YAWNBOT_ROOT/bin/deploy-ssh-command"

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

if [[ ! -e "$YAWNBOT_ROOT/image.env" ]]; then
  install -m 0600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /dev/null "$YAWNBOT_ROOT/image.env"
else
  chown "$DEPLOY_USER:$DEPLOY_USER" "$YAWNBOT_ROOT/image.env"
  chmod 0600 "$YAWNBOT_ROOT/image.env"
fi

if ! docker network inspect yawnbot-internal >/dev/null 2>&1; then
  docker network create yawnbot-internal >/dev/null
fi

home_dir="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
if [[ -z "$home_dir" || ! -d "$home_dir" ]]; then
  echo "error: cannot resolve home directory for $DEPLOY_USER" >&2
  exit 1
fi

if [[ -n "$GITHUB_DEPLOY_PUBLIC_KEY" ]]; then
  if [[ ! "$GITHUB_DEPLOY_PUBLIC_KEY" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com)[[:space:]]+[A-Za-z0-9+/=]+([[:space:]].*)?$ ]]; then
    echo "error: GITHUB_DEPLOY_PUBLIC_KEY does not look like a supported OpenSSH public key" >&2
    exit 1
  fi

  read -r key_type key_blob _ <<< "$GITHUB_DEPLOY_PUBLIC_KEY"
  install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$home_dir/.ssh"
  touch "$home_dir/.ssh/authorized_keys"
  chown "$DEPLOY_USER:$DEPLOY_USER" "$home_dir/.ssh/authorized_keys"
  chmod 0600 "$home_dir/.ssh/authorized_keys"

  forced_line="restrict,command=\"/opt/yawnbot/bin/deploy-ssh-command\" $GITHUB_DEPLOY_PUBLIC_KEY"
  if grep -Fqx "$forced_line" "$home_dir/.ssh/authorized_keys"; then
    :
  elif grep -Eq "(^|[[:space:]])${key_type}[[:space:]]+${key_blob}([[:space:]]|$)" "$home_dir/.ssh/authorized_keys"; then
    echo "error: the GitHub deploy key is already authorized with different SSH options; refusing to create an ambiguous unrestricted entry" >&2
    exit 1
  else
    printf '%s\n' "$forced_line" >> "$home_dir/.ssh/authorized_keys"
  fi
fi

if command -v restorecon >/dev/null 2>&1; then
  restorecon -RF "$home_dir/.ssh" "$YAWNBOT_ROOT" || true
fi

if ! runuser -u "$DEPLOY_USER" -- docker info >/dev/null 2>&1; then
  echo "error: deploy user still cannot access Docker; check docker group membership and daemon socket permissions" >&2
  exit 1
fi

if [[ -z "$GITHUB_DEPLOY_PUBLIC_KEY" ]]; then
  echo "warning: GITHUB_DEPLOY_PUBLIC_KEY was not provided; GitHub Actions SSH deployment is not authorized yet" >&2
fi

cat <<MSG
Production host bootstrap complete.

Installed:
  $YAWNBOT_ROOT/compose.yaml
  $YAWNBOT_ROOT/bin/deploy-release
  $YAWNBOT_ROOT/bin/deploy-ssh-command
  $YAWNBOT_ROOT/.env
  $YAWNBOT_ROOT/data/backups/
  $YAWNBOT_ROOT/deployments/

The release workflow passes a short-lived GitHub Actions package token over encrypted SSH stdin for each deploy, so no long-lived GHCR PAT is stored on this host.

Next checks:
  sudo -u $DEPLOY_USER docker info
  sudo -u $DEPLOY_USER test -r $YAWNBOT_ROOT/.env
  sudo -u $DEPLOY_USER test -x $YAWNBOT_ROOT/bin/deploy-release

Edit $YAWNBOT_ROOT/.env with the real production OneBot/WebUI/AI credentials before relying on the bot for production traffic.
MSG
