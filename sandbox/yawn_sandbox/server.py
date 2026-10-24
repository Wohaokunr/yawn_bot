"""Runner 的 HTTP API（默认走 Unix socket，开发时可用带 token 的 TCP）。

所有响应都是 JSON，除了 ``GET …/file`` 直接返回文件字节。错误统一为
``{"error": {"code": ..., "message": ...}}``，HTTP 状态码表达大类。
"""

from __future__ import annotations

import contextlib
import grp
import hmac
import json
import logging
import os
import signal
import socket
import socketserver
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlparse

from . import API_VERSION, RUNNER_VERSION
from .config import RunnerConfig, load_config
from .executor import LANGUAGES, RUN_STATUSES, Executor, RunRequest
from .history import RunHistory
from .limits import resolve_limits
from .system import installed_packages, isolation_report, resource_report
from .workspace import (
    TEXT_KINDS,
    WorkspaceError,
    WorkspaceStore,
    file_kind,
    file_mime,
    split_relative_path,
    validate_workspace_id,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("yawn_sandbox")

MAX_JSON_BODY = 512 * 1024
MAX_META_CHARS = 2000
_PRUNE_INTERVAL_SECONDS = 3600
_WORKSPACE_ERROR_STATUS = {
    "not_found": HTTPStatus.NOT_FOUND,
    "too_large": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
    "workspace_limit": HTTPStatus.INSUFFICIENT_STORAGE,
}


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _clean_meta(raw: Any) -> dict[str, Any]:
    """只保留短小的标量元数据（群号、成员号、来源、用途说明）。"""

    if not isinstance(raw, dict):
        return {}
    meta: dict[str, Any] = {}
    for key, value in list(raw.items())[:12]:
        name = str(key)[:32]
        if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            meta[name] = value
        else:
            meta[name] = str(value)[:200]
    if len(json.dumps(meta, ensure_ascii=False)) > MAX_META_CHARS:
        return {}
    return meta


class RunnerService:
    """与传输无关的业务层；测试直接调用它。"""

    def __init__(self, config: RunnerConfig, *, drop_privileges: bool | None = None) -> None:
        self.config = config
        self.store = WorkspaceStore(
            config.workspaces_dir,
            config.meta_dir,
            uid_base=config.uid_base,
            uid_span=config.uid_span,
            drop_privileges=drop_privileges,
        )
        self.executor = Executor(config, self.store)
        self.history = RunHistory(config.meta_dir / "history.jsonl", limit=config.history_limit)
        self.transport = "unix" if config.socket_path else "tcp"
        self.last_prune: dict[str, Any] = {}

    # ── 状态 ────────────────────────────────────────────────────────────────

    def isolation(self) -> dict[str, Any]:
        return isolation_report(
            drop_privileges=self.store.drop_privileges, socket_mode=self.transport
        )

    def health(self) -> dict[str, Any]:
        isolation = self.isolation()
        packages = installed_packages()
        return {
            "ok": True,
            "version": RUNNER_VERSION,
            "api": API_VERSION,
            "languages": list(LANGUAGES),
            "python": packages["python"],
            "packages": packages["highlights"],
            "package_count": packages["total"],
            "isolation": isolation,
            "accepting_runs": isolation["isolated"] or not self.config.require_isolation,
            "ceilings": {
                key: getattr(self.config.ceilings, key)
                for key in self.config.ceilings.__dataclass_fields__
            },
            "concurrency": self.config.concurrency,
            "queue_limit": self.config.queue_limit,
            "running": len(self.executor.active),
            "waiting": max(self.executor.waiting - len(self.executor.active), 0),
            "retention_days": self.config.retention_days,
            "max_upload_mb": self.config.max_upload_mb,
        }

    def stats(self) -> dict[str, Any]:
        workspaces = []
        total_bytes = 0
        total_files = 0
        for workspace_id in self.store.list_workspaces():
            workspace = self.store.get(workspace_id)
            if workspace is None:
                continue
            usage = self.store.usage(workspace)
            total_bytes += usage["bytes"]
            total_files += usage["files"]
            workspaces.append({"id": workspace_id, **usage})
        return {
            "resources": resource_report(self.config.state_dir),
            "workspaces": {
                "count": len(workspaces),
                "bytes": total_bytes,
                "files": total_files,
            },
            "runs": self.history.stats(),
            "active": self.executor.active_runs(),
            "last_prune": self.last_prune,
        }

    def packages(self) -> dict[str, Any]:
        return installed_packages()

    # ── 执行 ────────────────────────────────────────────────────────────────

    def run(self, body: dict[str, Any]) -> dict[str, Any]:
        workspace = validate_workspace_id(body.get("workspace"))
        code = body.get("code")
        if not isinstance(code, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_code", "code 必须是字符串")
        if self.config.require_isolation and not self.isolation()["isolated"]:
            raise ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "not_isolated",
                "沙箱容器的隔离检查未通过（仍有网络或无法降权），已拒绝执行",
            )
        language = str(body.get("language") or "python")
        if language not in LANGUAGES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_language", "language 只能是 python 或 shell")
        request = RunRequest(
            workspace=workspace,
            code=code,
            language=language,
            limits=resolve_limits(body.get("limits"), self.config.ceilings),
            meta=_clean_meta(body.get("meta")),
        )
        result = self.executor.run(request).to_dict()
        if result["status"] != "busy":
            self.history.record(result, code)
        return result

    def runs(self, query: dict[str, str]) -> dict[str, Any]:
        workspace = query.get("workspace") or None
        if workspace:
            workspace = validate_workspace_id(workspace)
        status = query.get("status") or None
        if status and status not in RUN_STATUSES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_status", "未知状态")
        try:
            limit = max(1, min(int(query.get("limit") or 50), 200))
            before = float(query["before"]) if query.get("before") else None
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_query", "分页参数不正确") from exc
        items = self.history.list(
            workspace=workspace,
            status=status,
            query=query.get("q"),
            limit=limit,
            before=before,
        )
        return {
            "items": items,
            "active": self.executor.active_runs(),
            "next_before": items[-1]["started_at"] if len(items) >= limit else None,
        }

    def run_detail(self, run_id: str) -> dict[str, Any]:
        item = self.history.get(run_id)
        if item is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "没有这次执行记录")
        return item

    def cancel(self, run_id: str) -> dict[str, Any]:
        return {"cancelled": self.executor.cancel(run_id)}

    # ── 工作区 ──────────────────────────────────────────────────────────────

    def _workspace(self, workspace_id: str, *, create: bool = False) -> Any:
        if create:
            return self.store.ensure(workspace_id)
        workspace = self.store.get(workspace_id)
        if workspace is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "工作区还不存在")
        return workspace

    def workspaces(self) -> dict[str, Any]:
        rows = []
        for workspace_id in self.store.list_workspaces():
            workspace = self.store.get(workspace_id)
            if workspace is None:
                continue
            usage = self.store.usage(workspace)
            last_mtime = 0.0
            for _path, info in self.store.iter_files(workspace, limit=5000):
                last_mtime = max(last_mtime, info.st_mtime)
            rows.append(
                {
                    "id": workspace_id,
                    "bytes": usage["bytes"],
                    "files": usage["files"],
                    "last_modified": round(last_mtime, 3) or None,
                    "running": any(
                        item["workspace"] == workspace_id
                        for item in self.executor.active_runs()
                    ),
                }
            )
        return {"items": rows}

    def workspace_files(self, workspace_id: str, query: dict[str, str]) -> dict[str, Any]:
        workspace = self.store.get(validate_workspace_id(workspace_id))
        if workspace is None:
            return {"items": [], "total": 0, "usage": {"bytes": 0, "files": 0}}
        try:
            limit = max(1, min(int(query.get("limit") or 200), 1000))
        except ValueError:
            limit = 200
        items, total = self.store.list_files(
            workspace, prefix=query.get("prefix") or "", limit=limit
        )
        return {
            "items": [item.to_dict() for item in items],
            "total": total,
            "usage": self.store.usage(workspace),
        }

    def read_file(self, workspace_id: str, query: dict[str, str]) -> tuple[bytes, str, str]:
        workspace = self._workspace(validate_workspace_id(workspace_id))
        path = "/".join(split_relative_path(query.get("path")))
        max_bytes = self.config.ceilings.file_mb * 1024 * 1024
        with contextlib.suppress(ValueError):
            if query.get("max_bytes"):
                max_bytes = max(1, min(int(query["max_bytes"]), max_bytes))
        data = self.store.read_file(workspace, path, max_bytes=max_bytes)
        return data, file_mime(path), file_kind(path)

    def write_file(self, workspace_id: str, query: dict[str, str], data: bytes) -> dict[str, Any]:
        if len(data) > self.config.max_upload_mb * 1024 * 1024:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large", "上传文件过大")
        workspace = self._workspace(validate_workspace_id(workspace_id), create=True)
        usage = self.store.usage(workspace)
        quota = self.config.ceilings.disk_quota_mb * 1024 * 1024
        if usage["bytes"] + len(data) > quota:
            raise ApiError(HTTPStatus.INSUFFICIENT_STORAGE, "quota_exceeded", "工作区空间不足")
        entry = self.store.write_file(workspace, str(query.get("path") or ""), data)
        return {"file": entry.to_dict()}

    def delete_file(self, workspace_id: str, query: dict[str, str]) -> dict[str, Any]:
        workspace = self._workspace(validate_workspace_id(workspace_id))
        removed = self.store.delete_path(workspace, str(query.get("path") or ""))
        return {"removed": removed}

    def reset_workspace(self, workspace_id: str, query: dict[str, str]) -> dict[str, Any]:
        workspace_id = validate_workspace_id(workspace_id)
        cancelled = self.executor.cancel_workspace(workspace_id)
        workspace = self.store.get(workspace_id)
        removed = self.store.reset(workspace) if workspace is not None else 0
        forgotten = 0
        if query.get("forget_history") in {"1", "true"}:
            forgotten = self.history.forget_workspace(workspace_id)
        return {"removed": removed, "cancelled": cancelled, "history_removed": forgotten}

    def prune(self) -> dict[str, Any]:
        started = time.time()
        removed = self.store.prune(older_than_days=self.config.retention_days)
        self.last_prune = {
            "at": round(started, 3),
            "removed": removed,
            "retention_days": self.config.retention_days,
        }
        return self.last_prune


