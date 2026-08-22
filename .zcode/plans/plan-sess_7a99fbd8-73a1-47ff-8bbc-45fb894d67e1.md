# WebUI 舒适动效打磨(纯 styles.css 为主)

目标:动效更有"呼吸感"且不打扰——统一节奏令牌、入场错峰、悬停微交互、慢状态过渡;关键约束是 **2.5s 轮询刷新不能重播入场动画**(React key 稳定保证只在挂载时播放,不改数据流)。

## 1. 动效令牌(:root)
新增统一令牌并让全站沿用:
- `--ease-soft: cubic-bezier(0.22, 0.9, 0.32, 1)` 顺滑出场
- `--ease-spring: cubic-bezier(0.34, 1.3, 0.5, 1)` 轻微回弹(卡片/头像微交互)
- `--ease-glide: cubic-bezier(0.4, 0, 0.2, 1)` 状态过渡(昼夜切换)
- `--dur-fast: 0.18s / --dur-med: 0.32s / --dur-slow: 0.9s`

## 2. 入场错峰(stagger)
- `.app-content > *` 现有 rise-in 加 nth-child(1–6) 递增 40ms 延迟,页面加载像"逐个浮现"。
- 对局中心实时卡片(Row 内 Col)入场错峰。
- 狼人杀圆桌 `.ww-seat` / 小屏 `.ww-seat-card`:pop-in 轻回弹,按 nth-child(1–12) 每座 +35ms,像依次落座;仅挂载时播放,轮询刷新不重播。
- 时间线新事件 `.ww-event`:新增 `ww-event-in`(淡入 + 8px 上移,0.35s,`--ease-soft`),只对新挂载的 seq 节点播放,滚动阅读不被打扰。

## 3. 悬停/交互微升级
- 现有 card hover-lift 与按钮 lift 改用 `--ease-spring` + `--dur-fast`,位移从固定值微调(卡片 -2px、头像 hover scale 1.06)。
- 头像 `.ww-avatar` hover 轻微放大 + 光泽加深;发言中放大改为 `--ease-spring` 弹入而非线性 scale。
- 时间线卡片 hover 浮起改 `--ease-soft`,阴影变化更细。

## 4. 慢状态过渡
- 圆桌昼夜切换从 0.8s ease 改 `--dur-slow`(0.9s)+ `--ease-glide`,过渡更"天色渐变";中央玻璃面板背景同步。
- Drawer 打开时内容加一次性淡入(`.ant-drawer-body > *` fade 0.3s)。

## 5. 收尾
- 全部沿用现有 `prefers-reduced-motion: reduce` 全局禁用,不新增违规。
- `tsc` + `npm run build` + `vitest run`(10 项)验证全过;不改动任何 tsx 逻辑与测试。