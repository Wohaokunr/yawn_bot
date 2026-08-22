# 番茄小说子插件接入 WebUI

范围(已确认):完整接入 —— WebUI 新增「番茄小说」页面,支持搜索/榜单/链接选书 → 选章节范围提交下载任务(提交时手填接收人 QQ,可选群号),外加任务管理(列表/详情/取消/重试/发送/删除)。

## 后端:新增 `src/plugins/yawn_core/webui/fanqie.py`

完全照抄 `games.py` 的领域路由范式:

- `router = APIRouter(prefix=API_PATH)`,复用 `deps.py` 的 `ReadSession`/`WriteSession`/`ok`/`page_params`
- 延迟解析子插件模块(games.py 模式,成功才缓存、失败不落缓存):`_fanqie_state()`(→ `..yawn_fanqie.state`)、`_fanqie_provider_mod()`(→ `..yawn_fanqie.provider`)、`_fanqie_config()`(→ Config 实例);子插件未加载时所有端点 503 优雅降级
- 每个外部请求独立 `async with FanqieProvider(config) as provider:`(与 commands.py 一致;搜索已被子插件 browser_search 的 `_SEARCH_LOCK` 全局串行,与群聊命令并发安全)

端点:

| 端点 | 说明 |
|---|---|
| `GET /fanqie/status` | available + limits(maxChapters/userActiveMax/groupActiveMax/queueMax/searchLimit/rankLimit 等,读子插件 Config)+ 活动任务计数(queued/running,ORM count) |
| `GET /fanqie/jobs` | 分页列表;筛选 status(all/queued/running/completed/failed/cancelled)、search(书名/作者/QQ/任务号);join FanqieBook 取书名作者,群名复用 games 的 `_group_names` 模式 |
| `GET /fanqie/jobs/{id}` | 任务详情 + 全部章节行(chapterIndex/title/itemId/isLocked/status/lastError/completedAt) |
| `POST /fanqie/jobs` | body:`{source, startChapter, endChapter, requesterUserId, groupId?}`;流程:resolve_book_reference/get_book → list_chapters → `check_feature_permission(requester, group, "fanqie")` 提前拒绝 → `state.submit_job`;错误(配额/范围/队列满)映射 422 带 message;成功返回 `{jobId}` 并 `hub.notify_change("fanqie_job", ...)` |
| `POST /fanqie/jobs/{id}/cancel·retry·send` | 直接复用 `state.cancel_job/retry_job/deliver_job`(重用各自内部校验与文件投递逻辑);成功后 notify_change |
| `DELETE /fanqie/jobs/{id}` | `state.delete_job` + notify_change |
| `GET /fanqie/search?keyword&order` | provider.search(order=related/new/hot);FanqieServiceUnavailable→503,ValueError→422 |
| `GET /fanqie/rank/categories`、`GET /fanqie/rank/books` | list_rank_categories / list_rank_books(gender/rankType/categoryId/limit) |
| `GET /fanqie/books/{bookId}`、`GET /fanqie/books/{bookId}/chapters` | get_book / list_chapters |

序列化约定:camelCase;`requesterUserId`/`groupId` 用 `str()`(service.py 大整数约定),job/chapter 自增 id 保持 int;时间用 `iso()`。

注册:`app.py` 导入 `from .fanqie import router as fanqie_router`,`register()` 中 `app.include_router(fanqie_router)`;POST/DELETE 自动落入既有审计中间件。

## 后端小改

- `service.py` `overview()` 的 `wanted` 集合加入 `"番茄小说"`,运行概览「插件状态」卡片显示其加载状态。

## 前端(webui/)

- `types.ts`:新增 FanqieStatus / FanqieJob / FanqieJobDetail / FanqieJobChapter / FanqieBookSummary / FanqieChapterRef / FanqieRankCategory 等(QQ/群号为 string,与后端一致)
- 新建 `src/fanqie.tsx` 导出 `FanqiePage`,Tabs 状态写 searchParams(同 games):
  - **找书下载**:Segmented 切换「关键词搜索 / 榜单 / 链接·ID」
    - 搜索:关键词 + 排序 Select(相关/最新/最热),loading 提示首次需启动无头浏览器可能较慢;结果卡片(书名/作者/简介 + 选择)
    - 榜单:性别/榜单类型/分类三级级联 Select → 书列表
    - 链接/ID:输入解析
    - 选书 → Drawer:书籍 Descriptions + 目录 Table(分页、锁定 Tag)+ 范围表单(起止章,默认 1..全书,前端按 maxChapters 校验提示)+ 接收人 QQ(必填)+ 群号(选填)+ 提交 → 成功后跳转任务 Tab
  - **下载任务**:status Select + search;Table(任务#/书名作者/进度 Progress/状态/发送状态/请求者/群/时间/操作);操作按状态显隐:取消(queued|running)、重试(failed|cancelled)、发送(completed)、删除(Popconfirm)、详情 Drawer(章节表);5s 轮询 + visibilitychange 暂停(同 games)+ entity.changed 自动刷新
- `App.tsx`:import FanqiePage、`<Route path="fanqie">`、菜单项(BookOutlined「番茄小说」)

## 测试与检查

- 新增 `tests/test_webui_fanqie.py`(沿用 test_webui.py 的直调 + sys.modules 打桩风格):resolver 降级/重试语义、子插件缺失时 503、提交校验错误映射、大整数序列化
- 新增 `webui/src/fanqie.test.tsx`(vitest,参考 games.test.tsx mock `./api`)
- 运行:`uv run pytest tests/test_webui_fanqie.py tests/test_webui.py`、`uv run ruff check`、pyright(如已配置);`cd webui && npm run test && npm run build`(dist 必须重建,后端 serve 的是 dist)
- Windows 编辑保持 LF 行尾,避免 ruff 整文件报格式问题

## 文档

- `docs/deployment.md` WebUI 章节补一句番茄小说页面;README 功能概览加一行。