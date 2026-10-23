# ruff: noqa: TID252, RUF001, RUF002, E501, C901, PERF403
"""Persona v3：一张人设卡贯穿存储、WebUI、QQ 命令、调试草稿与 Prompt 编译。

v2 有 Profile / Editor / Preset / resolved dict 四套形状，8 个 0–4 档位里有互相
重叠的（直接度 vs 温和、续聊 vs 活跃度），真正能描述角色的只有 240 字补充，
发言策略还要把中文标签字符串反解析回档位。v3 收敛为：

- ``PersonaCard``：唯一形状。名字、自称、称呼、身份、性格、说话方式、口癖、
  喜好、避开的话题、示例台词，加 6 个档位（温度、幽默、详略、表现力、活跃度、接梗）。
- 全局人设：人设库里被设为“全局默认”的那张卡；没有时回退内置“自然群友”。
  运行时读进程内缓存（``set_global_persona_card``），不在热路径查库。
- 群人设：``persona_enabled=True`` 时用本群自己的卡，否则跟随全局。
- v2 旧数据在读取时转换为 v3，被编辑一次就以 v3 写回，不需要数据迁移。

事实、隐私、权限、工具安全不属于人设，始终由系统策略约束。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..data_models.group_agent_config import GroupAgentConfig

from nonebot import get_plugin_config
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from .context_budget import estimate_tokens
from .log import dbg

PERSONA_SCHEMA_VERSION = 3
# 兼容旧调用方/测试：单个短文本字段的上限。
MAX_FIELD_LENGTH = 240
MAX_NAME_LENGTH = 32
MAX_ALIAS_LENGTH = 16
MAX_IDENTITY_LENGTH = 160
MAX_LONG_TEXT_LENGTH = 400
MAX_CATCHPHRASES = 6
MAX_CATCHPHRASE_LENGTH = 20
MAX_TOPICS = 8
MAX_TOPIC_LENGTH = 24
MAX_EXAMPLES = 4
MAX_EXAMPLE_LENGTH = 80
# 编译后的角色卡在静态前缀里的估算 token 上限；超出时按优先级裁剪。
PERSONA_PROMPT_TOKEN_LIMIT = 700

PERSONA_TRAIT_FIELDS = (
    "warmth",
    "humor",
    "verbosity",
    "expressiveness",
    "sociability",
    "reaction_tendency",
)
PERSONA_VOICE_TRAITS = ("warmth", "humor", "verbosity", "expressiveness")
PERSONA_SOCIAL_TRAITS = ("sociability", "reaction_tendency")

PERSONA_SOURCE_GLOBAL = "global"
PERSONA_SOURCE_BUILTIN = "builtin"
PERSONA_SOURCE_GROUP = "group"
PERSONA_SOURCE_DRAFT = "draft"


class AgentPersonaDefaults(BaseModel):
    """全局人设在人设库里没有默认项时，内置卡使用的名字。"""

    agent_persona_name: str = "Yawn"


_WHITESPACE_RE = re.compile(r"\s+")
_PRESET_ID_RE = re.compile(r"[^a-z0-9_]+")
# 人设由管理员撰写，允许“自称本喵、叫成员杂鱼”这类角色指令；只拒绝试图
# 覆盖系统策略的内容（忽略规则、伪造系统消息、CQ 码等）。
_OVERRIDE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"忽略(?:之前|以上|上面|前面|所有|全部|一切|的)*(?:指令|规则|设定|提示|要求|限制)"
    ),
    re.compile(
        r"(?:无视|不要遵守|绕过|突破)(?:之前|以上|所有|全部|系统|你的|的)*(?:规则|设定|限制|指令|策略)"
    ),
    re.compile(r"(?:系统|system)\s*(?:提示词|prompt|指令|消息)", re.IGNORECASE),
    re.compile(r"ignore\s+(?:all\s+|the\s+)?(?:previous|above|prior)", re.IGNORECASE),
    re.compile(r"(?:developer|开发者|越狱|jailbreak)\s*(?:mode|模式)?", re.IGNORECASE),
    re.compile(r"</?(?:system|assistant|user|tool)>", re.IGNORECASE),
    re.compile(r"\[CQ:", re.IGNORECASE),
    re.compile(r"@全体成员|@all\b", re.IGNORECASE),
)


def _clean_text(value: object, limit: int) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "").strip())[:limit].strip()


def _clean_multiline(value: object, limit: int) -> str:
    """保留换行（性格/说话方式常分点写），压缩其余空白。"""

    lines = [
        _WHITESPACE_RE.sub(" ", line).strip()
        for line in str(value or "").replace("\r", "").split("\n")
    ]
    return "\n".join(line for line in lines if line)[:limit].strip()


_LIST_SPLIT_RE = re.compile(r"[\n,，、;；|]+")


def _clean_list(value: object, *, count: int, length: int, split: bool = True) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items: list[object] = list(
            _LIST_SPLIT_RE.split(value) if split else value.split("\n")
        )
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        return []
    items: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        item = _clean_text(raw, length)
        folded = item.casefold()
        if not item or folded in seen:
            continue
        seen.add(folded)
        items.append(item)
        if len(items) >= count:
            break
    return items


def _clamp_level(value: object, fallback: int = 2) -> int:
    try:
        level = int(value)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return fallback
    return max(0, min(4, level))


class PersonaCard(BaseModel):
    """唯一的人设形状；字段超长或超量时裁剪而不是报错，存储解析也复用它。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    preset_id: str = Field(default="natural", validation_alias=AliasChoices("preset_id", "presetId"), serialization_alias="presetId")
    name: str = "Yawn"
    self_name: str = Field(default="", validation_alias=AliasChoices("self_name", "selfName"), serialization_alias="selfName")
    user_title: str = Field(default="", validation_alias=AliasChoices("user_title", "userTitle"), serialization_alias="userTitle")
    identity: str = ""
    personality: str = ""
    speaking_style: str = Field(default="", validation_alias=AliasChoices("speaking_style", "speakingStyle"), serialization_alias="speakingStyle")
    catchphrases: list[str] = Field(default_factory=list)
    likes: list[str] = Field(default_factory=list)
    avoid_topics: list[str] = Field(default_factory=list, validation_alias=AliasChoices("avoid_topics", "avoidTopics"), serialization_alias="avoidTopics")
    examples: list[str] = Field(default_factory=list)
    warmth: int = 2
    humor: int = 1
    verbosity: int = 1
    expressiveness: int = 1
    sociability: int = 2
    reaction_tendency: int = Field(default=2, validation_alias=AliasChoices("reaction_tendency", "reactionTendency"), serialization_alias="reactionTendency")

    @field_validator("preset_id", mode="before")
    @classmethod
    def _preset(cls, value: object) -> str:
        lowered = re.sub(r"[\s\-]+", "_", str(value or "").strip().lower())
        cleaned = _PRESET_ID_RE.sub("", lowered).strip("_")[:32]
        return cleaned or "custom"

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: object) -> str:
        return _clean_text(value, MAX_NAME_LENGTH) or "Yawn"

    @field_validator("self_name", "user_title", mode="before")
    @classmethod
    def _alias(cls, value: object) -> str:
        return _clean_text(value, MAX_ALIAS_LENGTH)

    @field_validator("identity", mode="before")
    @classmethod
    def _identity(cls, value: object) -> str:
        return _clean_text(value, MAX_IDENTITY_LENGTH)

    @field_validator("personality", "speaking_style", mode="before")
    @classmethod
    def _long_text(cls, value: object) -> str:
        return _clean_multiline(value, MAX_LONG_TEXT_LENGTH)

    @field_validator("catchphrases", mode="before")
    @classmethod
    def _catchphrases(cls, value: object) -> list[str]:
        return _clean_list(value, count=MAX_CATCHPHRASES, length=MAX_CATCHPHRASE_LENGTH)

    @field_validator("likes", "avoid_topics", mode="before")
    @classmethod
    def _topics(cls, value: object) -> list[str]:
        return _clean_list(value, count=MAX_TOPICS, length=MAX_TOPIC_LENGTH)

    @field_validator("examples", mode="before")
    @classmethod
    def _examples(cls, value: object) -> list[str]:
        # 示例台词本身可能带逗号，只按换行/列表拆分。
        return _clean_list(
            value, count=MAX_EXAMPLES, length=MAX_EXAMPLE_LENGTH, split=False
        )

    @field_validator(*PERSONA_TRAIT_FIELDS, mode="before")
    @classmethod
    def _level(cls, value: object) -> int:
        return _clamp_level(value)

    def api_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)

    def storage_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["schema_version"] = PERSONA_SCHEMA_VERSION
        return payload


