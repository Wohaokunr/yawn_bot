#!/usr/bin/env bash
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"
DEPLOY_PUBLIC_KEY="${DEPLOY_PUBLIC_KEY:-}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "error: run this script as root from the Tencent Cloud console/VNC session" >&2
  exit 1
fi

if [[ -z "$DEPLOY_PUBLIC_KEY" ]]; then
  cat >&2 <<'MSG'
error: DEPLOY_PUBLIC_KEY is required.
Example:
  DEPLOY_PUBLIC_KEY='ssh-ed25519 AAAA... yawnbot-admin' ./bootstrap-ssh-opencloudos9.sh
Only a PUBLIC key belongs here. Never pass a private key to this script.
MSG
  exit 1
fi

if [[ ! "$DEPLOY_PUBLIC_KEY" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com)[[:space:]]+[A-Za-z0-9+/=]+([[:space:]].*)?$ ]]; then
  echo "error: DEPLOY_PUBLIC_KEY does not look like a supported OpenSSH public key" >&2
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

dnf install -y openssh-server firewalld sudo policycoreutils
systemctl enable --now sshd
systemctl enable --now firewalld

if id "$DEPLOY_USER" >/dev/null 2>&1; then
  echo "user $DEPLOY_USER already exists; preserving account metadata"
else
  useradd --create-home --shell /bin/bash "$DEPLOY_USER"
fi

home_dir="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
if [[ -z "$home_dir" || ! -d "$home_dir" ]]; then
  echo "error: cannot resolve home directory for $DEPLOY_USER" >&2
  exit 1
fi

install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$home_dir/.ssh"
touch "$home_dir/.ssh/authorized_keys"
chown "$DEPLOY_USER:$DEPLOY_USER" "$home_dir/.ssh/authorized_keys"
chmod 0600 "$home_dir/.ssh/authorized_keys"

if ! grep -Fqx "$DEPLOY_PUBLIC_KEY" "$home_dir/.ssh/authorized_keys"; then
  printf '%s\n' "$DEPLOY_PUBLIC_KEY" >> "$home_dir/.ssh/authorized_keys"
fi

if command -v restorecon >/dev/null 2>&1; then
  restorecon -RF "$home_dir/.ssh" || true
fi

mapfile -t active_zones < <(firewall-cmd --get-active-zones | awk '/^[^[:space:]]/ {print $1}')
if ((${#active_zones[@]} == 0)); then
  active_zones=("$(firewall-cmd --get-default-zone)")
fi
for zone in "${active_zones[@]}"; do
  firewall-cmd --permanent --zone="$zone" --add-service=ssh >/dev/null
done
firewall-cmd --reload >/dev/null

cat <<MSG
SSH bootstrap complete for user: $DEPLOY_USER

Before hardening SSH, open a NEW terminal on your own computer and verify:
  ssh $DEPLOY_USER@<server-ip>

Do not close the current Tencent Cloud console/VNC session until key login succeeds.
This script deliberately does NOT disable password/root login yet; that is P2.
MSG
