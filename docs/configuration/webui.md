# WebUI 配置

YawnBot WebUI 与 NoneBot 共用同一个 FastAPI 进程，路径固定为 `/webui`。默认关闭。

```dotenv
WEBUI_ENABLED=false
WEBUI_ADMIN_TOKEN=
WEBUI_SESSION_TTL_HOURS=12
WEBUI_COOKIE_SECURE=false
WEBUI_USER_LOGIN_ENABLED=true
WEBUI_PUBLIC_URL=
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

## QQ 个人账号

普通 QQ 用户可以开一个只属于自己的网页账号，用「QQ 号 + 自己设的密码」登录（登录页的「QQ 账号」标签）。运维 Token 登录与访客访问码保持不变。

- **开通 / 重设密码**：私聊 Bot 发 `/网页密码`（不带参数）拿一次性设置码（8 位，10 分钟有效，只能用一次，最多试 5 次），再到登录页「用设置码设密码」。也可以直接私聊发 `/网页密码 新密码`，Bot 会尽量撤回这条消息、提示你自己撤回，并且这条消息不会写进日志；在群里发会被拒绝。也可以跟私聊 Agent 说“帮我弄个网页登录的设置码”，设置码由 Bot 直接私发，不经过模型，Agent 也不接受密码。
- **管理**：`/网页登录` 查看账号状态和可管理的群；`/网页登录 下线` 让所有设备退出；`/网页登录 注销` 删除账号。设密码、改密码都会让旧登录全部失效。
- **能看到什么**：
  - 「我管理的群」：只列出你是群主/管理员的群，打开时再向 QQ 实时确认一次身份（Bot 离线时可以只读查看，开关群功能要等 Bot 上线）。每个群有「群关系洞悉」（只读、按访客口径精简，不显示证据与来源）和「群功能设置」（与群里 `/面板` 写同一份设置）。
  - 「私聊人设」：调整 Bot 和你一对一私聊时的人设，受「私聊 Agent → 人设」策略约束（是否允许自己改、每日次数、可改字段）。
  - 「账号安全」：改密码、退出所有设备。
- **安全**：密码用 scrypt 加盐哈希，存放在 `data/webui/user_accounts.json`（权限 0600），不进数据库，因此**不需要迁移**；登录按 IP（5 分钟 20 次）和按账号（15 分钟 8 次）双重限速，账号不存在与密码错误返回同一句话。所有 `/me/*` 接口在服务端按会话里的 QQ 号与群角色校验，只能读写自己的数据。

```dotenv
# 关掉后 QQ 账号登录页签仍在，但登录会被拒绝；运维/访客登录不受影响
WEBUI_USER_LOGIN_ENABLED=true
# 对外访问地址，Bot 私聊里给出的登录链接用它（会带 ?login=qq 直接打开 QQ 账号登录）
WEBUI_PUBLIC_URL=https://bot.example.com/webui
```

## 地址

- 管理台：`http://HOST:PORT/webui`
- API：`/webui/api/v1`
- YawnBot liveness：`/healthz`

生产反向代理、备份、迁移与安全要求见 [部署与维护](../deployment.md)。
