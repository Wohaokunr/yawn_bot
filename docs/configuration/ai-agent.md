# AI 与 Agent 配置

AI 是可选能力。未配置 `AI_API_KEY` 时 YawnBot 仍可启动；`.env.example` 默认把 RPG/狼人杀 AI 关闭，避免首次部署因为模型端点未配置而产生大量失败日志。

## OpenAI-compatible 默认 Provider

```dotenv
AI_API_KEY=replace-with-real-key
AI_BASE_URL=https://your-openai-compatible-endpoint/v1
AI_MODEL=your-model
AI_MAX_TOKENS=1024
```

默认值由 `src/plugins/yawn_core/llm.py` 定义。Key 使用 SecretStr，不应出现在日志或版本库中。

## 模型档位

YawnBot 有 default / light / vision 三档：

```dotenv
AI_LIGHT_MODEL=your-light-model
AI_VISION_MODEL=your-vision-model
AI_DEFAULT_PROVIDER=default
AI_LIGHT_PROVIDER=default
AI_VISION_PROVIDER=default
AI_DEFAULT_THINKING=auto
AI_LIGHT_THINKING=disabled
AI_VISION_THINKING=disabled
AI_DEFAULT_MULTIMODAL=auto
AI_LIGHT_MULTIMODAL=auto
AI_DEFAULT_REASONING_EFFORT=auto
AI_LIGHT_REASONING_EFFORT=auto
AI_VISION_REASONING_EFFORT=auto
```

`*_THINKING` 可用 `auto / enabled / disabled`。`*_REASONING_EFFORT`（推理等级）可用 `auto / none / minimal / low / medium / high / xhigh`：`auto` 不发送参数，其余作为 `reasoning_effort` 发送给服务商，端点不认识该参数时请保持 `auto`。多模态可用 `auto / supported / unsupported`。light 模型留空时继承 default；vision 留空表示没有独立视觉降级档位（对话模型是 mimo-v2/2.5 的 pro/flash 时会自动用同一服务商的 `mimo-v2.5` 识图）。

已知只收文字的对话模型（deepseek-chat / deepseek-reasoner、mimo-v2/2.5 的 pro/flash）在 `AI_DEFAULT_MULTIMODAL=auto` 下不会直接收图片，群聊和私聊都先让识图模型转述再回答；如果你的端点其实支持图片，把 `AI_DEFAULT_MULTIMODAL` 设为 `supported`。

Agent 的所有模型请求（对话、主动发言、记忆整理、图片描述）都不再传 `max_tokens` 限制输出长度：推理模型常把输出预算耗在思考上，导致空回复或工具参数（例如沙箱代码）被截断。需要控制推理深浅和成本时调整推理等级；`inherit` 沿用所选档位的设置。`AI_MAX_TOKENS` 只影响私聊对话等其他功能。

## 多 Provider

```dotenv
AI_PROVIDERS=[{"id":"fast","base_url":"https://fast.example.com/v1"}]
AI_PROVIDER_API_KEYS={"fast":"replace-with-real-key"}
AI_LIGHT_PROVIDER=fast
```

Provider ID 只能使用小写字母、数字、下划线和连字符，`default` 为保留名称。一次请求不会在失败后自动切换到另一个 Provider。

## 子插件任务路由

```dotenv
AGENT_DIALOGUE_LLM_PROFILE=default
AGENT_DIALOGUE_THINKING=inherit
AGENT_PROACTIVE_LLM_PROFILE=light
AGENT_PROACTIVE_THINKING=inherit
AGENT_MEMORY_LLM_PROFILE=light
AGENT_MEMORY_THINKING=inherit
AGENT_IMAGE_LLM_PROFILE=vision
AGENT_IMAGE_THINKING=inherit
AGENT_DIALOGUE_REASONING_EFFORT=inherit
AGENT_PROACTIVE_REASONING_EFFORT=inherit
AGENT_MEMORY_REASONING_EFFORT=inherit
AGENT_IMAGE_REASONING_EFFORT=inherit
AGENT_ROUTER_ENABLED=true
AGENT_ROUTER_LLM_PROFILE=light
AGENT_ROUTER_THINKING=inherit
AGENT_ROUTER_REASONING_EFFORT=inherit

RPG_KP_LLM_PROFILE=default
RPG_KP_THINKING=inherit
RPG_NPC_ROUTER_LLM_PROFILE=light
RPG_NPC_ROUTER_THINKING=inherit
RPG_NPC_LLM_PROFILE=light
RPG_NPC_THINKING=inherit

WW_DECISION_LLM_PROFILE=default
WW_DECISION_THINKING=inherit
WW_SPEECH_LLM_PROFILE=light
WW_SPEECH_THINKING=inherit
```

Profile 可用 `default / light / vision`；任务级 thinking 可用 `inherit / auto / enabled / disabled`。

### 工具路由与不 @ 触发

`agent_router` 是一个很便宜的判断：每个对话回合把**只有当前这条消息**（外加“是不是回复、带没带图、有没有 @ 别人”三个布尔值）交给轻量模型，让它说出这条消息要不要 Bot 做事、第一步需要哪些工具包。它看不到群聊历史、成员资料和记忆。结论只决定第一轮先装哪些工具（最多两包，第三包作为提示），权限、白名单和沙箱开放范围照常逐一校验；路由失败时退回 `discover_tools` 渐进发现，不会按关键词去猜。

同一个判断也用于不 @ 的触发（需要群里开着「唤醒词」）：