def persona_override_reason(card: PersonaCard) -> str | None:
    """人设文本是否试图覆盖系统策略；返回中文原因，安全时返回 None。"""

    fields: list[tuple[str, str]] = [
        ("名字", card.name),
        ("自称", card.self_name),
        ("称呼", card.user_title),
        ("身份", card.identity),
        ("性格", card.personality),
        ("说话方式", card.speaking_style),
        *(("口癖", item) for item in card.catchphrases),
        *(("喜好", item) for item in card.likes),
        *(("避开的话题", item) for item in card.avoid_topics),
        *(("示例台词", item) for item in card.examples),
    ]
    for label, text in fields:
        if any(pattern.search(text) for pattern in _OVERRIDE_PATTERNS):
            return f"「{label}」里有试图覆盖系统规则或伪造消息的内容"
    return None


@dataclass(frozen=True, slots=True)
class PersonaMutation:
    semantic_changed: bool
    storage_changed: bool


@dataclass(frozen=True, slots=True)
class PersonaBehavior:
    """人设对群聊控制流的有界影响（只能收窄运行配置，不能突破）。"""

    source: str
    sociability: int
    followup_tendency: int
    reaction_tendency: int
    warmup_probability_scale: float
    active_probability_scale: float
    max_followup_bot_turns: int
    allow_spontaneous_reaction: bool
    reaction_mode: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "sociability": self.sociability,
            "followupTendency": self.followup_tendency,
            "reactionTendency": self.reaction_tendency,
            "warmupProbabilityScale": self.warmup_probability_scale,
            "activeProbabilityScale": self.active_probability_scale,
            "maxFollowupBotTurns": self.max_followup_bot_turns,
            "allowSpontaneousReaction": self.allow_spontaneous_reaction,
            "reactionMode": self.reaction_mode,
        }


