# sandbox — Agent 代码沙箱

群聊 Agent 的「全局虚拟机」：一个独立、无网络的 Python 执行容器。Agent 用它做数据
分析、画图、生成表格/文本文件和作曲，管理员在 WebUI「运行监控 → 代码沙箱」里查看和管理。

```
bot 容器 ──HTTP over Unix socket──▶ sandbox 容器（network: none，只读根）
  yawn_agent/sandbox_client.py         yawn_sandbox serve（root，仅负责降权与监督）
  webui/sandbox.py                       └─ 每次运行 fork 一个子进程：
                                            独立 uid、rlimit、工作区 cwd、干净环境
```

## 目录

| 路径 | 职责 |
|---|---|
| `yawn_sandbox/` | runner：只用标准库。`server.py`（Unix socket 上的 HTTP API、鉴权、并发队列）、`executor.py`（fork → 降权 → rlimit → 执行 → `wait4` 回收、按 uid 清扫残留进程、输出与配额检查）、`workspace.py`（每个工作区一个 uid，`openat`+`O_NOFOLLOW` 访问文件）、`history.py`（运行历史与统计）、`system.py`（隔离自检、资源统计）、`bootstrap.py`（子进程里的 notebook 式执行：最后一行表达式回显、`plt.show()` 自动存图）、`limits.py`、`config.py` |
| `yawnkit/` | 沙箱里预装的小工具库：`charts`（中文字体、`save()`）、`files`（`save_table` 存 csv/xlsx）、`music`（音符/和弦/和弦进行解析、合成 WAV、写 MIDI、鼓组） |
| `requirements.in` / `requirements.txt` | 沙箱里的第三方库（numpy、pandas、scipy、matplotlib、seaborn、sympy、networkx、pillow、openpyxl、mido、soundfile、jieba、wordcloud、qrcode 等），`--require-hashes` 锁定 |

## 隔离模型

- 容器层：`network_mode: none`、`read_only`、`cap_drop: ALL` 只加回降权需要的能力、`no-new-privileges`、`init: true`（回收孤儿进程）、内存/CPU/pids 上限。
- 进程层：每个工作区独立 uid（20000 起），目录 0700，工作区根 0711 不可列举；子进程 rlimit 限内存、CPU、单文件大小、打开文件数、进程数；墙钟超时后 killpg 并按 uid 扫 `/proc` 清理逃逸进程。
- 数据层：环境变量全部清空重建，Bot 的 `.env`、Token 和数据库都不在容器里；runner socket 0660 只给 bot 组。
- `GET /health` 返回隔离自检（`network_disabled`、`privilege_drop`、`readonly_root`、`no_new_privileges`、`child_reaper`）。自检不通过时 Bot 默认拒绝运行（WebUI 策略里的“允许未隔离运行”只用于本地开发）。

## 开发

```bash
uv run ruff check sandbox
uv run pyright sandbox/yawn_sandbox
uv run pytest -q tests/test_sandbox_runner.py   # 降权/隔离用例需要 root，否则自动跳过
```

本地不跑容器时可以直接起 runner（非 root 时不会降权，只适合开发）：

```bash
PYTHONPATH=sandbox SANDBOX_SOCKET=/tmp/yawn-sbx/runner.sock SANDBOX_STATE_DIR=/tmp/yawn-sbx/state \
  python -m yawn_sandbox serve
```

再给 Bot 配 `AGENT_SANDBOX_URL=unix:///tmp/yawn-sbx/runner.sock`。这种模式下隔离自检不会通过，需要在 WebUI 策略里临时打开“允许未隔离运行”；生产 Compose 设置了 `SANDBOX_REQUIRE_ISOLATION=true`，自检不过 runner 直接拒绝执行。

## 关联位置

- Bot 侧：`src/plugins/yawn_core/yawn_agent/sandbox_*.py`、`tool_handlers/sandbox.py`（约束见 `yawn_agent/AGENTS.md`「代码沙箱」）。
- WebUI：后端 `src/plugins/yawn_core/webui/sandbox.py`，前端 `webui/src/sandbox/`。
- 镜像与部署：根 `Dockerfile` 的 `sandbox-builder` 阶段，三个 Compose 的 `sandbox` 服务（见 `deploy/CLAUDE.md`）。
- 配置：`docs/configuration/ai-agent.md`「Agent 代码沙箱」。