- 消息里直接叫了人设名或 Bot 昵称，并且路由认为是在请 Bot 做事；
- 3 分钟内刚明确找过 Bot 的成员，没有 @ 别人、没有回复别人，接着提了一个需要工具的请求。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGENT_ROUTER_ENABLED` | `true` | 关闭后只靠 `discover_tools`，也没有不 @ 的隐式触发 |
| `AGENT_IMPLICIT_TRIGGER` | 开 | 设为 `off` 只关掉隐式触发，保留工具预装 |
| `AGENT_AI_WAIT_NOTICE_MODE` | `reaction` | 回合较慢时先给触发消息贴 👀；`text` 改为发一句话，`off` 关闭 |
| `AGENT_AI_WAIT_NOTICE_DELAY` | `6` | 多少秒后给出第一次反馈 |
| `AGENT_AI_WAIT_NOTICE_LONG_DELAY` | `25` | 已经贴过表情、沙箱还在跑时，多少秒后补一句进度 |

### 发言节奏（分条发送）

真人在群里很少把想说的话憋成一大段：平常聊天会顺手拆成两三条，激动时短句连发，搞怪时一个字一个字往外蹦，只有认真讨论、讲步骤时才一条发完。节奏本身就在表达心情，所以由模型自己选：

- 正文里用 `|||` 把要分开发的部分隔开，每部分就是一条 QQ 消息；
- 开头可写 `【节奏:整段】`、`【节奏:分条】`、`【节奏:连发】`、`【节奏:蹦字】` 指定节奏，标签发送前会剥掉；
- `send_message` 工具同样支持 text 里的 `|||`，也可以直接传 `rhythm`（`block/split/burst/pop`）。

发送层按节奏决定每条之前停多久：分条像正常打字（约 1~4.5 秒，按下一条字数算），连发几乎不停（0.35~1.4 秒），蹦字最快（0.2~0.75 秒）；Bot 当前情绪会微调快慢（愉快/恼火更快，关切/谨慎更慢），人设表现力高的更快更碎。只有第一条带自动引用，第一条之后不再检查触发过期（已经开口就把话说完）。

几条保护：

- 代码块、列表、表格整条发，不兜底拆；复杂问题和短回应（ping/确认/收尾）不兜底拆；
- 发送期间群里有人在排队找 Bot，剩下的几条合成一条立刻发完，信息不丢、也不让下一位干等；
- 任何一条发送失败或回执不确定就停下，不再刷后面的；没发出去的条目的临时文件会清理；
- 每条单独写进消息历史（有自己的 message_id，撤回、引用都对得上），去重与撞重按整句判断；
- WebUI「调试」里调用真实模型后，会显示“发送节奏预览”：拆成几条、每条前停多久。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGENT_REPLY_SPLIT` | `auto` | `auto` 模型标了就拆、没标时按段落/换行/句子保守兜底；`marked` 只按模型标记拆；`off` 一律一条发完 |
| `AGENT_REPLY_MAX_PARTS` | `3` | “分条”节奏最多几条（1–4）；连发最多 5 条、蹦字最多 8 条 |
| `AGENT_REPLY_PACE` | `1.0` | 停顿倍率，`0` 为不停顿连发，最大 3 |
| `AGENT_REPLY_MAX_SPREAD_SECONDS` | `10` | 一次分条里停顿总和上限（0–30 秒），超出就把剩下的合成一条 |

## Agent 媒体与文件

```dotenv
AGENT_MEDIA_CACHE_TTL=86400
AGENT_MEDIA_CACHE_DIR=data/agent_media
AGENT_MEDIA_ALLOWED_HOSTS=gchat.qpic.cn,multimedia.nt.qq.com.cn,*.qpic.cn
AGENT_MEDIA_HTTP_FALLBACK=true
AGENT_MEDIA_URL_PASSTHROUGH=true
AGENT_IMAGE_AUTO_CAPTION=triggered
AGENT_REMOTE_MEDIA_ENABLED=true
AGENT_REMOTE_MEDIA_TTL_DAYS=7
AGENT_REMOTE_MEDIA_MAX_BYTES=67108864
AGENT_REMOTE_MEDIA_PROVIDER=auto

AGENT_FILE_ROOT=data/agent_files
AGENT_FILE_ALLOWED_HOSTS=
AGENT_OPTIONAL_MESSAGE_SEGMENTS=
AGENT_SHARE_ALLOWED_HOSTS=
AGENT_RECEIVED_MEDIA_REUSE=deny
AGENT_DEBUG_LOG=false
AGENT_TRACE_PROMPT_CAPTURE=full
AGENT_PROMPT_OVERRIDES_PATH=
```

- `AGENT_MEDIA_ALLOWED_HOSTS` 默认允许 `gchat.qpic.cn`、`multimedia.nt.qq.com.cn` 和 `*.qpic.cn`（`*.` 开头匹配子域名）；显式置空时，群图片不会被 Agent 主动下载用于视觉理解。
- 识图取图顺序（`yawn_agent/media_fetch.py`）：消息段 `base64://` → 白名单链接（逐跳校验重定向）→ 共享目录 → `get_image` → `get_file`（返回里的 base64 需要 NapCat 打开 `enableLocalFile2Url`）→ 历史/引用图片链接过期时 `get_msg` 重新拉取。OneBot 调用 10 秒超时。
- `AGENT_MEDIA_HTTP_FALLBACK=true`：QQ 图床 HTTPS 握手失败时对同一白名单地址改用 http 重试一次。
- `AGENT_MEDIA_URL_PASSTHROUGH=true`：Bot 取不到字节时把白名单链接交给模型服务商下载；链接已过期、超限或不是图片时不会透传。
- `AGENT_IMAGE_AUTO_CAPTION`：`triggered`（默认）把 Bot 看过并转述过的图记进历史（`[图片]` 旁边带 `image_notes`）；`all` 收到图片就在后台转述（并发 2、每群每小时 30 次，会多花识图费用）；`off` 关闭。转述只在进程内保留 6 小时。
- 要不要把历史图片交给模型，不看字面：本人在 Bot 上次说话之后 3 分钟内发的图自动算本轮的图；工具路由模型返回 `image=true`（这条消息在说图）时取上下文里 15 分钟内最近的图。对话模型配成 `auto` 却拒收图片时会记住 1 小时，期间直接走识图转述。
- WebUI Agent 配置页的“识图状态”卡片（`GET /agent/vision/status`）显示当前识图路线、近 24 小时取图成败和失败原因对应的改法。
- `AGENT_REMOTE_MEDIA_ENABLED=true` 时，支持 Files API 的 Provider 会优先按内容哈希复用/上传远端媒体；关闭后直接使用本轮可用的 inline/URL 降级链。
- `AGENT_REMOTE_MEDIA_TTL_DAYS` 第一版统一为 7 天，同时约束本地 MediaAsset 与远端文件生命周期；不会默认创建永久文件。
- `AGENT_REMOTE_MEDIA_MAX_BYTES` 默认 64 MiB，也是 Agent 接受单个媒体资产的硬上限；inline `file_data`/base64 降级仍限制在 32 MiB 内。
- `AGENT_REMOTE_MEDIA_PROVIDER=auto` 仅在当前任务 Provider 明确支持 Files API 时启用远端文件语义。
- 历史/回复/工具图片不会原样恢复成历史 `assistant`/`system` 多模态消息，而会经 Media Context Projection 投影到**当前 `user` 消息**；远端/本地传输都失败时会显式提供 `caption_ready` 或 `unavailable` 状态，避免模型假装看过图片。
- `get_message`、`get_recent_group_messages`、群记忆检索等工具命中的图片会先物化为本地 MediaAsset；模型看到的工具结果只包含 `media=[{asset_id,type,available}]` 这类安全引用，不包含 OneBot `file`、QQ 签名 URL、base64 或 DeepSeek `file_id`。真正的视觉输入仍由 Dialogue/Speech 的媒体投影层绑定。
- Execution Trace 的“媒体解析”阶段会按图片合并展示 OneBot/get_image、读取与缓存、MIME/大小/SHA-256、Files API、脱敏 file_id/TTL、视觉 Provider/Model、输入类型和最终是否送入模型；工具查询得到的图片使用同一套诊断链路。
- 过期 MediaAsset 使用两阶段清理：先持久化 `cleanup_pending`，再删除远端文件；远端成功或 404 后持久化 `expired`，最后仅在没有其他活动资产引用时删除本地缓存。远端暂时失败会保留 `file_id` 与本地缓存供下次重试。
- `AGENT_FILE_ROOT` 必须是独立运行时目录，不要指向源码、数据库或系统目录。
- 常用文本、引用、@、QQ face、图片等消息段默认可用；`share/contact/location/music` 只有写入 `AGENT_OPTIONAL_MESSAGE_SEGMENTS` 才开放。
- XML、JSON、anonymous、`@all` 与任意原始 CQ payload 不提供开关给模型。
- `AGENT_SHARE_ALLOWED_HOSTS` 单独限制分享卡片链接域名。
- `AGENT_RECEIVED_MEDIA_REUSE` 默认 `deny`；如明确需要，可设置 `same_group` 仅允许同群复用已缓存媒体。
- `AGENT_DEBUG_LOG=true` 会输出大量包含群消息和 LLM 回复的调试信息，只适合本地短时排障。
- `AGENT_TRACE_PROMPT_CAPTURE=full`（默认）：每条执行 Trace 记录真实发给模型的完整请求（全部 messages、tools、参数和回复），在 WebUI 调试台 Trace 的“完整提示词”页查看。只存在内存里，每条 Trace 最多 16 次请求、单次 32 万字符，最近 48 条 Trace 保留；链接、本机路径、内嵌媒体会脱敏；只有管理员能看。设为 `off` 不再抓取。
- `AGENT_PROMPT_OVERRIDES_PATH`：WebUI“提示词”页保存的分段覆盖（删除、替换、追加、前置，全局或本群）写到这个 JSON 文件，默认 `data/yawn_agent/prompt_overrides.json`。文件损坏时 Bot 按代码默认提示词运行，页面会显示错误。不需要数据库迁移。