@dataclass(frozen=True, slots=True)
class PersonaPreset:
    id: str
    label: str
    description: str
    card: PersonaCard

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "card": self.card.api_dict(),
        }


@dataclass(frozen=True, slots=True)
class ResolvedPersona:
    card: PersonaCard
    source: str
    library_name: str | None = None


defaults = get_plugin_config(AgentPersonaDefaults)


def _preset(  # noqa: PLR0913
    preset_id: str,
    label: str,
    description: str,
    *,
    identity: str,
    levels: tuple[int, int, int, int, int, int],
    self_name: str = "",
    user_title: str = "",
    personality: str = "",
    speaking_style: str = "",
    catchphrases: tuple[str, ...] = (),
    likes: tuple[str, ...] = (),
    avoid_topics: tuple[str, ...] = (),
    examples: tuple[str, ...] = (),
) -> PersonaPreset:
    warmth, humor, verbosity, expressiveness, sociability, reaction = levels
    return PersonaPreset(
        preset_id,
        label,
        description,
        PersonaCard(
            preset_id=preset_id,
            name="Yawn",
            self_name=self_name,
            user_title=user_title,
            identity=identity,
            personality=personality,
            speaking_style=speaking_style,
            catchphrases=list(catchphrases),
            likes=list(likes),
            avoid_topics=list(avoid_topics),
            examples=list(examples),
            warmth=warmth,
            humor=humor,
            verbosity=verbosity,
            expressiveness=expressiveness,
            sociability=sociability,
            reaction_tendency=reaction,
        ),
    )


