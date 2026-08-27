# WebUI 配置

YawnBot WebUI 与 NoneBot 共用同一个 FastAPI 进程，路径固定为 `/webui`。默认关闭。

```dotenv
WEBUI_ENABLED=false
WEBUI_ADMIN_TOKEN=
WEBUI_SESSION_TTL_HOURS=12
WEBUI_COOKIE_SECURE=false
```

## 启用条件

1. `WEBUI_ENABLED=true`。
2. `WEBUI_ADMIN_TOKEN` 至少 32 个字符，并使用独立高熵随机值。
3. 原生部署需要先在 `webui/` 执行 `npm ci && npm run build`。
4. Docker 镜像在构建阶段自动执行前端构建，不需要宿主机安装 Node/npm。

启用 WebUI 但 `webui/dist/index.html` 不存在时，YawnBot 会在启动阶段直接报出明确错误，而不是等浏览器请求时才 500。

## Cookie 与反向代理

`WEBUI_SESSION_TTL_HOURS` 范围为 1–168 小时。公网 HTTPS 部署必须设置：

```dotenv
WEBUI_COOKIE_SECURE=true
```

`false` 只适用于本机或受控内网 HTTP 调试。反向代理负责 TLS 终止时，仍应根据浏览器实际访问协议设置 Secure Cookie。

WebUI 登录 Token、OneBot access token 与 AI API Key 必须彼此独立。

## 访客访问

访客访问策略、访问码轮换和群级 allowlist 由管理员登录 WebUI 后配置，不需要把访客明文凭据写进 `.env`。后端使用角色与群级 scope 校验；访客只有被授权群的只读数据投影，不获得环境配置、审计、调试、写 API 或管理员 WebSocket。

## 地址

- 管理台：`http://HOST:PORT/webui`
- API：`/webui/api/v1`
- YawnBot liveness：`/healthz`

生产反向代理、备份、迁移与安全要求见 [部署与维护](../deployment.md)。
