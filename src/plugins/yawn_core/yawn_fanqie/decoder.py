"""番茄页面状态、HTML 正文和公开字体映射解码工具。

字体 glyph 到字符的常量表来自公开的 fanqienovel 字体解码研究，仅保留
算法所需的数据映射；本插件不包含第三方下载器或其网络/任务代码。详见
``docs/fanqie-notice.md``。
"""

# 这些函数保留页面解析失败的中文诊断；不屏蔽未定义名称和导入错误。
# ruff: noqa: TRY003, TRY004

from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any

_FONT_MAP_TEXT = """
58670:0 58413:1 58678:2 58371:3 58353:4 58480:5 58359:6 58449:7 58540:8 58692:9
58712:a 58542:b 58575:c 58626:d 58691:e 58561:f 58362:g 58619:h 58430:i 58531:j
58588:k 58440:l 58681:m 58631:n 58376:o 58429:p 58555:q 58498:r 58518:s 58453:t
58397:u 58356:v 58435:w 58514:x 58482:y 58529:z 58515:A 58688:B 58709:C 58344:D
58656:E 58381:F 58576:G 58516:H 58463:I 58649:J 58571:K 58558:L 58433:M 58517:N
58387:O 58687:P 58537:Q 58541:R 58458:S 58390:T 58466:U 58386:V 58697:W 58519:X
58511:Y 58634:Z
58611:的 58590:一 58398:是 58422:了 58657:我 58666:不 58562:人 58345:在 58510:他
58496:有 58654:这 58441:个 58493:上 58714:们 58618:来 58528:到 58620:时 58403:大
58461:地 58481:为 58700:子 58708:中 58503:你 58442:说 58639:生 58506:国 58663:年
58436:着 58563:就 58391:那 58357:和 58354:要 58695:她 58372:出 58696:也 58551:得
58445:里 58408:后 58599:自 58424:以 58394:会 58348:家 58426:可 58673:下 58417:而
58556:过 58603:天 58565:去 58604:能 58522:对 58632:小 58622:多 58350:然 58605:于
58617:心 58401:学 58637:么 58684:之 58382:都 58464:好 58487:看 58693:起 58608:发
58392:当 58474:没 58601:成 58355:只 58573:如 58499:事 58469:把 58361:还 58698:用
58489:第 58711:样 58457:道 58635:想 58492:作 58647:种 58623:开 58521:美 58609:总
58530:从 58665:无 58652:情 58676:己 58456:面 58581:最 58509:女 58488:但 58363:现
58685:前 58396:些 58523:所 58471:同 58485:日 58613:手 58533:又 58589:行 58527:意
58593:动 58699:方 58707:期 58414:它 58596:头 58570:经 58660:长 58364:儿 58526:回
58501:位 58638:分 58404:爱 58677:老 58535:因 58629:很 58577:给 58606:名 58497:法
58662:间 58479:斯 58532:知 58380:世 58385:什 58405:两 58644:次 58578:使 58505:身
58564:者 58412:被 58686:高 58624:已 58667:亲 58607:其 58616:进 58368:此 58427:话
58423:常 58633:与 58525:活 58543:正 58418:感 58597:见 58683:明 58507:问 58621:力
58703:理 58438:尔 58536:点 58384:文 58484:几 58539:定 58554:本 58421:公 58347:特
58569:做 58710:外 58574:孩 58375:相 58645:西 58592:果 58572:走 58388:将 58370:月
58399:十 58651:实 58546:向 58504:声 58419:车 58407:全 58672:信 58675:重 58538:三
58465:机 58374:工 58579:物 58402:气 58702:每 58553:并 58360:别 58389:真 58560:打
58690:太 58473:新 58512:比 58653:才 58704:便 58545:夫 58641:再 58475:书 58583:部
58472:水 58478:像 58664:眼 58586:等 58568:体 58674:却 58490:加 58476:电 58346:主
58630:界 58595:门 58502:利 58713:海 58587:受 58548:听 58351:表 58547:德 58443:少
58460:克 58636:代 58585:员 58625:许 58694:稜 58428:先 58640:口 58628:由 58612:死
58446:安 58468:写 58410:性 58508:马 58594:光 58483:白 58544:或 58495:住 58450:难
58643:望 58486:教 58406:命 58447:花 58669:结 58415:乐 58444:色 58549:更 58494:拉
58409:东 58658:神 58557:记 58602:处 58559:让 58610:母 58513:父 58500:应 58378:直
58680:字 58352:场 58383:平 58454:报 58671:友 58668:关 58452:放 58627:至 58400:张
58455:认 58416:接 58552:告 58614:入 58582:笑 58534:内 58701:英 58349:军 58491:候
58467:民 58365:岁 58598:往 58425:何 58462:度 58420:山 58661:觉 58615:路 58648:带
58470:万 58377:男 58520:边 58646:风 58600:解 58431:叫 58715:任 58524:金 58439:快
58566:原 58477:吃 58642:妈 58437:变 58411:通 58451:师 58395:立 58369:象 58706:数
58705:四 58379:失 58567:满 58373:战 58448:远 58659:格 58434:士 58679:音 58432:轻
58689:目 58591:条 58682:呢
"""

