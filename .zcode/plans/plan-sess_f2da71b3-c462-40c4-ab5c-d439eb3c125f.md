# WebUI 关系边图谱视图 + 前端效果优化

## 目标
为 Agent 管理的「关系边」面板增加图结构（关系图谱）查看视图，并优化现有列表效果。技术选型（已确认）：**d3-force 做力导向布局 + 手写 SVG 渲染**（零 UI 框架依赖）；节点默认仅显示有关系的成员、可切换显示全部；图谱内点击节点/边可查看详情并直接编辑/删除（复用现有抽屉表单，管理闭环）。

## 后端：新增图数据端点

**1. `src/plugins/yawn_core/webui/service.py`** 新增 `load_relation_graph(session, group_id)`（与 `list_group_members` 同层）：
- 过滤隐私退出成员（沿用 `get_relations` 的 opted_out 逻辑，两端任一退出即剔除该边）
- 边：`AgentRelation` 按群全量，`order_by(id)`，limit 5001 探测截断（与 export 端点口径一致，上限 5000）
- 成员：`UserGroup join BotUser` 全量（limit 5001），节点 = 全部成员 ∪ 边端点（退群残留端点也要有节点，昵称回退 userId）
- 每节点输出 `{userId(字符串), nickname, groupNickname, role, linked(是否出现在边中), degree(边数)}`
- 返回 `{nodes, edges(serialize_relation), meta: {relationTruncated, memberTruncated}}`

**2. `src/plugins/yawn_core/webui/app.py`** 在 `get_relations`（app.py:933）附近新增 `GET /agent/groups/{group_id}/relations/graph`（ReadSession 鉴权），调用上述函数并以 `ok()` 信封返回。

## 前端：依赖与类型

**3. `webui/` 安装 `d3-force@^3` 与 devDep `@types/d3-force`**（纯计算包、无传递依赖；若网络不可用则降级为手写简化力导向并报告）。

**4. `webui/src/types.ts`** 新增 `AgentRelationGraphNode`、`AgentRelationGraph`（nodes/edges/meta）接口。

## 前端：图谱组件（新文件 `webui/src/relation-graph.tsx`）

导出纯函数（供 vitest 直接单测，遵循 games.test.tsx 的导出函数测试传统）：
- `runRelationLayout(nodeIds, links, opts)` → `Map<userId, {x,y}>`：d3-force `forceSimulation` + `forceLink/forceManyBody/forceCollide/forceX/forceY`，`stop()` 后手动 `tick()` ~300 次得到确定性终局布局；节点初值手动赋环形位置（避免 phyllotaxis 初始化的不确定感）；高置信边理想距离更短；大图（>400 节点）降迭代次数；支持 pinned 位置保留
- `groupEdgesByEndpoint(edges)`：按无序端点对分组（A→B 与 B→A 同组），组内第 i 条产出对称曲率偏移（同一对节点多条边不重叠）
- `edgePath(x1,y1,x2,y2,offset,r1,r2)`：二次贝塞尔曲线 path，端点截断到节点半径外（给箭头留位）
- `RELATION_TYPE_GRAPH_COLORS` + `relationTypeColor(type)`：好友=绿、情侣/伴侣=粉、对立=红、亲属=紫、师徒=橙、同事/同学=蓝、mentions=灰（虚线）、自定义回退灰；箭头 marker 按色生成于 `<defs>`
- `filterGraphData(graph, typeFilter, showIsolated)`：类型过滤边；过滤后无边的节点默认隐藏、开 `showIsolated` 才显示（含无边成员）
- `fitTransform(positions, w, h, padding)` / `wheelZoom(transform, factor, cx, cy)`：fit-view 与围绕指针缩放的纯数学

`RelationGraphView` 组件（props: `graph`、`onEditRelation`、`onDeleteRelation`）：
- 手写 SVG：节点圆（大小按 degree、中心显示昵称首字、下方昵称 label 带白描边）、边贝塞尔曲线（粗细=1+confidence×2、颜色按类型、箭头示方向）
- 交互：节点拖拽（pointer events，拖后 pin、双击取消 pin）；空白拖拽平移；wheel 缩放（React onWheel 为 passive，需 ref + `addEventListener('wheel', h, {passive:false})`）；工具栏：显示孤立成员 Switch、重新布局、重置视图（fit）、缩放 ±；角落图例；hover/选中节点时高亮相连边并显示边类型 label、其余淡化；meta.truncated 时顶部 Alert 提示截断
- 点击节点 → Drawer：成员昵称/QQ + 该成员相关边明细（方向/类型/对端/备注/置信度）+ 每条「编辑/删除」；点击边 → 选中并展示同样操作入口（回调复用 RelationsPanel 的现有编辑抽屉与删除逻辑）
- ResizeObserver 适配容器尺寸（布局在固定虚拟画布计算，fit 适配实际容器，resize 不需重算布局）

## 前端：RelationsPanel 改造（`webui/src/agent.tsx:271-342`）

- 顶部统计卡 `section-row`（沿用 MemoriesPanel 模式）：关系边总数 / 涉及成员数 / 类型分布 Tag 列表 —— 数据来自 graph 端点
- 视图切换：`Segmented`「列表视图 / 图谱视图」，状态写入 URL searchParams（`?tab=relations&view=graph`，保留 tab 参数，刷新不丢）
- 保留类型过滤 Select 与「新增关系边」按钮（图谱视图下类型过滤作用于边）；`useApiQuery` 拉 graph 端点（内置 entity.changed 自动刷新，增删改后两视图同步）
- 表格视图效果优化：主体/客体列由纯 QQ 号改为「昵称 + QQ」两行展示（昵称映射来自 graph nodes，无昵称回退 QQ）
- 图谱视图挂载 `RelationGraphView`，编辑/删除复用现有两个 Drawer 与 `remove/openEdit/saveEdit` 逻辑不动

## 样式（`webui/src/styles.css`）

新增「关系图谱」分节：`.rg-*` 类名，使用现有樱花主题 token（`--sakura-*`、`--ease-soft`）——图谱容器卡片、图例、节点 label、hover 高亮过渡、工具栏。

## 测试

- **后端 `tests/test_webui.py`**：新增 graph 端点测试，参照现有 `_FakeRelationSession`/`_FakeSessionFactory` + monkeypatch 模式（fake session 按查询分发不同结果）：验证隐私退出边被过滤、nodes = 成员 ∪ 边端点且含 linked/degree、ID 序列化为字符串、truncated 标记
- **前端 `webui/src/relation-graph.test.tsx`**：纯函数测试——布局确定性（同输入同输出）、连通节点间距小于初始、pinned 不动、无 NaN；分组曲率对称、A→B/B→A 同组；颜色映射与回退；`filterGraphData` 孤立节点隐藏/保留逻辑；`fitTransform`/`wheelZoom` 数学与空输入防御

## 验证

1. `pytest tests/test_webui.py`（后端）
2. `cd webui && npm test`（vitest）+ `npm run typecheck` + `npm run build`
3. ruff/pyright 检查改动的 Python 文件（沿用项目既有工具链）

## 不改动的部分
关系 CRUD 端点与表单逻辑、隐私过滤口径、狼人杀/跑团等其他面板均不动；`AgentDetailPage` 的 Tabs 结构不动（图谱在「关系边」tab 内切换）。