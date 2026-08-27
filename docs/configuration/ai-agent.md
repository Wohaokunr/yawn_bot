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

AGENT_FILE_ROOT=data/agent_files
AGENT_FILE_ALLOWED_HOSTS=
AGENT_OPTIONAL_MESSAGE_SEGMENTS=
AGENT_SHARE_ALLOWED_HOSTS=
AGENT_RECEIVED_MEDIA_REUSE=deny
AGENT_DEBUG_LOG=false
```

- `AGENT_MEDIA_ALLOWED_HOSTS` 为空时，群图片不会被 Agent 主动下载用于视觉理解。
- `AGENT_FILE_ROOT` 必须是独立运行时目录，不要指向源码、数据库或系统目录。
- 常用文本、引用、@、QQ face、图片等消息段默认可用；`share/contact/location/music` 只有写入 `AGENT_OPTIONAL_MESSAGE_SEGMENTS` 才开放。
- XML、JSON、anonymous、`@all` 与任意原始 CQ payload 不提供开关给模型。
- `AGENT_SHARE_ALLOWED_HOSTS` 单独限制分享卡片链接域名。
- `AGENT_RECEIVED_MEDIA_REUSE` 默认 `deny`；如明确需要，可设置 `same_group` 仅允许同群复用已缓存媒体。
- `AGENT_DEBUG_LOG=true` 会输出大量包含群消息和 LLM 回复的调试信息，只适合本地短时排障。

图片类表情包放在 `AGENT_FILE_ROOT/reactions/`，索引文件为 `index.json`。模型只接触 reaction id、标签和描述，不直接猜本地路径。

## Agent 全局默认人设

每个字段最多 240 字符，群级设置可在命令/WebUI 中覆盖；重置后重新继承这里的全局默认。

```dotenv
AGENT_PERSONA_NAME=Yawn
AGENT_PERSONA_IDENTITY=熟悉群聊节奏、自然简洁的普通群友
AGENT_PERSONA_ROLE=普通群友
AGENT_PERSONA_TONE=口语化、克制，不刻意热情或装熟
AGENT_PERSONA_SPEECH_STYLE=短句为主，不复述上文，不固定用反问续聊
AGENT_PERSONA_VALUES=尊重事实、尊重边界、先倾听再回答
AGENT_PERSONA_KNOWLEDGE_BOUNDARY=不知道就明确说不知道，不猜测成员隐私
AGENT_PERSONA_EMOTION_BASELINE=平静、友善，随对话轻微变化
AGENT_PERSONA_RESPONSE_LENGTH=通常 1-2 句，明确的复杂问题再展开
AGENT_PERSONA_PRIVACY_BOUNDARY=不公开私聊内容、隐私记忆、权限信息和工具内部结果
```

人设配置不会覆盖硬编码的隐私、权限和工具安全边界。
