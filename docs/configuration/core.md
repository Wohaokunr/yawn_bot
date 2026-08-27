# Core、OneBot 与存储配置

## NoneBot / HTTP

| 变量 | 默认/示例 | 用途 |
| --- | --- | --- |
| `ENVIRONMENT` | `prod` | 选择 `.env.<environment>`。 |
| `DRIVER` | `~fastapi` | HTTP/FastAPI Driver。正向 WS 追加 `~websockets`，HTTP API 连接追加 `~httpx`。 |
| `HOST` | `127.0.0.1` | 原生部署监听地址；Compose 会覆盖为 `0.0.0.0`。 |
| `PORT` | `8080` | YawnBot HTTP、OneBot 反向 WS 和 WebUI 共用端口。 |
| `LOG_LEVEL` | `INFO` | NoneBot 日志级别。 |
| `SUPERUSERS` | `["123456789"]` | 超级用户 QQ，必须是 JSON 字符串数组。 |
| `SESSION_EXPIRE_TIMEOUT` | `00:02:00` | NoneBot 多轮会话过期时间。 |

健康检查固定为 `GET /healthz`。它只表示 YawnBot FastAPI 进程可响应，不等价于 QQ 账号已经上线。

## OneBot V11

YawnBot 只依赖 OneBot V11 协议，不把 NapCat、Lagrange 等实现打进自己的镜像。

### 反向 WebSocket（推荐）

```dotenv
DRIVER=~fastapi
ONEBOT_V11_ACCESS_TOKEN=replace-with-a-random-onebot-token
```

OneBot 实现连接：

```text
ws://127.0.0.1:8080/onebot/v11/ws
```

Docker 部署时，如果 OneBot 实现在宿主机，连接宿主机公开的 `8080`；如果它在同一个 Docker network，可连接 `ws://yawnbot:8080/onebot/v11/ws`。两端 Token 必须一致。

### 正向 WebSocket

```dotenv
DRIVER=~fastapi+~websockets
ONEBOT_V11_WS_URLS=["ws://127.0.0.1:3001"]
```

### HTTP API

```dotenv
DRIVER=~fastapi+~httpx
ONEBOT_V11_API_ROOTS={"123456789":"http://127.0.0.1:5700"}
```

不要把 OneBot Token 与 WebUI 管理 Token 或 AI Key 复用。

## localstore / SQLite / ORM

```dotenv
LOCALSTORE_USE_CWD=true
SQLALCHEMY_ENGINE_OPTIONS={"connect_args":{"timeout":30}}
ALEMBIC_CONTEXT={"compare_server_default":true}
```

`LOCALSTORE_USE_CWD=true` 时运行时数据位于项目的 `data/`。Docker Compose 将 `/app/data` 持久化为 named volume。

仓库中的 `data/nonebot_plugin_orm/migrations/` 是源码，不是运行时垃圾。Docker 镜像会额外保存一份不可变 migration 副本，容器启动时同步到 mounted volume，再运行 `nb orm upgrade heads`，避免旧 volume 遮住新版本 migration。

原生部署升级仍建议手动执行：

```bash
uv run nb orm heads
uv run nb orm upgrade heads
uv run nb orm current
```

生产迁移、备份和回滚流程见 [部署与维护](../deployment.md)。

## Sentry（可选）

如果安装的 `nonebot-plugin-sentry` 需要错误上报，可配置：

```dotenv
SENTRY_DSN=
SENTRY_ENVIRONMENT=production
SENTRY_RELEASE=yawnbot@0.1.0
SENTRY_SAMPLE_RATE=1.0
SENTRY_DEBUG=false
```

留空 `SENTRY_DSN` 表示不启用上报。生产环境按隐私和流量要求调整采样率，排障结束后关闭 SDK debug 日志。
