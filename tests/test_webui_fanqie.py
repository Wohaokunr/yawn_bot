from __future__ import annotations

import builtins
import importlib
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest
from fastapi import HTTPException

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

fanqie = importlib.import_module("src.plugins.yawn_core.webui.fanqie")
app_module = importlib.import_module("src.plugins.yawn_core.webui.app")

_STATE_KEY = "src.plugins.yawn_core.yawn_fanqie.state"
_PROVIDER_KEY = "src.plugins.yawn_core.yawn_fanqie.provider"
_MODELS_KEY = "src.plugins.yawn_core.yawn_fanqie.models"


def test_resolvers_use_loaded_sub_plugin_modules() -> None:
    # 与 games 的回归同源：延迟解析必须命中子插件注册进 sys.modules 的
    # 同一实例，否则页面永远显示"子插件未加载"。
    assert fanqie._fanqie_state() is sys.modules[_STATE_KEY]
    assert fanqie._fanqie_provider() is sys.modules[_PROVIDER_KEY]
    assert fanqie._fanqie_models() is sys.modules[_MODELS_KEY]
    cfg = fanqie._fanqie_config()
    assert cfg is not None and cfg.fanqie_max_chapters >= 1


def test_state_resolver_retry_after_failure() -> None:
    # 解析失败不得落缓存，否则子插件晚加载后页面无法自动恢复。
    fanqie._state_resolved = False
    fanqie._state_module = None
    try:
        with pytest.MonkeyPatch.context() as patch:
            real_import = builtins.__import__

            def failing_import(name: str, *args: object, **kwargs: object) -> Any:
                if "yawn_fanqie" in name:
                    raise ImportError
                return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

            patch.setattr(builtins, "__import__", failing_import)
            assert fanqie._fanqie_state() is None
            assert fanqie._state_resolved is False
    finally:
        fanqie._state_resolved = False
        fanqie._state_module = None
    assert fanqie._fanqie_state() is sys.modules[_STATE_KEY]
    assert fanqie._state_resolved is True


@pytest.mark.asyncio
async def test_endpoints_degrade_to_503_when_sub_plugin_missing() -> None:
    try:
        with pytest.MonkeyPatch.context() as patch:
            real_import = builtins.__import__

            def failing_import(name: str, *args: object, **kwargs: object) -> Any:
                if "yawn_fanqie" in name:
                    raise ImportError
                return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

            patch.setattr(builtins, "__import__", failing_import)
            fanqie._state_resolved = False
            fanqie._state_module = None
            fanqie._provider_resolved = False
            fanqie._provider_module = None
            fanqie._models_resolved = False
            fanqie._models_module = None
            fanqie._config_resolved = False
            fanqie._config_instance = None
            # 任务列表走 models、搜索走 provider+config、任务操作走 state，
            # 三类依赖都必须在子插件缺失时统一降级为 503。
            for call in (
                lambda: fanqie.list_fanqie_jobs(
                    None, page=1, page_size=20, status_filter="all", search=""
                ),
                lambda: fanqie.fanqie_search(None, keyword="测试", order="related"),
                lambda: fanqie.cancel_fanqie_job(1, None),
            ):
                with pytest.raises(HTTPException) as exc_info:
                    await call()
                assert exc_info.value.status_code == 503  # noqa: PLR2004
            # status 端点不抛错，只报告 available=False。
            payload = (await fanqie.fanqie_status(None))["data"]
            assert payload["available"] is False
            assert payload["active"] is None
    finally:
        fanqie._state_resolved = False
        fanqie._provider_resolved = False
        fanqie._models_resolved = False
        fanqie._config_resolved = False


def test_provider_error_mapping() -> None:
    provider = sys.modules[_PROVIDER_KEY]
    assert (
        fanqie._provider_error(provider.FanqieServiceUnavailable("慢")).status_code
        == 503  # noqa: PLR2004
    )
    assert (
        fanqie._provider_error(provider.FanqieProviderError("挂")).status_code == 502  # noqa: PLR2004
    )
    assert fanqie._provider_error(ValueError("参数")).status_code == 422  # noqa: PLR2004
    assert fanqie._provider_error(KeyError("boom")).status_code == 500  # noqa: PLR2004


def test_serialize_job_stringifies_big_identifiers() -> None:
    job = SimpleNamespace(
        id=7,
        requester_user_id=9_007_199_254_740_992,
        group_id=9_007_199_254_740_993,
        start_chapter=1,
        end_chapter=10,
        total_chapters=10,
        completed_chapters=3,
        status="running",
        cancel_requested=False,
        output_name=None,
        send_status="pending",
        last_error=None,
        send_error=None,
        created_at=None,
        started_at=None,
        completed_at=None,
    )
    payload = fanqie._serialize_job(job, None, "测试群")
    assert payload["requesterUserId"] == "9007199254740992"
    assert payload["groupId"] == "9007199254740993"
    assert payload["groupName"] == "测试群"
    assert payload["title"] is None


class _FakeChapter:
    def __init__(self, index: int) -> None:
        self.item_id = f"item-{index}"
        self.title = f"第{index}章"
        self.index = index
        self.is_locked = False


