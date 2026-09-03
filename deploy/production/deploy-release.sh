#!/bin/sh
set -eu

root=${YAWNBOT_ROOT:-/opt/yawnbot}
image=${1:-}
version=${2:-}
commit=${3:-}
fourth_arg=${4:-}
fifth_arg=${5:-}
browser_image=""
auth_mode=""
case "$fourth_arg" in
    github-token-stdin|registry-token-stdin) auth_mode=$fourth_arg ;;
    "") ;;
    *) browser_image=$fourth_arg; auth_mode=$fifth_arg ;;
esac
keep_backups=${YAWNBOT_BACKUP_KEEP:-10}
pull_attempts=${YAWNBOT_PULL_ATTEMPTS:-2}
pull_timeout_seconds=${YAWNBOT_PULL_TIMEOUT_SECONDS:-1200}
pull_retry_delay_seconds=${YAWNBOT_PULL_RETRY_DELAY_SECONDS:-15}
pull_heartbeat_seconds=${YAWNBOT_PULL_HEARTBEAT_SECONDS:-30}
pull_diagnostic_interval_seconds=${YAWNBOT_PULL_DIAGNOSTIC_INTERVAL_SECONDS:-120}
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

validate_image_ref() {
    ref=$1
    label=$2
    case "$ref" in
        ghcr.io/wohaokunr/yawn_bot@sha256:*|sgccr.ccs.tencentyun.com/yawn_bot/yawn_bot@sha256:*) ;;
        *) echo "invalid immutable $label image reference: $ref" >&2; exit 2 ;;
    esac
}

validate_image_ref "$image" "application"
if [ -n "$browser_image" ]; then
    validate_image_ref "$browser_image" "browser"
fi
case "$version" in
    v[0-9]*.[0-9]*.[0-9]*) ;;
    *) echo "invalid release version: $version" >&2; exit 2 ;;
esac
case "$commit" in
    [0-9a-f][0-9a-f]*) ;;
    *) echo "invalid commit SHA: $commit" >&2; exit 2 ;;
esac