def _query(path: str) -> tuple[list[str], dict[str, str]]:
    parsed = urlparse(path)
    segments = [unquote(item) for item in parsed.path.split("/") if item]
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
    return segments, query


def _dispatch(  # noqa: PLR0912
    service: RunnerService,
    method: str,
    segments: list[str],
    query: dict[str, str],
    body: bytes,
) -> Any:
    if not segments or segments[0] != "v1":
        raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "未知接口")
    route = segments[1:]

    def _json() -> dict[str, Any]:
        if len(body) > MAX_JSON_BODY:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large", "请求体过大")
        try:
            data = json.loads(body or b"{}")
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_json", "请求体不是 JSON") from exc
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_json", "请求体必须是对象")
        return data

    match (method, route):
        case ("GET", ["health"]):
            return service.health()
        case ("GET", ["stats"]):
            return service.stats()
        case ("GET", ["packages"]):
            return service.packages()
        case ("POST", ["run"]):
            return service.run(_json())
        case ("GET", ["runs"]):
            return service.runs(query)
        case ("GET", ["runs", "active"]):
            return {"items": service.executor.active_runs()}
        case ("GET", ["runs", run_id]):
            return service.run_detail(run_id)
        case ("POST", ["runs", run_id, "cancel"]):
            return service.cancel(run_id)
        case ("GET", ["workspaces"]):
            return service.workspaces()
        case ("DELETE", ["workspaces", workspace_id]):
            return service.reset_workspace(workspace_id, query)
        case ("GET", ["workspaces", workspace_id, "files"]):
            return service.workspace_files(workspace_id, query)
        case ("GET", ["workspaces", workspace_id, "file"]):
            return service.read_file(workspace_id, query)
        case ("PUT", ["workspaces", workspace_id, "file"]):
            return service.write_file(workspace_id, query, body)
        case ("DELETE", ["workspaces", workspace_id, "file"]):
            return service.delete_file(workspace_id, query)
        case ("POST", ["maintenance", "prune"]):
            return service.prune()
    raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "未知接口")


