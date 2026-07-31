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

There is no test suite.

### Database migrations (nonebot-plugin-orm + Alembic)

**数据库迁移一律由用户手动执行，Claude 不要运行 `nb orm revision/upgrade/downgrade`。** 模型变更后只需提醒用户生成并应用迁移。

The SQLite database lives at `data/nonebot_plugin_orm/db.sqlite3` (`LOCALSTORE_USE_CWD=true` keeps data under the repo). Canonical migrations are in `data/nonebot_plugin_orm/migrations/yawn_core/`, with the werewolf sub-plugin (`bind_key=yawn_werewolf`) nested under `data/nonebot_plugin_orm/migrations/yawn_core/yawn_werewolf/` (the copies under `src/plugins/yawn_core/migrations/` and `src/plugins/yawn_core/yawn_werewolf/migrations/` are duplicates — do not edit those).

```bash
uv run nb orm revision -m "description"   # generate from model changes (autogenerate by default; --sql to disable)
uv run nb orm upgrade head                # apply migrations
uv run nb orm downgrade -1                # roll back one step
```

Notes: `nb orm revision` generates into a temp dir first and only syncs back to `data/` on `upgrade`; detecting `server_default` removals requires `ALEMBIC_CONTEXT={"compare_server_default": true}` in `.env` (already set).

## Architecture

### Plugin loading

`src/plugins/yawn_core/__init__.py` explicitly imports every feature module (`ai_chat`, `checkin`, `friend_approve`, `help_panel`, `panel`, `permission`, `presence`) — **a new module is not loaded until it is imported there**. The werewolf sub-plugin (`yawn_werewolf/`) is different: it is loaded dynamically by `_load_sub_plugins()` via `nonebot.load_plugin`, and a load failure is logged and skipped without taking down the other features. `__init__.py` also registers an `on_startup` hook that enables SQLite WAL mode + busy_timeout on all sqlite engines.

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

Private-chat-only conversational mode built on an OpenAI-compatible API (Xiaomi MiMo). `/对话` toggles a persistent "chat mode" per user: plain messages are enqueued into a per-user `asyncio.Queue` consumed by a background worker task (`chat_state.py`) that serializes AI calls and auto-exits after 10 min idle. The worker uses its **own DB session** (`get_session()`), not the handler's. History is persisted in `ChatSession`/`ChatMessage` (soft-deleted via `is_deleted`); `reply_chain.py` resolves QQ reply-quote chains into prompt context. The AI endpoint/key/model come from `AIChatConfig` in `llm.py` (`AI_API_KEY` required, `AI_BASE_URL`/`AI_MODEL`/`AI_MAX_TOKENS` overridable via `.env`).

### Shared LLM client (`llm.py`)

`ai_chat` (streaming) and the werewolf AI players (non-streaming) share one `AsyncOpenAI` singleton built from `AIChatConfig` at import time — a missing `AI_API_KEY` fails bot startup by design. `complete()` is the non-streaming path: total timeout via `asyncio.wait_for` (callers pass their own) plus a global `Semaphore(6)` concurrency cap; any failure/timeout/empty reply logs and returns `None`, so callers must degrade. The streaming path in `ai_chat.py` deliberately uses a per-chunk *idle* timeout instead of a total cap, because the reasoning model can be slow to produce the first byte — keep that asymmetry in mind when tuning timeouts.

### Werewolf sub-plugin (`yawn_werewolf/`)

Group-chat werewolf game (预女猎白 board, 9–12 players) with LLM-driven AI players. Two asyncio tasks per game: the **engine** (`engine.py`, `run_game`) is the sole owner of state mutation and group broadcast, while the **AI driver** (`ai_player.py`) consumes sync engine hooks (DM prompts, announcements, captured speeches), calls `llm.complete()`, parses replies through the shared command DSL (`dsl.py`), and injects `Action`s into `game.action_queue` — the engine treats AI and human actions identically. Command matchers (`commands.py`) only validate and enqueue. Every failure (LLM timeout, invalid target) degrades to a safe default (abstain/pass) and never stalls a game. See `yawn_werewolf/CLAUDE.md` for the module map, write-ownership rules, and AI timing conventions.

## Conventions

- Ruff is configured with an extensive ruleset (see `[tool.ruff.lint]`); E402/B008/UP037 are ignored because NoneBot2's `require()` and `Depends()` patterns violate them. `allowed-confusables` whitelists Chinese punctuation — keep it in sync if ruff flags new CJK characters.
- Long handlers routinely carry `# noqa: C901/PLR0915`-style suppressions; match the existing style rather than refactoring flagged functions incidentally.