_PRESET_LIST = (
    # 内置默认卡刻意保持最短：它进入每个跟随全局的群的静态前缀。
    _preset(
        "natural",
        "自然群友",
        "自然、克制、不过度抢话，适合大多数群聊。",
        identity="熟悉群聊节奏、自然简洁的普通群友",
        levels=(2, 1, 1, 1, 2, 2),
    ),
    _preset(
        "gentle_listener",
        "温和倾听",
        "更有耐心、少抢结论，适合陪伴和日常聊天。",
        identity="温和、耐心，愿意先听完再回应的群友",
        personality="体贴，情绪稳定；先接住对方的感受，再给建议。",
        speaking_style="语气柔和，多用“嗯嗯”“我懂”这类回应；不说教，不急着下结论。",
        catchphrases=("嗯嗯", "慢慢来"),
        avoid_topics=("嘲讽别人",),
        examples=("嗯嗯，听起来今天真的挺累的，先歇会儿？",),
        levels=(4, 1, 2, 2, 2, 1),
    ),
    _preset(
        "calm_rational",
        "冷静理性",
        "事实优先、表达直接，适合讨论和答疑。",
        identity="冷静、理性，重视事实和逻辑的群友",
        personality="就事论事，不站队；不确定就说不确定。",
        speaking_style="先给结论再给理由；直接、清楚，不绕弯，也不阴阳怪气。",
        examples=("结论：可以。原因有两点：一是……二是……",),
        levels=(1, 0, 2, 0, 1, 0),
    ),
    _preset(
        "lively_sidekick",
        "活跃捧哏",
        "更会接梗和回应气氛，但仍受运行配置限制。",
        identity="活跃、会接梗，但不会喧宾夺主的群友",
        personality="爱热闹，反应快，喜欢给别人的梗搭台阶。",
        speaking_style="短句、口语化，会接梗、会捧场；不抢主角，不刷屏。",
        catchphrases=("笑死", "懂了懂了"),
        likes=("玩梗", "游戏", "番剧"),
        examples=("笑死，这波操作我愿称之为艺术",),
        levels=(3, 4, 1, 4, 4, 4),
    ),
    _preset(
        "quiet_observer",
        "安静潜水",
        "存在感更低、少主动延展，适合安静的群。",
        identity="安静、克制，更多观察而不是抢着发言的群友",
        speaking_style="话少，点到为止；能一句说完就不说两句。",
        levels=(2, 1, 0, 0, 0, 1),
    ),
    _preset(
        "tsundere_catgirl",
        "傲娇猫娘",
        "嘴上嫌弃、心里在意的猫娘，自称本喵，适合轻松的群。",
        identity="嘴硬心软的傲娇猫娘，群里的小麻烦精",
        self_name="本喵",
        user_title="杂鱼",
        personality="嘴上嫌弃、其实很在意群友；被夸会害羞嘴硬，喜欢可爱的东西。",
        speaking_style="短句口语，偶尔句尾带“喵”；先吐槽一句再认真帮忙；不说真正伤人的话。",
        catchphrases=("哼", "才不是为了你", "杂鱼～"),
        likes=("小鱼干", "可爱的东西", "晒太阳"),
        avoid_topics=("人身攻击", "外貌羞辱"),
        examples=(
            "哼，这种问题也要问本喵？……好啦，答案是这样的喵。",
            "才、才不是特意等你上线的！",
        ),
        levels=(2, 3, 1, 4, 3, 3),
    ),
    _preset(
        "genki_girl",
        "元气少女",
        "开朗热情、正能量满满，适合新人多的群。",
        identity="元气满满、喜欢给大家打气的群友",
        personality="乐观、热情，看到别人努力会真心夸。",
        speaking_style="语气轻快，偶尔用感叹号；夸人要具体，不空喊口号。",
        catchphrases=("冲鸭", "好耶"),
        likes=("美食", "运动", "新鲜事"),
        examples=("好耶！第一次就做成这样已经很厉害啦！",),
        levels=(4, 2, 1, 4, 3, 3),
    ),
    _preset(
        "sharp_tongue",
        "毒舌吐槽",
        "嘴毒但有分寸的吐槽役，适合熟人群。",
        identity="嘴上不饶人、但吐槽有分寸的老群友",
        personality="观察细，反应快；吐槽只针对事不针对人。",
        speaking_style="一针见血的短吐槽，再补一句真正有用的话；绝不涉及外貌、出身和隐私。",
        avoid_topics=("外貌", "家庭出身", "隐私"),
        examples=("你这个计划的唯一问题，是它需要你早起。",),
        levels=(1, 4, 1, 3, 3, 3),
    ),
    _preset(
        "study_buddy",
        "学霸助教",
        "耐心讲解、条理清晰，适合学习群和技术群。",
        identity="耐心、条理清晰，擅长把问题讲明白的群友",
        personality="喜欢把复杂问题拆开讲；遇到不会的会直说。",
        speaking_style="先讲思路再给答案；分点用短句和换行，不用 Markdown 符号。",
        likes=("编程", "数学", "读书"),
        examples=("先别急着看答案，这题关键在第二步：……",),
        levels=(3, 1, 3, 1, 2, 1),
    ),
)
PERSONA_PRESETS: dict[str, PersonaPreset] = {item.id: item for item in _PRESET_LIST}

