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
    ww_signup_timeout: int = 180
    # 报名窗口剩余多少秒时第一次提醒
    ww_signup_warn_remain: int = 60
    # 报名窗口剩余多少秒时第二次（末次）提醒
    ww_signup_warn_remain_final: int = 20

    # 报名阶段选身份开关：开启后玩家可私聊 /选身份 请求期望角色，
    # 多人同选一个身份时发牌在请求者中按份数随机分配
    ww_role_request: bool = True

    # 女巫 / 预言家夜间行动阶段的时长（秒）：二者收到有效行动即提前
    # 结束，只有彻底无行动才会等满窗口
    ww_night_timeout: int = 60
    # 狼人阶段专属时长（秒）：两段式狼队讨论 + 串行出刀需要更长窗口，
    # 与女巫/预言家解耦，避免把后两者也拖长
    ww_wolf_timeout: int = 180
    # 夜间通用心跳播报间隔（秒）：夜晚全群禁言期间每隔该间隔播报一条
    # 不含角色/阶段信息的氛围文案，填补长夜死寂（旧语义"按子阶段剩余
    # 秒数点名提醒"已废弃——那会向全群暴露当前行动角色）
    ww_night_warn_remain: int = 30

    # 每人发言时长（秒），竞选发言与 PK 发言同样使用
    ww_speech_timeout: int = 120
    # 投票阶段时长（秒），警长投票 / 放逐投票 / PK 投票同样使用
    ww_vote_timeout: int = 90

    # 猎人开枪决策时长（秒）
    ww_hunter_timeout: int = 60
    # 遗言时长（秒）
    ww_last_words_timeout: int = 60

    # 警长竞选报名窗口（秒）
    ww_sheriff_register_timeout: int = 45
    # 移交警徽决策时长（秒）；同时复用为白天发言排序决策窗口
    ww_badge_timeout: int = 45

    # OneBot API 调用超时（秒）：消息发送、禁言、成员查询等全部经
    # api.py 封装并包 asyncio.wait_for，协议端挂起时降级为 warning，
    # 不卡死引擎任务
    ww_api_timeout: float = 10.0

    # ── AI 玩家 ──
    # AI 玩家总开关
    ww_ai_enabled: bool = True
    # /开始游戏 人数不足时自动用 AI 补位到最低开局数
    ww_ai_autofill: bool = True
    # 单局 AI 玩家数量上限
    ww_ai_max: int = 11
    # AI 单次决策（非发言）的 LLM 调用超时（秒）。
    # 推理模型非流式补全须等整段推理完成才有响应，需与各阶段
    # 窗口对齐，过短会导致 AI 全程超时托管。
    ww_ai_decision_timeout: float = 90.0
    # AI 发言的 LLM 调用超时（秒），对齐发言窗口
    ww_ai_speech_timeout: float = 90.0
    # AI 决策（非发言）单次生成的最大 token 数。推理模型会把大量
    # token 耗在内部推理上，预算不足会被截断（finish_reason=length）
    # 返回空内容而全程托管，故给足余量
    ww_ai_max_tokens: int = 4096
    # AI 发言单次生成的最大 token 数（同样需覆盖推理开销）
    ww_ai_speech_max_tokens: int = 2048
    # 狼队是否进行两段式讨论：先各自 说XXX 提议并互看，再统一 刀N。
    # 关闭则退回单段直接出刀
    ww_ai_wolf_discuss: bool = True
    # 狼队讨论阶段（提议）的 LLM 调用超时（秒）：短于决策超时，
    # 给随后的刀口阶段留出窗口时间
    ww_ai_discuss_timeout: float = 45.0
    # 有 AI 参与时警长竞选报名窗口的延长量（秒）：
    # 给 AI 的竞选决策留出 LLM 调用时间，避免迟到的上警被丢弃
    ww_ai_register_buffer: int = 15
