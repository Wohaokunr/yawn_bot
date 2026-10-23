#!/bin/sh
set -eu

# 面板管理的配置文件每次启动都重新加载；加载器设置 YAWNBOT_DOTENV_KEYS 后
# 重新执行本脚本，迁移和主进程都能拿到文件里的配置。
if [ -n "${YAWNBOT_ENV_FILE:-}" ] && [ -z "${YAWNBOT_DOTENV_KEYS+x}" ]; then
    exec python /usr/local/bin/yawnbot-load-env "$0" "$@"
fi

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