### 提示词分段与覆盖

Agent 发给模型的每一段文字都在 `yawn_agent/prompt_sections.py` 里登记为一个分段（核心策略、安全与边界、分层表头、上下文数据、工具说明、发言策略、主动发言、工具路由、识图、记忆整理，以及每个工具的描述）。WebUI 调试分区的“提示词”页可以：

- 查看每段的默认文字、最近一次真实生成的样例、生效文字和 Token 估算；
- 按全局或本群删除、替换、追加、前置；本群可选“用代码默认”，不继承全局覆盖；
- “一键删除安全上下文”去掉全部安全分段（需要确认）；删除只影响模型看到的文字，工具权限分级、特权配额、critical 动作门槛、出站校验、隐私退出过滤这些代码保护仍然生效；
- 导入/导出覆盖、查看每段修改历史（每段保留 12 条）、对比默认与改动；
- 不保存直接“预览完整提示词”：草稿只作用于这一次调试运行。

没有任何覆盖时，提示词与改动前逐字一致。整层被删空时，这一条 system 消息不再发送。上下文数据分段（群背景、成员资料、实时状态、当前回合）只能整块保留或删除。

图片类表情包放在 `AGENT_FILE_ROOT/reactions/`，索引文件为 `index.json`。模型只接触 reaction id、标签和描述，不直接猜本地路径。

### 群文件发送

- 要让 Agent 能发的文件直接放进 `AGENT_FILE_ROOT`（可以有子目录，`reactions/` 和以 `.` 开头的路径除外）。模型先用 `list_agent_files` 看到相对文件名，再用 `send_file` 上传，不会接触绝对路径。
- `send_file` 可以带 `folder_id` 上传到指定群文件夹；显示名会去掉路径和非法字符，没写扩展名时自动补上源文件扩展名。
- exe、bat、ps1、vbs、apk、jar、sh 等可执行/脚本类文件一律拒绝发送。
- 远程链接仍然只允许 `AGENT_FILE_ALLOWED_HOSTS` 里的域名；重定向会逐跳重新校验白名单，跳出白名单的下载直接拒绝。

## 私聊 Agent

好友私聊默认由 Agent 直接回复，不用先发 `/对话`。默认用和群里同一套人设，也可以给每位用户单独设（见下方“每用户私聊人设”），能替本人签到、查签到和好感、记住或忘掉关于本人的事、看看共同群里最近在聊什么、查命令用法；也是本人的私人小助理：笔记、待办、到点提醒（可每天/工作日/每周重复）、倒数日、心情记录、聊天偏好（回复长短、表情、称呼、免打扰、早安问候、签到提醒、生日）、计算与单位换算、日期推算、骰子抽签、今日运势、世界时间、联网搜索、翻以前的私聊记录、查看或清空自己的数据、给管理员反馈。成员发 `/退出` 后不再自动回复他，再发 `/对话` 恢复。Agent 不可用（子插件没加载、模型出错、超过日上限）时自动退回原来的纯对话。

