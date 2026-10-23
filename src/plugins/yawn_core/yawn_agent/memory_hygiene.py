# ruff: noqa: RUF001, PLR2004, TC003
"""长期记忆写入前的确定性卫生检查：脱敏、注入拦截、敏感类别与近似去重。

记忆一旦落库会在之后数周反复进入 Prompt，所以这里只做零成本、可测试的
纯函数检查，整理任务、对话工具和 WebUI 都复用同一套规则。模型提取再准，
也不能让手机号、证件号、链接或一句"忽略之前的指令"变成长期事实。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from itertools import pairwise

# 统一占位符：保留"这里曾有一个联系方式"的语义，去掉可重新识别的值。
PII_PLACEHOLDER = "[已隐藏]"
NAME_PLACEHOLDER = "某成员"

# 顺序有意义：先匹配长号码（身份证/银行卡），再匹配手机号，避免长号码被
# 截成手机号后残留尾数。
_PII_PATTERNS: tuple[re.Pattern[str], ...] = (
    # URL 与 www 链接（含签名下载地址）。
    re.compile(r"(?:https?|ftp)://\S+|www\.\S+", re.IGNORECASE),
    # 邮箱。
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    # 18 位身份证（末位可为 X）。
    re.compile(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])"),
    # 16-19 位银行卡号，允许空格或短横分组。
    re.compile(r"(?<!\d)\d{4}(?:[ -]?\d{4}){2,3}(?:[ -]?\d{1,3})?(?!\d)"),
    # 大陆手机号（可带 +86）。
    re.compile(r"(?<!\d)(?:\+?86[ -]?)?1[3-9]\d{9}(?!\d)"),
    # 座机：区号-号码。
    re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)"),
    # IPv4。
    re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"),
    # 本机绝对路径（Windows 盘符与常见 POSIX 根目录）。
    re.compile(r"[A-Za-z]:\\[^\s，。,;；]+"),
    re.compile(
        r"(?<![\w.])/(?:home|root|tmp|var|opt|etc|Users|mnt|data)/[^\s，。,;；]+"
    ),
    # 明确标注的 QQ/微信号。
    re.compile(
        r"(?:QQ|qq|扣扣|微信|vx|VX|wx|WX|v信)\s*(?:号)?\s*[:：]?\s*[A-Za-z0-9_-]{5,20}"
    ),
)

# 记忆内容里出现这些"对模型说话"的句式，说明它是注入尝试或聊天原文，
# 而不是关于成员的事实；整条拒绝而不是部分替换。
_INSTRUCTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"忽略(?:之前|以上|上面|前面|所有|全部)?的?(?:指令|规则|设定|提示|要求)"
    ),
    re.compile(r"(?:无视|不要遵守|绕过)(?:之前|以上|你的)?的?(?:规则|设定|限制|指令)"),
    re.compile(r"(?:系统|system)\s*(?:提示词|prompt|指令|消息)", re.IGNORECASE),
    re.compile(r"ignore\s+(?:all\s+|the\s+)?(?:previous|above|prior)", re.IGNORECASE),
    re.compile(r"(?:从现在起|从今以后|以后)你(?:要|必须|只能|就是|是)"),
    re.compile(r"你(?:必须|只能|永远要)(?:回答|回复|说|称呼|服从|听)"),
    re.compile(r"(?:假装你是|你现在是|请你扮演|你来扮演|你要扮演)"),
    re.compile(r"(?:developer|开发者)\s*(?:mode|模式)", re.IGNORECASE),
    re.compile(r"</?(?:system|assistant|user|tool)>", re.IGNORECASE),
    re.compile(r"\[CQ:", re.IGNORECASE),
)

# 高敏感个人类别（健康、宗教、政治立场、性取向、财务、精确住址）不进入
# 群内画像：即使成员本人说过，自动沉淀并在之后反复引用也属于越界。
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:抑郁|焦虑症|躁郁|精神病|自杀|自残|癌|艾滋|HIV|乙肝|怀孕|流产|确诊|病历|吃药治疗)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:信仰|信教|基督徒|穆斯林|佛教徒|宗教)"),
    re.compile(r"(?:政治立场|党员身份|支持.{0,6}党|反对.{0,6}党)"),
    re.compile(r"(?:同性恋|双性恋|性取向|跨性别|出柜)"),
    re.compile(r"(?:负债|欠款|网贷|工资(?:是|有|为)?\d|月薪\d|年薪\d|存款\d)"),
    re.compile(r"(?:住在|家住|家在|地址是).{0,12}(?:路|街|号|小区|栋|单元|室)"),
    re.compile(r"(?:身份证|护照|驾照)号"),
)

_WHITESPACE_RE = re.compile(r"\s+")
_WRAPPING_QUOTES = "\"'“”‘’「」『』"
_LIST_SEPARATORS_RE = re.compile(r"[,，;；/|]+")


def scrub_pii(text: str) -> str:
    """把联系方式、证件号、链接、本机路径替换为占位符。"""

    scrubbed = str(text or "")
    for pattern in _PII_PATTERNS:
        scrubbed = pattern.sub(PII_PLACEHOLDER, scrubbed)
    # 连续多个占位符合并为一个，避免"[已隐藏][已隐藏]"浪费 token。
    return re.sub(
        rf"(?:{re.escape(PII_PLACEHOLDER)}\s*){{2,}}", PII_PLACEHOLDER, scrubbed
    )


def contains_pii(text: str) -> bool:
    return scrub_pii(text) != str(text or "")


def looks_like_instruction(text: str) -> bool:
    """内容是否像在给模型下指令（提示注入或原文复制）。"""

    candidate = str(text or "")
    return any(pattern.search(candidate) for pattern in _INSTRUCTION_PATTERNS)


def is_sensitive_fact(text: str) -> bool:
    """内容是否落在不应自动沉淀的高敏感个人类别。"""

    candidate = str(text or "")
    return any(pattern.search(candidate) for pattern in _SENSITIVE_PATTERNS)


def normalize_fact_content(text: str, *, max_chars: int = 200) -> str:
    """折叠空白、去掉包裹引号与句末标点，统一事实的存储形态。"""

    collapsed = _WHITESPACE_RE.sub(" ", str(text or "")).strip().rstrip("。.!！~～ ")
    while (
        len(collapsed) >= 2
        and collapsed[0] in _WRAPPING_QUOTES
        and (collapsed[-1] in _WRAPPING_QUOTES)
    ):
        collapsed = collapsed[1:-1].strip()
    collapsed = collapsed.rstrip("。.!！~～ ")
    return collapsed[:max_chars]


def normalize_list_fact(text: str, *, separator: str = "、") -> str:
    """把模型用逗号/分号/斜杠混排的多值事实统一为「、」分隔。"""

    parts = [
        part.strip()
        for part in _LIST_SEPARATORS_RE.sub(separator, str(text or "")).split(separator)
    ]
    return separator.join(dict.fromkeys(part for part in parts if part))


def scrub_names(text: str, names: Iterable[str]) -> str:
    """把（已隐私退出等）成员名替换为中性称呼；短名不替换避免误伤。"""

    scrubbed = str(text or "")
    for name in sorted({str(item).strip() for item in names}, key=len, reverse=True):
        if len(name) >= 2:
            scrubbed = scrubbed.replace(name, NAME_PLACEHOLDER)
    return scrubbed


def scrub_user_ids(text: str, user_ids: Iterable[int]) -> str:
    scrubbed = str(text or "")
    for user_id in {int(item) for item in user_ids if int(item) > 0}:
        scrubbed = re.sub(rf"(?<!\d){user_id}(?!\d)", NAME_PLACEHOLDER, scrubbed)
    return scrubbed


def char_bigrams(text: str) -> set[str]:
    compact = _WHITESPACE_RE.sub("", str(text or "").casefold())
    if len(compact) < 2:
        return {compact} if compact else set()
    return {"".join(pair) for pair in pairwise(compact)}


def similarity(left: str, right: str) -> float:
    """字符二元组 Jaccard 相似度，0~1。"""

    a, b = char_bigrams(left), char_bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_near_duplicate(left: str, right: str, *, threshold: float = 0.75) -> bool:
    """两条事实是否只是同一说法的轻微改写（含互为子串）。"""

    a = normalize_fact_content(left).casefold()
    b = normalize_fact_content(right).casefold()
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return similarity(a, b) >= threshold


def reject_reason(text: str, *, allow_sensitive: bool = False) -> str | None:
    """事实写入前的拒绝原因；None 表示可以写入（写入前仍应 scrub_pii）。"""

    candidate = normalize_fact_content(text)
    if not candidate:
        return "empty"
    if looks_like_instruction(candidate):
        return "instruction"
    if not allow_sensitive and is_sensitive_fact(candidate):
        return "sensitive"
    if scrub_pii(candidate).strip() in {"", PII_PLACEHOLDER}:
        return "pii_only"
    return None


def sanitize_fact(text: str, *, max_chars: int = 200) -> str | None:
    """规范化 + 脱敏；不可写入时返回 None。"""

    if reject_reason(text) is not None:
        return None
    cleaned = normalize_fact_content(
        scrub_pii(normalize_fact_content(text)), max_chars=max_chars
    )
    return cleaned or None


def sanitize_summary(
    text: str,
    *,
    forbidden_names: Iterable[str] = (),
    forbidden_user_ids: Iterable[int] = (),
    max_chars: int = 1600,
) -> str:
    """摘要脱敏：去 PII、抹掉隐私退出成员的名字与号码。

    摘要是叙述性文本，注入句式不整条拒绝（会让整天记忆丢失），而是由
    调用方在 ``looks_like_instruction`` 命中时决定是否降级。
    """

    scrubbed = scrub_pii(str(text or ""))
    scrubbed = scrub_names(scrubbed, forbidden_names)
    scrubbed = scrub_user_ids(scrubbed, forbidden_user_ids)
    scrubbed = re.sub(r"[ \t\r\f\v]+", " ", scrubbed)
    scrubbed = re.sub(r"\n\s*\n+", "\n", scrubbed)
    return scrubbed.strip()[:max_chars]


__all__ = [
    "NAME_PLACEHOLDER",
    "PII_PLACEHOLDER",
    "char_bigrams",
    "contains_pii",
    "is_near_duplicate",
    "is_sensitive_fact",
    "looks_like_instruction",
    "normalize_fact_content",
    "normalize_list_fact",
    "reject_reason",
    "sanitize_fact",
    "sanitize_summary",
    "scrub_names",
    "scrub_pii",
    "scrub_user_ids",
    "similarity",
]
