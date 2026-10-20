"""模型时间戳的 Python 侧默认值。"""

from datetime import datetime, timedelta, timezone

_BJ_TZ = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    """全库约定：naive 北京时间（UTC+8）。

    ``server_default=CURRENT_TIMESTAMP`` 在 SQLite 中写入的是 UTC，序列化时会被
    当作北京时间而整体早 8 小时；模型同时声明 Python 侧 ``default``/``onupdate``
    后，ORM 写入都走这里，与其余代码的 ``now_beijing()`` 口径一致。
    """

    return datetime.now(_BJ_TZ).replace(tzinfo=None)