| 变量 | 默认 | 说明 |
|---|---|---|
| `PRIVATE_AGENT_ENABLED` | `true` | 总开关；关掉后私聊只有原来的纯对话 |
| `PRIVATE_AGENT_AUTO_REPLY` | `true` | 好友私聊不用 `/对话` 直接回复；关掉后只有进入对话模式才回复 |
| `PRIVATE_AGENT_FRIENDS_ONLY` | `true` | 自动接管只对好友生效，临时会话仍需 `/对话` |
| `PRIVATE_AGENT_DAILY_TURNS` | `120` | 每人每天最多多少轮（0–10000，0 不限），超出当天退回纯对话 |
| `PRIVATE_AGENT_MAX_TOOL_ROUNDS` | `8` | 每轮最多调用几轮工具（1–20） |
| `PRIVATE_AGENT_TURN_TAKING` | `true` | 发言判断：Agent 可以只回 `【不回】` 或 `【等他说完】`，他刚开口又发来新消息时把这轮让给下一轮，避免连发几条换来几段回复；`false` 恢复每条必回 |
| `PRIVATE_AGENT_WAIT_SECONDS` | `25` | 选择“等他说完”后最多等几秒（5–600），期间没有新消息就自己接上话 |
| `PRIVATE_AGENT_STATE_PATH` | 空 | 成员暂停自动回复的状态文件，留空为 `data/private_agent/state.json` |
| `PRIVATE_AGENT_WEB_DAILY` | `10` | 私聊里每人每天联网搜索/读网页次数（0 关闭）；仍受「网页搜索」总开关、屏蔽域名和全站预算约束 |
| `PRIVATE_AGENT_PROACTIVE` | `true` | 允许私聊主动发消息（用户设的提醒、早安、签到提醒、生日、倒数日）；关掉后全部不发 |
| `PRIVATE_AGENT_MERGE_WINDOW_MS` | `1500` | 连发合并：几秒内连发的几条私聊合成一轮回复（0 关闭） |
| `PRIVATE_AGENT_DATA_DIR` | 空 | 个人数据目录（每人一个 JSON），留空为 `data/private_agent/users` |
| `PRIVATE_AGENT_PERSONA_POLICY_PATH` | 空 | 私聊人设策略文件，留空为个人数据目录旁边的 `persona_policy.json`（WebUI「私聊 Agent → 人设」里改，不用手写） |
| `PRIVATE_AGENT_ACCEPT_MEDIA` | `true` | 私聊里收图片/文件/语音交给 Agent；虚拟电脑开着时自动放进本人电脑的 `inbox/` |
| `PRIVATE_AGENT_TASKS_ENABLED` | `true` | 主从多智能体：主 Agent 可把耗时多步的活派给后台小助手 |
| `PRIVATE_AGENT_TASK_PER_USER` | `6` | 每人同时运行的小助手任务数（1–50），多的排队；排队也最多这么多 |
| `PRIVATE_AGENT_TASK_SLOTS` | `30` | 全站同时运行的小助手任务数（1–200），多的排队 |
| `PRIVATE_AGENT_TASK_DAILY` | `90` | 每人每天最多派几个小助手任务（0 不限） |
| `PRIVATE_AGENT_TASK_MAX_ROUNDS` | `30` | 每个小助手任务最多几轮工具（2–100），最后一轮只能交付 |
| `PRIVATE_AGENT_TASK_CONTINUATIONS` | `2` | 轮次用完还没交付时最多再续几段（0–10），续跑前先自查计划与备忘 |
| `PRIVATE_AGENT_TASK_TIMEOUT_SECONDS` | `1800` | 每个小助手任务最长运行秒数（30–7200，不含排队），超时按没做成汇报 |
| `PRIVATE_AGENT_PROGRESS_STYLE` | `natural` | 后台进度消息样子：`natural` 用人设口吻随口报一声，只有同时做好几件事才带上是哪件；`template` 是旧的「标题」 n/m：… |
| `PRIVATE_AGENT_PROGRESS_MIN_GAP_SECONDS` | `10` | 同一个人两条进度至少隔几秒（0–600），太近的只记录不发，派活那句回复也算一条 |
| `PRIVATE_AGENT_HANDOFF_AFTER` | `3` | 主 Agent 一轮里自己调了这么多次重活工具（联网、读网页、跑代码、能力包）还没答完时，提醒它放到后台（0 不提醒） |
| `PRIVATE_AGENT_AUTO_HANDOFF` | `true` | 主 Agent 工具轮次用完还在调工具时，系统自动把这件事放到后台接着做，并让它先简短回一句 |
| `PRIVATE_AGENT_REPORT_BATCH_SECONDS` | `2` | 几件后台的事前后脚做完时，等几秒合成一次汇报（0–60） |

- 私聊里只会读到用户本人所在、且群里 Agent 开着、本人没有退出记忆的群；群里的关系和画像只以词语出现，不给数值。
- 私聊里记住的事只在私聊里用，不会带进群聊；本人明确要求时才同步到群里。
- 私聊提示词在 WebUI「私聊 Agent → 提示词」页：按主回合、汇报、协调、后台小助手、记忆整理五条链路列出每一段，可以对所有私聊改，也可以填一位用户的 QQ 只给他改（本人设置优先于全局）；体检会标出被删的安全段、缺占位符、和默认一样的覆盖；“预览”用这位用户的真实资料和最近聊天拼出完整请求，不调用模型。群聊的提示词工作台不再列出私聊分段。
- 「私聊 Agent → 最近请求」按用户保存每次真实发给模型的请求（每人最近 8 次、最多 64 人，只在内存里），标明是哪条链路、用的什么人设；可以暂停抓取。`AGENT_TRACE_PROMPT_CAPTURE=off` 时不抓。

### 每用户私聊人设

每位用户在私聊里可以有自己的人设，只影响和他的私聊，群聊不受影响。生效顺序：本人专属卡 → 本人选的人设库模板 → 私聊默认（策略里选的人设库模板）→ 群聊全局人设 → 内置默认。

- 管理员在 WebUI「私聊 Agent → 人设」里给某位用户设专属卡、让他跟随人设库模板、套预设、从某个群复制群聊人设、导入导出、存进人设库、锁定、撤销、恢复跟随默认，或批量处理。
- 用户也可以在私聊里自己说（“以后自称本喵”“换成温柔一点的”“恢复默认人设”），Agent 用 `my_persona` 工具改；受策略约束：总开关、是否允许本人改、每天最多几次（默认 10）、哪些字段能改、能不能套预设/选模板。管理员锁定后本人不能再改。
- 每次修改都留历史（最近 10 版，记谁改的、改了哪些字段），随时可以撤销。人设里不能写让 Agent 无视规则的话，本人改时也不能放手机号、证件号。
- 有专属人设时，早安/签到提醒这类定时问候会带上他人设里的自称和口癖；表现力调到最低档时，私聊表情包每日额度会相应收窄（不会超过后台设的上限）。
- 他在聊天偏好里设过“叫我…”时，称呼以他自己的为准。

### 私聊里的发言节奏、表情包、图片与虚拟电脑

- 分条与节奏和群聊同一套（`AGENT_REPLY_SPLIT`、`AGENT_REPLY_MAX_PARTS`、`AGENT_REPLY_PACE`）：模型开头写【节奏:整段/分条/连发/蹦字】，用 `|||` 分条。
- 表情包：只用“所有群可用”的那部分库；WebUI「表情包 → 策略」里的私聊开关、每人每日张数（默认 20）和冷却（默认 20 秒）约束发送。
- 虚拟电脑：每位用户一个独立的沙箱工作区 `p<QQ>`，和群的 `g<群号>` 互不可见；WebUI「代码沙箱 → 策略」里的私聊开关、每人每日运行次数（默认 500）、自动入箱与每人每日入箱 MB 约束它，不占任何群配额。回复里写 `[图片:sandbox:路径]` / `[文件:sandbox:路径]` 发出电脑里的文件（文件走 `upload_private_file`）。

