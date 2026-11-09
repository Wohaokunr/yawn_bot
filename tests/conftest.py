"""全套件共用的隔离：好感玩法的个人数据目录与余额缓存。

``affinity_perks`` 在进程内缓存每人的好感余额（给没有数据库会话的额度检查点用），
``affinity_store`` 默认写 ``data/affinity``。每个测试换一个临时目录、清空缓存，
免得上一个测试里某个用户的等级让下一个测试的额度凭空变多。
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _isolate_affinity_perks(tmp_path_factory, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("AFFINITY_DATA_DIR", str(tmp_path_factory.mktemp("affinity")))
    # 只清已经加载的模块：在这里 import 会提前触发插件包初始化（需要 nonebot.init）
    perks = sys.modules.get("src.plugins.yawn_core.affinity_perks")
    policy = sys.modules.get("src.plugins.yawn_core.affinity_policy")
    if perks is not None:
        perks.clear_points_cache()
    if policy is not None:
        policy.clear_cache()
