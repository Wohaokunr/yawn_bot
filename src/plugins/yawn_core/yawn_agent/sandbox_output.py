# ruff: noqa: E501, C901
"""把 runner 的运行结果整理成模型最好用的样子。

旧版只做“头 70% + 尾 30%”的字符截断：长 traceback 被拦腰截断后，最关键的
最后一行异常经常看不全；matplotlib 的字体警告还会挤掉真正的输出。这里：

- traceback 只留用户代码帧和最后一帧库代码，开头给出 ``error`` 摘要
  （异常类型、消息、行号、那一行代码），模型不用在一堆库帧里找原因；
- 常见错误给对症提示（不能 pip、没有网络、文件不存在、语法/缩进错误…）；
- 运行成功时滤掉无关警告；截断按行切，不在半个数字处断开，并说明怎么拿全量。
"""

from __future__ import annotations

import re
from typing import Any

_TRACEBACK_HEAD = "Traceback (most recent call last):"
_FRAME_RE = re.compile(r'^  File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.+))?$')
_USER_FILE = "main.py"
_EXCEPTION_LINE_RE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt|Iteration)\w*|KeyboardInterrupt)(?::\s*(?P<msg>.*))?$")
# 运行成功时这些 stderr 行只是噪音。
_NOISE_PATTERNS = (
    re.compile(r"Glyph \d+ .*missing from (?:current )?font", re.IGNORECASE),
    re.compile(r"findfont: Font family .* not found", re.IGNORECASE),
    re.compile(r"(?:Future|Deprecation|Pending\w*|Runtime|User)Warning"),
    re.compile(r"^\s*warnings\.warn\("),
    re.compile(r"Matplotlib is building the font cache", re.IGNORECASE),
)
_ERROR_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"ModuleNotFoundError|No module named", re.IGNORECASE),
     "沙箱不能 pip install；换成预装库（numpy/pandas/scipy/matplotlib/seaborn/sympy/networkx/pillow/openpyxl/mido/jieba/wordcloud/qrcode/yawnkit），或用标准库实现"),
    (re.compile(r"ConnectionError|gaierror|Name or service not known|urlopen error|Network is unreachable|NewConnectionError|Temporary failure in name resolution", re.IGNORECASE),
     "沙箱没有网络，不能下载或访问网址；需要的数据请写在代码里或让对方发文件"),
    (re.compile(r"FileNotFoundError|No such file or directory"),
     "文件不存在：先用 list_sandbox_files 看工作区里真实的文件名，路径用相对路径"),
    (re.compile(r"IndentationError|TabError"),
     "缩进不一致：统一用 4 个空格缩进后重写这段代码"),
    (re.compile(r"SyntaxError"),
     "语法错误：检查括号、引号和冒号是否成对，中文标点不要写进代码"),
    (re.compile(r"NameError"),
     "用到了没定义的名字：每次运行都是新进程，上次运行里的变量不会保留，需要的话重新计算或从文件读取"),
    (re.compile(r"KeyError"),
     "键/列名不存在：先 print(df.columns) 或 print(dict.keys()) 看清真实名字"),
    (re.compile(r"PermissionError|Read-only file system"),
     "只能写当前工作区目录，不要写系统目录或绝对路径"),
    (re.compile(r"UnicodeDecodeError"),
     "文件编码不是 UTF-8：读取时试试 encoding='gbk' 或 errors='replace'"),
)
_STATUS_HINTS = {
    "timeout": "超过运行时限：减少数据量/循环次数，或把 timeout_seconds 调大（不超过管理员上限）",
    "cpu_limit": "CPU 时间用完了：换更高效的算法（向量化、减少嵌套循环）",
    "memory_limit": "内存不够：分块处理数据，别一次性生成超大数组/表格",
    "disk_limit": "工作区空间满了：先用 delete_sandbox_file 删掉不需要的文件",
    "output_limit": "输出刷屏被强制结束：只打印关键结果，大量数据写进文件",
    "killed": "进程被系统结束，可能是资源超限；换更小的数据量再试",
    "cancelled": "这次运行被管理员取消了，如实说明",
}