class _FakeProvider:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def resolve_book_reference(self, source: str) -> SimpleNamespace:
        assert source == "7123456789012345678"
        return SimpleNamespace(book_id="7123456789012345678", title="测试书")

    async def list_chapters(self, _book_id: str) -> list[_FakeChapter]:
        return [_FakeChapter(index) for index in range(1, 21)]


def _fake_session() -> Any:
    @asynccontextmanager
    async def factory() -> Any:
        yield SimpleNamespace()

    return factory


async def _allow_permission(
    _user_id: int, _group_id: int | None, _feature: str, _session: Any
) -> bool:
    return True


@pytest.mark.asyncio
async def test_create_job_reuses_state_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted: dict[str, Any] = {}

    async def fake_submit_job(  # noqa: PLR0913,PLR0917
        requester_user_id: int,
        group_id: int | None,
        book: Any,
        chapters: list[Any],
        start_chapter: int,
        end_chapter: int,
    ) -> tuple[int | None, str | None]:
        submitted.update(
            requester=requester_user_id,
            group_id=group_id,
            book_id=book.book_id,
            chapters=len(chapters),
            start=start_chapter,
            end=end_chapter,
        )
        return 42, None

    fake_state = SimpleNamespace(submit_job=fake_submit_job)
    fake_provider = SimpleNamespace(FanqieProvider=_FakeProvider)
    monkeypatch.setattr(fanqie, "_state_module", fake_state)
    monkeypatch.setattr(fanqie, "_state_resolved", True)
    monkeypatch.setattr(fanqie, "_provider_module", fake_provider)
    monkeypatch.setattr(fanqie, "_provider_resolved", True)
    monkeypatch.setattr(fanqie, "_config_instance", SimpleNamespace())
    monkeypatch.setattr(fanqie, "_config_resolved", True)
    monkeypatch.setattr(fanqie, "get_session", _fake_session())
    monkeypatch.setattr(fanqie, "check_feature_permission", _allow_permission)

    body = fanqie.FanqieSubmitBody.model_validate(
        {
            "source": "7123456789012345678",
            "startChapter": 2,
            "endChapter": 5,
            "requesterUserId": 123456,
            "groupId": 654321,
        }
    )
    payload = (await fanqie.create_fanqie_job(body, None))["data"]
    assert payload == {"jobId": 42}
    assert submitted == {
        "requester": 123456,
        "group_id": 654321,
        "book_id": "7123456789012345678",
        "chapters": 20,
        "start": 2,
        "end": 5,
    }


@pytest.mark.asyncio
async def test_create_job_maps_state_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_submit_job(*_args: Any, **_kwargs: Any) -> tuple[None, str]:
        return None, "单次最多下载 500 章"

    monkeypatch.setattr(
        fanqie, "_state_module", SimpleNamespace(submit_job=failing_submit_job)
    )
    monkeypatch.setattr(fanqie, "_state_resolved", True)
    provider_stub = SimpleNamespace(FanqieProvider=_FakeProvider)
    monkeypatch.setattr(fanqie, "_provider_module", provider_stub)
    monkeypatch.setattr(fanqie, "_provider_resolved", True)
    monkeypatch.setattr(fanqie, "_config_instance", SimpleNamespace())
    monkeypatch.setattr(fanqie, "_config_resolved", True)
    monkeypatch.setattr(fanqie, "get_session", _fake_session())
    monkeypatch.setattr(fanqie, "check_feature_permission", _allow_permission)

    body = fanqie.FanqieSubmitBody.model_validate(
        {
            "source": "7123456789012345678",
            "startChapter": 1,
            "endChapter": 2,
            "requesterUserId": 1,
        }
    )
    with pytest.raises(HTTPException) as exc_info:
        await fanqie.create_fanqie_job(body, None)
    assert exc_info.value.status_code == 422  # noqa: PLR2004
    assert "最多下载" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_create_job_rejects_disabled_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def deny_permission(
        _user_id: int, _group_id: int | None, _feature: str, _session: Any
    ) -> bool:
        return False

    monkeypatch.setattr(fanqie, "get_session", _fake_session())
    monkeypatch.setattr(fanqie, "check_feature_permission", deny_permission)
    body = fanqie.FanqieSubmitBody.model_validate(
        {
            "source": "7123456789012345678",
            "startChapter": 1,
            "endChapter": 2,
            "requesterUserId": 1,
        }
    )
    with pytest.raises(HTTPException) as exc_info:
        await fanqie.create_fanqie_job(body, None)
    assert exc_info.value.status_code == 403  # noqa: PLR2004


def test_fanqie_routes_registered_on_fastapi_app() -> None:
    # 新版 FastAPI include_router 惰性挂载，app.routes 查不到模板路径，
    # 改为校验路由本身的路径集合与 app 模块的挂载链接。
    paths = {route.path for route in fanqie.router.routes}
    assert "/webui/api/v1/fanqie/status" in paths
    assert "/webui/api/v1/fanqie/jobs" in paths
    assert "/webui/api/v1/fanqie/jobs/{job_id}/cancel" in paths
    assert "/webui/api/v1/fanqie/search" in paths
    assert "/webui/api/v1/fanqie/rank/categories" in paths
    assert "/webui/api/v1/fanqie/resolve" in paths
    assert "/webui/api/v1/fanqie/books/{book_id}/chapters" in paths
    assert app_module.fanqie_router is fanqie.router
