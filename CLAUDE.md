# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

YawnBot is a QQ chat bot built on **NoneBot2** with the **OneBot V11** adapter. Python ≥3.10, dependencies managed by **uv**. All bot logic lives in a single local plugin package, `src/plugins/yawn_core`. Comments, docstrings, and user-facing text are in Chinese.

## Commands

```bash
uv sync                        # install dependencies (add --extra dev for pyright/ruff)
uv run nb run --reload         # run the bot in dev mode (hot reload)
uv run ruff check src          # lint
uv run ruff format src         # format (LF line endings, 88 cols)
uv run pyright src             # type check (standard mode, python 3.9 target in config)
```

There is no project-wide test suite; `yawn_rpg` has focused tests under
`src/plugins/yawn_core/yawn_rpg/tests/`. Run them with the project virtual
environment when changing RPG behavior.

### Database migrations (nonebot-plugin-orm + Alembic)

**数据库迁移一律由用户手动执行，Claude 不要运行 `nb orm revision/upgrade/downgrade`。** 模型变更后只需提醒用户生成并应用迁移。

The SQLite database lives at `data/nonebot_plugin_orm/db.sqlite3` (`LOCALSTORE_USE_CWD=true` keeps data under the repo). Canonical migrations are in `data/nonebot_plugin_orm/migrations/yawn_core/`, with the game sub-plugins nested per bind_key (`data/nonebot_plugin_orm/migrations/yawn_core/yawn_werewolf/`, `.../yawn_rpg/`) (the copies under `src/plugins/yawn_core/*/migrations/` are duplicates — do not edit those).

```bash
uv run nb orm revision -m "description"   # generate from model changes (autogenerate by default; --sql to disable)
uv run nb orm upgrade head                # apply migrations
uv run nb orm downgrade -1                # roll back one step
```

Notes: `nb orm revision` generates into a temp dir first and only syncs back to `data/` on `upgrade`; detecting `server_default` removals requires `ALEMBIC_CONTEXT={"compare_server_default": true}` in `.env` (already set).

## Architecture

### Plugin loading

`src/plugins/yawn_core/__init__.py` explicitly imports every feature module (`ai_chat`, `checkin`, `friend_approve`, `help_panel`, `panel`, `permission`, `presence`) — **a new module is not loaded until it is imported there**. The game sub-plugins (`yawn_werewolf/`, `yawn_rpg/`) are different: they are loaded dynamically by `_load_sub_plugins()` (iterating a known name list) via `nonebot.load_plugin`, and a load failure is logged and skipped without taking down the other features. Every attempt is retained in `get_sub_plugin_load_report()`, and startup emits a summary; keep this report in sync when adding optional sub-plugins. `__init__.py` also registers an `on_startup` hook that enables SQLite WAL mode + busy_timeout on all sqlite engines.

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
