# WebUI 配置页折叠化 + 二次元×Apple 液态玻璃全站升级

## 改动文件
- `webui/src/styles.css` — 液态玻璃令牌、三层玻璃质感与全站应用
- `webui/src/glass.ts`（新增）— 全局指针光效监听（一个小模块，main.tsx 引入一次）
- `webui/src/environment.tsx` — 配置块折叠 + 交互美化
- `webui/src/environment.test.tsx` — 新增折叠/角标/浮动条测试

## 1. 液态玻璃核心质感（三层叠加，全站统一）

**新增令牌（`:root`）：** `--glass-bg`/`--glass-bg-strong`（半透明白 0.55/0.72）、`--glass-blur: 20px`、`--glass-saturate: 170%`、`--glass-edge`（镜面高光）、`--glass-shadow`、`--ease-bounce: cubic-bezier(0.34, 1.56, 0.64, 1)`（比现有 `--ease-spring` 回弹更强，用于按压释放）。

`.liquid-glass` 类由三层构成：

**① 主体玻璃**：半透明底 + `backdrop-filter: blur(20px) saturate(170%)`、22px 圆角、柔和樱花投影、inset 顶/底 1px 镜面高光。

**② 局部光效（鼠标跟随光斑）**：
- `glass.ts` 在 document 上挂一个委托 `pointermove` 监听（rAF 节流），对命中的 `.liquid-glass` 元素写入 `--glass-mx`/`--glass-my`（相对坐标），离开时清除；不命中时不做任何事，性能开销极小
- CSS 用一个专用径向渐变层：`radial-gradient(200px circle at var(--glass-mx,50%) var(--glass-my,50%), rgba(255,255,255,0.38), transparent 65%)`，默认 opacity 0，hover 时快速淡入——鼠标走到哪里，哪里亮起一小块光斑
- 指针为粗设备（触屏）时不启用；`prefers-reduced-motion` 下光斑位置仍跟随但无过渡动画（全局开关已覆盖）

**③ 边缘折射**：
- 玻璃边缘用一个环形伪元素模拟折射：`padding: 1.5px` + `mask-composite: exclude` 挖空中心，只在边缘 1.5px 环带上叠加更强的 `backdrop-filter: blur(6px) brightness(1.1) saturate(180%)`，背景在玻璃边缘处被弯折变形——这正是液态玻璃「边缘折射」的视觉特征
- 叠加上浅下深的渐变 rim 边框（顶部白亮、底部微暗），强化厚玻璃感
- 不支持 `mask-composite` 的浏览器自动退化为普通 inset 高光，不影响使用

**④ Q弹按压（干脆回弹）**：
- 可按元素（按钮、折叠面板头、标签）统一按压行为：`:active` 时 `scale(0.95)` 快速下压（~120ms），松开时用 `--ease-bounce` 过渡回弹（轻微过冲到 1.02 再归位，~350ms）——干脆、Q弹
- 应用于：全局 `.ant-btn`、折叠面板 header、浮动保存条按钮；`prefers-reduced-motion` 全局规则已禁用所有 transition，天然无障碍

**全站应用**：`.app-header`、`.app-content .ant-card`（保留现有 hover 上浮）、嵌套卡片不加 backdrop-filter（避免玻璃叠玻璃、控性能）。

## 2. environment.tsx：配置块可折叠 + 交互美化

- antd v6 `Collapse`（`items` + 受控 `activeKey`）：`llm-models`「LLM 模型档位」、`task-routing`「子插件任务路由」、每个 section 分组一个面板（约 12 个分组，全部展开页面极长）
- 默认：两个 LLM 面板展开，section 分组收起（现有测试选择器仍可命中）
- 面板 header：展示字体标题 + 条目数 chip + **未保存修改数角标**（折叠时也可见，防止改动被藏住）+ Q弹按压
- 搜索联动：输入非空时自动展开匹配分组；搜索框旁「全部展开 / 全部收起」；搜索框玻璃胶囊化
- 浮动保存条：有未保存修改时从底部滑出玻璃胶囊条——「N 项未保存」+「保存全部修改」+「撤销全部」，可访问名与页头「保存 N 项」按钮区分，不破坏现有测试
- 二次元点缀（复用 `stat-badge tone-*` 色板）：模型档位默认🌸/轻量🌱/识图📷徽章，任务路由 Agent🌸/RPG🎲/狼人杀🐺 徽章
- 数据流不变：changes 状态、save/undo、PATCH、409 冲突处理全部保持

## 3. 测试与验证

- `environment.test.tsx` 新增：面板折叠/展开可见性、未保存角标、搜索自动展开、浮动保存条提交 PATCH；现有测试保持通过
- `cd webui && npx vitest run`；`cd webui && npm run build`（tsc + vite）

## 不改动
- 后端 `/environment` API 与数据结构；其他页面组件结构（仅经全局 CSS 获得玻璃质感）