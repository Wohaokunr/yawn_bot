#!/bin/sh
set -eu

migration_source="/opt/yawnbot/migrations"
migration_target="/app/data/nonebot_plugin_orm/migrations"

if [ -d "$migration_source" ]; then
    mkdir -p "$(dirname "$migration_target")"
    rm -rf "$migration_target"
    cp -a "$migration_source" "$migration_target"
fi

case "${YAWNBOT_AUTO_MIGRATE:-true}" in
    1|true|TRUE|yes|YES|on|ON)
        echo "[deploy] applying ORM migrations"
        nb orm upgrade heads
        ;;
    *)
        echo "[deploy] automatic ORM migration disabled"
        ;;
esac

exec "$@"
