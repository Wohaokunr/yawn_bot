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

## Agent Persona v2

Persona v2 把**角色、临时情绪、系统策略**拆成三个不同层级：

1. **System Policy**：事实性、隐私、权限、工具安全与 Prompt 注入防护，不允许 Persona 覆盖。
2. **Persona Profile**：稳定角色设定，包括模板、身份、说话风格、社交倾向与自定义补充。
3. **Dynamic Emotion**：随近期群聊事件短暂变化的 Bot 表达状态，会自动衰减，只影响措辞和表现力。

全局只保留一个轻量环境变量用于默认名字：

```dotenv
AGENT_PERSONA_NAME=Yawn
```

详细角色风格由 Persona v2 的结构化模板决定。内置模板包括：`自然群友`、`温和倾听`、`冷静理性`、`活跃捧哏`、`安静潜水`。每个群可以继续微调：

- 说话风格：温和程度、幽默程度、直接程度、回复详略、表现力。
- 社交倾向：社交活跃度、续聊倾向、接梗/反应倾向。
- 身份文字：名字、身份定位、群内角色，以及一段最多 240 字符的自定义补充。

QQ 使用 `/Agent人设` 进入“模板 → 风格/社交微调 → 预览确认”的会话入口。P6 起不再支持 `/Agent人设 设置 key=value ...`，WebUI 也不再接受 flat `overrides`；Persona 的唯一写入格式是结构化 `persona_profile`。WebUI 可以把**尚未保存的 Persona 草稿**交给 Agent Debug 试演，试演复用真实 `build_messages()` 和可选真实模型调用，但不会保存 Persona、执行工具、发送 QQ、写入记忆、修改动态情绪或主动参与状态。

### P4：Persona 控制真实群聊行为

Persona 的社交特征会进入真实控制流，而不只是写进 Prompt。优先级固定为：**System Policy → 群运行配置硬门槛/上限 → Persona 行为倾向 → 当前上下文**。Persona 只能在管理员已经允许的范围内进一步收窄行为。

- `sociability` 缩放暖场/热闹插话候选概率。0–4 档对应 `0.15 / 0.45 / 0.75 / 0.90 / 1.00`。
- `followup_tendency` 限制一个短会话中 Bot 的总发言数。0–4 档对应 `1 / 2 / 3 / 4 / 4`；1 表示本轮回答后不再自动续聊。
- `reaction_tendency` 控制主动参与/自动续聊时是否允许 Agent 自发使用 reaction。0–1 档会过滤自发 reaction；用户明确要求发送表情包时仍走普通 dialogue 工具路径。

P6 删除了 runtime 的 v1 fallback。升级迁移会把旧群的历史参与节奏显式写成最终 v2 social traits，因此兼容只存在于 migration 中，运行时没有 `legacy` Persona 分支。

### P5：动态情绪

动态情绪使用已有 `emotion_state` JSON 保存，不新增 LLM 调用，也不新增用户画像字段。它描述的是 **Bot 自己的临时表达 stance**，不是对成员心理状态的诊断。

当前状态包括：`平静 / 亲和 / 愉快 / 好奇 / 关切 / 谨慎 / 轻微不耐`。系统只依据粗粒度交互信号更新，例如友好反馈、玩梗氛围、质疑、冲突语气或需要关切的表达；不会把原消息文本、用户 ID 或“某成员正在焦虑/生气”这类推断写入情绪状态。

- 直接 @/回复/唤醒 Agent 的事件影响更强；普通群聊氛围只产生较弱 ambient 影响。
- 状态约以 **35 分钟半衰期**自动回落到平静，长时间无事件会清空持久状态。
- Persona 的 `expressiveness` 只决定同一情绪在文字里表现多少，不改变底层情绪状态。
- `emotion_state` 只调整临时表达，不改变事实、记忆、权限、安全、工具能力或主动参与硬门槛。即使状态为“轻微不耐”，也不得攻击、羞辱、报复或升级冲突。
- 已通过 `/Agent隐私` 退出的成员消息在解析前就被拦截，因此不会写记忆，也不会影响动态情绪。
- WebUI 与 `/Agent人设 查看` 会展示当前动态情绪、强度、粗粒度原因与衰减状态。Debug 草稿只重新计算“当前 Persona 会把这份情绪表现到什么程度”，不会写回 `emotion_state`。

### P6：历史兼容债务清理

P6 的数据库迁移在 Persona v2 数据落稳后执行一次性清理：

- 删除 `persona`、`persona_override`、`persona_schema_version` 三个旧列；运行时只读取 `persona_profile`。
- 旧 `tone / speech_style / temperament / response_length / values` 会尽量压缩到 `custom_notes`，避免角色文字静默丢失。
- 旧 `legacy_policy_fields`、`knowledge_boundary`、`privacy_boundary` 不再迁移进 Persona，因为它们已经由不可覆盖的 System Policy 接管。
- 旧群若尚未保存 P4 结构化 social traits，迁移会把原来的参与节奏显式物化为 v2 traits，再删除 runtime fallback。
- downgrade 仍提供 best-effort 回填，保证迁移链可以回退，但降级后的旧格式不再属于当前运行 API。

以下旧环境变量也已退休，不再作为 Persona 配置入口：`AGENT_PERSONA_IDENTITY`、`AGENT_PERSONA_ROLE`、`AGENT_PERSONA_TONE`、`AGENT_PERSONA_SPEECH_STYLE`、`AGENT_PERSONA_VALUES`、`AGENT_PERSONA_EMOTION_BASELINE`、`AGENT_PERSONA_RESPONSE_LENGTH`、`AGENT_PERSONA_KNOWLEDGE_BOUNDARY`、`AGENT_PERSONA_PRIVACY_BOUNDARY`。
