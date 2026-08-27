# Maintainer host bootstrap (OpenCloudOS 9)

This directory is for **maintainer-operated production hosts**, not the public self-host quick start. It deliberately contains no server IPs, credentials, tokens, private keys, QQ session state, database files or environment files.

The public/open-source path remains the repository-root `compose.yaml`. Maintainer production deployment will consume the same CI-built release image, but host identity and secrets stay outside Git.

## P0 — preflight

Run from the Tencent Cloud console/VNC session:

```bash
bash deploy/host/preflight-opencloudos9.sh
```

The report is written to `/var/tmp/yawnbot-preflight-*.txt` and includes OS/kernel/architecture, CPU/memory/disk, active listeners, failed systemd units, SSH, firewalld, SELinux and any existing container runtime. It intentionally does not print environment files or authorized key contents.

Review the report before changing remote access. In particular, note unexpected listening ports, low disk space, failed units and pre-existing firewall rules.

## P1 — establish key-based SSH

Generate a key on the administrator workstation if needed:

```bash
ssh-keygen -t ed25519 -a 64 -f ~/.ssh/yawnbot_admin
```

Copy only the `.pub` value into the bootstrap command:

```bash
sudo DEPLOY_PUBLIC_KEY='ssh-ed25519 AAAA... yawnbot-admin' \
  bash deploy/host/bootstrap-ssh-opencloudos9.sh
```

The script creates or reuses the unprivileged `deploy` account, installs/starts OpenSSH and firewalld, writes the public key with strict permissions, restores SELinux labels, and ensures the SSH service is allowed by the host firewall.

Before P2, **open a second terminal** and verify key login works. Keep the Tencent Cloud console/VNC session open until that succeeds.

## P2 — harden SSH and the ingress baseline

Only after a new key-authenticated SSH session succeeds:

```bash
sudo CONFIRM_KEY_LOGIN=yes \
  bash deploy/host/harden-ssh-opencloudos9.sh
```

The hardening drop-in:

- enables public-key authentication;
- disables password and keyboard-interactive authentication;
- disables root SSH login;
- disables X11, agent forwarding and SSH tunnels;
- keeps **local TCP forwarding** enabled so maintainers can later tunnel the NapCat/SnowLuma management UI without exposing it publicly;
- validates the resulting OpenSSH configuration with `sshd -t` before reloading `sshd`.

The script does not blindly delete existing firewalld rules. After P2, review:

```bash
sudo firewall-cmd --list-all
sudo ss -lntup
```

For the Tencent Cloud Security Group, the P0-P2 target is:

- allow TCP/22 only from trusted administrator source IPs;
- do not publish YawnBot 8080, OneBot ports, NapCat/SnowLuma WebUI, VNC or noVNC;
- keep application services private until the production Compose/reverse-proxy stages explicitly open them.

Tencent Cloud Security Group rules are outside the guest OS and therefore cannot be changed by these host scripts without separate Tencent Cloud API credentials.

## Separation of public release and private production state

Never commit any of the following:

- production server IP/hostname if it is intended to stay private;
- VNC, SSH or QQ passwords;
- SSH private keys;
- GitHub deployment keys;
- OneBot access tokens;
- WebUI administrator tokens;
- AI/provider API keys;
- production `.env` files;
- QQ profiles, SQLite databases, backups or runtime media.

Later production automation should reference the target host through GitHub `production` Environment secrets/variables and keep the runtime `.env` under `/opt/yawnbot` on the server. The deploy workflow should only select and activate a CI-built release image/digest; it should not rebuild the application on the server.
