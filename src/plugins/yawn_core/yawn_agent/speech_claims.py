# ruff: noqa: E501,FBT003
"""未经工具验证的“已完成”声明检测。

模型偶尔会在没有调用工具、或工具失败后仍然说“已经禁言了/公告发好了”。
这里按动作族匹配完成时声明，只有本轮对应工具确实成功时才保留；否则删掉
该句，整段都是虚假声明时换成如实说明。零 AI 成本、确定性。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_COMPLETION = r"(?:已经|已|搞定|好了|完成|成功|这就|马上)"
# 查询/列表语境里的动作词不是完成声明，例如“查了一下禁言列表”。
_READ_CONTEXT_RE = re.compile(r"(?:查|看了|看一下|列表|名单|记录|多少|哪些|有谁|是谁)")
_NEGATION_RE = re.compile(r"(?:没|未|不能|无法|失败|不了|不行|做不到|吗|么|？|\?|如果|要不要|是否|可以帮|能不能)")

# 动作族 → (声明正则, 能证明该声明的工具)
_CLAIM_FAMILIES: dict[str, tuple[re.Pattern[str], frozenset[str]]] = {
    "mute": (
        re.compile(rf"{_COMPLETION}[^。！？!?\n]{{0,12}}(?:禁言|禁了|关小黑屋)|禁言(?:了|成功|好了)"),
        frozenset({"mute_member", "set_whole_group_mute"}),
    ),
    "unmute": (
        re.compile(rf"{_COMPLETION}[^。！？!?\n]{{0,12}}(?:解除禁言|解禁|放出来)|(?:解禁|解除禁言)(?:了|成功|好了)"),
        frozenset({"unmute_member", "set_whole_group_mute"}),
    ),
    "notice": (
        re.compile(rf"{_COMPLETION}[^。！？!?\n]{{0,12}}(?:发布|发|贴|置顶)[^。！？!?\n]{{0,4}}公告|公告(?:已经|已)?(?:发布|发出|发好|贴好|置顶)(?:好)?(?:了|成功)"),
        frozenset({"create_group_announcement"}),
    ),
    "notice_delete": (
        re.compile(rf"{_COMPLETION}[^。！？!?\n]{{0,12}}(?:删除|删掉|撤掉)[^。！？!?\n]{{0,4}}公告"),
        frozenset({"delete_group_notice"}),
    ),
    "recall": (
        re.compile(rf"{_COMPLETION}[^。！？!?\n]{{0,12}}撤回|撤回(?:了|成功|好了)"),
        frozenset({"recall_message", "recall_bot_message"}),
    ),
    "essence": (
        re.compile(rf"{_COMPLETION}[^。！？!?\n]{{0,12}}(?:设为精华|设成精华|加精|加入精华)|(?:加精|设为精华)(?:了|成功)"),
        frozenset({"set_essence_message"}),
    ),
    "file": (
        re.compile(rf"{_COMPLETION}[^。！？!?\n]{{0,12}}(?:发|传|上传)[^。！？!?\n]{{0,6}}(?:群文件|文件)|文件(?:已经|已)?(?:发|传|上传)(?:好|完)?(?:了|成功)"),
        frozenset({"send_file"}),
    ),
    # 只核验"忘掉/删除记忆"：声称删了隐私数据却没删是隐私问题。"记住了"
    # 不核验，被动整理任务本来就会从聊天里沉淀事实。
    "forget": (
        re.compile(rf"{_COMPLETION}[^。！？!?\n]{{0,8}}(?:忘掉|忘记|删掉|清除)[^。！？!?\n]{{0,8}}(?:记忆|资料|画像)|(?:忘掉|删掉|清除)(?:了|啦)[^。！？!?\n]{{0,6}}(?:记忆|资料)"),
        frozenset({"forget_about_me"}),
    ),
    "kick": (
        re.compile(rf"{_COMPLETION}[^。！？!?\n]{{0,12}}(?:踢出|踢了|移出群)"),
        frozenset({"kick_member"}),
    ),
    "rename_group": (
        re.compile(rf"{_COMPLETION}[^。！？!?\n]{{0,12}}(?:改群名|群名改|修改群名)"),
        frozenset({"set_group_name"}),
    ),
    "card": (
        re.compile(rf"{_COMPLETION}[^。！？!?\n]{{0,12}}(?:改|设置|修改)[^。！？!?\n]{{0,6}}(?:群名片|名片)"),
        frozenset({"set_group_card"}),
    ),
    "title": (
        re.compile(rf"{_COMPLETION}[^。！？!?\n]{{0,12}}(?:设置|改|给)[^。！？!?\n]{{0,6}}头衔"),
        frozenset({"set_special_title"}),
    ),
    # 代码沙箱：没真正跑过 run_python 就说“跑完了/图画好了/曲子生成了”是编造结果。
    "sandbox_run": (
        re.compile(
            rf"{_COMPLETION}[^。！？!?\n]{{0,8}}(?:运行|跑|执行)[^。！？!?\n]{{0,6}}(?:代码|程序|脚本)"
            r"|(?:代码|程序|脚本)(?:已经|已)?(?:跑|运行|执行)(?:完|好)?(?:了|成功)"
            r"|(?:跑|运行)出来(?:的)?结果"
            r"|(?:图表|折线图|柱状图|饼图|散点图|词云|曲子|旋律|音乐)(?:已经|已)?(?:画|生成|做|写)(?:好|完|出来)(?:了|啦)"
        ),
        frozenset({"run_python", "run_shell"}),
    ),
}

_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]*|\n")
UNVERIFIED_FALLBACK = "这个操作我这边没有执行成功。"


@dataclass(frozen=True, slots=True)
class ClaimCheck:
    text: str
    families: tuple[str, ...]
    changed: bool


def _claimed_families(sentence: str) -> list[str]:
    if _NEGATION_RE.search(sentence) or _READ_CONTEXT_RE.search(sentence):
        return []
    return [
        family
        for family, (pattern, _tools) in _CLAIM_FAMILIES.items()
        if pattern.search(sentence)
    ]


def check_action_claims(text: str, verified_tools: frozenset[str] | set[str]) -> ClaimCheck:
    """删除没有成功工具支撑的完成时声明；返回清理后的文本和命中的动作族。"""

    pieces = _SENTENCE_RE.findall(str(text or ""))
    kept: list[str] = []
    unverified: list[str] = []
    for piece in pieces:
        families = [
            family
            for family in _claimed_families(piece)
            if not (_CLAIM_FAMILIES[family][1] & set(verified_tools))
        ]
        if families:
            unverified.extend(item for item in families if item not in unverified)
            continue
        kept.append(piece)
    if not unverified:
        return ClaimCheck(str(text or ""), (), False)
    remaining = "".join(kept).strip()
    return ClaimCheck(remaining or UNVERIFIED_FALLBACK, tuple(unverified), True)


__all__ = ["UNVERIFIED_FALLBACK", "ClaimCheck", "check_action_claims"]