def compact_traceback(stderr: str) -> str:
    """只留用户代码帧 + 最后一帧库代码 + 异常行；不是 traceback 时原样返回。"""

    if _TRACEBACK_HEAD not in stderr:
        return stderr
    head, _, rest = stderr.partition(_TRACEBACK_HEAD)
    lines = rest.splitlines()
    frames: list[list[str]] = []
    tail: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _FRAME_RE.match(line):
            block = [line]
            index += 1
            while index < len(lines) and lines[index].startswith("    "):
                block.append(lines[index])
                index += 1
            frames.append(block)
            continue
        if line.strip():
            tail.append(line)
        index += 1
    kept: list[list[str]] = []
    library_last: list[str] | None = None
    for block in frames:
        match = _FRAME_RE.match(block[0])
        if match and match.group("file").endswith(_USER_FILE):
            kept.append(block)
            library_last = None
        else:
            library_last = block
    if library_last is not None:
        kept.append(library_last)
    dropped = len(frames) - len(kept)
    out = [head.rstrip()] if head.strip() else []
    out.append(_TRACEBACK_HEAD)
    if dropped > 0:
        out.append(f"  …（省略 {dropped} 层库内部调用）…")
    for block in kept:
        out.extend(block)
    out.extend(tail)
    return "\n".join(out)


def error_summary(stderr: str, code: str | None = None) -> dict[str, Any] | None:
    """从 stderr 提取“哪一行、什么错”。找不到就返回 None。"""

    lines = [line for line in stderr.splitlines() if line.strip()]
    exception: str | None = None
    for line in reversed(lines):
        stripped = line.strip()
        if _EXCEPTION_LINE_RE.match(stripped):
            exception = stripped
            break
    if exception is None:
        return None
    summary: dict[str, Any] = {"exception": exception[:300]}
    user_line: int | None = None
    for line in lines:
        match = _FRAME_RE.match(line)
        if match and match.group("file").endswith(_USER_FILE):
            user_line = int(match.group("line"))
    if user_line is None:
        syntax = re.search(r'File "[^"]*main\.py", line (\d+)', stderr)
        if syntax:
            user_line = int(syntax.group(1))
    if user_line is not None:
        summary["line"] = user_line
        if code:
            source = code.splitlines()
            if 0 < user_line <= len(source):
                summary["code"] = source[user_line - 1].strip()[:200]
    for pattern, hint in _ERROR_HINTS:
        if pattern.search(exception) or pattern.search(stderr[-1500:]):
            summary["fix"] = hint
            break
    return summary


def strip_noise(stderr: str) -> str:
    """去掉成功运行里的字体/弃用警告；警告下一行的源码回显一起去掉。"""

    out: list[str] = []
    skip_next = False
    for line in stderr.splitlines():
        if skip_next:
            skip_next = False
            if line.startswith("  "):
                continue
        if any(pattern.search(line) for pattern in _NOISE_PATTERNS):
            skip_next = True
            continue
        out.append(line)
    return "\n".join(out).strip("\n")


def trim_lines(text: str, limit: int, *, keep_tail: bool = False) -> tuple[str, bool]:
    """按行截断到 ``limit`` 字以内；保留头尾，中间说明省略了多少行。"""

    text = text.strip("\n")
    if len(text) <= limit:
        return text, False
    lines = text.splitlines()
    if len(lines) <= 1:
        if keep_tail:
            return "…" + text[-limit:], True
        head_chars = int(limit * 0.7)
        return f"{text[:head_chars]}…（省略 {len(text) - limit} 字）…{text[-(limit - head_chars):]}", True
    head_budget = int(limit * (0.3 if keep_tail else 0.7))
    tail_budget = limit - head_budget
    head: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > head_budget:
            break
        head.append(line)
        used += len(line) + 1
    tail: list[str] = []
    used = 0
    for line in reversed(lines[len(head):]):
        if used + len(line) + 1 > tail_budget:
            break
        tail.insert(0, line)
        used += len(line) + 1
    skipped = len(lines) - len(head) - len(tail)
    if skipped <= 0:
        return text[:limit], True
    middle = f"…（省略中间 {skipped} 行，共 {len(lines)} 行）…"
    return "\n".join([*head, middle, *tail]), True


def status_hint(status: str) -> str | None:
    return _STATUS_HINTS.get(status)


__all__ = [
    "compact_traceback",
    "error_summary",
    "status_hint",
    "strip_noise",
    "trim_lines",
]