registry_host=${image%%/*}
if [ -n "$browser_image" ]; then
    browser_registry_host=${browser_image%%/*}
    [ "$browser_registry_host" = "$registry_host" ] || {
        echo "application and browser images must use the same registry host" >&2
        exit 2
    }
fi
case "$auth_mode" in
    "") ;;
    github-token-stdin)
        [ "$registry_host" = "ghcr.io" ] || {
            echo "github-token-stdin is only valid for ghcr.io" >&2
            exit 2
        }
        IFS= read -r registry_username || { echo "missing registry username on stdin" >&2; exit 2; }
        IFS= read -r registry_password || { echo "missing registry password on stdin" >&2; exit 2; }
        [ -n "$registry_username" ] || { echo "empty registry username" >&2; exit 2; }
        [ -n "$registry_password" ] || { echo "empty registry password" >&2; exit 2; }
        docker_config_dir=$(mktemp -d)
        chmod 700 "$docker_config_dir"
        export DOCKER_CONFIG="$docker_config_dir"
        printf '%s' "$registry_password" | docker login "$registry_host" \
            --username "$registry_username" --password-stdin >/dev/null
        unset registry_password
        ;;
    registry-token-stdin)
        case "$registry_host" in
            ghcr.io|sgccr.ccs.tencentyun.com) ;;
            *) echo "unsupported registry host: $registry_host" >&2; exit 2 ;;
        esac
        IFS= read -r registry_username || { echo "missing registry username on stdin" >&2; exit 2; }
        IFS= read -r registry_password || { echo "missing registry password on stdin" >&2; exit 2; }
        [ -n "$registry_username" ] || { echo "empty registry username" >&2; exit 2; }
        [ -n "$registry_password" ] || { echo "empty registry password" >&2; exit 2; }
        docker_config_dir=$(mktemp -d)
        chmod 700 "$docker_config_dir"
        export DOCKER_CONFIG="$docker_config_dir"
        printf '%s' "$registry_password" | docker login "$registry_host" \
            --username "$registry_username" --password-stdin >/dev/null
        unset registry_password
        ;;
    *) echo "invalid registry auth mode: $auth_mode" >&2; exit 2 ;;
esac

[ -d "$root/data/backups" ] || {
    echo "$root/data/backups is missing; run the one-time server bootstrap" >&2
    exit 3
}
[ -r "$root/bin/write-deployment-record.py" ] || {
    echo "$root/bin/write-deployment-record.py is missing; synchronize the production control plane" >&2
    exit 3
}
mkdir -p "$root/deployments"
chmod 700 "$root/deployments"
exec 9>"$root/deploy.lock"
flock -n 9 || { echo "another production deployment is running" >&2; exit 3; }

compose() {
    docker compose --env-file "$root/image.env" -f "$root/compose.yaml" "$@"
}

pull_diagnostics() {
    reason=${1:-heartbeat}
    elapsed=${2:-0}
    diagnostic_registry=${3:-$registry_host}

    echo "[deploy:pull:diag] reason=$reason registry=$diagnostic_registry elapsed=${elapsed}s timeout=${pull_timeout_seconds}s"

    if command -v getent >/dev/null 2>&1; then
        resolved_count=$(getent ahosts "$diagnostic_registry" 2>/dev/null \
            | awk '{print $1}' \
            | sort -u \
            | wc -l \
            | tr -d ' ')
        echo "[deploy:pull:diag] registry_dns_addresses=${resolved_count:-0}"
    fi

    if command -v ss >/dev/null 2>&1; then
        https_connections=$(ss -H -tn state established '( dport = :443 )' 2>/dev/null \
            | wc -l \
            | tr -d ' ')
        echo "[deploy:pull:diag] established_https_connections=${https_connections:-0}"
    fi

    docker system df 2>/dev/null \
        | sed 's/^/[deploy:pull:diag] docker-df /' \
        || true
}

pull_image() {
    pull_ref=$1
    pull_label=$2
    pull_registry=${pull_ref%%/*}

    if docker image inspect "$pull_ref" >/dev/null 2>&1; then
        echo "immutable $pull_label image already present locally; skipping registry pull: $pull_ref"
        return 0
    fi

    attempt=1
    while [ "$attempt" -le "$pull_attempts" ]; do
        echo "pulling immutable $pull_label image (attempt $attempt/$pull_attempts): $pull_ref"
        pull_started_at=$(date +%s)
        pull_diagnostics "$pull_label-start" 0 "$pull_registry"
        (
            last_diagnostic_at=$pull_started_at
            while :; do
                sleep "$pull_heartbeat_seconds" || exit 0
                now=$(date +%s)
                elapsed=$((now - pull_started_at))
                echo "$pull_label image pull still running (attempt $attempt/$pull_attempts, elapsed ${elapsed}s)"
                if [ $((now - last_diagnostic_at)) -ge "$pull_diagnostic_interval_seconds" ]; then
                    pull_diagnostics "$pull_label-heartbeat" "$elapsed" "$pull_registry"
                    last_diagnostic_at=$now
                fi
            done
        ) &
        pull_heartbeat_pid=$!

        pull_status=0
        timeout --foreground "$pull_timeout_seconds" docker pull "$pull_ref" || pull_status=$?
        pull_finished_at=$(date +%s)
        pull_elapsed=$((pull_finished_at - pull_started_at))

        kill "$pull_heartbeat_pid" 2>/dev/null || true
        wait "$pull_heartbeat_pid" 2>/dev/null || true
        pull_heartbeat_pid=""

        if [ "$pull_status" -eq 0 ]; then
            docker image inspect "$pull_ref" >/dev/null 2>&1 || {
                echo "docker pull reported success but immutable $pull_label image is not present: $pull_ref" >&2
                pull_diagnostics "$pull_label-missing-image" "$pull_elapsed" "$pull_registry"
                return 1
            }
            echo "immutable $pull_label image pull completed: $pull_ref (duration ${pull_elapsed}s)"
            return 0
        fi

        pull_diagnostics "$pull_label-failure" "$pull_elapsed" "$pull_registry"
        if [ "$pull_status" -eq 124 ]; then
            echo "$pull_label image pull attempt $attempt timed out after ${pull_timeout_seconds}s" >&2
        else
            echo "$pull_label image pull attempt $attempt failed with exit code $pull_status" >&2
        fi

        if [ "$attempt" -ge "$pull_attempts" ]; then
            echo "$pull_label image pull failed after $pull_attempts attempts" >&2
            return "$pull_status"
        fi

        echo "retrying $pull_label image pull in ${pull_retry_delay_seconds}s; completed layers remain cached"
        sleep "$pull_retry_delay_seconds"
        attempt=$((attempt + 1))
    done
}

wait_for_browser() {
    browser_container=$(compose --profile fanqie-browser ps -q playwright)
    [ -n "$browser_container" ] || {
        echo "[deploy:browser] Playwright container was not created" >&2
        return 1
    }
    for _ in $(seq 1 45); do
        browser_health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$browser_container" 2>/dev/null || true)
        case "$browser_health" in
            healthy)
                echo "[deploy:browser] healthy"
                return 0
                ;;
            exited|dead)
                break
                ;;
        esac
        sleep 2
    done
    echo "[deploy:browser] failed healthcheck" >&2
    compose --profile fanqie-browser logs --tail 100 playwright >&2 || true
    return 1
}

container=$(docker ps -aq --filter label=com.docker.compose.project=yawnbot \
    --filter label=com.docker.compose.service=yawnbot | head -n 1)
playwright_container=$(docker ps -aq --filter label=com.docker.compose.project=yawnbot \
    --filter label=com.docker.compose.service=playwright | head -n 1)
previous_image=""
previous_browser_image=""
if [ -n "$container" ]; then
    previous_image=$(docker inspect --format '{{.Config.Image}}' "$container")
fi
if [ -n "$playwright_container" ]; then
    previous_browser_image=$(docker inspect --format '{{.Config.Image}}' "$playwright_container")
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
    # Use a one-off container so a crash-looping application cannot block the
    # pre-deploy backup between Docker state inspection and docker exec.
    backup_result=$(docker run --rm -i --entrypoint python \
        -v "$root/data:/app/data" "$previous_image" - \
        "$backup_container_path" "$keep_backups" <<PY
$backup_code
PY
    )
    if [ "$backup_result" != "DB_ABSENT" ]; then
        backup_host_path="$root/data/backups/$backup_name"
        [ -s "$backup_host_path" ] || { echo "SQLite backup was not created" >&2; exit 4; }
    fi
fi

echo "[deploy:pull] start"
pull_image "$image" "application"
if [ -n "$browser_image" ]; then
    pull_image "$browser_image" "browser"
fi
echo "[deploy:pull] success"
{
    printf 'YAWNBOT_IMAGE=%s\n' "$image"
    if [ -n "$browser_image" ]; then
        printf 'YAWNBOT_BROWSER_IMAGE=%s\n' "$browser_image"
        printf 'FANQIE_BROWSER_WS_ENDPOINT=ws://playwright:3000/\n'
    else
        printf 'YAWNBOT_BROWSER_IMAGE=\n'
        printf 'FANQIE_BROWSER_WS_ENDPOINT=\n'
    fi
} > "$root/image.env.tmp"
chmod 600 "$root/image.env.tmp"
mv -f "$root/image.env.tmp" "$root/image.env"

if [ -n "$browser_image" ]; then
    echo "[deploy:browser] start"
    compose --profile fanqie-browser up -d --no-deps playwright
    wait_for_browser || exit 7
else
    compose --profile fanqie-browser stop playwright >/dev/null 2>&1 || true
fi

if [ -n "$container" ]; then
    docker stop "$container" >/dev/null 2>&1 || true
fi

echo "[deploy:migrate] start"
compose run --rm --no-deps yawnbot nb orm upgrade heads
migration_after=$(compose run --rm --no-deps yawnbot nb orm current 2>&1)
migration_heads=$(compose run --rm --no-deps yawnbot nb orm heads 2>&1)
echo "[deploy:migrate] success"

echo "[deploy:start] start"
compose up -d --no-deps yawnbot
echo "[deploy:start] success"

echo "[deploy:health] waiting for http://127.0.0.1:8080/healthz"
healthy=false
for _ in $(seq 1 60); do
    if curl --fail --silent --max-time 3 "http://127.0.0.1:8080/healthz" >/dev/null 2>&1; then
        healthy=true
        break
    fi
    sleep 2
done
if [ "$healthy" != "true" ]; then
    echo "[deploy:health] failed" >&2
    compose logs --tail 200 yawnbot >&2
    echo "new image failed healthcheck; database and image were not automatically rolled back" >&2
    exit 5
fi
echo "[deploy:health] success"

echo "[deploy:record] writing deployment metadata"
if ! DEPLOYMENT_ROOT="$root/deployments" \
    PREVIOUS_IMAGE="$previous_image" \
    CURRENT_IMAGE="$image" \
    PREVIOUS_BROWSER_IMAGE="$previous_browser_image" \
    CURRENT_BROWSER_IMAGE="$browser_image" \
    COMMIT_SHA="$commit" \
    RELEASE_VERSION="$version" \
    DB_BACKUP="$backup_host_path" \
    MIGRATION_BEFORE="$migration_before" \
    MIGRATION_AFTER="$migration_after" \
    MIGRATION_HEADS="$migration_heads" \
    DEPLOYED_AT="$timestamp" \
    python3 "$root/bin/write-deployment-record.py"; then
    echo "[deploy:record] failed; application is healthy but deployment metadata was not written" >&2
    exit 6
fi
echo "[deploy:record] success"

echo "deployed $image"
if [ -n "$browser_image" ]; then
    echo "browser runtime $browser_image"
fi
