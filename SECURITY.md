# Security Policy

YawnBot 处理 QQ/OneBot 消息、WebUI 会话、AI provider 凭据以及持久化群聊数据。安全问题请优先私下报告，不要在公开 Issue 中披露可利用细节或真实凭据。

## 支持范围

项目当前处于 0.x 快速开发阶段。安全修复优先面向：

- 默认分支 `main`；
- 最新稳定 GitHub Release；
- 必要时最近一个仍可安全修补的稳定版本。

旧版本可能不会获得完整回补。升级前请备份 `data/` 并阅读 Release Notes 与 migration 说明。

## 什么属于安全问题

包括但不限于：

- WebUI 鉴权绕过、会话固定、CSRF 绕过或管理员/访客权限提升；
- 未授权 OneBot 操作、命令权限绕过或跨群数据访问；
- RCE、命令注入、路径穿越、任意文件读写；
- SSRF 或可绕过 allowlist 的外部资源访问；
- AI/OneBot/WebUI/GitHub/SSH 等 secret 泄露；
- 访客数据投影泄露证据消息、内部 ID、已 opt-out 用户或其他不应公开的数据；
- 恶意上传、媒体解析或归档处理导致的安全问题；
- migration、备份或部署流程导致敏感数据意外公开；
- Actions workflow 允许不受信任代码获得生产 secret 或部署权限。

普通功能 Bug、安装问题和功能建议请使用对应 Issue Form。

## 如何报告

优先使用 GitHub 仓库的 **Security → Report a vulnerability / Private vulnerability reporting**（如果该功能已启用）。在仓库尚未开启该功能时，请通过仓库所有者公开的 GitHub 联系方式建立私下渠道，不要先创建公开 Issue。

报告中建议包含：

- 受影响版本或 commit；
- 受影响组件；
- 前置条件；
- 最小复现步骤或 PoC；
- 实际影响；
- 你认为可行的缓解方式（可选）。

请不要发送真实 API Key、QQ Cookie/登录态、生产数据库、SSH 私钥或无关用户隐私数据。需要说明 secret 泄露时，请使用已吊销的测试凭据或对值进行不可逆脱敏。

## 维护者处理原则

收到有效报告后，维护者将根据风险：

1. 复现并确认影响范围；
2. 立即吊销已暴露凭据（如适用）；
3. 在私有修复分支中处理；
4. 补充回归测试；
5. 发布修复版本和必要的升级/轮换说明；
6. 在风险允许后公开安全说明并致谢报告者（除非报告者希望匿名）。

项目不承诺固定响应 SLA，但会优先处理可造成远程控制、权限提升、凭据泄露或跨群隐私泄露的问题。

## Secret 处理基线

以下内容不得提交到 Git、Issue、PR、Actions artifact 或公开日志：

- `AI_API_KEY` 等模型服务密钥；
- `ONEBOT_V11_ACCESS_TOKEN`；
- `WEBUI_ADMIN_TOKEN` 与访客凭据；
- `DEPLOY_SSH_PRIVATE_KEY`、GitHub PAT、Cloudflare 等基础设施 token；
- QQ/NapCat 登录态、Cookie、二维码会话数据；
- 真实 `.env`、数据库和备份。

当前 HEAD 由 `tools/repo_guard.py` 检查；开源发布前还应执行全历史 secret 审计，确认所有 branch/tag 可达历史没有残留凭据。

## 生产部署隔离

仓库中的生产部署参考实现使用 GitHub `production` Environment、受限 SSH forced command、不可变 image digest 和服务器本地 secret。公开仓库或 fork 不应拥有维护者生产 secrets，外部 PR 也不应通过 workflow 获得生产部署能力。
