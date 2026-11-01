"""text 包：文本统计、中文分词词频、词云、正则抽取与替换、按行处理、对比、关键词。"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from pathlib import Path
from typing import Any

from . import CapError, cap, clip_list, clip_text, dest, read_text, ref, src

_STOP = frozenset(
    (
        "的",
        "了",
        "是",
        "在",
        "我",
        "你",
        "他",
        "她",
        "它",
        "们",
        "和",
        "与",
        "及",
        "就",
        "都",
        "而",
        "也",
        "很",
        "还",
        "又",
        "着",
        "吧",
        "啊",
        "呢",
        "吗",
        "嘛",
        "呀",
        "这",
        "那",
        "有",
        "没",
        "不",
        "个",
        "上",
        "下",
        "中",
        "一",
        "二",
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "is",
        "are",
        "was",
        "were",
        "be",
        "it",
        "this",
        "that",
        "with",
        "as",
        "at",
        "by",
        "from",
    )
)


def _source(path: str, text: str) -> tuple[str, str]:
    if path:
        target = src(path, kind="文本文件")
        return read_text(target), target.stem
    if str(text).strip():
        return str(text), "文本"
    raise CapError("给 path（文件）或 text（直接给文字）")


def _words(text: str, min_len: int = 2) -> list[str]:
    import jieba

    jieba.setLogLevel(60)
    return [
        w.strip().lower()
        for w in jieba.lcut(text)
        if len(w.strip()) >= int(min_len)
        and w.strip().lower() not in _STOP
        and not w.strip().isdigit()
        and re.search(r"\w", w)
    ]


def _save_text(content: str, output: str, default: str) -> str:
    target = dest(output, default)
    target.write_text(content, encoding="utf-8")
    return ref(target)


@cap("text.stats")
def stats(path: str = "", text: str = "") -> dict[str, Any]:
    content, _ = _source(path, text)
    lines = content.splitlines()
    return {
        "chars": len(content),
        "chars_no_space": len(re.sub(r"\s", "", content)),
        "chinese_chars": len(re.findall(r"[一-鿿]", content)),
        "english_words": len(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", content)),
        "lines": len(lines),
        "blank_lines": sum(1 for line in lines if not line.strip()),
        "paragraphs": len([p for p in re.split(r"\n\s*\n", content) if p.strip()]),
        "read_minutes": round(len(content) / 400, 1),
    }


@cap("text.word_freq")
def word_freq(
    path: str = "", text: str = "", top: int = 30, min_len: int = 2
) -> dict[str, Any]:
    content, _ = _source(path, text)
    counts = Counter(_words(content, min_len))
    return {
        "top": counts.most_common(max(1, min(int(top), 200))),
        "distinct": len(counts),
    }


@cap("text.wordcloud")
def wordcloud(
    path: str = "",
    text: str = "",
    output: str = "",
    width: int = 1200,
    height: int = 800,
    background: str = "white",
) -> dict[str, Any]:
    from wordcloud import WordCloud

    content, stem = _source(path, text)
    counts = Counter(_words(content))
    if not counts:
        raise CapError("没有可以做词云的词")
    font = next(
        (
            p
            for p in ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",)
            if Path(p).exists()
        ),
        None,
    )
    cloud = WordCloud(
        width=int(width),
        height=int(height),
        background_color=str(background),
        font_path=font,
        max_words=200,
    )
    cloud.generate_from_frequencies(dict(counts))
    target = dest(output, f"{stem}_词云.png")
    cloud.to_file(str(target))
    return {"file": ref(target), "top": counts.most_common(10)}


@cap("text.regex_extract")
def regex_extract(
    pattern: str, path: str = "", text: str = "", group: int = 0, unique: bool = True
) -> dict[str, Any]:
    content, _ = _source(path, text)
    try:
        compiled = re.compile(str(pattern), re.MULTILINE)
    except re.error as exc:
        raise CapError(f"正则写错了：{exc}") from exc
    found = [
        m.group(int(group)) if compiled.groups >= int(group) else m.group(0)
        for m in compiled.finditer(content)
    ]
    if unique:
        found = list(dict.fromkeys(found))
    return clip_list(found, 200)


@cap("text.replace")
def replace(
    find: str,
    replacement: str,
    path: str = "",
    text: str = "",
    regex: bool = False,
    output: str = "",
) -> dict[str, Any]:
    content, stem = _source(path, text)
    if regex:
        try:
            result, count = re.subn(
                str(find), str(replacement), content, flags=re.MULTILINE
            )
        except re.error as exc:
            raise CapError(f"正则写错了：{exc}") from exc
    else:
        count = content.count(str(find))
        result = content.replace(str(find), str(replacement))
    payload: dict[str, Any] = {"replaced": count}
    if path:
        payload["file"] = _save_text(
            result, output, f"{stem}_替换{Path(path).suffix or '.txt'}"
        )
    else:
        payload.update(clip_text(result))
    return payload


def _line_tool(
    path: str, text: str, output: str, suffix: str, transform: Any
) -> dict[str, Any]:
    content, stem = _source(path, text)
    lines = content.splitlines()
    result = transform(lines)
    payload: dict[str, Any] = {"before": len(lines), "after": len(result)}
    joined = "\n".join(result)
    if path:
        payload["file"] = _save_text(joined + "\n", output, f"{stem}_{suffix}.txt")
    else:
        payload.update(clip_text(joined))
    return payload


@cap("text.dedupe_lines")
def dedupe_lines(
    path: str = "", text: str = "", ignore_case: bool = False, output: str = ""
) -> dict[str, Any]:
    def transform(lines: list[str]) -> list[str]:
        seen: set[str] = set()
        kept = []
        for line in lines:
            key = line.strip().lower() if ignore_case else line.strip()
            if key in seen:
                continue
            seen.add(key)
            kept.append(line)
        return kept

    return _line_tool(path, text, output, "去重", transform)


@cap("text.sort_lines")
def sort_lines(
    path: str = "",
    text: str = "",
    descending: bool = False,
    numeric: bool = False,
    output: str = "",
) -> dict[str, Any]:
    def key(line: str) -> Any:
        if numeric:
            match = re.search(r"-?\d+(?:\.\d+)?", line)
            return float(match.group()) if match else float("inf")
        return line

    return _line_tool(
        path,
        text,
        output,
        "排序",
        lambda lines: sorted(lines, key=key, reverse=bool(descending)),
    )


@cap("text.filter_lines")
def filter_lines(
    pattern: str, path: str = "", text: str = "", invert: bool = False, output: str = ""
) -> dict[str, Any]:
    try:
        compiled = re.compile(str(pattern))
    except re.error as exc:
        raise CapError(f"正则写错了：{exc}") from exc
    return _line_tool(
        path,
        text,
        output,
        "筛行",
        lambda lines: [ln for ln in lines if bool(compiled.search(ln)) != bool(invert)],
    )


@cap("text.extract")
def extract(kind: str, path: str = "", text: str = "") -> dict[str, Any]:
    """抽取常见实体：url / email / phone（中国大陆手机号）/ number / date / ip / chinese。"""

    patterns = {
        "url": r"https?://[^\s<>\"'）)】]+",
        "email": r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
        "phone": r"(?<!\d)1[3-9]\d{9}(?!\d)",
        "number": r"-?\d+(?:,\d{3})*(?:\.\d+)?%?",
        "date": r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?",
        "ip": r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])",
        "chinese": r"[一-鿿]+",
    }
    if kind not in patterns:
        raise CapError(f"kind 只能是 {'/'.join(patterns)}")
    content, _ = _source(path, text)
    found = list(dict.fromkeys(re.findall(patterns[kind], content)))
    return clip_list(found, 200)


@cap("text.case")
def case(mode: str, path: str = "", text: str = "", output: str = "") -> dict[str, Any]:
    """大小写/简繁之外的常见变换：upper / lower / title / snake / camel / strip / fullwidth_to_half。"""

    content, stem = _source(path, text)
    if mode == "upper":
        result = content.upper()
    elif mode == "lower":
        result = content.lower()
    elif mode == "title":
        result = content.title()
    elif mode == "snake":
        result = re.sub(
            r"[\s\-]+", "_", re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", content)
        ).lower()
    elif mode == "camel":
        parts = re.split(r"[\s_\-]+", content.strip())
        result = (
            parts[0].lower() + "".join(p[:1].upper() + p[1:].lower() for p in parts[1:])
            if parts
            else ""
        )
    elif mode == "strip":
        result = "\n".join(line.strip() for line in content.splitlines())
    elif mode == "fullwidth_to_half":
        result = "".join(
            chr(ord(ch) - 0xFEE0)
            if 0xFF01 <= ord(ch) <= 0xFF5E
            else (" " if ch == "　" else ch)
            for ch in content
        )
    else:
        raise CapError(
            "mode 只能是 upper/lower/title/snake/camel/strip/fullwidth_to_half"
        )
    if path:
        return {
            "file": _save_text(
                result, output, f"{stem}_{mode}{Path(path).suffix or '.txt'}"
            )
        }
    return clip_text(result)


@cap("text.diff")
def diff(
    a: str = "", b: str = "", path_a: str = "", path_b: str = "", context: int = 2
) -> dict[str, Any]:
    left = read_text(src(path_a)) if path_a else str(a)
    right = read_text(src(path_b)) if path_b else str(b)
    lines = list(
        difflib.unified_diff(
            left.splitlines(),
            right.splitlines(),
            "A",
            "B",
            lineterm="",
            n=max(0, int(context)),
        )
    )
    ratio = difflib.SequenceMatcher(None, left, right).ratio()
    return {
        "similarity": round(ratio, 4),
        **clip_text("\n".join(lines) or "（完全一样）"),
    }


@cap("text.keywords")
def keywords(path: str = "", text: str = "", top: int = 20) -> dict[str, Any]:
    """TF-IDF 关键词（jieba.analyse），比词频更能挑出这篇文章特有的词。"""

    import jieba.analyse

    content, _ = _source(path, text)
    pairs = jieba.analyse.extract_tags(
        content, topK=max(1, min(int(top), 100)), withWeight=True
    )
    return {"keywords": [[w, round(s, 4)] for w, s in pairs]}


@cap("text.summary_sentences")
def summary_sentences(path: str = "", text: str = "", count: int = 5) -> dict[str, Any]:
    """抽取式摘要：按关键词得分挑出最有代表性的几句（不改写原文）。"""

    content, _ = _source(path, text)
    sentences = [
        s.strip()
        for s in re.split(r"(?<=[。！？!?；;\n])", content)
        if len(s.strip()) > 5
    ]
    if not sentences:
        raise CapError("没有可挑的句子")
    weights = Counter(_words(content))
    scored = sorted(
        range(len(sentences)),
        key=lambda i: (
            -sum(weights[w] for w in _words(sentences[i])) / (len(sentences[i]) ** 0.5)
        ),
    )
    chosen = sorted(scored[: max(1, min(int(count), 20))])
    return {
        "sentences": [sentences[i] for i in chosen],
        "total_sentences": len(sentences),
    }


@cap("text.split_by")
def split_by(
    path: str, separator: str = "", lines_per_file: int = 0, output: str = ""
) -> dict[str, Any]:
    """把大文本按分隔符（如 '第.*章' 的正则）或固定行数拆成多份。"""

    content, stem = _source(path, "")
    if separator:
        try:
            parts = [
                p
                for p in re.split(f"(?=^{separator})", content, flags=re.MULTILINE)
                if p.strip()
            ]
        except re.error as exc:
            raise CapError(f"正则写错了：{exc}") from exc
    elif int(lines_per_file) > 0:
        lines = content.splitlines(keepends=True)
        step = int(lines_per_file)
        parts = ["".join(lines[i : i + step]) for i in range(0, len(lines), step)]
    else:
        raise CapError("给 separator（正则）或 lines_per_file")
    folder = dest(output or f"out/{stem}_拆分/", "x").parent
    files = []
    for index, part in enumerate(parts, start=1):
        target = folder / f"{stem}_{index:03d}.txt"
        target.write_text(part, encoding="utf-8")
        files.append(ref(target))
    return {"count": len(files), "files": files[:30]}


@cap("text.encoding_fix")
def encoding_fix(path: str, output: str = "") -> dict[str, Any]:
    """常见乱码修复：把被当成 latin-1/GBK 误解码的文字尝试还原成 UTF-8。"""

    content, stem = _source(path, "")
    best = content
    tried = []
    for wrong, right in (
        ("latin-1", "utf-8"),
        ("gbk", "utf-8"),
        ("latin-1", "gbk"),
        ("utf-8", "gbk"),
    ):
        try:
            candidate = content.encode(wrong).decode(right)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        tried.append(f"{wrong}->{right}")
        if len(re.findall(r"[一-鿿]", candidate)) > len(re.findall(r"[一-鿿]", best)):
            best = candidate
    if best == content:
        return {"fixed": False, "tried": tried, "note": "看不出能修好的乱码"}
    return {
        "fixed": True,
        "file": _save_text(best, output, f"{stem}_修复.txt"),
        **clip_text(best, 500),
    }