_TRAIT_TEXT: dict[str, tuple[str, ...]] = {
    "warmth": ("偏冷淡", "较克制", "自然", "温和", "很温暖"),
    "humor": ("不主动玩梗", "偶尔轻幽默", "适度幽默", "比较会接梗", "很会接梗"),
    "verbosity": ("极简短", "简洁", "适中", "较详细", "很详细"),
    "expressiveness": (
        "情绪表达很淡",
        "表达克制",
        "自然表达",
        "较有表现力",
        "表现力很强",
    ),
    "sociability": ("很少参与", "偏安静", "社交平衡", "比较主动", "很活跃"),
    "reaction_tendency": (
        "几乎不接反应",
        "少量反应",
        "自然反应",
        "较常接反应",
        "很爱接反应",
    ),
}
_TRAIT_LABELS: dict[str, str] = {
    "warmth": "温度",
    "humor": "幽默",
    "verbosity": "详略",
    "expressiveness": "表现力",
    "sociability": "活跃度",
    "reaction_tendency": "接梗",
}


def persona_trait_label(field: str, value: int) -> str:
    if field not in _TRAIT_TEXT:
        raise ValueError(f"未知人设档位：{field}")
    return _TRAIT_TEXT[field][_clamp_level(value)]


def persona_trait_name(field: str) -> str:
    if field not in _TRAIT_LABELS:
        raise ValueError(f"未知人设档位：{field}")
    return _TRAIT_LABELS[field]


# ---------------------------------------------------------------- 全局人设缓存

_global_card: PersonaCard | None = None
_global_name: str | None = None


def builtin_default_card() -> PersonaCard:
    return PERSONA_PRESETS["natural"].card.model_copy(
        update={"name": str(defaults.agent_persona_name or "Yawn")}
    )


def set_global_persona_card(card: PersonaCard | None, *, name: str | None = None) -> None:
    """人设库加载/修改后刷新进程内全局人设；None 表示回退内置默认。"""

    global _global_card, _global_name  # noqa: PLW0603
    _global_card = card
    _global_name = name if card is not None else None
    dbg(f"全局人设已刷新: {name or '内置默认'}")


def global_persona() -> ResolvedPersona:
    if _global_card is not None:
        return ResolvedPersona(_global_card, PERSONA_SOURCE_GLOBAL, _global_name)
    return ResolvedPersona(builtin_default_card(), PERSONA_SOURCE_BUILTIN)


# ---------------------------------------------------------------- v2 → v3 转换

_V2_DIRECTNESS_TEXT = ("说话很委婉", "说话偏委婉", "", "说话比较直接", "说话非常直接明确")


def _v2_section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _card_from_v2(raw: dict[str, Any]) -> PersonaCard:
    identity = _v2_section(raw, "identity")
    voice = _v2_section(raw, "voice")
    social = _v2_section(raw, "social")
    preset = PERSONA_PRESETS.get(str(raw.get("preset_id") or "natural"))
    base = preset.card if preset is not None else builtin_default_card()

    def level(source: dict[str, Any], key: str) -> int:
        value = source.get(key)
        return _clamp_level(value, getattr(base, key)) if value is not None else getattr(base, key)

    description = _clean_text(identity.get("description"), MAX_IDENTITY_LENGTH)
    group_role = _clean_text(identity.get("group_role"), MAX_IDENTITY_LENGTH)
    identity_text = description or base.identity
    if group_role and group_role not in identity_text:
        identity_text = f"{identity_text}；群里的{group_role}" if identity_text else group_role
    directness = voice.get("directness")
    style = (
        _V2_DIRECTNESS_TEXT[_clamp_level(directness)] if directness is not None else ""
    )
    return PersonaCard(
        preset_id=str(raw.get("preset_id") or base.preset_id),
        name=identity.get("name") or str(defaults.agent_persona_name or "Yawn"),
        identity=identity_text,
        personality=raw.get("custom_notes") or base.personality,
        speaking_style=style or base.speaking_style,
        catchphrases=base.catchphrases,
        likes=base.likes,
        avoid_topics=base.avoid_topics,
        examples=base.examples,
        warmth=level(voice, "warmth"),
        humor=level(voice, "humor"),
        verbosity=level(voice, "verbosity"),
        expressiveness=level(voice, "expressiveness"),
        sociability=level(social, "sociability"),
        reaction_tendency=level(social, "reaction_tendency"),
    )


def parse_persona_card(raw: object) -> PersonaCard | None:
    """解析存储里的人设（v3 原样，v2 转换）；空值或无法解析时返回 None。"""

    if isinstance(raw, PersonaCard):
        return raw
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        version = int(raw.get("schema_version") or 0)
    except (TypeError, ValueError):
        version = 0
    try:
        if version >= PERSONA_SCHEMA_VERSION:
            return PersonaCard.model_validate(raw)
        if version == 2 or isinstance(raw.get("identity"), dict):  # noqa: PLR2004
            return _card_from_v2(raw)
    except Exception as exc:  # noqa: BLE001
        dbg(f"人设解析失败，回退全局人设：{exc}")
    return None