### 主从多智能体（后台小助手）

主 Agent 照常聊天；遇到联网查资料并整理、写代码算数据画图、处理文件、写长文这类耗时多步的活，用 `delegate_task` 派给后台小助手（子智能体）。小助手只看到主 Agent 写的任务单，看不到聊天记录和用户资料，只有联网、虚拟电脑、小工具箱和 finish / ask_user / report_progress 工具，不能改个人数据也不能直接聊天。

- 小助手每完成一步发一条模板进度（如「查天气」2/4：已查到三地数据），免打扰时段不发，每个任务最多 20 条。
- 做完（或需要问用户、没做成）后，主 Agent 拿到结果用人设转述，可以分条、发图、发文件；免打扰时段完成且离派活超过 10 分钟的结果等免打扰结束再发。
- 用户可以问进度、取消，或回答小助手的问题（主 Agent 用 `tasks` 工具转交，小助手带着原上下文接着做）。
- 任务记录在个人数据 JSON 的 `tasks` 里（最近 20 个），不建表；Bot 重启时没跑完的任务记为“重启中断”，不自动重跑。
- 个人助理数据不进数据库，也不需要迁移；成员说“清空我的数据”并确认后，笔记、待办、提醒、倒数日、心情、偏好和私聊记忆一起删除（聊天记录本身用 `/新对话` 清上下文）。
- 主动消息只发给有个人数据的人，免打扰时段、关掉主动消息或 `/退出` 暂停后都不发；本人设的提醒照常送达。反馈会写进 `data/private_agent/feedback.jsonl` 并私聊转给超级管理员。
- WebUI「全局用户 → 详情」里有“好感与签到”（等级、来源、流水、各群签到、手动调整）和“私聊 Agent”（当前状态、今日轮数、暂停/恢复自动回复、查看和删除私聊记忆）两个标签页。

## Agent 代码沙箱

Agent 可以在一个独立、无网络的 Python 容器里运行代码：数据分析、画图、生成表格/文本文件、作曲（WAV/MIDI）。部署和隔离细节见 [sandbox/README.md](../../sandbox/README.md)。

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_SANDBOX_URL` | `unix:///run/yawnbot-sandbox/runner.sock` | runner 地址；Compose 已经把 socket 卷共享给 bot。置空即关闭沙箱 |
| `AGENT_SANDBOX_TOKEN` | 空 | 只有把 runner 暴露成 TCP 时才需要；Unix socket 部署留空 |
| `AGENT_SANDBOX_INBOX_HOSTS` | 内置 QQ 域名 | 群聊附件入箱时允许下载的域名，逗号分隔，`*.qq.com` 匹配子域名；重定向逐跳重新校验 |
| `AGENT_SANDBOX_INBOX_LOCAL_ROOTS` | 空 | NapCat 只回本机路径时允许读取的共享目录，逗号分隔；只在 NapCat 与 Bot 挂载同一目录时配置 |

- 开放范围、仅管理员、成员/群每日次数、冷却、超时、内存、工作区配额、输出长度都在 WebUI「代码沙箱 → 策略与限额」里改，存在 `data/agent_sandbox/settings.json`，不需要数据库迁移、也不需要重启。
- 工具族 `sandbox`（`run_python`、`run_shell`、`list_sandbox_files`、`read_sandbox_file`、`delete_sandbox_file`、`sandbox_info`）通过工具路由预装或 `discover_tools` 装载；沙箱离线、隔离自检失败或本群未开放时整族隐藏，模型会如实说明。
- `run_shell` 是同一个群工作区里的 bash 终端（ls/cat/grep/find/wc/jq/unzip 等），需要更新后的 runner 镜像；旧 runner 不报告 `shell` 能力时这个工具自动隐藏。
- 同一回合里“报错 → 修正 → 再跑”不再被冷却挡住（仍计入每日次数）；剩余冷却很短时会等一下而不是报错；runner 忙或刚重启时自动重试一次。
- 运行失败时模型拿到的是精简过的 traceback 和一条 `error` 摘要（哪一行、什么错、怎么修），输出按行截断。默认输出上限调到 6000 字。
- 每个群一个持久工作区。生成的图片/音频用 `sandbox:文件名` 引用，由 `send_message` 的 image/record 段发送；其他文件用 `send_file` 上传群文件。
- 运行历史默认保留 14 天，WebUI 可以手动清理；运行记录里有完整代码，Agent 审计只记代码长度。

### 群聊附件进内置电脑

群友在群里发图片、文件、语音、视频，再让 Agent“用电脑处理一下”，附件会被放进这个群工作区的 `inbox/` 目录，`run_python` / `run_shell` 直接读。

- 取文件内容的顺序：消息段里的 base64 → OneBot 接口（`get_record` 转 mp3、`get_group_file_url`、`get_image`、`get_file`）→ 白名单 QQ 域名下载 → `AGENT_SANDBOX_INBOX_LOCAL_ROOTS` 里的共享目录。NapCat 和 Bot 不共享文件系统，所以 **NapCat 需要打开 `enableLocalFile2Url`**（仓库自带的 NapCat 模板已经打开；老部署要重新渲染一次 `deploy/napcat/render-yawnbot-config.py` 并重启 NapCat）。
- 文件名按内容哈希命名（如 `inbox/图片_3f2a9c1b04.png`），同一个附件不会重复放；可执行文件会被改成 `.txt` 后缀，模型看不到原始 URL 或 OneBot 文件标识。
- 自动放入的时机在 WebUI「代码沙箱 → 策略与限额 → 群聊附件入箱」里选：`用到内置电脑时`（默认，工具路由判断这轮要用沙箱时）、`每次都放`、`只在 Agent 要时`。决定只看结构化事实（消息里有没有附件、沙箱工具是否可用），不做关键词匹配。
- 新工具：`import_attachments`（把本条、引用或之前的附件放进去）、`list_chat_attachments`（列出最近可放入的附件和已放入的位置）、`import_group_file`（按群文件 id 放入群文件）。运行代码前如果这轮的附件还没放进去，会自动补放一次。
- 限额：单个附件大小、每轮最多几个、可回溯多久的历史附件、每群每天总量，都在同一张卡片里改；超限会如实告诉成员原因。
- 隐私：只给开放沙箱的群放附件；退出 Agent 记忆的成员发的附件不会被放入，删除成员记忆时也会抹掉他的入箱记录。
- 入箱记录在 `data/agent_sandbox/inbox_log.jsonl`（最多 1000 条，不含 URL 和本机路径），WebUI「代码沙箱 → 附件收件箱」展示今日统计、失败原因（谁来修）、取文件链路自检和每条记录。

