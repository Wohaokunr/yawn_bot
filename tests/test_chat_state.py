"""AI 对话队列背压与退出清理的回归测试。"""

from __future__ import annotations

import sys
import types
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "src" / "plugins" / "yawn_core"
PACKAGE = types.ModuleType("yawn_core")
PACKAGE.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault("yawn_core", PACKAGE)

from yawn_core.chat_state import enqueue, enter_mode, exit_mode


def test_chat_queue_rejects_burst_and_exit_drains() -> None:
    user_id = 987654
    state = enter_mode(user_id)

    assert all(enqueue(state, (None, None, None)) for _ in range(8))
    assert not enqueue(state, (None, None, None))

    assert exit_mode(user_id)
    assert state.queue.empty()
    assert not exit_mode(user_id)
