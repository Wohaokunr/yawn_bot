#!/usr/bin/env bash
set -euo pipefail

REPORT="${REPORT:-/var/tmp/yawnbot-preflight-$(date -u +%Y%m%dT%H%M%SZ).txt}"

section() {
  printf '\n===== %s =====\n' "$1"
}

run_optional() {
  local label="$1"
  shift
  section "$label"
  if command -v "$1" >/dev/null 2>&1; then
    "$@" || true
  else
    printf 'command not installed: %s\n' "$1"
  fi
}

{
  section "YawnBot host preflight"
  printf 'generated_utc=%s\n' "$(date -u +%FT%TZ)"
  printf 'hostname=%s\n' "$(hostname -f 2>/dev/null || hostname)"

  section "Operating system"
  if [[ -r /etc/os-release ]]; then
    cat /etc/os-release
    # shellcheck disable=SC1091
    source /etc/os-release
    os_id="${ID:-unknown}"
    os_version="${VERSION_ID:-unknown}"
    if [[ "${os_id,,}" != *opencloudos* ]] || [[ "$os_version" != 9* ]]; then
      printf 'WARNING: expected OpenCloudOS 9, detected ID=%s VERSION_ID=%s\n' "$os_id" "$os_version"
    fi
  else
    printf 'WARNING: /etc/os-release is missing\n'
  fi

  section "Kernel and architecture"
  uname -a
  printf 'architecture=%s\n' "$(uname -m)"

  run_optional "CPU" lscpu
  run_optional "Memory" free -h
  run_optional "Block devices" lsblk -o NAME,SIZE,FSTYPE,TYPE,MOUNTPOINTS
  run_optional "Filesystem usage" df -hT
  run_optional "Network addresses" ip -brief address
  run_optional "Listening TCP/UDP sockets" ss -lntup
  run_optional "Failed systemd units" systemctl --failed --no-pager

  section "SSH service"
  systemctl status sshd --no-pager 2>/dev/null || true
  if command -v sshd >/dev/null 2>&1; then
    sshd -T 2>/dev/null | grep -E '^(port|permitrootlogin|passwordauthentication|pubkeyauthentication|kbdinteractiveauthentication|allowtcpforwarding|gatewayports) ' || true
  fi

  section "Firewall"
  systemctl status firewalld --no-pager 2>/dev/null || true
  if command -v firewall-cmd >/dev/null 2>&1; then
    firewall-cmd --get-active-zones 2>/dev/null || true
    firewall-cmd --list-all 2>/dev/null || true
  fi

  section "SELinux"
  if command -v getenforce >/dev/null 2>&1; then
    getenforce
  else
    printf 'getenforce is not installed\n'
  fi

  section "Container runtime (informational only; P3 installs it)"
  if command -v docker >/dev/null 2>&1; then
    docker version 2>/dev/null || true
  else
    printf 'docker=not-installed\n'
  fi
  if command -v podman >/dev/null 2>&1; then
    podman version 2>/dev/null || true
  else
    printf 'podman=not-installed\n'
  fi

  section "Preflight result"
  printf 'This report intentionally excludes environment files, authorized_keys contents and application secrets.\n'
  printf 'Review listening ports, failed units, disk capacity and SSH/firewall state before continuing.\n'
} | tee "$REPORT"

printf '\nSaved preflight report to %s\n' "$REPORT"