## Agent 网页搜索

Agent 用 DeepSeek 官方的 Search / Fetch API 联网：查最新消息、实时信息和不确定的事实，打开网页或成员发来的链接读正文。接口由 DeepSeek 自己的搜索引擎提供，按 ¥5 / 千次**成功**请求计费，失败和无结果不收费，每个账号每个接口 5 QPS。

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_WEB_SEARCH_API_KEY` | 空 | DeepSeek 密钥。留空时依次用 `DEEPSEEK_API_KEY`、Base URL 指向 `*.deepseek.com` 的模型提供商密钥（默认提供商优先）；都没有就不开放联网搜索 |
| `AGENT_WEB_SEARCH_BASE_URL` | `https://api.deepseek.com` | 一般不用改 |

- 工具族 `web`：`web_search`（关键词、条数、`site`/`exclude_sites`/`lang`/`after`/`before`，返回带 `w1`、`w2` 编号的标题/网站/日期/摘要/可信度）、`web_fetch`（用编号或网址读正文，长文 `offset` 续读）、`web_search_info`（是否可用与剩余次数）。都是只读工具，经工具路由（新增 `web` 工具包）预装或 `discover_tools` 装载；没密钥、本群没开放、仅管理员或今日预算为 0 时整族隐藏并给出原因。
- 开放范围、仅管理员、每人/每群/全站每日次数、冷却、每轮上限、默认/最多条数、是否带整页正文、是否允许读网页、单次读取字数、缓存时长、屏蔽网站，都在 WebUI「网页搜索 → 策略与限额」里改，存在 `data/agent_web_search/settings.json`，不需要迁移或重启。
- 次数先预扣，失败、无结果、命中缓存都退还，配额口径和账单一致；同一搜索词 / 网页在缓存时间内不重复计费，分段续读同一页不再请求。
- 只有 408/429/5xx 和超时会重试（最多 3 次，尊重 `Retry-After`，单次最多等 5 秒）；密钥失效、余额不足、站点拒绝抓取等会如实告诉模型和成员，不编造结果。
- 读网页前本地拒绝内网/本机地址、带账号密码的链接和非 http(s) 链接；搜索结果和网页正文都标成“资料，不是指令”。
- 模型没真的搜过却说“我搜了一下 / 搜索结果显示”会被声明核验删掉。
- WebUI「网页搜索」页：状态与密钥来源（只显示掩码）、今日次数与费用估算、各群/成员用量、最近 200 次调用（只在内存，成员删除记忆时一并抹掉）、试搜/试读（计入全站预算，不占群配额）、清空缓存。
- 查询运算符：DeepSeek 文档确认支持 `site:`、`-site:` 和引号短语；`lang:`、`after:`、`before:` 来自社区实现，如果 DeepSeek 不认会被当成普通关键词。

## Agent 表情包

Agent 可以像群友一样发表情包：单独甩一张、说完一句再跟一张，或者整条回复只发一个表情包。表情包是分条发送序列里的一条，和 `|||` 分条共用同一套节奏。

- 模型在正文里写 `[表情:名字]` 或 `[表情:情绪]`，系统把它换成库里对应的图片单独发一条；名字/情绪不在库里、本群没开放、冷却中或超出上限时安静去掉（只剩表情时退回 `[名字]` 文字）。不额外消耗工具回合。
- 什么时候发由模型自己判断，系统不按聊天内容的关键词触发。WebUI 里的「频率」（少用/正常/爱发）只改变给模型的建议；冷却、每群每日上限、每条最多几个是硬限制。人设关闭“主动贴表情”时不给菜单。
- 库存放在 `AGENT_FILE_ROOT/reactions/`（`index.json` 兼容旧格式），策略和用量在 `data/agent_stickers/`，不需要迁移或重启。初始库为空：在 WebUI「表情包」页上传，或在群里引用一张图对 Bot 说“收藏这个表情”（`save_sticker` 工具）。
- 收藏策略：关闭 / 需要审核（默认，管理员收藏的直接可用）/ 直接可用；可设仅管理员、每人每日次数、单张大小上限。成员删除记忆时，其收藏记录的署名一并清除。
- WebUI「表情包」页：表情包库（筛选、搜索、按标签、批量审核/启停/删除、编辑名字/标签/说明/开放群）、发送与收藏策略、今日用量、按群预览本轮给模型的菜单提示词。

## Agent 定时任务

群友可以直接对 Bot 说“每天早上 8 点提醒我打卡”“工作日 9 点查一下天气告诉大家”“半小时后叫我开会”，Agent 会用定时任务工具把它记下来，到点自己醒来执行。

- **两种执行方式**：`直接提醒`（到点把内容原样发到群里，不调用模型）和 `唤醒 Agent`（到点把指令交给 Agent，它像被人叫到一样思考、可以查资料/跑代码，再决定说什么）。唤醒回合里 Agent 不能再建定时任务，也不能用群管理等高危工具。
- **时间规则**：单次（指定时间或 N 分钟后）、固定间隔（可限定每天的时间窗口，支持跨午夜）、每天、每周（选星期）、每月（选日期，`-1` 表示月末）、标准 5 段 cron（`0` 和 `7` 都是周日，支持 `@daily` 等别名）；还可以设开始/结束时间、只在某几个月、跳过指定日期（节假日），以及最多执行几次。全部按北京时间。
- **工具**：`schedule_task`（可先 `dry_run` 预览接下来几次）、`list_scheduled_tasks`、`update_scheduled_task`（改内容/时间、暂停、恢复）、`cancel_scheduled_task`（取消一个或“我设的全部”）。成员只能改/取消自己设的，群主和管理员可以管本群所有任务。相同内容和规则的任务会直接复用，不会重复创建。
- **可靠性**：调度器每 20 秒检查一次，执行前先把“下次时间”推进并落库，Bot 崩溃重启也不会重复执行；停机期间错过的任务按策略补跑一次或跳过；失败按 2/5/15 分钟重试，连续失败到上限自动暂停；创建者退群后任务自动暂停；Bot 不在线、群 Agent 被关掉、当日次数用完时记为跳过并写明原因。
- **策略与限额**在 WebUI「定时任务 → 策略与限额」里改，存在 `data/agent_schedule/settings.json`，不需要重启：开放范围（所有群 / 白名单 / 禁用名单）、谁能创建（成员 / 仅管理员）、是否允许唤醒 Agent、每群/每人进行中的任务上限、循环任务最小间隔、最远可定多久、每群每日执行次数、迟到宽限与补跑、连续失败上限、免打扰时段（推迟或跳过）、并发群数、单次唤醒时限、提醒前缀、每个任务保留多少条运行记录。
- WebUI「定时任务」页可以查看所有群的任务、按群/状态/方式筛选、批量暂停恢复取消、手动新建或编辑（右侧实时显示接下来 10 次触发时间）、立即执行一次，以及查看运行记录和对应的 Agent 执行轨迹。
- **需要数据库迁移**：本功能新增表 `yawn_core_agentscheduledtask`，升级后执行一次 `nb orm upgrade`。迁移前 Agent 会说定时任务暂不可用，WebUI 页面会提示迁移。

