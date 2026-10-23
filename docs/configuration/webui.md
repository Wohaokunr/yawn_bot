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
2. `WEBUI_ADMIN_TOKEN` 非空。长度不再强制，但公网访问建议 16 位以上的独立随机值；过短、字符单一或含常见弱口令片段时，启动日志和配置页「配置体检」会提示偏弱。
3. 原生部署需要先在 `webui/` 执行 `npm ci && npm run build`。
4. Docker 镜像在构建阶段自动执行前端构建，不需要宿主机安装 Node/npm。

启用条件不满足（Token 为空、前端产物缺失、WebUI 配置值写错）时，YawnBot 只跳过 WebUI 并在日志里写明原因，Bot 本体照常运行，不会再因为 WebUI 配置导致整个进程启动失败。`WEBUI_SESSION_TTL_HOURS` 越界会自动收敛到 1–168。

会话 Cookie 的签名密钥由服务器本地随机密钥（`data/webui/session.key`，首次启动自动生成）和运维 Token 共同派生：修改 Token 会让旧会话失效，而即使 Token 较短，拿到一份 Cookie 也无法离线穷举出 Token。

## 配置页

「环境配置」页按分类浏览（模型与提供商、运行与连接、管理后台与安全、Agent、游戏、番茄小说、自定义），支持跨分类搜索和“已自定义 / 未保存 / 有问题 / 外部覆盖 / 敏感项”筛选，状态写在 URL 的 `env.*` 参数里。顶部「配置体检」会列出当前文件里的非法值、偏弱的 Token、缺失的密钥等问题并可一键定位。

保存前前后端都会按配置目录推断的类型和范围校验（整数、小数、布尔、JSON、枚举、说明里写的取值范围），WebUI 启用时也不允许清空运维 Token。保存后需要重启生效；Docker 部署（镜像设置了 `YAWNBOT_SUPERVISED=1`）可直接在页面点「重启 YawnBot」，进程退出后由容器 `restart: unless-stopped` 拉起。

由镜像或 compose 固定的部署参数（`YAWNBOT_PINNED_ENV_KEYS` 列出的 `HOST`、`PORT` 等）在页面上标为「部署固定」，与真正的外部覆盖区分开。

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
