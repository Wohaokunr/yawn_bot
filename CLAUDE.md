# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

YawnBot is a QQ chat bot built on **NoneBot2** with the **OneBot V11** adapter. Python ≥3.10, dependencies managed by **uv**. All bot logic lives in a single local plugin package, `src/plugins/yawn_core`. Comments, docstrings, and user-facing text are in Chinese.

## 项目结构与文档导航

本仓库的 `CLAUDE.md` 是分层的：本文件给全局规则与目录总览，各重要目录下的 `CLAUDE.md` 讲该目录的职责、结构和关键文件，并互相链接。进入某个目录工作前，先读对应的 `CLAUDE.md`。

| 路径 | 职责 | 文档 |
|---|---|---|
| `src/plugins/yawn_core/` | 唯一的本地 NoneBot 插件：核心功能模块 + 可选子插件 | [yawn_core/CLAUDE.md](src/plugins/yawn_core/CLAUDE.md) |
| `├─ data_models/` | yawn_core 与 Agent 共用的 ORM 模型 | [data_models/CLAUDE.md](src/plugins/yawn_core/data_models/CLAUDE.md) |
| `├─ webui/` | WebUI 后端（FastAPI，`/webui/api/v1`） | [webui/CLAUDE.md](src/plugins/yawn_core/webui/CLAUDE.md) |
| `├─ yawn_agent/` | 群聊 Agent 子插件 | [yawn_agent/CLAUDE.md](src/plugins/yawn_core/yawn_agent/CLAUDE.md)（约束见同目录 [AGENTS.md](src/plugins/yawn_core/yawn_agent/AGENTS.md)） |
| `├─ yawn_werewolf/` | 狼人杀子插件 | [yawn_werewolf/CLAUDE.md](src/plugins/yawn_core/yawn_werewolf/CLAUDE.md) |
| `├─ yawn_rpg/` | CoC 跑团子插件（AI KP + YAML 模组） | [yawn_rpg/CLAUDE.md](src/plugins/yawn_core/yawn_rpg/CLAUDE.md) |
| `└─ yawn_fanqie/` | 番茄小说公开内容下载子插件 | [yawn_fanqie/CLAUDE.md](src/plugins/yawn_core/yawn_fanqie/CLAUDE.md) |
| `webui/` | WebUI 前端（React 19 + antd + Vite），构建产物 `webui/dist` 由后端托管 | [webui/CLAUDE.md](webui/CLAUDE.md) |
| `tests/` | 主 pytest 套件（运行时、插件、WebUI 后端、部署契约） | [tests/CLAUDE.md](tests/CLAUDE.md) |
| `tools/` | 仓库卫生/许可证审计脚本、RPG 模组编辑器与离线试玩器 | [tools/CLAUDE.md](tools/CLAUDE.md) |
| `data/` | SQLite 与 **迁移正本**（`data/nonebot_plugin_orm/migrations/`） | [data/CLAUDE.md](data/CLAUDE.md) |
| `deploy/`、`Dockerfile`、`compose.yaml` | Docker 镜像、NapCat、生产发布与主机初始化 | [deploy/CLAUDE.md](deploy/CLAUDE.md) |
| `docs/` | 配置参考、部署、设计说明与路线图 | [docs/CLAUDE.md](docs/CLAUDE.md) |
| `.github/workflows/` | CI（`ci.yml`，Python 3.10 跑 ruff/pyright/pytest + WebUI 测试构建）、发布、部署、审计 | 见 [deploy/CLAUDE.md](deploy/CLAUDE.md) 与 [tests/CLAUDE.md](tests/CLAUDE.md) |
| `AGENTS.md` | RPG 与 RPG 模组编辑器的维护约束（下方已导入） | [AGENTS.md](AGENTS.md) |

根目录其他文件：`pyproject.toml`（依赖、NoneBot 插件声明、ruff/pyright/pytest 配置）、`.env.example`（最小可运行配置；完整配置项目录在 `src/plugins/yawn_core/webui/environment_catalog.dotenv`，说明在 [docs/configuration/](docs/configuration/README.md)）、`README.md` / `CONTRIBUTING.md` / `SECURITY.md`（面向使用者与贡献者）。

### 跨目录关联速查

相距较远但必须一起改的位置：

