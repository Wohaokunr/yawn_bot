# 跑团子插件深度整合进 WebUI

范围（用户已确认全选）：①对局详情观战 ②私密信息视角 ③模组库页面 ④赛后回放，写操作边界扩展为「只读+强停+行动投递」。

## 设计原则（项目约束）
- 只读走内存快照；一切写操作经 `submit_action()` 入队、引擎单写，复刻 `commands._action()` 的阶段/场景/探索轮/战斗轮快照，不直接改局内状态（AGENTS.md 契约）。
- WebUI 是管理员通道：私密信息（HP/SAN、个人线索手记、NPC 秘密）只进 WebUI API、前端默认遮蔽（沿用狼人杀"完整信息+前端遮蔽"先例），绝不进任何群播报路径。
- 子插件模块一律延迟解析 + 失败降级（`_rpg_state()` 模式）；写请求自动进现有审计中间件。

## 一、后端：`webui/games.py` 扩展

1. 新增延迟解析器 `_rpg_engine()`（`..yawn_rpg.engine`）、`_rpg_config()`（`get_plugin_config(yawn_rpg Config)`，fanqie.py 同款）。
2. `GET /games/rpg/{group_id}/detail`（ReadSession）：
   - `game`：复用 `_rpg_live_game`（公共口径，与 /games/live 一致）
   - `players`：管理员完整视角（seat/userId/charName/confirmed/incapped/**hp/san**/rerollsLeft/dmOk）
   - `situationText`=public_situation_text、`clueBoardText`=clue_board_text、`groupLog`=list(game.group_log)（≤200 条）、`signupUserIds`、`pendingDeduction`（proposerUserId/clueIds/conclusion/confirmations 转 sorted list）、`completedDeductions`
3. `GET /games/rpg/{group_id}/players/{user_id}/private`（ReadSession）：`situationText`=private_situation_text、`journalText`=private_journal_text；非在局玩家 404。
4. `POST /games/rpg/{group_id}/actions`（WriteSession），请求体 `RpgActionSubmit{userId:int, kind, text?, minutes?}`：
   - 允许 kind：PLAY 期 `SAY`/`WAIT`/`PASS_TURN`（actor=指定在局玩家，authority="player"，占其配额，与群内自由发言同路径）；SIGNUP 期 `MODULE_SELECT`/`START_GAME`（authority="admin"，actor=房主；engine 已有 admin 分支豁免房主校验）
   - 模块级 `_rpg_action_snapshot()` 复刻 commands._action 快照逻辑（注释标注两处同步维护）
   - `submit_action()` + config 配额；SubmitResult→HTTP：ACCEPTED→ok + hub.notify_change；STALE/DUPLICATE→409（中文 message）；QUEUE_FULL/USER_LIMIT→429

## 二、后端：新文件 `webui/rpg_modules.py`（模组库）
- `_rpg_module_schema()` 延迟解析；`GET /rpg/modules` 列表（id/name/description/difficulty/min-max players/startScene + 场景/NPC/怪物/线索/推论/结局/事件计数）；`GET /rpg/modules/{module_id}` 详情（场景含 narration/exits/检查点、NPC 含 facts 与社交概要、怪物、线索全文、推论 required/grant、结局 outcome、具名事件、time/generic_endings；管理员视角不过滤剧透）
- `app.py` register() 中 include_router。

## 三、回放持久化缺口（当前 RPGGame 表无 event_log_id，赛后无法定位 JSONL）
- `yawn_rpg/models.py`：RPGGame 加 `event_log_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)`（业务列禁 server_default）
- 新 alembic 迁移（`data/nonebot_plugin_orm/migrations/yawn_core/yawn_rpg/`），`nb orm upgrade` + `nb orm check` 一起验证
- `engine._persist_start` 写入 `game.event_log_id`
- `games.py`：rpg_history 序列化加 `eventLogId`；新端点 `GET /games/rpg/history/{row_id}/replay`：查行→无 event_log_id 404（旧局优雅降级）→ `load_replay(event_log_id, game_kind="rpg", view="public").as_dict()`（replay.py 属父插件包，直接 import）

## 四、前端
- `types.ts`：RpgDetailPlayer/RpgGameDetail/RpgPlayerPrivate/RpgActionKind/RpgModuleSummary/RpgModuleDetail/RpgReplay；RpgHistoryGame 加 `eventLogId`
- `games.tsx`：
  - RpgTab 加「管理台显示私密状态」Switch + viewGroupId；RpgLiveCard 加「进入对局」按钮
  - 新 `RpgGameDrawer`（2.5s 轮询 detail，页面隐藏暂停，同 WerewolfGameDrawer）：节奏 Descriptions、公共局面/线索板 `<pre>`、玩家表（HP/SAN 默认 🔒 遮蔽，reveal 后显示且行展开懒加载 private 端点显示私密局面/手记）、行动日志 Timeline（groupLog 倒序）、底部行动投递表单（选玩家+按 phase 过滤的类型选项+文本；选项构造纯函数 `rpgActionOptions(phase)` 导出供测试）
  - 战绩表加「回放」按钮（ended && eventLogId）→ `RpgReplayDrawer`：summary 头 + 事件 Timeline + warnings Alert
- 新文件 `modules.tsx`：`/modules` 路由 + 侧边栏「模组库」菜单（BookOutlined）；列表 + 详情 Drawer（Descriptions + 各实体 Tabs/Table）
- `App.tsx`：注册路由与菜单

## 五、测试与质量门
- 新 `tests/test_webui_rpg.py`（FastAPI()+register+TestClient 模式）：detail 字段与私密口径、private 200/404、actions 入队成功/DUPLICATE 409/STALE 409/清理（discard_game）、modules 列表非空与 404、replay（临时 JSONL 写入 get_event_log_dir() 后清理；无 event_log_id 404）
- 前端 Vitest：rpgActionOptions；`npm run typecheck + test + build`
- 后端：pytest、Ruff、Pyright；注意 Mimosa 钩子对 ORM 一行式 select 误报（用 clauses 列表写法）、Windows 编辑保持 LF

## 执行顺序
1. models+迁移+engine 写 event_log_id（先打通回放数据缺口）
2. games.py 四个新端点 + history 字段
3. rpg_modules.py + app.py 注册
4. 前端 types/games/modules/App
5. 测试与全部检查、npm build

## 实施状态（2026-08-22）
- [x] RPGGame 增加 `event_log_id`，迁移已升级并通过 ORM check；引擎开局持久化回放编号。
- [x] WebUI 对局详情、玩家私密信息、行动投递和公开赛后回放端点已完成。
- [x] 模组库延迟解析、列表/详情 API 与前端只读模组页面已完成。
- [x] 对局抽屉、私密信息默认遮蔽、行动表单、回放抽屉及侧边栏路由已完成。
- [x] 后端 FastAPI/TestClient 回归与 RPG 回归、Ruff、Pyright、compileall 已完成。
- [x] 前端 Vitest、TypeScript typecheck 与生产构建已完成。