FONT_GLYPH_MAP: dict[str, str] = {}
for _item in _FONT_MAP_TEXT.split():
    _key, _value = _item.split(":", 1)
    FONT_GLYPH_MAP[_key] = _value

_INITIAL_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_PUA_RE = re.compile(r"[\ue000-\uf8ff\U000f0000-\U000ffffd]")


def extract_initial_state(page: str) -> dict[str, Any]:
    """提取页面内的 ``window.__INITIAL_STATE__`` JSON。"""

    match = _INITIAL_STATE_RE.search(page)
    if match is None:
        raise ValueError("页面缺少 __INITIAL_STATE__")
    decoder = json.JSONDecoder()
    try:
        state, _ = decoder.raw_decode(page[match.end() :].lstrip())
    except json.JSONDecodeError as exc:
        raise ValueError("__INITIAL_STATE__ 不是有效 JSON") from exc
    if not isinstance(state, dict):
        raise ValueError("__INITIAL_STATE__ 顶层不是对象")
    return state


def html_to_text(content: str) -> str:
    """把章节 HTML 清理成保留段落换行的纯文本。"""

    text = re.sub(r"<\s*br\s*/?\s*>", "\n", content, flags=re.IGNORECASE)
    text = re.sub(
        r"</?\s*(?:p|div|section|article|li|h[1-6])\b[^>]*>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    text = _TAG_RE.sub("", text)
    text = unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    output: list[str] = []
    for line in lines:
        if line or (output and output[-1]):
            output.append(line)
    return "\n".join(output).strip()


class _ReaderContentParser(HTMLParser):
    """提取公开阅读页 ``muye-reader-content`` 容器中的段落。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._container_depth: int | None = None
        self._paragraph_depth = 0
        self._current: list[str] = []
        self.paragraphs: list[str] = []

    @property
    def _in_container(self) -> bool:
        return self._container_depth is not None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._container_depth is None and tag == "div":
            classes = next(
                (value or "" for key, value in attrs if key.lower() == "class"),
                "",
            )
            if "muye-reader-content" in classes.split():
                self._container_depth = self._depth
        if tag == "br":
            if self._in_container and self._paragraph_depth:
                self._current.append("\n")
            return
        self._depth += 1
        if self._in_container and tag == "p":
            self._paragraph_depth += 1

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() != "br":
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._in_container and tag == "p" and self._paragraph_depth:
            self._paragraph_depth -= 1
            if self._paragraph_depth == 0 and self._current:
                self.paragraphs.append("".join(self._current))
                self._current = []
        if self._depth:
            self._depth -= 1
        if self._container_depth is not None and self._depth == self._container_depth:
            self._container_depth = None
            self._paragraph_depth = 0
            self._current = []

    def handle_data(self, data: str) -> None:
        if self._in_container and self._paragraph_depth:
            self._current.append(data)


def extract_reader_content(page: str) -> str:
    """从阅读页 DOM 备用提取章节段落，不推断或修改访问权限。"""

    parser = _ReaderContentParser()
    parser.feed(page)
    parser.close()
    paragraphs = [
        paragraph.strip()
        for paragraph in parser.paragraphs
        if paragraph.strip()
    ]
    return html_to_text("\n\n".join(paragraphs))


def decrypt_pua(text: str, mapping: dict[str, str] | None = None) -> str:
    """用字体生成的 PUA 映射替换正文中的字符。"""

    active = mapping or {}
    return "".join(active.get(char, char) for char in text)


def contains_pua(text: str) -> bool:
    """判断文本是否仍含有待解码的私用区字符。"""

    return _PUA_RE.search(text) is not None


def font_glyph_to_text(glyph_name: str) -> str | None:
    """把 ``gid58670``/``glyph58670`` 等字体 glyph 名称映射成字符。"""

    match = re.search(r"(?:gid|glyph)?(\d+)$", glyph_name, re.IGNORECASE)
    return FONT_GLYPH_MAP.get(match.group(1)) if match else None
