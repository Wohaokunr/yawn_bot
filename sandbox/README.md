# sandbox — Agent 代码沙箱

群聊 Agent 的「全局虚拟机」和私聊 Agent 的「虚拟电脑」：一个独立的 Python 执行容器，
接在只通外网的 `sandbox-egress` 网络上，内网地址一律被 runner 的防火墙挡住。Agent 用它做数据
分析、画图、生成表格/文本文件和作曲，管理员在 WebUI「运行监控 → 代码沙箱」里查看和管理。

```
bot 容器 ──HTTP over Unix socket──▶ sandbox 容器（sandbox-egress，只读根，NET_ADMIN 管 iptables）
  yawn_agent/sandbox_client.py         yawn_sandbox serve（root，仅负责降权与监督）
  webui/sandbox.py                       └─ 每次运行 fork 一个子进程：
                                            独立 uid、rlimit、工作区 cwd、干净环境
```

## 目录

| 路径 | 职责 |
|---|---|
| `yawn_sandbox/` | runner：只用标准库。`server.py`（Unix socket 上的 HTTP API、鉴权、并发队列）、`executor.py`（fork → 降权 → rlimit → 执行 → `wait4` 回收、按 uid 清扫残留进程、输出与配额检查）、`workspace.py`（每个工作区一个 uid，`openat`+`O_NOFOLLOW` 访问文件）、`history.py`（运行历史与统计）、`netpolicy.py`（按工作区 uid 开关外网、始终拒绝内网/保留地址的 iptables 链）、`system.py`（隔离自检、资源统计）、`bootstrap.py`（子进程里的 notebook 式执行：最后一行表达式回显、`plt.show()` 自动存图）、`limits.py`、`config.py` |
| `yawnkit/` | 沙箱里预装的小工具库：`charts`（中文字体、`save()`）、`files`（`save_table` 存 csv/xlsx）、`music`（音符/和弦/和弦进行解析、合成 WAV、写 MIDI、鼓组） |
| `yawnkit/caps/` | 能力包（1.2）：13 个包 167 个现成能力（files、archive、net、image、pdf、doc、data、text、dev、audio、apk、math、system），`call(name, args)` 返回结构化结果，`run()` 把结果包在 `<<YAWNKIT_RESULT>>` 标记里打印。Bot 侧目录在 `yawn_agent/computer_kits.py`，两边的能力名与参数由 `tests/test_private_computer_kits.py` 对照签名保证一致；新增能力两边都要登记 |
| `requirements.in` / `requirements.txt` | 沙箱里的第三方库（numpy、pandas、scipy、matplotlib、seaborn、sympy、networkx、pillow、openpyxl、mido、soundfile、jieba、wordcloud、qrcode 等），`--require-hashes` 锁定 |

## 隔离模型

- 容器层：只接 `sandbox-egress`（不与 bot/NapCat 同网）、`read_only`、`cap_drop: ALL` 只加回降权需要的能力、`no-new-privileges`、`init: true`（回收孤儿进程）、内存/CPU/pids 上限。
- 进程层：每个工作区独立 uid（20000 起），目录 0700，工作区根 0711 不可列举；子进程 rlimit 限内存、CPU、单文件大小、打开文件数、进程数；墙钟超时后 killpg 并按 uid 扫 `/proc` 清理逃逸进程。
- 数据层：环境变量全部清空重建，Bot 的 `.env`、Token 和数据库都不在容器里；runner socket 0660 只给 bot 组。
- 网络层：runner 以 root + `NET_ADMIN` 在容器自己的网络命名空间里建 `YAWN_SBX`/`YAWN_SBX_BLOCK` 链：工作区 uid 的出站先放行回环（Docker DNS），再按 Bot 的 `network` 参数（私聊默认开、群聊默认关，WebUI 可改）拦截，最后拒绝宿主机、局域网、云元数据等内网/保留地址。拿不到 iptables 时 `control=false`，容器有网就判为未隔离。环境变量：`SANDBOX_NETWORK_DEFAULT`（Bot 没说时是否放行）、`SANDBOX_BLOCK_PRIVATE`、`SANDBOX_NETWORK_CONTROL`。
- `GET /health` 返回隔离自检（`network_disabled`、`network_guarded`、`privilege_drop`、`readonly_root`、`no_new_privileges`、`child_reaper`）。自检不通过时 Bot 默认拒绝运行（WebUI 策略里的“允许未隔离运行”只用于本地开发）。

## 文件 API（runner 1.2.0）

`GET /v1/health` 的 `features` 列出可选能力：`shell`、`upload_guard`、`rename`、`mkdir`、`dir_summary`、`stream_upload`、`network_policy`、`capabilities`；`network` 字段是出网策略状态（`GET /v1/network` 同）。Bot 和 WebUI 按它决定显示哪些操作，旧 runner 自动降级。

| 接口 | 说明 |
|---|---|
| `PUT /v1/workspaces/{id}/file?path=…&overwrite=0&quota_mb=…` | 上传，边收边写临时文件再改名（单文件默认上限 `SANDBOX_MAX_UPLOAD_MB=1024`）；`quota_mb` 只能把本次可用的工作区空间收紧；`overwrite=0` 时同名文件返回 409 `exists`；超过文件数上限会拒绝 |
| `POST /v1/workspaces/{id}/rename` | `{"from","to","overwrite"}`，只在同一工作区内移动，逐级 `O_NOFOLLOW`，拒绝符号链接 |
| `POST /v1/workspaces/{id}/mkdir` | `{"path"}`，建多级目录 |
| `GET /v1/workspaces/{id}/files?dirs=1` | 额外返回每个子目录的文件数和字节数（WebUI 文件夹视图用） |

群聊附件由 Bot 写进 `inbox/`，文件类型识别覆盖 heic/tiff、opus/amr/silk、mkv/avi/flv、docx/pptx/epub、rar/xz 等常见格式。沙箱里没有 ffmpeg（音频只支持 libsndfile 能读的 wav/flac/ogg/mp3），模型会如实说明做不到的转换；PDF 用 pypdf（含 AES 加解密）。

## 开发

```bash
uv run ruff check sandbox
uv run pyright sandbox/yawn_sandbox
uv run pytest -q tests/test_sandbox_runner.py tests/test_sandbox_network_upload.py   # 降权/隔离用例需要 root，否则自动跳过
uv run pytest -q tests/test_private_computer_kits.py   # 能力包目录对齐 + 标准库能力
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
