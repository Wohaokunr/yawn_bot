# ruff: noqa: FAST002, TID252, E501, C901, PERF401
"""「私聊 Agent → 主动提议」：全站策略、今天的用量、每个人现在问不问/为什么、候选预览、手动试一次。

全部仅管理员可用；写操作由 ``app.py`` 的中间件统一记入操作审计（WebAdminAudit）。
这里只读写个人数据里的 ``offers`` 键和策略文件，不碰私聊聊天记录（只读最后一条的时间）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from nonebot import get_bots
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..yawn_agent import private_offer_policy as policy
from ..yawn_agent import private_offer_turn as turn
from ..yawn_agent import private_offers as po
from ..yawn_agent import private_store as store
from ..yawn_agent.context import now_beijing
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, ok

router = APIRouter(prefix=f"{API_PATH}/private-agent/offers")


class OfferUserPrefsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None：跟随全站默认；True/False：替他开/关（他自己在私聊里还能再改）
    enabled: bool | None = None
    follow_default: bool = False
    interval_hours: float | None = Field(default=None, ge=0.5, le=48)
    reset_interval: bool = False
    # >0 暂停这么多小时；0 取消暂停；None 不动
    snooze_hours: float | None = Field(default=None, ge=0, le=720)
    unmute: list[str] = Field(default_factory=list, max_length=20)
    mute: list[str] = Field(default_factory=list, max_length=20)


class OfferResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clear_prefs: bool = False


def _bot() -> Any:
    bots = [bot for bot in get_bots().values() if bot is not None]
    return bots[0] if bots else None


def _check_user(user_id: int) -> None:
    if user_id <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "userId 不对")


def _status() -> dict[str, Any]:
    return {"env_on": turn.env_on(), "agent_on": turn.agent_on()}


def _user_row(user_id: int, record: dict[str, Any], settings: policy.OfferSettings, now: Any) -> dict[str, Any]:
    data = po.peek(record)
    last = next((row for row in reversed(data["log"]) if row.get("status") in {"sent", "skipped", "failed", "user_spoke"}), None)
    return {
        "user_id": user_id,
        "prefs": po.prefs_view(record, settings, now),
        "last": _log_view(last) if last else None,
        "last_check": data.get("last_check") or None,
        "stats": po.stats(record),
        "today": po.sent_today(data, now),
        "pending": bool(data.get("pending")),
    }


def _log_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "at": row.get("at"),
        "status": row.get("status"),
        "status_label": po.STATUS_LABELS.get(str(row.get("status")), row.get("status")),
        "by": row.get("by") or "auto",
        "items": list(row.get("items") or []),
        "considered": list(row.get("considered") or []),
        "text": row.get("text") or "",
        "outcome": row.get("outcome"),
        "outcome_label": po.OUTCOME_LABELS.get(str(row.get("outcome")), "") if row.get("outcome") else "",
        "replied_at": row.get("replied_at"),
    }


@router.get("")
async def get_offers_overview(
    _session: AdminReadSession,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """全站策略、种类目录、今天的用量和有个人数据的用户（最近问过的在前）。"""

    settings = policy.load_settings()
    now = now_beijing()
    rows = []
    for user_id in store.known_user_ids():
        rows.append(_user_row(user_id, store.load(user_id), settings, now))
    rows.sort(key=lambda row: (str((row.get("last") or {}).get("at") or ""), row["user_id"]), reverse=True)
    return ok(
        {
            **_status(),
            "settings": settings.model_dump(),
            "kinds": policy.kind_catalog(),
            "reasons": policy.REASON_LABELS,
            "outcomes": po.OUTCOME_LABELS,
            "usage": policy.usage_snapshot(),
            "users": rows[:limit],
            "total_users": len(rows),
        }
    )


@router.put("/settings")
async def put_offer_settings(body: dict[str, Any], _session: AdminWriteSession) -> dict[str, Any]:
    """整份替换全站策略（没给的字段用默认值）。"""

    try:
        settings = policy.OfferSettings.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.errors(include_url=False)) from exc
    saved = policy.save_settings(settings)
    return ok({"settings": saved.model_dump()})


@router.get("/users/{user_id}")
async def get_offer_user(user_id: int, _session: AdminReadSession) -> dict[str, Any]:
    """一个人：现在问不问、为什么、下一次什么时候；能拿来问的事（含被筛掉的和原因）；最近的记录。"""

    _check_user(user_id)
    settings = policy.load_settings()
    now = now_beijing()
    plan = await turn.plan_for(_bot(), user_id, now, settings=settings)
    record = store.load(user_id)
    data = po.peek(record)
    return ok(
        {
            **_status(),
            **plan.view(),
            "prefs": po.prefs_view(record, settings, now),
            "own": {
                "enabled": data["prefs"].get("enabled"),
                "interval_hours": data["prefs"].get("interval_hours"),
                "snooze_until": data["prefs"].get("snooze_until"),
                "muted_kinds": dict(data["prefs"].get("muted_kinds") or {}),
            },
            "log": [_log_view(row) for row in reversed(data["log"])],
            "stats": po.stats(record),
            "cooling": [{"key": key, "at": at} for key, at in sorted(data["seen"].items(), key=lambda kv: str(kv[1]), reverse=True)[:30]],
        }
    )


@router.put("/users/{user_id}/prefs")
async def put_offer_user_prefs(user_id: int, body: OfferUserPrefsBody, _session: AdminWriteSession) -> dict[str, Any]:
    """替他改：开/关/跟随默认、间隔、暂停、静音或恢复某类事。"""

    _check_user(user_id)
    settings = policy.load_settings()
    now = now_beijing()
    unknown = [kind for kind in [*body.mute, *body.unmute] if kind not in policy.KIND_LABELS]
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"没有这一类：{'、'.join(unknown)}")

    def apply(record: dict[str, Any]) -> dict[str, Any]:
        if body.follow_default:
            po.set_enabled(record, None)
        elif body.enabled is not None:
            po.set_enabled(record, body.enabled)
        if body.reset_interval:
            po.set_interval(record, None, settings)
        elif body.interval_hours is not None:
            po.set_interval(record, body.interval_hours, settings)
        if body.snooze_hours is not None:
            if body.snooze_hours > 0:
                po.snooze(record, now, body.snooze_hours)
            else:
                po.state(record)["prefs"].pop("snooze_until", None)
        for kind in body.mute:
            po.mute_kind(record, kind, now, None)
        for kind in body.unmute:
            po.unmute_kind(record, kind)
        return po.prefs_view(record, settings, now)

    return ok({"prefs": store.update(user_id, apply)})


@router.post("/users/{user_id}/reset")
async def post_offer_user_reset(user_id: int, body: OfferResetBody, _session: AdminWriteSession) -> dict[str, Any]:
    """清掉他的提议记录和冷却（问过的事可以重新拿来问）；``clear_prefs`` 连他的设置一起恢复默认。"""

    _check_user(user_id)
    store.update(user_id, lambda record: po.reset(record, keep_prefs=not body.clear_prefs))
    return ok({"reset": True})


@router.post("/users/{user_id}/run")
async def post_offer_user_run(user_id: int, _session: AdminWriteSession) -> dict[str, Any]:
    """现在就试一次：跳过间隔、没回次数和每日上限，但仍尊重他的开关、/退出、好友、夜间与免打扰。"""

    _check_user(user_id)
    bot = _bot()
    if bot is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Bot 不在线，发不出去")
    result = await turn.offer_once(bot, user_id, now_beijing(), force=True, by="webui")
    return ok(result)


__all__ = ["router"]
