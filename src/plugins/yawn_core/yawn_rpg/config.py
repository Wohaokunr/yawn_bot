"""跑团子插件配置。

所有字段可通过环境变量覆盖（字段名大写即可，
如 RPG_SIGNUP_TIMEOUT=180）。
"""

from typing import Optional

from pydantic import BaseModel


class Config(BaseModel):
    """跑团玩法、建卡与 AI 配置。"""

    # ── 房间与报名 ──
    # 最低开局人数（模组自身的 min_players 取两者较大值生效）
    rpg_min_players: int = 1
    # 房间人数上限（同时受模组 max_players 约束）
    rpg_max_players: int = 6
    # 报名窗口（秒）
    rpg_signup_timeout: int = 120
    # 报名窗口剩余多少秒时提醒一次
    rpg_signup_warn_remain: int = 60

    # ── 建卡（私聊）──
    # 建卡窗口（秒）：超时未确认的角色卡自动确认
    rpg_char_create_timeout: int = 180
    # 每人整卡重掷次数上限
    rpg_char_reroll_max: int = 3
    # 可自由分配的技能点总量；None 表示按 CoC 惯例取 INT×2
    rpg_char_skill_pool: Optional[int] = None
    # 建卡期间单项技能可达到的上限
    rpg_char_skill_cap: int = 75

    # ── 局内节奏 ──
    # PLAY 阶段无人行动的解散时长（秒）
    rpg_idle_timeout: int = 600
    # 空闲剩余多少秒时提醒一次
    rpg_idle_warn_remain: int = 120
    # 连续自由发言的合批窗口（秒）：窗口内的 SAY 合并为一次 KP 调用
    rpg_say_settle_window: float = 2.5
    # 单条玩家发言截断长度
    rpg_speech_truncate: int = 300
    # 每次合批最多喂给 KP 的发言条数
    rpg_kp_max_batch_lines: int = 6
    # 局面上下文（群聊记录）保留行数
    rpg_max_context_lines: int = 40

    # ── AI 主持人（KP）──
    # AI 总开关：关闭后全程确定性模式（关键词自动检定 + 固定文案）
    rpg_ai_enabled: bool = True
    # 单个 KP 回合内允许的工具调用轮数上限
    rpg_ai_max_tool_rounds: int = 4
    # 单个 KP 回合的总时长预算（秒）
    rpg_ai_turn_timeout: float = 90.0
    # KP 单次 LLM 调用超时（秒）。推理模型非流式补全须等整段推理
    # 完成才有响应，过短会全程超时降级
    rpg_kp_timeout: float = 40.0
    # KP 单次生成的最大 token 数（需覆盖推理开销，否则
    # finish_reason=length 截断返空）
    rpg_kp_max_tokens: int = 2048
    # KP 生成温度（叙述类调高）
    rpg_kp_temperature: float = 0.8
    # KP 最终旁白截断长度
    rpg_kp_max_output_chars: int = 400
    # 两次 KP 调用之间的最小间隔（秒）：宁延长合批窗口也不丢发言
    rpg_kp_min_interval: float = 3.0
    # AI 工具 deal_damage/heal 单次生效上限（钳制 AI 数值权）
    rpg_ai_max_damage_per_call: int = 5
    # AI 工具 san_check 单次损失上限
    rpg_ai_max_san_loss: int = 10
