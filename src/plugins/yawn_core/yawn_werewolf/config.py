"""狼人杀子插件配置。

所有字段可通过环境变量覆盖（字段名大写即可，
如 WW_SIGNUP_TIMEOUT=180）。
"""

from pydantic import BaseModel


class Config(BaseModel):
    """狼人杀玩法与超时配置。"""

    # 最小开局人数
    ww_min_players: int = 9
    # 满员自动开局人数
    ww_max_players: int = 12

    # 报名窗口（秒）
    ww_signup_timeout: int = 120
    # 报名窗口剩余多少秒时提醒一次
    ww_signup_warn_remain: int = 60

    # 每个夜间行动阶段的时长（秒）
    ww_night_timeout: int = 60
    # 夜间阶段剩余多少秒时提醒一次
    ww_night_warn_remain: int = 30

    # 每人发言时长（秒），竞选发言与 PK 发言同样使用
    ww_speech_timeout: int = 90
    # 投票阶段时长（秒），警长投票 / 放逐投票 / PK 投票同样使用
    ww_vote_timeout: int = 60

    # 猎人开枪决策时长（秒）
    ww_hunter_timeout: int = 60
    # 遗言时长（秒）
    ww_last_words_timeout: int = 60

    # 警长竞选报名窗口（秒）
    ww_sheriff_register_timeout: int = 30
    # 移交警徽决策时长（秒）
    ww_badge_timeout: int = 30

    # ── AI 玩家 ──
    # AI 玩家总开关
    ww_ai_enabled: bool = True
    # /开始游戏 人数不足时自动用 AI 补位到最低开局数
    ww_ai_autofill: bool = True
    # 单局 AI 玩家数量上限
    ww_ai_max: int = 11
    # AI 单次决策（非发言）的 LLM 调用超时（秒）
    ww_ai_decision_timeout: float = 15.0
    # AI 发言的 LLM 调用超时（秒）
    ww_ai_speech_timeout: float = 20.0
