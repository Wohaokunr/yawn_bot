#!/usr/bin/env bash
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"
CONFIRM_KEY_LOGIN="${CONFIRM_KEY_LOGIN:-}"
DROPIN="/etc/ssh/sshd_config.d/90-yawnbot-hardening.conf"

if [[ "${EUID}" -ne 0 ]]; then
  echo "error: run this script as root" >&2
  exit 1
fi

if [[ "$CONFIRM_KEY_LOGIN" != "yes" ]]; then
  cat >&2 <<'MSG'
error: refusing to disable password/root SSH before key login is confirmed.
First verify a NEW SSH session works, then run:
  CONFIRM_KEY_LOGIN=yes ./harden-ssh-opencloudos9.sh
MSG
  exit 1
fi

if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  echo "error: deploy user $DEPLOY_USER does not exist" >&2
  exit 1
fi

home_dir="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
authorized_keys="$home_dir/.ssh/authorized_keys"
if [[ ! -s "$authorized_keys" ]]; then
  echo "error: $authorized_keys is missing or empty" >&2
  exit 1
fi

install -d -m 0755 /etc/ssh/sshd_config.d
if [[ -f "$DROPIN" ]]; then
  cp -a "$DROPIN" "${DROPIN}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
fi

cat > "$DROPIN" <<'CFG'
# Managed by YawnBot maintainer host bootstrap.
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PermitEmptyPasswords no
MaxAuthTries 4
LoginGraceTime 30
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding local
GatewayPorts no
PermitTunnel no
ClientAliveInterval 120
ClientAliveCountMax 2
CFG

sshd -t
systemctl reload sshd

systemctl enable --now firewalld
firewall-cmd --permanent --add-service=ssh >/dev/null
firewall-cmd --reload >/dev/null

cat <<'MSG'
SSH hardening applied successfully.

Host firewall baseline:
  - SSH service is explicitly allowed.
  - Existing unrelated firewalld services/ports are NOT deleted automatically, because doing so blindly can break a cloud image or existing service.

Review them now with:
  firewall-cmd --list-all
  ss -lntup

Tencent Cloud Security Group is a separate control plane. For the P0-P2 baseline it should allow TCP/22 only from your trusted administration source(s); do not expose YawnBot/NapCat/VNC ports publicly.
MSG
