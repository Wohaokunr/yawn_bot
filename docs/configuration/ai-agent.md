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
```

`*_THINKING` 可用 `auto / enabled / disabled`。多模态可用 `auto / supported / unsupported`。light 模型留空时继承 default；vision 留空表示没有独立视觉降级档位。

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

## Agent 媒体与文件

```dotenv
AGENT_MEDIA_CACHE_TTL=86400
AGENT_MEDIA_CACHE_DIR=data/agent_media
AGENT_MEDIA_ALLOWED_HOSTS=gchat.qpic.cn,multimedia.nt.qq.com.cn
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
```

- `AGENT_MEDIA_ALLOWED_HOSTS` 默认仅允许 `gchat.qpic.cn` 与 `multimedia.nt.qq.com.cn`；显式置空时，群图片不会被 Agent 主动下载用于视觉理解。
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

图片类表情包放在 `AGENT_FILE_ROOT/reactions/`，索引文件为 `index.json`。模型只接触 reaction id、标签和描述，不直接猜本地路径。

### 群文件发送

- 要让 Agent 能发的文件直接放进 `AGENT_FILE_ROOT`（可以有子目录，`reactions/` 和以 `.` 开头的路径除外）。模型先用 `list_agent_files` 看到相对文件名，再用 `send_file` 上传，不会接触绝对路径。
- `send_file` 可以带 `folder_id` 上传到指定群文件夹；显示名会去掉路径和非法字符，没写扩展名时自动补上源文件扩展名。
- exe、bat、ps1、vbs、apk、jar、sh 等可执行/脚本类文件一律拒绝发送。
- 远程链接仍然只允许 `AGENT_FILE_ALLOWED_HOSTS` 里的域名；重定向会逐跳重新校验白名单，跳出白名单的下载直接拒绝。

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
