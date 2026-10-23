"""Agent 审计结果的统一分类口径。

审计表的 ``result`` 由不同写入方各自命名：工具执行写 ``success / error``，
主动发言写参与决策 ``speak / wait / close``，出站消息写 ``success /
degraded_to_text / send_failed ...``。WebUI 统计、筛选和标签都必须先经过这里
归类，不能直接拿 ``result == "success"`` 判断成败——主动发言从来不写
``success``，那样会把每一次正常的“发出”或“这会儿不说话”都画成失败。
"""

from __future__ import annotations

from typing import Literal

AuditOutcome = Literal["success", "skipped", "failed", "unknown"]

AUDIT_SUCCESS_RESULTS = frozenset({"success", "speak", "degraded_to_text"})
AUDIT_SKIPPED_RESULTS = frozenset({"wait", "close"})
AUDIT_FAILURE_RESULTS = frozenset(
    {"error", "send_failed", "validation_failed", "unsupported_segment"}
)
AUDIT_UNKNOWN_RESULTS = frozenset({"delivery_unknown"})


def classify_audit_result(result: str | None) -> AuditOutcome:
    """把一条审计的 ``result`` 归为 成功 / 跳过 / 失败 / 待确认。

    未登记的新结果按失败处理：宁可在面板上显眼，也不要把异常静默算成功。
    """

    value = str(result or "").strip().lower()
    if value in AUDIT_SUCCESS_RESULTS:
        return "success"
    if value in AUDIT_SKIPPED_RESULTS:
        return "skipped"
    if value in AUDIT_UNKNOWN_RESULTS:
        return "unknown"
    return "failed"


def audit_results_for(outcome: str) -> frozenset[str]:
    """筛选用：某一类结果对应的全部 ``result`` 字面量。"""

    return {
        "success": AUDIT_SUCCESS_RESULTS,
        "skipped": AUDIT_SKIPPED_RESULTS,
        "failed": AUDIT_FAILURE_RESULTS,
        "unknown": AUDIT_UNKNOWN_RESULTS,
    }.get(outcome, frozenset())


__all__ = [
    "AUDIT_FAILURE_RESULTS",
    "AUDIT_SKIPPED_RESULTS",
    "AUDIT_SUCCESS_RESULTS",
    "AUDIT_UNKNOWN_RESULTS",
    "AuditOutcome",
    "audit_results_for",
    "classify_audit_result",
]