## Agent 群管理工具

特权工具都要同时满足：调用者实时是群主/管理员、在 WebUI「允许的特权工具」里勾选、当日特权额度还有剩余。执行顺序固定为参数校验 → 权限校验 → 预扣额度 → 执行 → 审计，失败自动退还额度；同一轮对话里重复的同一操作只执行一次，每轮最多 3 次管理操作。

- 公告：`create_group_announcement` 支持置顶（`pinned`）、需要群成员确认（`confirm_required`）、引导修改群名片（`show_edit_card`）；与现有公告内容几乎相同时会拒绝重复发布。
- 禁言：勾选「禁言」后 `unmute_member`（解除禁言）自动可用，也可以只勾选「仅解除禁言」。
- 撤回：`recall_message` 撤回群里的消息（机器人需要是管理员）；`recall_bot_message` 只撤回机器人自己 2 分钟内的消息，勾选 `recall_message` 时自动可用。
- 只读辅助：`find_group_member`（按昵称找人）、`list_group_admins`、`search_group_files`、`get_group_file_space`、`list_agent_files` 不需要白名单。

## Agent 人设 v3

人设把**角色、临时情绪、系统策略**拆成三个不同层级：

1. **System Policy**：事实性、隐私、权限、工具安全与 Prompt 注入防护，不允许人设覆盖。
2. **人设卡（PersonaCard）**：稳定的角色设定。WebUI、QQ 命令、人设库、调试草稿都使用这同一种形状。
3. **Dynamic Emotion**：随近期群聊事件短暂变化的 Bot 表达状态，会自动衰减，只影响措辞和表现力。

全局只保留一个轻量环境变量，用作内置默认人设的名字：

```dotenv
AGENT_PERSONA_NAME=Yawn
```

### 人设卡的内容

- **名字 / 自称 / 对成员的称呼**：例如名字“猫猫”、自称“本喵”、称呼成员“杂鱼”；留空就用“我”和群名片。
- **一句话身份、性格、说话方式**：自由文字，性格和说话方式各最多 400 字。
- **口癖**（最多 6 条）、**喜欢的话题**、**避开的话题**（各最多 8 条）、**示例台词**（最多 4 句）。模型会被提示“口癖偶尔用、示例只学语气不照抄”。
- **六个 0–4 档位**：温度、幽默、详略、表现力、活跃度、接梗。v2 的“直接程度”交给“说话方式”文字描述，“续聊倾向”改由活跃度推导。

超长或越界的输入会被裁剪/夹到范围内，而不是让整次保存失败。人设文字里出现“忽略以上规则”、伪造 system/CQ 码、`@全体成员` 之类试图覆盖系统策略的内容会被拒绝；“自称本喵、叫成员杂鱼”这种角色指令是允许的。

编译后的人设有独立的 token 预算（700），超出时按 示例 → 喜好 → 避开话题 → 口癖 → 长文本 的顺序自动精简，WebUI 会显示“已自动精简”和模型实际看到的人设 JSON。

内置模板共 9 个：`自然群友`、`温和倾听`、`冷静理性`、`活跃捧哏`、`安静潜水`、`傲娇猫娘`、`元气少女`、`毒舌吐槽`、`学霸助教`。套用模板会替换性格、说话方式、口癖和档位，保留名字。

### 生效顺序与人设库

每个群实际使用的人设按固定顺序决定：

1. **本群自定义**：在 WebUI 选“当前群自定义”并保存，或 QQ 里保存过。
2. **全局人设**：人设库里被设为“全局人设”的那一张（同一时刻最多一张）。
3. **内置默认**：人设库没有全局人设，或人设库表还没迁移时使用。

切换为“跟随全局”不会删除本群草稿，再切回自定义时可以继续编辑；“恢复全局”才会真正清空本群人设。

人设库（WebUI → Agent → 人设 → 人设库）可以把调好的人设存起来，给其它群一键“用于本群”（复制一份，之后互不影响），也可以设为全局人设。全局人设变化会立即刷新到所有跟随全局的群。人设库需要执行一次数据库迁移（`b8d0f2a4c6e9_agent_persona_library`）：

```bash
nb orm upgrade
```

迁移之前人设库接口会返回“请先执行数据库迁移”，对话照常使用内置默认人设。存量 v2 人设在读取时自动转换为 v3，被编辑保存一次后即以 v3 格式落库，不需要额外的数据迁移。

### QQ 命令

`/Agent人设` 进入会话菜单：切换模板、修改文字设定（名字、自称、称呼、身份、性格、说话方式、口癖、喜好、避开话题、示例台词）、调档位、使用人设库、试演、恢复全局。所有修改都先预览，回复“确认保存”才会写入。快捷子命令：

- `/Agent人设 查看`：当前人设来源、摘要和动态情绪。
- `/Agent人设 模板 <名称或序号>`：套用内置模板。
- `/Agent人设 人设库`：列出人设库。
- `/Agent人设 使用 <人设库名称或序号>`：把人设库里的人设用于本群。

WebUI 可以把**尚未保存的人设草稿**交给 Agent Debug 试演，试演复用真实 `build_messages()` 和可选真实模型调用，但不会保存人设、执行工具、发送 QQ、写入记忆、修改动态情绪或主动参与状态。

### 人设控制真实群聊行为

人设的社交档位会进入真实控制流，而不只是写进 Prompt。优先级固定为：**System Policy → 群运行配置硬门槛/上限 → 人设行为倾向 → 当前上下文**。人设只能在管理员已经允许的范围内进一步收窄行为。

- 活跃度（`sociability`）缩放暖场/热闹插话候选概率，0–4 档对应 `0.15 / 0.45 / 0.75 / 0.90 / 1.00`；同时限制一个短会话中 Bot 的总发言数，0–4 档对应 `1 / 2 / 3 / 4 / 4`（1 表示本轮回答后不再自动续聊）。
- 接梗（`reaction_tendency`）控制主动参与/自动续聊时是否允许自发使用 reaction。0–1 档会过滤自发 reaction；用户明确要求发送表情包时仍走普通对话工具路径。
- 发群公告时，公告正文始终写成书面、完整的通知，不带角色口癖。