- **ORM 模型 ↔ 迁移**：`src/plugins/yawn_core/data_models/`、各子插件 `models.py` ↔ `data/nonebot_plugin_orm/migrations/`（迁移由用户手动执行，见下文）。
- **WebUI 前后端**：`webui/src/*.tsx` 通过 `webui/src/api.ts` 调用 `src/plugins/yawn_core/webui/*_routes.py`；后端 `webui/config.py` 从 `webui/dist` 托管前端。
- **配置项**：插件 `config.py` 的字段 ↔ `webui/environment_catalog.dotenv`（`tests/test_webui_environment.py` 会检查）↔ `docs/configuration/*.md` ↔ `.env.example`。
- **RPG 模组**：`yawn_rpg/modules/*.yaml` + `yawn_rpg/module_schema.py` ↔ `tools/rpg_module_editor/`、`tools/rpg_playtest/` ↔ WebUI `webui/rpg_modules.py` 与前端 `webui/src/modules.tsx`。
- **对局记录**：狼人杀/跑团引擎 ↔ `yawn_core/event_log.py`、`replay.py`、`metrics.py` ↔ WebUI `webui/games.py` 与前端 `webui/src/games.tsx`；设计见 [docs/game-event-log.md](docs/game-event-log.md)、[docs/game-replay.md](docs/game-replay.md)。
- **共享 LLM**：`yawn_core/llm.py` 被 `ai_chat`、`yawn_agent`、`yawn_rpg`、`yawn_werewolf` 共用。
- **发布链路**：`Dockerfile` / `deploy/` ↔ `.github/workflows/release.yml`、`deploy-existing.yml` ↔ `tests/test_production_*.py`、`tests/test_deployment_cache_contract.py`。

@AGENTS.md

## Commands

```bash
uv sync                        # install dependencies (add --extra dev for pyright/ruff)
uv run nb run --reload         # run the bot in dev mode (hot reload)
uv run ruff check src          # lint
uv run ruff format src         # format (LF line endings, 88 cols)
uv run pyright src             # type check (standard mode, python 3.9 target in config)
```

Tests: `uv run pytest -q` runs the default suite (`testpaths` = `tests/` +
`src/plugins/`, including `yawn_rpg/tests/`). The RPG editor/playtest suites
under `tools/` are run explicitly by path. CI uses Python 3.10. See
[tests/CLAUDE.md](tests/CLAUDE.md) for the layout and which tests to run for
each area.

### Database migrations (nonebot-plugin-orm + Alembic)

**数据库迁移一律由用户手动执行，Claude 不要运行 `nb orm revision/upgrade/downgrade`。** 模型变更后只需提醒用户生成并应用迁移。

The SQLite database lives at `data/nonebot_plugin_orm/db.sqlite3` (`LOCALSTORE_USE_CWD=true` keeps data under the repo). Canonical migrations are in `data/nonebot_plugin_orm/migrations/yawn_core/`, with the game sub-plugins nested per bind_key (`data/nonebot_plugin_orm/migrations/yawn_core/yawn_werewolf/`, `.../yawn_fanqie/`); the main-branch (RPG, reminders) revisions plus some follow-ups of other branches sit directly in `data/nonebot_plugin_orm/migrations/` — branch membership is decided by `down_revision`, not by directory (see [data/CLAUDE.md](data/CLAUDE.md)) (the copies under `src/plugins/yawn_core/*/migrations/` are duplicates — do not edit those).

```bash
uv run nb orm revision -m "description"   # generate from model changes (autogenerate by default; --sql to disable)
uv run nb orm upgrade head                # apply migrations
uv run nb orm downgrade -1                # roll back one step
```

Notes: `nb orm revision` generates into a temp dir first and only syncs back to `data/` on `upgrade`; detecting `server_default` removals requires `ALEMBIC_CONTEXT={"compare_server_default": true}` in `.env` (already set).

Warnings (learned from the a0445ad1df97 incident, 2026-08):

- With multiple branch heads, `nb orm revision --autogenerate` picks the base head **without regard to which branch owns the changed tables** — yawn_core model changes once got chained onto the main/RPG branch, producing a migration that altered `yawn_core_*` tables before their prerequisites existed and broke fresh installs. After any autogen, verify the new revision's `down_revision` is on the branch that owns the touched tables; otherwise hand-write the migration into `data/nonebot_plugin_orm/migrations/yawn_core/` with `down_revision` set to that branch's head.
- `migrations/versions/` at the repo root is the ORM's default primary version location. Every `nb orm` command builds its script directory in a temp dir by merging `data/nonebot_plugin_orm/migrations/`, each plugin's `src/**/migrations/`, and `migrations/versions/` — so deleting a migration requires removing it from **every** location, or the next command resurrects it.
- **`upgrade`/`downgrade`/`stamp`/`sync` delete migration files on completion** (learned from a 2026-08-22 incident): internally `_move_run_scripts` rmtrees `data/nonebot_plugin_orm/migrations/` and copies back only files reachable as ancestors of the command's *destination* revisions. Any destination that doesn't cover all branches therefore **silently deletes the uncovered branches' migration files** (e.g. `stamp <main> <yawn_core>` wiped the entire werewolf and fanqie chains). Prefer full-coverage destinations like `heads`; if a partial destination is unavoidable, back up `data/nonebot_plugin_orm/migrations/` first. `current`/`check`/`heads`/`history` are read-only and never delete files.

