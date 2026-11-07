# WebUI 配置

YawnBot WebUI 与 NoneBot 共用同一个 FastAPI 进程，路径固定为 `/webui`。默认关闭。

```dotenv
WEBUI_ENABLED=false
WEBUI_ADMIN_TOKEN=
WEBUI_SESSION_TTL_HOURS=12
WEBUI_COOKIE_SECURE=false
WEBUI_USER_LOGIN_ENABLED=true
WEBUI_PUBLIC_URL=
WEBUI_REMEMBER_DAYS=30
WEBUI_LOGIN_ALERT=true
WEBUI_TRUSTED_PROXIES=
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

WebUI 放在 Nginx/Caddy 等反向代理后面时，后端直接看到的来源 IP 是代理本身：登录限速会把所有人算成一个来源，设备列表和异地登录提醒也认不出地点。把代理的地址写进 `WEBUI_TRUSTED_PROXIES`（逗号分隔，支持网段，如 `127.0.0.1,172.18.0.0/16`），只有来自这些地址的请求才读取 `X-Forwarded-For` / `CF-Connecting-IP` / `X-Real-IP`；留空则一律不信任转发头，防止伪造。

前面还有 Cloudflare（橙色云）时，再加一个 `cloudflare`（展开为 Cloudflare 公布的全部回源 IP 段）。例如生产 Compose 里 Caddy 和 Bot 同在 `yawnbot-internal` 网络：

```dotenv
# 网段用 docker network inspect yawnbot-internal -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 查
WEBUI_TRUSTED_PROXIES=172.18.0.0/16,cloudflare
```

`caddy reverse-proxy` 默认不信任上游，会把 `X-Forwarded-For` 写成它看到的 Cloudflare 节点；Bot 发现这一跳在 Cloudflare 段里，才改读 Cloudflare 加的 `CF-Connecting-IP`。绕过 Cloudflare 直连源站的请求上一跳不在可信段里，伪造这个头无效。只写 Caddy 网段、不写 `cloudflare` 时，记下的会是 Cloudflare 节点 IP，节点一变就会误报“新地点”。

## 登录状态与自动登录

- 三种登录（运维、QQ 账号、访客）都有「自动登录」勾选框（默认勾选，浏览器会记住你的选择）。勾选时签发长效 Cookie，`WEBUI_REMEMBER_DAYS` 天（1–365，默认 30）内再打开 WebUI 免登录；不勾选时是浏览器会话 Cookie，最长 `WEBUI_SESSION_TTL_HOURS` 小时，关掉浏览器就要重新登录。公共电脑上不要勾选。
- 会话剩余有效期不到一半时，`GET /auth/session` 会用同一个会话编号和 CSRF 续签；前端打开页面、每 10 分钟、以及切回标签页时都会调用它，所以正在使用的页面和常用的浏览器不会中途掉线。
- 登录失效时登录页会提示“登录已过期”，QQ 账号登录会记住上次填的 QQ 号。

## 访客访问

访客访问策略、访问码轮换和群级 allowlist 由管理员登录 WebUI 后配置，不需要把访客明文凭据写进 `.env`。后端使用角色与群级 scope 校验；访客只有被授权群的只读数据投影，不获得环境配置、审计、调试、写 API 或管理员 WebSocket。

## QQ 个人账号

普通 QQ 用户可以开一个只属于自己的网页账号，用「QQ 号 + 自己设的密码」登录（登录页的「QQ 账号」标签）。运维 Token 登录与访客访问码保持不变。

- **开通 / 重设密码**：私聊 Bot 发 `/网页密码`（不带参数）拿一次性设置码（8 位，10 分钟有效，只能用一次，最多试 5 次），再到登录页「用设置码设密码」。也可以直接私聊发 `/网页密码 新密码`，Bot 会尽量撤回这条消息、提示你自己撤回，并且这条消息不会写进日志；在群里发会被拒绝。也可以跟私聊 Agent 说“帮我弄个网页登录的设置码”，设置码由 Bot 直接私发，不经过模型，Agent 也不接受密码。
- **让私聊 Agent 带着开通（新人引导）**：跟 Agent 说“我想登录网页”“帮我设个网页密码”，Agent 用 `webui_account` 工具：`guide` 告诉你进行到哪一步、登录网址（`WEBUI_PUBLIC_URL` + `/?login=qq`）和账号怎么填；`ask_password` 让 Bot 直接私发提示，你**下一条私聊**就是你自己定的密码，由系统直接收走、按同样的规则校验、scrypt 加密保存（5 分钟内有效，不合格可重发，连错 3 次或发「取消」结束，发命令也会结束）。这条消息不写日志、不进对话历史、不给模型看，Agent 只知道“设好了没有”；设好后 Bot 直接把登录网址发给你，并记进聊天记录方便 Agent 接着引导。
- **管理**：`/网页登录` 查看账号状态、在线设备数和可管理的群；`/网页登录 设备` 查看在线设备与最近登录记录；`/网页登录 提醒 新地点|每次|关闭` 设置登录提醒；`/网页登录 下线` 让所有设备退出；`/网页登录 注销` 删除账号。设密码、改密码都会让旧登录全部失效。也可以直接问私聊 Agent“我的网页现在登录着几台设备”，Agent 用 `webui_account` 的 `sessions` 动作只读查看（只给设备描述和 IP 段）。
- **登录设备**：每次 QQ 账号登录都登记为一台设备（按 User-Agent 认出系统和浏览器、IP 段、登录时间、最近活跃、是否自动登录），网页「账号与数据」里可以看到全部在线设备，单独下线某一台、或“下线其他设备”（不改密码）；每个账号最多同时 20 台，超出时最久没用的那台自动下线。功能上线前就登录着的会话第一次访问时自动补登记，不会被踢下线。
- **异地登录提醒**：登录的 IP 段（IPv4 /24、IPv6 /48）不在以前的登录记录和在线设备里时记为“新地点”，Bot 私聊提醒本人（时间、设备、IP 段和处理办法），并在登录记录里标出；本人可以改成每次登录都提醒或关闭，`WEBUI_LOGIN_ALERT=false` 全站不发。第一次登录不提醒。提醒内容会记进私聊历史，私聊 Agent 下一轮能接着回答“这是怎么回事”。
- **数据**：设备与登录记录存在 `data/webui/user_devices.json`（权限 0600，只存会话编号的哈希，拿到文件也冒充不了会话；最近 20 次登录），**不需要迁移**；注销账号时一起删除。
- **能看到什么**：
  - 「我管理的群」：只列出你是群主/管理员的群，打开时再向 QQ 实时确认一次身份（Bot 离线时可以只读查看，开关群功能要等 Bot 上线）。每个群有「群关系洞悉」（只读、按访客口径精简，不显示证据与来源）和「群功能设置」（与群里 `/面板` 写同一份设置）。
  - 「私聊人设」：调整 Bot 和你一对一私聊时的人设，受「私聊 Agent → 人设」策略约束（是否允许自己改、每日次数、可改字段）。
  - 「账号与数据」：改密码、登录设备（单独下线 / 下线其他设备 / 退出所有设备）、最近登录记录、登录提醒设置。
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
