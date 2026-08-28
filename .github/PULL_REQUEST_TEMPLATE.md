## 改了什么

<!-- 用几句话说明可观察到的变化。 -->

## 为什么

<!-- 对应 Issue、故障场景或设计目标。 -->

## 验证

- [ ] `python tools/repo_guard.py`
- [ ] `uv run pytest -q`
- [ ] `uv run ruff check src tests tools`
- [ ] `uv run pyright src tools`
- [ ] `git diff --check`
- [ ] WebUI 相关：`npm test -- --run`
- [ ] WebUI 相关：`npm run typecheck`
- [ ] WebUI 相关：`npm run build`

## 数据库 / migration

- [ ] 本 PR 不修改 ORM schema
- [ ] 如修改 schema，已提交并审查对应 migration
- [ ] 如修改 schema，已验证空 SQLite `nb orm upgrade heads` 与 `nb orm check`

## 配置与兼容性

- [ ] 没有新增配置；或已更新 `docs/configuration/` 与必要的 `.env.example`
- [ ] 没有 breaking change；或已在下方说明升级/迁移方式
- [ ] 没有提交真实 Token、Cookie、API Key、SSH key、数据库或用户隐私数据

## 权限 / WebUI

- [ ] 本 PR 不扩大权限边界；或已补充管理员/访客/跨群访问回归测试
- [ ] 访客只读接口没有返回不必要的内部字段或 opt-out 用户数据

## 生产部署

- [ ] 本 PR 不修改 `.github/workflows/release.yml`、`.github/workflows/deploy-existing.yml` 或 `deploy/production/`
- [ ] 如果修改上述路径，已明确说明为什么必须修改，并验证：不可变 image digest、服务器本地 secrets、SQLite 部署前备份、forced-command SSH 权限边界均未被削弱

## 补充说明

<!-- 截图、日志、breaking changes、回滚说明等。不要粘贴真实凭据。 -->
