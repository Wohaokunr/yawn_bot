# ruff: noqa: ANN001, E501, F811, PLR2004
"""私聊群公告确认循环（修复）：等确认的操作进本轮资料、同轮自确认后直接收尾、跨轮沿用同一个确认。"""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest
from test_private_agent import (  # noqa: F401  (fixture)
    NOW,
    USER,
    _Bot,
    _message,
    _request,
    _script,
    maker,
)

_PKG = "src.plugins.yawn_core"
gm = importlib.import_module(f"{_PKG}.yawn_agent.group_manage")
pagent = importlib.import_module(f"{_PKG}.yawn_agent.private_agent")
capabilities = importlib.import_module(f"{_PKG}.yawn_agent.capabilities")
bridge = importlib.import_module(f"{_PKG}.private_chat_bridge")

GID = 777001


class ManageBot(_Bot):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def call_api(self, api: str, **params: Any) -> Any:
        self.calls.append(api)
        if api == "get_group_member_info":
            return {"user_id": params["user_id"], "role": "admin"}
        if api == "get_group_member_list":
            return [{"user_id": USER, "role": "admin", "nickname": "小明"}]
        return {}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge.private_agent_config, "group_manage_data_dir", str(tmp_path / "gm"))
    gm.reset_for_tests()
    capabilities.reset_capability_cache()
    arrivals = {"n": 0}
    monkeypatch.setattr(gm, "_arrivals", lambda _uid: arrivals["n"])

    async def candidates(_session, _uid):
        return [(GID, "猫猫群", "admin")]

    async def hint(_session, _uid):
        return True

    monkeypatch.setattr(gm, "candidate_groups", candidates)
    monkeypatch.setattr(gm, "refresh_manager_hint", hint)
    monkeypatch.setattr(pagent, "now_beijing", lambda: NOW)
    yield arrivals
    gm.reset_for_tests()


ANNOUNCE = ("group_manage", {"action": "announce", "content": "周六晚八点群语音"})


@pytest.mark.asyncio
async def test_self_confirm_in_same_turn_ends_turn_instead_of_looping(maker, monkeypatch) -> None:
    seen: list[dict] = []
    replies = [
        _message(calls=[ANNOUNCE]),
        _message(calls=[("group_manage", {"action": "confirm", "confirm_id": "x"})]),
        # 下面这些如果被用到，说明还在一轮里打转
        _message(calls=[ANNOUNCE]),
        _message(calls=[ANNOUNCE]),
    ]
    queue_final = _message("要在猫猫群发这条公告吗：周六晚八点群语音")
    replies.insert(2, queue_final)
    _script(monkeypatch, replies, seen)
    bot = ManageBot()
    async with maker() as session:
        out = await pagent.handle_private_turn(_request(session, bot, "帮我发个群公告"))
    assert out and "公告" in out
    assert len(seen) == 3  # 发起 → 自确认（被告知等他）→ 不带工具直接收尾
    assert seen[-1]["tool_choice"] == "none" or not seen[-1]["tools"]
    assert "_send_group_notice" not in bot.calls


@pytest.mark.asyncio
async def test_next_turn_sees_waiting_confirm_id_and_executes(maker, monkeypatch, _env) -> None:
    pending = gm.create_pending(USER, GID, "猫猫群", "announce", {"content": "周六晚八点群语音", "pinned": False, "confirm_required": False}, "在「猫猫群」发群公告")
    _env["n"] = 1  # 他回了“发吧”
    seen: list[dict] = []
    _script(
        monkeypatch,
        [
            _message(calls=[("group_manage", {"action": "confirm", "confirm_id": pending.id})]),
            _message("发好啦"),
        ],
        seen,
    )
    bot = ManageBot()
    async with maker() as session:
        out = await pagent.handle_private_turn(_request(session, bot, "发吧"))
    first = seen[0]["messages"]
    tail = json.dumps(first[-1]["content"], ensure_ascii=False)
    assert first[-1]["role"] == "system" and pending.id in tail and "group_manage_waiting" in tail
    # 等待确认的操作只在最后一层（本轮资料），不进前缀
    assert pending.id not in str(first[0]["content"])
    assert "_send_group_notice" in bot.calls
    assert out == "发好啦"
