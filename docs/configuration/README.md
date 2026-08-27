# 配置参考

根目录 `.env.example` 只保留第一次部署需要理解的最小配置。其余配置按功能拆分到本目录，避免新用户为了启动 Bot 先阅读几百行模板。

## 读取规则

YawnBot 使用 NoneBot / Pydantic 配置：环境变量名称为字段名的大写形式。通常先复制 `.env.example` 为 `.env`，再只添加你实际需要覆盖的高级项。不要把真实 Token、API Key、Cookie 或私有地址提交到 Git。

`ENVIRONMENT=prod` 时 NoneBot 还会读取 `.env.prod`；显式进程环境变量优先于文件。生产环境建议由容器编排或密钥管理系统注入敏感值。

## 文档入口

- [Core、OneBot 与存储](core.md)：NoneBot、OneBot V11、SQLite/localstore、Sentry。
- [AI 与 Agent](ai-agent.md)：OpenAI-compatible Provider、模型路由、Agent 媒体/文件、人设。
- [WebUI](webui.md)：管理台启用、管理 Token、Cookie 与访客访问。
- [番茄、RPG 与狼人杀](fanqie-games.md)：浏览器、下载任务和游戏高级参数。

## 最小配置与高级配置的边界

`.env.example` 中的值应满足“无 AI Key 也能启动”的基础场景。AI、WebUI 和浏览器搜索都是可选能力；启用后如果运行时依赖缺失，启动日志会给出可执行的诊断。Docker 构建会自动构建 WebUI 并安装 Playwright Chromium，原生部署则需要按对应专题文档安装这些可选运行时依赖。