## Architecture

### Plugin loading

`src/plugins/yawn_core/__init__.py` explicitly imports every feature module (`ai_chat`, `checkin`, `friend_approve`, `help_panel`, `panel`, `permission`, `presence`, `reminder`, `replay`, … — see the import list there) — **a new module is not loaded until it is imported there**. The sub-plugins (`yawn_werewolf/`, `yawn_rpg/`, `yawn_fanqie/`, `yawn_agent/`) are different: they are loaded dynamically by `_load_sub_plugins()` (iterating `_SUB_PLUGIN_SPECS`) via `nonebot.load_plugin`, and a load failure is logged and skipped without taking down the other features. Every attempt is retained in `get_sub_plugin_load_report()`, and startup emits a summary; keep this report in sync when adding optional sub-plugins. `__init__.py` also registers an `on_startup` hook that enables SQLite WAL mode + busy_timeout on all sqlite engines.

Third-party nonebot plugins must be declared in `pyproject.toml` under `[tool.nonebot.plugins]` (package → module mapping); adapters under `[tool.nonebot.adapters]`.

### Data models (`data_models/`)

All ORM classes subclass `nonebot_plugin_orm.Model` (SQLAlchemy 2.0 `Mapped`/`mapped_column` style). Table names are auto-prefixed with the plugin name (e.g. `UserGroup` → `yawn_core_usergroup`) — foreign keys in `__table_args__` must reference these prefixed names. Models use naive Beijing-time datetimes (`datetime.now(UTC+8).replace(tzinfo=None)`); keep this convention. New model modules must be imported in `data_models/__init__.py` to be registered.

### Permission system (`permission.py`)

Three-level feature gating. A feature is gated by adding its key to `FEATURE_REGISTRY`, then injecting `require_feature("<key>")` into handlers:

```python
@matcher.handle()
async def handler(event, session, _perm: None = require_feature("checkin")) -> None: ...
```

The type annotation **must be `None`** (not `Dependent`) or Pydantic validation fails. Group-chat resolution chain: `UserFeature` (per-user override) → `GroupFeature` (per-group) → default-allow. Private-chat chain: `GlobalUserFeature` → default-allow. Superusers are also subject to feature switches. Absence of a record means *allowed* — records only exist to deny/override.

### Command metadata drives the help panel

Every feature module declares `__plugin_meta__ = PluginMetadata(...)` with an `extra["commands"]` list (name, aliases, description, feature key, scope, superuser flag). `help_panel.py` scans submodules at runtime and filters these by the permission system to render `/help`. When adding a command, register it here too.

### Presence tracking (`presence.py`)

An `event_preprocessor` upserts `BotUser` / `BotGroup` / `UserGroup` on every message event — this is how users and groups enter the database, and what checkin/permission records hang foreign keys off of.

### AI chat (`ai_chat.py`, `chat_state.py`, `reply_chain.py`)

Private-chat-only conversational mode built on an OpenAI-compatible API (Xiaomi MiMo). `/对话` toggles a persistent "chat mode" per user: plain messages are enqueued into a per-user `asyncio.Queue` consumed by a background worker task (`chat_state.py`) that serializes AI calls and auto-exits after 10 min idle. The worker uses its **own DB session** (`get_session()`), not the handler's. History is persisted in `ChatSession`/`ChatMessage` (soft-deleted via `is_deleted`); `reply_chain.py` resolves QQ reply-quote chains into prompt context. The AI endpoint/key/model come from `AIChatConfig` in `llm.py` (`AI_API_KEY` is optional at startup; `AI_BASE_URL`/`AI_MODEL`/`AI_MAX_TOKENS` are overridable via `.env`).

### Shared LLM client (`llm.py`)

`ai_chat` (streaming), RPG, and the werewolf AI players (non-streaming) share one lazily constructed `AsyncOpenAI` client from `AIChatConfig`. Importing `yawn_core` does not require `AI_API_KEY`; the first actual AI call creates the client, while a missing/blank key follows the same unavailable-service fallback as other LLM failures. `complete()` is the non-streaming path: total timeout via `asyncio.wait_for` (callers pass their own) plus a global `Semaphore(6)` concurrency cap; any failure/timeout/empty reply logs and returns `None`, so callers must degrade. The streaming path in `ai_chat.py` deliberately uses a per-chunk *idle* timeout instead of a total cap, because the reasoning model can be slow to produce the first byte — keep that asymmetry in mind when tuning timeouts.