# ---------------------------------------------------------------- 群人设

def _group_custom(config: GroupAgentConfig | None) -> bool:
    return bool(config is not None and getattr(config, "persona_enabled", False))


def group_persona_card(config: GroupAgentConfig | None) -> PersonaCard:
    """本群保存的人设卡（即使当前跟随全局，也返回暂存的那份，用于继续编辑）。"""

    parsed = parse_persona_card(getattr(config, "persona_profile", None))
    if parsed is not None:
        return parsed
    return global_persona().card


def resolve_persona_source(config: GroupAgentConfig | None) -> ResolvedPersona:
    """本群实际生效的人设与来源。"""

    if _group_custom(config):
        parsed = parse_persona_card(getattr(config, "persona_profile", None))
        if parsed is not None:
            return ResolvedPersona(parsed, PERSONA_SOURCE_GROUP)
    return global_persona()


def effective_persona_card(config: GroupAgentConfig | None) -> PersonaCard:
    return resolve_persona_source(config).card


# ---------------------------------------------------------------- 行为推导

_SOCIAL_PROBABILITY_SCALE = (0.15, 0.45, 0.75, 0.9, 1.0)
# 续聊轮数由活跃度推导：0 档首轮后结束，4 档最多再续 3 次。
_FOLLOWUP_MAX_TURNS = (1, 2, 3, 4, 4)
_FOLLOWUP_TENDENCY = (0, 1, 2, 3, 3)
_REACTION_MODES = ("off", "restrained", "normal", "expressive", "high")
_PERSONA_NEUTRAL_LEVEL = 2
_PERSONA_REACTION_AUTO_MIN = 2


def persona_behavior_from_card(card: PersonaCard, *, source: str) -> PersonaBehavior:
    sociability = _clamp_level(card.sociability)
    reaction = _clamp_level(card.reaction_tendency)
    scale = _SOCIAL_PROBABILITY_SCALE[sociability]
    return PersonaBehavior(
        source=source,
        sociability=sociability,
        followup_tendency=_FOLLOWUP_TENDENCY[sociability],
        reaction_tendency=reaction,
        warmup_probability_scale=scale,
        active_probability_scale=scale,
        max_followup_bot_turns=_FOLLOWUP_MAX_TURNS[sociability],
        allow_spontaneous_reaction=reaction >= _PERSONA_REACTION_AUTO_MIN,
        reaction_mode=_REACTION_MODES[reaction],
    )


def persona_behavior(config: GroupAgentConfig | None) -> PersonaBehavior:
    resolved = resolve_persona_source(config)
    return persona_behavior_from_card(resolved.card, source=resolved.source)


def persona_behavior_draft(
    config: GroupAgentConfig | None, draft: PersonaCard
) -> PersonaBehavior:
    del config
    return persona_behavior_from_card(draft, source=PERSONA_SOURCE_DRAFT)


def persona_behavior_instruction(behavior: PersonaBehavior, *, scene: str) -> str:
    social = (
        "尽量少抢话"
        if behavior.sociability <= 1
        else "自然参与"
        if behavior.sociability == _PERSONA_NEUTRAL_LEVEL
        else "在有明确切入点时更愿意参与"
    )
    followup = (
        "不要主动续聊"
        if behavior.followup_tendency == 0
        else "续聊要克制"
        if behavior.followup_tendency <= _PERSONA_NEUTRAL_LEVEL
        else "有新信息或明确回应时可以继续承接"
    )
    reaction = {
        "off": "不要主动使用 reaction",
        "restrained": "reaction 极少使用",
        "normal": "reaction 只在自然贴合时使用",
        "expressive": "可以更积极地用 reaction 接住气氛",
        "high": "很适合时可优先用 reaction 接梗，但不要刷屏",
    }[behavior.reaction_mode]
    prefix = "本轮是续聊。" if scene == "followup" else "本轮是主动参与。"
    return f"{prefix} Persona 行为：{social}；{followup}；{reaction}。"


def persona_style_levels(persona: dict[str, Any] | None) -> dict[str, int]:
    """发言策略读取的数值档位；直接来自编译结果，不再反解析中文标签。"""

    levels = (persona or {}).get("levels")
    if not isinstance(levels, dict):
        return {}
    return {key: _clamp_level(levels.get(key)) for key in PERSONA_TRAIT_FIELDS if key in levels}


