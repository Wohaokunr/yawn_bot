#!/bin/sh
set -eu

root=${YAWNBOT_ROOT:-/opt/yawnbot}
image=${1:-}
version=${2:-}
commit=${3:-}
auth_mode=${4:-}
keep_backups=${YAWNBOT_BACKUP_KEEP:-10}
pull_attempts=${YAWNBOT_PULL_ATTEMPTS:-2}
pull_timeout_seconds=${YAWNBOT_PULL_TIMEOUT_SECONDS:-2400}
pull_retry_delay_seconds=${YAWNBOT_PULL_RETRY_DELAY_SECONDS:-15}
pull_heartbeat_seconds=${YAWNBOT_PULL_HEARTBEAT_SECONDS:-30}
docker_config_dir=""
pull_heartbeat_pid=""

cleanup() {
    if [ -n "$pull_heartbeat_pid" ]; then
        kill "$pull_heartbeat_pid" 2>/dev/null || true
        wait "$pull_heartbeat_pid" 2>/dev/null || true
        pull_heartbeat_pid=""
    fi
    if [ -n "$docker_config_dir" ] && [ -d "$docker_config_dir" ]; then
        rm -rf "$docker_config_dir"
        docker_config_dir=""
    fi
}

handle_signal() {
    signal=$1
    trap - EXIT HUP INT TERM
    cleanup
    case "$signal" in
        HUP) exit 129 ;;
        INT) exit 130 ;;
        TERM) exit 143 ;;
    esac
}

trap cleanup EXIT
trap 'handle_signal HUP' HUP
trap 'handle_signal INT' INT
trap 'handle_signal TERM' TERM

case "$image" in
    ghcr.io/wohaokunr/yawn_bot@sha256:*) ;;
    *) echo "invalid immutable image reference: $image" >&2; exit 2 ;;
esac
case "$version" in
    v[0-9]*.[0-9]*.[0-9]*) ;;
    *) echo "invalid release version: $version" >&2; exit 2 ;;
esac
case "$commit" in
    [0-9a-f][0-9a-f]*) ;;
    *) echo "invalid commit SHA: $commit" >&2; exit 2 ;;
esac
case "$auth_mode" in
    "") ;;
    github-token-stdin)
        IFS= read -r ghcr_username || { echo "missing GHCR username on stdin" >&2; exit 2; }
        IFS= read -r ghcr_token || { echo "missing GHCR token on stdin" >&2; exit 2; }
        [ -n "$ghcr_username" ] || { echo "empty GHCR username" >&2; exit 2; }
        [ -n "$ghcr_token" ] || { echo "empty GHCR token" >&2; exit 2; }
        docker_config_dir=$(mktemp -d)
        chmod 700 "$docker_config_dir"
        export DOCKER_CONFIG="$docker_config_dir"
        printf '%s' "$ghcr_token" | docker login ghcr.io \
            --username "$ghcr_username" --password-stdin >/dev/null
        unset ghcr_token
        ;;
    *) echo "invalid registry auth mode: $auth_mode" >&2; exit 2 ;;
esac

[ -d "$root/data/backups" ] || {
    echo "$root/data/backups is missing; run the one-time server bootstrap" >&2
    exit 3
}
mkdir -p "$root/deployments"
chmod 700 "$root/deployments"
exec 9>"$root/deploy.lock"
flock -n 9 || { echo "another production deployment is running" >&2; exit 3; }

compose() {
    docker compose --env-file "$root/image.env" -f "$root/compose.yaml" "$@"
}

pull_image() {
    attempt=1
    while [ "$attempt" -le "$pull_attempts" ]; do
        echo "pulling immutable image (attempt $attempt/$pull_attempts): $image"
        pull_started_at=$(date +%s)
        (
            while :; do
                sleep "$pull_heartbeat_seconds" || exit 0
                now=$(date +%s)
                elapsed=$((now - pull_started_at))
                echo "image pull still running (attempt $attempt/$pull_attempts, elapsed ${elapsed}s)"
            done
        ) &
        pull_heartbeat_pid=$!

        pull_status=0
        timeout --foreground "$pull_timeout_seconds" docker pull "$image" || pull_status=$?

        kill "$pull_heartbeat_pid" 2>/dev/null || true
        wait "$pull_heartbeat_pid" 2>/dev/null || true
        pull_heartbeat_pid=""

        if [ "$pull_status" -eq 0 ]; then
            docker image inspect "$image" >/dev/null 2>&1 || {
                echo "docker pull reported success but immutable image is not present: $image" >&2
                return 1
            }
            echo "immutable image pull completed: $image"
            return 0
        fi

        if [ "$pull_status" -eq 124 ]; then
            echo "image pull attempt $attempt timed out after ${pull_timeout_seconds}s" >&2
        else
            echo "image pull attempt $attempt failed with exit code $pull_status" >&2
        fi

        if [ "$attempt" -ge "$pull_attempts" ]; then
            echo "image pull failed after $pull_attempts attempts" >&2
            return "$pull_status"
        fi

        echo "retrying image pull in ${pull_retry_delay_seconds}s; completed layers remain cached"
        sleep "$pull_retry_delay_seconds"
        attempt=$((attempt + 1))
    done
}

