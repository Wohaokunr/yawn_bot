#!/bin/sh
set -eu

root=${YAWNBOT_ROOT:-/opt/yawnbot}
expected_sha256=${1:-}
tmp_dir=""

cleanup() {
    if [ -n "$tmp_dir" ] && [ -d "$tmp_dir" ]; then
        rm -rf "$tmp_dir"
    fi
}
trap cleanup EXIT HUP INT TERM

printf '%s\n' "$expected_sha256" | grep -Eq '^[0-9a-f]{64}$' || {
    echo "invalid control-plane sha256" >&2
    exit 2
}

for command_name in docker grep sha256sum tar; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "required command is missing: $command_name" >&2
        exit 3
    }
done

[ -d "$root/bin" ] || { echo "$root/bin is missing; run host bootstrap" >&2; exit 3; }
[ -r "$root/.env" ] || { echo "$root/.env is missing" >&2; exit 3; }
[ -r "$root/onebot.env" ] || { echo "$root/onebot.env is missing" >&2; exit 3; }

tmp_dir=$(mktemp -d "$root/.control-plane.XXXXXX")
bundle="$tmp_dir/control-plane.tar.gz"
cat > "$bundle"

actual_sha256=$(sha256sum "$bundle" | awk '{print $1}')
[ "$actual_sha256" = "$expected_sha256" ] || {
    echo "control-plane bundle checksum mismatch" >&2
    exit 4
}

expected_files='compose.yaml
deploy-release
deploy-ssh-command
sync-control-plane'
actual_files=$(tar -tzf "$bundle" | LC_ALL=C sort)
expected_sorted=$(printf '%s\n' "$expected_files" | LC_ALL=C sort)
[ "$actual_files" = "$expected_sorted" ] || {
    echo "control-plane bundle contains unexpected paths" >&2
    printf 'expected:\n%s\nactual:\n%s\n' "$expected_sorted" "$actual_files" >&2
    exit 4
}

tar -xzf "$bundle" -C "$tmp_dir"
for file in compose.yaml deploy-release deploy-ssh-command sync-control-plane; do
    [ -f "$tmp_dir/$file" ] && [ ! -L "$tmp_dir/$file" ] || {
        echo "invalid control-plane file: $file" >&2
        exit 4
    }
done

sh -n "$tmp_dir/deploy-release"
sh -n "$tmp_dir/deploy-ssh-command"
sh -n "$tmp_dir/sync-control-plane"

cp "$root/.env" "$tmp_dir/.env"
cp "$root/onebot.env" "$tmp_dir/onebot.env"
chmod 600 "$tmp_dir/.env" "$tmp_dir/onebot.env"
validation_image='ghcr.io/wohaokunr/yawn_bot@sha256:0000000000000000000000000000000000000000000000000000000000000000'
YAWNBOT_IMAGE="$validation_image" docker compose \
    -f "$tmp_dir/compose.yaml" config --quiet

install -m 0644 "$tmp_dir/compose.yaml" "$root/compose.yaml.new"
install -m 0750 "$tmp_dir/deploy-release" "$root/bin/deploy-release.new"
install -m 0750 "$tmp_dir/deploy-ssh-command" "$root/bin/deploy-ssh-command.new"
install -m 0750 "$tmp_dir/sync-control-plane" "$root/bin/sync-control-plane.new"

mv -f "$root/compose.yaml.new" "$root/compose.yaml"
mv -f "$root/bin/deploy-release.new" "$root/bin/deploy-release"
mv -f "$root/bin/deploy-ssh-command.new" "$root/bin/deploy-ssh-command"
mv -f "$root/bin/sync-control-plane.new" "$root/bin/sync-control-plane"
printf '%s\n' "$actual_sha256" > "$root/control-plane.sha256.tmp"
chmod 0600 "$root/control-plane.sha256.tmp"
mv -f "$root/control-plane.sha256.tmp" "$root/control-plane.sha256"

echo "production control plane synchronized: $actual_sha256"