### Werewolf sub-plugin (`yawn_werewolf/`)

Group-chat werewolf game with LLM-driven AI players. Four boards in the `roles.BOARDS` registry — 预女猎白 (9–12 players) plus three 12-player boards (预女猎白混 with a 混血儿 who wins iff its chosen owner's faction wins; 禁言骑士/禁票骑士 with a nightly silence-or-vote-ban elder and a dueling 骑士); the host switches board via `/板子` during signup, and signup caps, AI autofill targets, and the deal-time headcount guard all derive from the selected board. Two asyncio tasks per game: the **engine** (`engine.py`, `run_game`) is the sole owner of state mutation and group broadcast, while the **AI driver** (`ai_player.py`) consumes sync engine hooks (DM prompts, announcements, captured speeches), calls `llm.complete()`, parses replies through the shared command DSL (`dsl.py`), and injects `Action`s into `game.action_queue` — the engine treats AI and human actions identically. Command matchers (`commands.py`) only validate and enqueue. Every failure (LLM timeout, invalid target) degrades to a safe default (abstain/pass) and never stalls a game. Group broadcasts are leak-safe by design: night progress is role-blind ambient lines (a missing per-role announcement would reveal role status) and every vote ends with a full tally broadcast. See `yawn_werewolf/CLAUDE.md` for the module map, write-ownership rules, and AI timing conventions.

### RPG sub-plugin (`yawn_rpg/`)

Group-chat CoC 7e TRPG with an AI game master (KP). One asyncio task per game: the **engine** (`engine.py`, `run_game`) is again the sole owner of state mutation and group broadcast. Unlike werewolf there is **no separate AI driver task** — the KP is a single narrator, so the engine runs an inline tool-calling agent loop (`run_kp_turn`): it calls `llm.complete_with_tools()` (shared client, added for this plugin), and **every AI↔system interaction goes through tool_calls** (`request_check`, `san_check`, `transition_scene`, `grant_clue`, `speak_as_npc`, `monster_attack`, `end_session`, `query_story`, …) which the engine's `execute_tool` fully validates against the scenario module before executing — the AI never touches state, and all dice/HP/SAN are resolved by the system (`dice.py`). KP and NPC viewpoints are separated: the KP only supplies an *intent* for NPC speech, and actual NPC lines are generated by a separate stateless NPC agent (`ai_npc.py`) from that NPC's own persona/knows/secrets; the KP prompt carries a once-per-game scenario overview (all ending/event names) and can query ending backstories via `query_story`, while per-turn prompts hold only spoiler-free current-scene data. Player messages arriving mid-KP-turn are absorbed between tool rounds (injected into the conversation; other commands buffered and run after the narration) so the system and KP stay in step. Game logic is driven by pre-configured YAML scenario modules (`modules/*.yaml`, validated by `module_schema.py`) that bound what the KP may do. Character sheets are system-generated and tweaked by players in private chat during `CHAR_CREATE`. Degradation: `RPG_AI_ENABLED=false` or any LLM failure falls back to deterministic keyword-triggered checks + canned module text (NPC lines fall back to `fallback_line`), so a game never stalls. See `yawn_rpg/CLAUDE.md` for the tool catalog, validation rules, and the anti-spoiler prompt boundary.

Player-flow rules: `/局面` is a read-only public/private projection — group output may contain only scene, clock, roster, exits, public clues, combat order/current actor, and pending public actions; HP/SAN, personal clues, private NPC facts, and relationship bands are sent only to the requesting player by DM. `/线索` follows the same visibility boundary. Signup and character creation expose a monotonic stage deadline; insufficient `/开始游戏` feedback leaves signup open. Exploration uses a soft round, but its timeout starts only while all action buffers are empty and no player input is being processed. Actions are rejected by phase/scene/exploration-round/combat-round/current-actor snapshots, never by wall-clock queue age; invalid actions return `executed=False` and do not consume the main action, while a rolled check or real NPC refusal does. SAY messages are collected for the settle window before concurrent routing, preserving source order and using the first NPC route as the KP batch boundary. The one-shot AI wait notice is ephemeral and excluded from `group_log`. Keep these rules intact when changing `engine.py` or `commands.py`.

## Conventions

- Ruff is configured with an extensive ruleset (see `[tool.ruff.lint]`); E402/B008/UP037 are ignored because NoneBot2's `require()` and `Depends()` patterns violate them. `allowed-confusables` whitelists Chinese punctuation — keep it in sync if ruff flags new CJK characters.
- Long handlers routinely carry `# noqa: C901/PLR0915`-style suppressions; match the existing style rather than refactoring flagged functions incidentally.