container=$(docker ps -aq --filter label=com.docker.compose.project=yawnbot \
    --filter label=com.docker.compose.service=yawnbot | head -n 1)
previous_image=""
if [ -n "$container" ]; then
    previous_image=$(docker inspect --format '{{.Config.Image}}' "$container")
fi

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
safe_version=$(printf '%s' "$version" | tr -c 'A-Za-z0-9._-' '_')
backup_name="pre-deploy-${safe_version}-${timestamp}.sqlite3"
backup_container_path="/app/data/backups/$backup_name"
backup_host_path=""
migration_before=""

backup_code='import os, sqlite3, sys
src="/app/data/nonebot_plugin_orm/db.sqlite3"
dst=sys.argv[1]
keep=int(sys.argv[2])
if not os.path.exists(src):
    print("DB_ABSENT")
    raise SystemExit(0)
os.makedirs(os.path.dirname(dst), exist_ok=True)
with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
    source.backup(target)
with sqlite3.connect(dst) as check:
    result=check.execute("PRAGMA integrity_check").fetchone()[0]
if result != "ok":
    raise SystemExit(f"backup integrity_check failed: {result}")
backups=sorted(
    (p for p in os.scandir(os.path.dirname(dst)) if p.name.startswith("pre-deploy-") and p.name.endswith(".sqlite3")),
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)
for old in backups[keep:]:
    os.unlink(old.path)
print(dst)'

if [ -n "$container" ]; then
    migration_before=$(docker exec "$container" nb orm current 2>&1 || true)
    if [ "$(docker inspect --format '{{.State.Running}}' "$container")" = "true" ]; then
        backup_result=$(docker exec -i "$container" python - "$backup_container_path" "$keep_backups" <<PY
$backup_code
PY
        )
    else
        backup_result=$(docker run --rm -i --entrypoint python \
            -v "$root/data:/app/data" "$previous_image" - \
            "$backup_container_path" "$keep_backups" <<PY
$backup_code
PY
        )
    fi
    if [ "$backup_result" != "DB_ABSENT" ]; then
        backup_host_path="$root/data/backups/$backup_name"
        [ -s "$backup_host_path" ] || { echo "SQLite backup was not created" >&2; exit 4; }
    fi
fi

pull_image
printf 'YAWNBOT_IMAGE=%s\n' "$image" > "$root/image.env.tmp"
chmod 600 "$root/image.env.tmp"
mv -f "$root/image.env.tmp" "$root/image.env"

if [ -n "$container" ] && [ "$(docker inspect --format '{{.State.Running}}' "$container")" = "true" ]; then
    docker stop "$container" >/dev/null
fi

compose run --rm --no-deps yawnbot nb orm upgrade heads
migration_after=$(compose run --rm --no-deps yawnbot nb orm current 2>&1)
migration_heads=$(compose run --rm --no-deps yawnbot nb orm heads 2>&1)
compose up -d --no-deps yawnbot

healthy=false
for _ in $(seq 1 60); do
    if curl --fail --silent --show-error "http://127.0.0.1:8080/healthz" >/dev/null; then
        healthy=true
        break
    fi
    sleep 2
done
if [ "$healthy" != "true" ]; then
    compose logs --tail 200 yawnbot >&2
    echo "new image failed healthcheck; database and image were not automatically rolled back" >&2
    exit 5
fi

docker run --rm --entrypoint python \
    --user "$(id -u):$(id -g)" \
    -v "$root/deployments:/deployments" \
    -e PREVIOUS_IMAGE="$previous_image" \
    -e CURRENT_IMAGE="$image" \
    -e COMMIT_SHA="$commit" \
    -e RELEASE_VERSION="$version" \
    -e DB_BACKUP="$backup_host_path" \
    -e MIGRATION_BEFORE="$migration_before" \
    -e MIGRATION_AFTER="$migration_after" \
    -e MIGRATION_HEADS="$migration_heads" \
    -e DEPLOYED_AT="$timestamp" \
    "$image" -c 'import json, os, pathlib
data={
    "previous_image": os.environ["PREVIOUS_IMAGE"],
    "current_image": os.environ["CURRENT_IMAGE"],
    "commit_sha": os.environ["COMMIT_SHA"],
    "release_version": os.environ["RELEASE_VERSION"],
    "db_backup": os.environ["DB_BACKUP"],
    "migration_before": os.environ["MIGRATION_BEFORE"].splitlines(),
    "migration_after": os.environ["MIGRATION_AFTER"].splitlines(),
    "migration_heads": os.environ["MIGRATION_HEADS"].splitlines(),
    "deployed_at": os.environ["DEPLOYED_AT"],
    "status": "healthy",
}
root=pathlib.Path("/deployments")
record=root / f"deploy-{os.environ[\"RELEASE_VERSION\"]}-{os.environ[\"DEPLOYED_AT\"]}.json"
payload=json.dumps(data, ensure_ascii=False, indent=2) + "\n"
record.write_text(payload, encoding="utf-8")
(root / "current.json").write_text(payload, encoding="utf-8")'

echo "deployed $image"