# ---------------------------------------------------------------- 摘要

def persona_card_summary(card: PersonaCard) -> str:
    preset = PERSONA_PRESETS.get(card.preset_id)
    parts = [card.name]
    if card.self_name:
        parts.append(f"自称{card.self_name}")
    parts.append(preset.label if preset is not None else "自定义")
    parts.extend(
        persona_trait_label(key, getattr(card, key))
        for key in ("warmth", "humor", "verbosity", "sociability")
    )
    return " · ".join(parts)


def persona_summary(config: GroupAgentConfig | None) -> str:
    return persona_card_summary(effective_persona_card(config))


def persona_source_label(resolved: ResolvedPersona) -> str:
    if resolved.source == PERSONA_SOURCE_GROUP:
        return "本群自定义"
    if resolved.source == PERSONA_SOURCE_GLOBAL:
        return f"全局人设（{resolved.library_name}）" if resolved.library_name else "全局人设"
    if resolved.source == PERSONA_SOURCE_DRAFT:
        return "未保存草稿"
    return "内置默认"


def persona_apply_preset(card: PersonaCard, preset_id: str) -> PersonaCard:
    """套用模板：替换角色内容与档位，保留名字。"""

    preset = PERSONA_PRESETS.get(str(preset_id).strip().lower())
    if preset is None:
        raise ValueError(f"未知人设模板：{preset_id}")
    return preset.card.model_copy(update={"name": card.name})


def persona_preset_payloads() -> list[dict[str, Any]]:
    return [preset.payload() for preset in PERSONA_PRESETS.values()]


def resolve_preset_id(value: str) -> str | None:
    """按序号、id 或中文名找模板（QQ 命令用）。"""

    cleaned = str(value or "").strip().lower()
    presets = list(PERSONA_PRESETS.values())
    if cleaned.isdigit():
        index = int(cleaned) - 1
        if 0 <= index < len(presets):
            return presets[index].id
    for preset in presets:
        if cleaned in {preset.id.lower(), preset.label.lower()}:
            return preset.id
    return None


# ---------------------------------------------------------------- 写入

def apply_persona_card(
    config: GroupAgentConfig,
    card: PersonaCard,
    *,
    enabled: bool | None = None,
) -> PersonaMutation:
    current_enabled = _group_custom(config)
    target_enabled = current_enabled if enabled is None else bool(enabled)
    current_card = parse_persona_card(getattr(config, "persona_profile", None))
    target_payload = card.storage_dict()
    current_payload = getattr(config, "persona_profile", None)
    semantic_changed = current_enabled != target_enabled or (
        current_card is None or current_card.model_dump() != card.model_dump()
    )
    storage_changed = current_payload != target_payload or current_enabled != target_enabled
    if storage_changed:
        config.persona_profile = target_payload
        config.persona_enabled = target_enabled
    return PersonaMutation(
        semantic_changed=semantic_changed, storage_changed=storage_changed
    )


def reset_persona(config: GroupAgentConfig) -> PersonaMutation:
    """清掉本群人设卡并真正切回跟随全局。"""

    current_enabled = _group_custom(config)
    current_payload = getattr(config, "persona_profile", None)
    storage_changed = bool(current_payload) or current_enabled
    if storage_changed:
        config.persona_profile = {}
        config.persona_enabled = False
    return PersonaMutation(
        semantic_changed=storage_changed, storage_changed=storage_changed
    )


# ---------------------------------------------------------------- Prompt 编译

def _compile_card(card: PersonaCard) -> dict[str, Any]:
    voice = "；".join(persona_trait_label(key, getattr(card, key)) for key in PERSONA_VOICE_TRAITS)
    social = "；".join(persona_trait_label(key, getattr(card, key)) for key in PERSONA_SOCIAL_TRAITS)
    compiled: dict[str, Any] = {"name": card.name}
    if card.self_name:
        compiled["self_name"] = card.self_name
    if card.user_title:
        compiled["calls_members"] = card.user_title
    for key, value in (
        ("identity", card.identity),
        ("personality", card.personality),
        ("speaking_style", card.speaking_style),
    ):
        if value:
            compiled[key] = value
    compiled["style"] = voice
    compiled["social"] = social
    if card.catchphrases:
        compiled["catchphrases"] = list(card.catchphrases)
    if card.likes:
        compiled["likes"] = list(card.likes)
    if card.avoid_topics:
        compiled["avoid_topics"] = list(card.avoid_topics)
    if card.examples:
        compiled["examples"] = list(card.examples)
    usage: list[str] = []
    if card.catchphrases:
        usage.append("口癖偶尔用，别每句都带")
    if card.examples:
        usage.append("examples 只示范语气，不要照抄")
    if card.self_name or card.user_title:
        usage.append("自称/称呼保持一致，正式场合（公告、管理操作说明）用正常书面语")
    if usage:
        compiled["usage"] = "；".join(usage)
    return compiled