def make_handler(service: RunnerService) -> type[BaseHTTPRequestHandler]:
    token = service.config.token
    max_body = max(service.config.max_upload_mb * 1024 * 1024, MAX_JSON_BODY)

    class Handler(BaseHTTPRequestHandler):
        server_version = f"yawn-sandbox/{RUNNER_VERSION}"
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            logger.debug("%s " + format, self.command, *args)

        def address_string(self) -> str:
            return "unix" if not isinstance(self.client_address, tuple) else str(self.client_address[0])

        def _send(self, status: HTTPStatus, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            if not token:
                return True
            header = self.headers.get("Authorization", "")
            return hmac.compare_digest(header, f"Bearer {token}")

        def _handle(self) -> None:
            try:
                if not self._authorized():
                    raise ApiError(HTTPStatus.UNAUTHORIZED, "unauthorized", "缺少或错误的 token")  # noqa: TRY301
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0 or length > max_body:
                    raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large", "请求体过大")  # noqa: TRY301
                body = self.rfile.read(length) if length else b""
                segments, query = _query(self.path)
                result = _dispatch(service, self.command, segments, query, body)
            except ApiError as exc:
                self._send(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
                return
            except WorkspaceError as exc:
                status = _WORKSPACE_ERROR_STATUS.get(exc.code, HTTPStatus.BAD_REQUEST)
                self._send(status, {"error": {"code": exc.code, "message": str(exc)}})
                return
            except Exception:
                logger.exception("沙箱请求处理失败")
                self._send(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": {"code": "internal_error", "message": "沙箱内部错误"}},
                )
                return
            if isinstance(result, tuple):
                data, mime, kind = result
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-File-Kind", kind)
                self.send_header(
                    "X-File-Text", "1" if kind in TEXT_KINDS else "0"
                )
                self.end_headers()
                self.wfile.write(data)
                return
            self._send(HTTPStatus.OK, {"data": result})

        do_GET = _handle  # noqa: N815
        do_POST = _handle  # noqa: N815
        do_PUT = _handle  # noqa: N815
        do_DELETE = _handle  # noqa: N815

    return Handler


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def _bind_unix(path: Path, gid: int | None, handler: type[BaseHTTPRequestHandler]) -> _UnixServer:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    server = _UnixServer(str(path), handler)
    # 只有 Bot 所在的组能连；工作区 uid 不在这个组里，用户代码连不上自己的 runner。
    if gid is not None and os.geteuid() == 0:
        os.chown(path.parent, 0, gid)
        os.chmod(path.parent, 0o750)
        os.chown(path, 0, gid)
    os.chmod(path, 0o660)
    return server


def serve(config: RunnerConfig | None = None) -> None:
    config = config or load_config()
    logging.basicConfig(
        level=os.environ.get("SANDBOX_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    service = RunnerService(config)
    handler = make_handler(service)
    server: socketserver.BaseServer
    if config.socket_path is not None:
        server = _bind_unix(config.socket_path, config.socket_gid, handler)
        where = str(config.socket_path)
    else:
        if not config.token:
            raise SystemExit("SANDBOX_HTTP 模式必须设置 SANDBOX_TOKEN")
        server = ThreadingHTTPServer((config.http_host or "127.0.0.1", config.http_port), handler)
        where = f"{config.http_host}:{config.http_port}"
    isolation = service.isolation()
    if not isolation["isolated"]:
        logger.warning("沙箱隔离检查未全部通过：%s", isolation["checks"])
    if not isolation["checks"]["child_reaper"]:
        logger.warning("runner 是 PID 1，孤儿进程不会被回收；请在 compose 里给 sandbox 服务加 init: true")
    if config.socket_gid is not None:
        with contextlib.suppress(KeyError):
            logger.info("socket 组: %s", grp.getgrgid(config.socket_gid).gr_name)
    logger.info("yawn-sandbox %s 监听 %s", RUNNER_VERSION, where)

    stop = threading.Event()

    def _prune_loop() -> None:
        while not stop.wait(_PRUNE_INTERVAL_SECONDS):
            try:
                service.prune()
            except Exception:  # noqa: PERF203 - 每小时一次，单次失败不能让清理线程退出
                logger.exception("定期清理失败")

    threading.Thread(target=_prune_loop, daemon=True).start()

    def _shutdown(_signum: int, _frame: Any) -> None:
        stop.set()
        for item in service.executor.active_runs():
            service.executor.cancel(item["run_id"])
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        if config.socket_path is not None:
            with contextlib.suppress(OSError):
                config.socket_path.unlink()


def healthcheck(config: RunnerConfig | None = None) -> int:
    """容器 HEALTHCHECK：通过 socket 请求 /v1/health。"""

    config = config or load_config()
    request = b"GET /v1/health HTTP/1.0\r\nHost: sandbox\r\n"
    if config.token:
        request += f"Authorization: Bearer {config.token}\r\n".encode()
    request += b"\r\n"
    try:
        if config.socket_path is not None:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(3)
            client.connect(str(config.socket_path))
        else:
            client = socket.create_connection(
                (config.http_host or "127.0.0.1", config.http_port), timeout=3
            )
        with client:
            client.sendall(request)
            response = client.recv(64)
    except OSError:
        return 1
    return 0 if response.startswith((b"HTTP/1.0 200", b"HTTP/1.1 200")) else 1


__all__ = ["ApiError", "RunnerService", "healthcheck", "make_handler", "serve"]