- 活跃度 0–1 档的人设在一段冷场里只尝试暖场 1 次，其余 2 次（见下节）。

## Bot 和群友的关系

记忆整理会顺带沉淀 **Bot 自己和每位成员的关系**：认识多久、熟不熟、对方怎么叫 Bot、Bot 怎么叫对方、说话分寸、你们之间的梗、对方的边界，以及一起经历过的事。

- 熟悉度只按结构算：成员 @Bot、回复 Bot、Bot 刚说完话后的接话、Bot 的回复，以及来往的天数。一天聊很多不如持续来往很多天。
- 好感和信任由记忆模型观察，每次只能小幅调整，一句玩笑改不掉稳定的关系标签。
- 聊天时 Bot 只看到“挺熟的朋友，他叫你喵喵，可以互相吐槽，别提考试”这类描述，看不到数值，也被要求不硬提旧梗。
- 成员可以直接让 Bot 记住“我们之间”的称呼、梗、边界或经历，也可以让它“忘掉我们的关系”；`/Agent隐私` 退出的成员不会被记录。
- 一个月没来往的好感会慢慢回到中性，一年没来往的自动关系会被删除。
- WebUI → Agent → 关系 → 「Bot 视角」可以查看、修改（改过的字段会锁定，自动整理不再覆盖）、补充共同经历或重置。

这个功能需要执行一次数据库迁移（`c4e6a8b0d2f3_agent_bot_bond`），迁移前 Bot 照常聊天，只是不沉淀关系：

```bash
nb orm upgrade
```

## 冷场暖场

群里没人说话时 Agent 会偶尔暖场，但不会对着空群一直自言自语。规则全部由消息历史和审计推导，进程重启后依然有效，无需迁移：

- 冷场以**真人**最后一次发言为准（包括隐私退出成员：他们的消息不进 Prompt，但在说话就说明群没冷场）；Bot 自己的消息不会重置冷场计时。
- 一段冷场里 Bot 主动说了 2 条（低活跃度人设 1 条）都没人接，就停到有真人开口为止；模型判定 `close`（这段冷场不该打扰）同样暂停到有人说话。
- 每多一条没人接的消息，下一次暖场需要的安静时长翻倍（15 → 30 → 60 分钟，上限 6 小时）；模型说 `wait` 或生成失败后的重试也按次数翻倍退避（上限 3 小时），不再每几分钟调用一次 LLM。
- `proactive_probability` 表示“这次冷场暖场的概率”：同一段冷场每分钟用同一个骰子点数，不会被每分钟重掷放大成几乎必然。概率在冷场阈值到两倍阈值之间爬升，真人沉默超过 6 / 24 小时后依次减半，每多一条没人接再减半。
- 北京时间 1:00–8:00 不暖场；真人沉默超过 48 小时视为群已经不聊了，不再暖场；每群每天最多暖场 4 次（且不超过 `daily_limit`）。
- `idle_threshold_minutes` 低于 5 分钟时按 5 分钟处理。
- 暖场 Prompt 会告诉模型已经安静了多久、已经说了几条没人接、提过哪些话题，并禁止接着自己上一条往下说、自问自答或说“怎么没人理我”。
- 主动发言与续聊发送前做近似撞重检查（只换了标点/语气词的复读也会被拦下）。

WebUI Agent 驾驶舱的「对话节奏 → 主动」会显示本段冷场已说几条没人接、今日暖场次数和当前门槛；暖场因为没人接被暂停时，健康检查会给出「冷场暖场已暂停」提示。`/Agent设置` 的状态摘要也会列出这些规则。

### P5：动态情绪

动态情绪使用已有 `emotion_state` JSON 保存，不新增 LLM 调用，也不新增用户画像字段。它描述的是 **Bot 自己的临时表达 stance**，不是对成员心理状态的诊断。

当前状态包括：`平静 / 亲和 / 愉快 / 好奇 / 关切 / 谨慎 / 轻微不耐`。系统只依据粗粒度交互信号更新，例如友好反馈、玩梗氛围、质疑、冲突语气或需要关切的表达；不会把原消息文本、用户 ID 或“某成员正在焦虑/生气”这类推断写入情绪状态。

- 直接 @/回复/唤醒 Agent 的事件影响更强；普通群聊氛围只产生较弱 ambient 影响。
- 状态约以 **35 分钟半衰期**自动回落到平静，长时间无事件会清空持久状态。
- 人设的“表现力”档位只决定同一情绪在文字里表现多少，不改变底层情绪状态；跟随全局时按全局人设的表现力计算，不读本群暂存草稿。
- `emotion_state` 只调整临时表达，不改变事实、记忆、权限、安全、工具能力或主动参与硬门槛。即使状态为“轻微不耐”，也不得攻击、羞辱、报复或升级冲突。
- 已通过 `/Agent隐私` 退出的成员消息在解析前就被拦截，因此不会写记忆，也不会影响动态情绪。
- WebUI 与 `/Agent人设 查看` 会展示当前动态情绪、强度、粗粒度原因与衰减状态。Debug 草稿只重新计算“当前 Persona 会把这份情绪表现到什么程度”，不会写回 `emotion_state`。

### 历史兼容（v2 时期）

v2 时期的数据库迁移曾执行一次性清理（v3 沿用其结果，没有再改表结构之外的旧数据）：

- 删除 `persona`、`persona_override`、`persona_schema_version` 三个旧列；运行时只读取 `persona_profile`。
- 旧 `tone / speech_style / temperament / response_length / values` 会尽量压缩到 `custom_notes`，避免角色文字静默丢失。
- 旧 `legacy_policy_fields`、`knowledge_boundary`、`privacy_boundary` 不再迁移进 Persona，因为它们已经由不可覆盖的 System Policy 接管。
- 旧群若尚未保存 P4 结构化 social traits，迁移会把原来的参与节奏显式物化为 v2 traits，再删除 runtime fallback。
- downgrade 仍提供 best-effort 回填，保证迁移链可以回退，但降级后的旧格式不再属于当前运行 API。

以下旧环境变量也已退休，不再作为 Persona 配置入口：`AGENT_PERSONA_IDENTITY`、`AGENT_PERSONA_ROLE`、`AGENT_PERSONA_TONE`、`AGENT_PERSONA_SPEECH_STYLE`、`AGENT_PERSONA_VALUES`、`AGENT_PERSONA_EMOTION_BASELINE`、`AGENT_PERSONA_RESPONSE_LENGTH`、`AGENT_PERSONA_KNOWLEDGE_BOUNDARY`、`AGENT_PERSONA_PRIVACY_BOUNDARY`。