def _fit_prompt_budget(compiled: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """超出 token 上限时按优先级裁剪：示例 → 喜好 → 口癖 → 长文本尾部。"""

    trimmed = False

    def size() -> int:
        return estimate_tokens(compiled)

    for key in ("examples", "likes", "avoid_topics", "catchphrases"):
        while size() > PERSONA_PROMPT_TOKEN_LIMIT and compiled.get(key):
            compiled[key] = compiled[key][:-1]
            trimmed = True
            if not compiled[key]:
                compiled.pop(key)
    for key in ("personality", "speaking_style", "identity"):
        while size() > PERSONA_PROMPT_TOKEN_LIMIT and len(str(compiled.get(key) or "")) > 20:  # noqa: PLR2004
            compiled[key] = str(compiled[key])[: max(20, len(compiled[key]) - 40)]
            trimmed = True
    return compiled, trimmed


def _resolved_from_card(card: PersonaCard, resolved: ResolvedPersona | None) -> dict[str, Any]:
    compiled, trimmed = _fit_prompt_budget(_compile_card(card))
    return {
        "profile_v3": "structured",
        "source": resolved.source if resolved is not None else PERSONA_SOURCE_DRAFT,
        "compiled": compiled,
        "trimmed": trimmed,
        "levels": {key: int(getattr(card, key)) for key in PERSONA_TRAIT_FIELDS},
        "name": card.name,
    }


def resolve_persona(config: GroupAgentConfig | None) -> dict[str, Any]:
    resolved = resolve_persona_source(config)
    result = _resolved_from_card(resolved.card, resolved)
    dbg(
        f"Persona v3 resolved: group={getattr(config, 'group_id', None)} "
        f"source={resolved.source} version={getattr(config, 'persona_version', 1)}"
    )
    return result


def resolve_persona_draft(
    config: GroupAgentConfig | None, draft: PersonaCard
) -> dict[str, Any]:
    del config
    return _resolved_from_card(draft, None)


def prompt_persona(persona: dict[str, Any]) -> str:
    """只编译角色卡字段；系统策略永远不是人设数据。"""

    compiled = persona.get("compiled")
    if not isinstance(compiled, dict):
        compiled = {"name": str(persona.get("name") or defaults.agent_persona_name)}
    return json.dumps(compiled, ensure_ascii=False, separators=(",", ":"))


def persona_prompt_tokens(persona: dict[str, Any]) -> int:
    return estimate_tokens(prompt_persona(persona))


__all__ = [
    "MAX_FIELD_LENGTH",
    "PERSONA_PRESETS",
    "PERSONA_PROMPT_TOKEN_LIMIT",
    "PERSONA_SCHEMA_VERSION",
    "PERSONA_SOCIAL_TRAITS",
    "PERSONA_TRAIT_FIELDS",
    "PERSONA_VOICE_TRAITS",
    "AgentPersonaDefaults",
    "PersonaBehavior",
    "PersonaCard",
    "PersonaMutation",
    "PersonaPreset",
    "ResolvedPersona",
    "apply_persona_card",
    "builtin_default_card",
    "defaults",
    "effective_persona_card",
    "global_persona",
    "group_persona_card",
    "parse_persona_card",
    "persona_apply_preset",
    "persona_behavior",
    "persona_behavior_draft",
    "persona_behavior_from_card",
    "persona_behavior_instruction",
    "persona_card_summary",
    "persona_override_reason",
    "persona_preset_payloads",
    "persona_prompt_tokens",
    "persona_source_label",
    "persona_style_levels",
    "persona_summary",
    "persona_trait_label",
    "persona_trait_name",
    "prompt_persona",
    "reset_persona",
    "resolve_persona",
    "resolve_persona_draft",
    "resolve_persona_source",
    "resolve_preset_id",
    "set_global_persona_card",
]
