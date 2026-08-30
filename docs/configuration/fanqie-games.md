# 番茄、RPG 与狼人杀配置

## 番茄小说

番茄子插件只处理公开且明确免费的内容。Playwright 的 **Python 客户端属于 YawnBot 运行依赖**，但 Chromium 已从主应用镜像中拆出，不再跟随每次普通 YawnBot 发布重复分发。

Docker 部署推荐使用独立 Playwright sidecar。sidecar 只存在于 Compose 内部网络，不应把 `3000` 端口发布到公网；生产发布会固定 Playwright 版本并复用同一个 Chromium runtime digest，直到 Playwright/browser runtime 输入发生变化。

原生 Windows/Linux 部署仍可使用本机 Chromium。默认 headless 模式只需要 headless shell：

```bash
uv run playwright install --only-shell chromium
```

如果明确把 `FANQIE_BROWSER_HEADLESS=false` 用于本机可视化调试，则安装完整 Chromium：

```bash
uv run playwright install chromium
```

### 请求、队列与保留

```dotenv
FANQIE_REQUEST_TIMEOUT=30
FANQIE_REQUEST_RETRIES=2
FANQIE_REQUEST_DELAY=0.5
FANQIE_QUEUE_MAX=20
FANQIE_USER_ACTIVE_MAX=1
FANQIE_GROUP_ACTIVE_MAX=3
FANQIE_MAX_CHAPTERS=500
FANQIE_MAX_FILE_BYTES=33554432
FANQIE_FILE_RETENTION_HOURS=24
FANQIE_SEARCH_LIMIT=5
FANQIE_RANK_LIMIT=10
```

### 浏览器搜索

```dotenv
FANQIE_BROWSER_TIMEOUT=30
FANQIE_BROWSER_HEADLESS=true
FANQIE_BROWSER_PROFILE_DIR=
FANQIE_BROWSER_WS_ENDPOINT=
```

留空 `FANQIE_BROWSER_WS_ENDPOINT` 时使用本机 Playwright Chromium；Docker sidecar 模式设置为内部地址 `ws://playwright:3000/`。不要把它指向不受信任的远程 Playwright 服务，因为该端点具备完整浏览器控制能力。

留空 profile dir 时使用 localstore 下的插件专用目录，不读取个人 Chrome/Edge profile，也不保存 QQ 登录态。sidecar 模式会把 cookies、localStorage 与 IndexedDB 作为 Playwright `storage-state.json` 保存到同一个插件数据目录，并使用仅当前用户可读写的文件权限，以保留番茄公开页面自己的会话连续性。

仓库根目录源码 Compose 和公开 Release Compose 都把浏览器作为 `fanqie-browser` profile 提供。需要番茄浏览器搜索时，设置：

```dotenv
FANQIE_BROWSER_WS_ENDPOINT=ws://playwright:3000/
```

然后以 `--profile fanqie-browser` 启动 Compose。维护者生产 CD 会自动写入该内部端点并启动对应 immutable browser image，不需要手工配置。

### 免费正文来源与回退

```dotenv
FANQIE_APP_PROTOCOL_ENABLED=true
FANQIE_THIRD_PARTY_API_BASE=http://101.35.133.34:5000
FANQIE_THIRD_PARTY_API_TIMEOUT=30
FANQIE_THIRD_PARTY_API_RETRIES=1
FANQIE_THIRD_PARTY_FALLBACK_BASE=https://api.fanqietc.com
FANQIE_THIRD_PARTY_FALLBACK_TOKEN=
```

外部服务可能变更或不可用。不要向这些地址发送 QQ 登录信息、Cookie 或私人凭据。详细来源与使用边界见 [`docs/fanqie-notice.md`](../fanqie-notice.md)。

### 可选本机 helper

```dotenv
FANQIE_MOBILE_HELPER_PATH=
FANQIE_MOBILE_HELPER_STARTUP_TIMEOUT=15
FANQIE_MOBILE_HELPER_TIMEOUT=120
```

只允许填写管理员自行安装且信任的本地绝对路径，不接受远程 URL。

## RPG

首次部署不使用模型服务时建议：

```dotenv
RPG_AI_ENABLED=false
```

常用房间参数：

```dotenv
RPG_MIN_PLAYERS=1
RPG_MAX_PLAYERS=6
RPG_SIGNUP_TIMEOUT=120
RPG_SIGNUP_WARN_REMAIN=60
RPG_CHAR_CREATE_TIMEOUT=180
RPG_CHAR_REROLL_MAX=3
RPG_IDLE_TIMEOUT=600
RPG_ACTION_QUEUE_MAX=100
RPG_USER_PENDING_MAX=5
RPG_EXPLORE_ROUND_TIMEOUT=60
RPG_COMBAT_TURN_TIMEOUT=45
```

AI/KP 常用高级项：

```dotenv
RPG_AI_MAX_TOOL_ROUNDS=4
RPG_AI_TURN_TIMEOUT=90
RPG_KP_TIMEOUT=40
RPG_KP_MAX_TOKENS=2048
RPG_KP_TEMPERATURE=0.8
RPG_KP_MAX_OUTPUT_CHARS=250
RPG_NPC_TIMEOUT=15
RPG_NPC_MAX_TOKENS=512
RPG_NPC_TEMPERATURE=0.9
RPG_NPC_ROUTER_TIMEOUT=4
RPG_NPC_ROUTER_MAX_TOKENS=128
```

还有角色创建、上下文、社交情绪和世界反应等细项，默认值与边界以 `src/plugins/yawn_core/yawn_rpg/config.py` 为单一真相源；LLM 档位路由见 [AI 与 Agent](ai-agent.md)。

## 狼人杀

首次部署不使用模型服务时建议：

```dotenv
WW_AI_ENABLED=false
```

常用参数：

```dotenv
WW_MIN_PLAYERS=9
WW_MAX_PLAYERS=12
WW_SIGNUP_TIMEOUT=180
WW_SIGNUP_WARN_REMAIN=60
WW_SIGNUP_WARN_REMAIN_FINAL=20
WW_NIGHT_TIMEOUT=60
WW_WOLF_TIMEOUT=180
WW_SPEECH_TIMEOUT=120
WW_VOTE_TIMEOUT=90
WW_HUNTER_TIMEOUT=60
WW_LAST_WORDS_TIMEOUT=60
WW_SHERIFF_REGISTER_TIMEOUT=45
WW_BADGE_TIMEOUT=45
```

AI 玩家：

```dotenv
WW_AI_AUTOFILL=true
WW_AI_MAX=11
WW_AI_DECISION_TIMEOUT=90
WW_AI_SPEECH_TIMEOUT=90
WW_AI_MAX_TOKENS=4096
WW_AI_SPEECH_MAX_TOKENS=2048
WW_AI_WOLF_DISCUSS=true
WW_AI_DISCUSS_TIMEOUT=45
WW_AI_REGISTER_BUFFER=15
```

完整默认值与校验范围以 `src/plugins/yawn_core/yawn_werewolf/config.py` 为准；模型 Provider 与档位仍使用共享 LLM 配置。
