"""PDF 글자 좌표로 읽기 순서·띄어쓰기·문단을 복원하는 레이아웃 엔진.

PDF 라이브러리가 넘겨준 글자 단위 좌표(Glyph)만 사용하며 Qt·PDF 패키지에 의존하지
않는다. 처리 순서는 다음과 같다.

1. 글자 정규화와 가짜 굵게(같은 글자를 조금씩 겹쳐 여러 번 찍는 방식) 중복 제거
2. 2단 편집의 단 사이 여백 탐지와 띠(band) 단위 읽기 순서 결정
3. 줄 안 띄어쓰기 판정
   - 실제 공백 글자가 들어 있는 PDF는 그 공백만 신뢰한다(양쪽 정렬로 벌어진 자간 무시).
   - 공백 글자가 없는 PDF는 줄마다 기준 자간을 구해 그보다 확실히 넓은 간격만 띄우고,
     애매한 간격은 같은 문서에서 확실하게 판정된 글자 쌍 통계로 결정한다.
4. 줄바꿈이 문단 안의 자동 줄바꿈인지, 실제 문단·문항·선택지 경계인지 판정해 이어 붙이기
5. 반복 머리말·꼬리말 제거, 페이지 사이 문단 연결, 그림 영역 자리표시자 삽입
"""
from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import median
from typing import NamedTuple, Optional, Sequence

PLACEHOLDER_OPEN = "\ue000"
PLACEHOLDER_CLOSE = "\ue001"
PLACEHOLDER_RE = re.compile("\ue000([A-Za-z0-9_-]{1,40})\ue001")

_CHOICE_VARIANTS = str.maketrans("➀➁➂➃➄❶❷❸❹❺", "①②③④⑤①②③④⑤")
_DROP_CHARS = {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad"}
_SPACE_CHARS = {"\u00a0", "\u2000", "\u2001", "\u2002", "\u2003", "\u2004", "\u2005",
                "\u2006", "\u2007", "\u2008", "\u2009", "\u200a", "\u202f", "\u205f",
                "\u3000", "\t"}

# 공백 앞/뒤 판정용 문장부호
_CLOSING_BRACKETS = set(")]}」』”’〉》】〕")
_CLOSING = set(".,!?;:%％…") | _CLOSING_BRACKETS
_OPENING = set("([{「『“‘〈《【〔")
_NO_SPACE_AROUND = set("·ㆍ~～")
_SENTENCE_END = set(".?!,;:")

_MARKER_RE = re.compile(
    r"^(?:"
    r"[①②③④⑤⑥⑦⑧⑨⑩⑴⑵⑶⑷⑸]"
    r"|\d{1,3}\s*[.)](?!\d)"
    r"|\[\s*\d{1,3}\s*(?:[~～\-–—]\s*\d{1,3}\s*)?\]"
    r"|[※*＊•◦▪■□◆◇○●◎△▲▽▼☐☑]"
    r"|[ㄱ-ㅎ]\s*[.)]"
    r"|\(\s*[가-하ㄱ-ㅎA-Za-z0-9]{1,2}\s*\)"
    r"|[<〈＜《\[【]\s*보\s*기\s*[>〉＞》\]】]\s*$"
    r"|[A-E]\s*[.)](?=\s)"
    r"|[-–—]\s"
    r")"
)
_GROUP_HEADER_RE = re.compile(r"^\s*[\[【(（]\s*\d{1,3}\s*[~～\-–—]\s*\d{1,3}\s*[\]】)）]")
_INSTRUCTION_RE = re.compile(r"(?:다음|아래)\s*(?:의\s*)?(?:글|지문|자료).{0,20}(?:읽고|보고).{0,25}(?:물음|문제)")
_STANDALONE_RE = re.compile(r"^\s*\(\s*(?:중략|하략|후략|전략|상략)\s*\)\s*$|^\s*[…·.]{3,}\s*$")
_PAGE_NUMBER_RE = re.compile(r"^[-–—(\[<]?#{1,4}[-–—)\]>]?$|^#{1,4}/#{1,4}$|^(?:p\.?|쪽)?#{1,4}(?:쪽|page)?$", re.IGNORECASE)


class TextStyle(NamedTuple):
    bold: bool = False
    italic: bool = False
    underline: bool = False
    color: str = ""

    @property
    def is_default(self) -> bool:
        return not (self.bold or self.italic or self.underline or self.color)


DEFAULT_STYLE = TextStyle()


@dataclass(slots=True)
class Glyph:
    """PDF 한 글자. 좌표는 페이지 왼쪽 위 기준(pt), oy는 글자 기준선."""

    char: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    ox: float
    oy: float
    bold: bool = False
    italic: bool = False
    underline: bool = False
    color: str = ""

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def style(self) -> TextStyle:
        return TextStyle(self.bold, self.italic, self.underline, self.color)


@dataclass(slots=True)
class LayoutRegion:
    """텍스트 흐름에서 빼내 그림으로 보여 줄 영역(<보기>·표·그림)."""

    region_id: str
    bbox: tuple[float, float, float, float]
    kind: str = "figure"


@dataclass
class PageInput:
    page_number: int
    width: float
    height: float
    glyphs: list[Glyph] = field(default_factory=list)
    regions: list[LayoutRegion] = field(default_factory=list)
    frames: list[tuple[float, float, float, float]] = field(default_factory=list)
    gutter: Optional[tuple[float, float]] = None
    gutter_detected: bool = False
    duplicate_glyphs: int = 0


@dataclass
class PageLayout:
    page_number: int
    text: str = ""
    styles: list[TextStyle] = field(default_factory=list)
    region_texts: dict[str, str] = field(default_factory=dict)
    join_next: bool = False
    join_space: bool = False
    removed_margin_lines: int = 0
    duplicate_glyphs: int = 0
    columns: int = 1

    @property
    def content_text(self) -> str:
        """품질 판정용: 자리표시자를 빼고 그림 영역 안 글자를 포함한 텍스트."""
        parts = [strip_placeholders(self.text)]
        parts.extend(text for text in self.region_texts.values() if text)
        return "\n".join(part for part in parts if part.strip())


# ---------------------------------------------------------------------------
# 공개 도우미


def placeholder(region_id: str) -> str:
    return f"{PLACEHOLDER_OPEN}{region_id}{PLACEHOLDER_CLOSE}"


def strip_placeholders(text: str) -> str:
    """자리표시자 줄을 제거한 사람이 읽는 텍스트."""
    if not text or PLACEHOLDER_OPEN not in text:
        return text or ""
    lines = []
    for line in text.split("\n"):
        cleaned = PLACEHOLDER_RE.sub("", line)
        if line.strip() and not cleaned.strip():
            continue
        lines.append(cleaned)
    return "\n".join(lines)


def placeholders_to_tokens(text: str) -> str:
    """AI 프롬프트용으로 자리표시자를 읽을 수 있는 [[그림:id]] 토큰으로 바꾼다."""
    return PLACEHOLDER_RE.sub(lambda match: f"[[그림:{match.group(1)}]]", text or "")


def normalize_char(char: str) -> str:
    if not char:
        return ""
    if char in _DROP_CHARS or (len(char) == 1 and ord(char) < 32 and char not in "\t"):
        return ""
    if char in _SPACE_CHARS:
        return " "
    if char in (PLACEHOLDER_OPEN, PLACEHOLDER_CLOSE):
        return "□"
    return char.translate(_CHOICE_VARIANTS)


def prepare_glyphs(glyphs: Sequence[Glyph]) -> tuple[list[Glyph], int]:
    """문자 정규화 후 가짜 굵게 중복을 제거한다. (정리된 글자, 제거 수)"""
    normalized: list[Glyph] = []
    for glyph in glyphs:
        if not (math.isfinite(glyph.x0) and math.isfinite(glyph.x1)
                and math.isfinite(glyph.ox) and math.isfinite(glyph.oy)):
            continue
        char = normalize_char(glyph.char)
        if not char:
            continue
        glyph.char = char
        if glyph.size <= 0 or not math.isfinite(glyph.size):
            glyph.size = max(1.0, glyph.y1 - glyph.y0)
        if glyph.x1 < glyph.x0:
            glyph.x0, glyph.x1 = glyph.x1, glyph.x0
        normalized.append(glyph)
    return remove_overprint_duplicates(normalized)


def remove_overprint_duplicates(glyphs: Sequence[Glyph]) -> tuple[list[Glyph], int]:
    """같은 글자를 거의 같은 위치에 겹쳐 찍은 가짜 굵게를 하나로 합친다.

    실제로 반복된 글자('하하하', '22')는 글자 폭만큼 떨어져 있으므로 남고,
    0.2em 이내로 겹친 복제본만 제거한다. 복제가 있던 글자는 굵게로 표시한다.
    """
    kept: list[Glyph] = []
    buckets: dict[tuple[str, int, int], list[int]] = defaultdict(list)
    removed = 0
    for glyph in glyphs:
        size = max(1.0, glyph.size)
        cell = size * 0.5
        key_x = math.floor(glyph.ox / cell)
        key_y = math.floor(glyph.oy / cell)
        width = max(0.1, glyph.x1 - glyph.x0)
        tolerance_x = max(0.35, min(0.45 * width, 0.2 * size))
        tolerance_y = 0.3 * size
        duplicate: Optional[Glyph] = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for index in buckets.get((glyph.char, key_x + dx, key_y + dy), ()):
                    other = kept[index]
                    if (
                        abs(other.ox - glyph.ox) <= tolerance_x
                        and abs(other.oy - glyph.oy) <= tolerance_y
                        and abs(other.size - glyph.size) <= 0.25 * max(other.size, glyph.size)
                    ):
                        duplicate = other
                        break
                if duplicate is not None:
                    break
            if duplicate is not None:
                break
        if duplicate is not None:
            removed += 1
            if glyph.char != " " and duplicate.color == glyph.color:
                duplicate.bold = True
            duplicate.underline = duplicate.underline or glyph.underline
            continue
        buckets[(glyph.char, key_x, key_y)].append(len(kept))
        kept.append(glyph)
    return kept, removed


def detect_gutter(glyphs: Sequence[Glyph], width: float, height: float) -> Optional[tuple[float, float]]:
    """2단 편집의 단 사이 여백(x0, x1)을 찾는다. 1단이면 None.

    오른쪽 단 줄들이 같은 x에서 시작하고, 그 앞 여백을 가로지르는 줄이 적으며,
    양쪽에 모두 충분한 본문이 있을 때만 2단으로 판단한다.
    """
    del height
    text = [glyph for glyph in glyphs if not glyph.char.isspace()]
    if len(text) < 60 or width <= 100:
        return None
    rows = _cluster_rows(text)
    if len(rows) < 8:
        return None
    row_intervals = [_row_intervals(row) for row in rows]
    median_size = median(glyph.size for glyph in text)
    starts = sorted(
        start
        for intervals in row_intervals
        for start, _end in intervals
        if 0.28 * width <= start <= 0.72 * width
    )
    window = max(4.0, 1.6 * median_size)
    scored: list[tuple[int, float]] = []
    for index, start in enumerate(starts):
        count = bisect_right(starts, start + window) - index
        scored.append((count, start))
    scored.sort(key=lambda item: (-item[0], item[1]))
    tried: list[float] = []
    for count, anchor in scored:
        if count < 3 or len(tried) >= 6:
            break
        if any(abs(anchor - previous) < window for previous in tried):
            continue
        tried.append(anchor)
        # 창 안에서 가장 많이 반복된 시작 x(±1pt)를 오른쪽 단 시작선으로 본다.
        cluster = Counter(round(start) for start in starts if anchor <= start <= anchor + window)
        repeated = [value for value, hits in cluster.items() if hits >= 2]
        aligned = min(repeated) if repeated else cluster.most_common(1)[0][0]
        g1 = min(start for start in starts if abs(round(start) - aligned) <= 0.5)
        ends = [
            end
            for intervals in row_intervals
            for _start, end in intervals
            if g1 - 0.35 * width < end < g1 - max(1.0, 0.3 * median_size)
        ]
        if not ends:
            continue
        g0 = max(ends)
        if g1 - g0 < max(4.0, 0.5 * median_size):
            continue
        result = _validate_gutter(rows, row_intervals, text, g0, g1)
        if result is not None:
            return result
    return None


def consensus_gutter(gutters: Sequence[Optional[tuple[float, float]]], text_pages: int) -> Optional[tuple[float, float]]:
    """여러 페이지에서 같은 자리의 단 여백이 반복되면 문서 공통 단 여백으로 본다."""
    found = [gutter for gutter in gutters if gutter is not None]
    if len(found) < 2 or len(found) < 0.4 * max(1, text_pages):
        return None
    middle = median(gutter[1] for gutter in found)
    close = [gutter for gutter in found if abs(gutter[1] - middle) <= 12]
    if len(close) < 0.7 * len(found):
        return None
    return (median(gutter[0] for gutter in close), median(gutter[1] for gutter in close))


def gutter_fits(glyphs: Sequence[Glyph], gutter: tuple[float, float]) -> bool:
    """문서 공통 단 여백을 개별 판정이 실패한 페이지에 적용해도 되는지 확인한다."""
    text = [glyph for glyph in glyphs if not glyph.char.isspace()]
    if not text:
        return False
    rows = _cluster_rows(text)
    g0, g1 = gutter
    center = (g0 + g1) / 2
    crossing = 0
    left = right = 0
    for row in rows:
        intervals = _row_intervals(row)
        if any(start < center < end for start, end in intervals):
            crossing += 1
            continue
        left += any(glyph.cx < g0 + 1 for glyph in row)
        right += any(glyph.cx > g1 - 1 for glyph in row)
    return crossing <= 0.4 * len(rows) and left >= 1 and right >= 1


def _validate_gutter(rows, row_intervals, text, g0: float, g1: float) -> Optional[tuple[float, float]]:
    crossing = 0
    left_rows = right_rows = 0
    left_glyphs = right_glyphs = 0
    right_markers = 0
    center = (g0 + g1) / 2
    for row, intervals in zip(rows, row_intervals, strict=True):
        if any(start < center < end for start, end in intervals):
            crossing += 1
            continue
        left = [glyph for glyph in row if glyph.cx < g0 + 1]
        right = sorted((glyph for glyph in row if glyph.cx > g1 - 1), key=lambda glyph: glyph.x0)
        if left:
            left_rows += 1
            left_glyphs += len(left)
        if right:
            right_rows += 1
            right_glyphs += len(right)
            if left and right[0].char in "②④⑵⑷":
                right_markers += 1
    if crossing > 0.4 * len(rows) or left_rows < 3 or right_rows < 3:
        return None
    if left_glyphs < 0.05 * len(text) or right_glyphs < 0.05 * len(text):
        return None
    # 한 줄에 '① … ② …'가 나란히 놓인 선택지 표는 2단이 아니다.
    if right_markers >= 0.6 * right_rows:
        return None
    return (g0, g1)


@dataclass
class TextLine:
    """레이아웃 판정용 줄. 그림 영역 탐지에도 사용한다."""

    glyphs: list[Glyph]
    column: int
    x0: float
    x1: float
    y0: float
    y1: float
    baseline: float
    size: float
    region_id: str = ""
    region_kind: str = ""
    # 이후 단계에서 채우는 값
    chars: list[Glyph] = field(default_factory=list)
    explicit_after: list[bool] = field(default_factory=list)
    leading_space: bool = False
    trailing_space: bool = False
    space_count: int = 0
    units: list[tuple[str, Optional[TextStyle]]] = field(default_factory=list)
    unit_glyphs: list[Optional[Glyph]] = field(default_factory=list)
    text: str = ""
    container: tuple = ()
    rel: float = 0.0
    hanging_rel: Optional[float] = None
    marker: bool = False
    first_word_width: float = 0.0
    continuation: bool = False
    margin: bool = False

    @property
    def is_region(self) -> bool:
        return bool(self.region_id)

    @property
    def compact(self) -> str:
        return "".join(glyph.char for glyph in self.glyphs if not glyph.char.isspace())


def build_lines(glyphs: Sequence[Glyph], gutter: Optional[tuple[float, float]]) -> list[TextLine]:
    """글자를 기준선으로 묶고 단 여백에서 나눈 줄 목록."""
    lines: list[TextLine] = []
    for row in _cluster_rows(list(glyphs)):
        for column, part in _split_row(row, gutter):
            line = _make_line(part, column)
            if line is not None:
                lines.append(line)
    return _merge_small_lines(lines)


def layout_document(pages: Sequence[PageInput]) -> list[PageLayout]:
    """문서 전체를 한꺼번에 배치해 페이지별 텍스트·서식을 만든다."""
    prepared = [_prepare_page(page) for page in pages]
    _remove_repeated_margins(prepared)

    model = _SpacingModel()
    for page in prepared:
        for line in page.text_lines:
            _collect_evidence(line, page.context, model)
    for page in prepared:
        for line in page.text_lines:
            _finalize_line(line, page.context, model)
        page.region_texts = {
            region_id: _region_text(glyphs, page.context, model)
            for region_id, glyphs in page.region_glyphs.items()
        }
        page.compute_containers()
    _apply_document_edges(prepared)

    _decide_breaks(prepared)
    trailing_kept = _trailing_spaces_kept(prepared)
    _decide_join_spaces(prepared, model, trailing_kept)
    return [page.serialize() for page in prepared]


# ---------------------------------------------------------------------------
# 줄 만들기


def _cluster_rows(glyphs: list[Glyph]) -> list[list[Glyph]]:
    ordered = sorted(glyphs, key=lambda glyph: (glyph.oy, glyph.ox))
    rows: list[list[Glyph]] = []
    current: list[Glyph] = []
    anchor = 0.0
    anchor_size = 0.0
    for glyph in ordered:
        if current and glyph.oy - anchor <= 0.35 * max(anchor_size, glyph.size, 1.0):
            current.append(glyph)
            if not glyph.char.isspace():
                anchor_size = max(anchor_size, glyph.size) if anchor_size else glyph.size
            continue
        if current:
            rows.append(current)
        current = [glyph]
        anchor = glyph.oy
        anchor_size = glyph.size
    if current:
        rows.append(current)
    return rows


def _row_intervals(row: list[Glyph]) -> list[tuple[float, float]]:
    glyphs = sorted((glyph for glyph in row if not glyph.char.isspace()), key=lambda glyph: glyph.x0)
    if not glyphs:
        return []
    size = median(glyph.size for glyph in glyphs)
    merge_gap = max(4.0, 0.8 * size)
    intervals: list[tuple[float, float]] = []
    start, end = glyphs[0].x0, glyphs[0].x1
    for glyph in glyphs[1:]:
        if glyph.x0 - end <= merge_gap:
            end = max(end, glyph.x1)
        else:
            intervals.append((start, end))
            start, end = glyph.x0, glyph.x1
    intervals.append((start, end))
    return intervals


def _split_row(row: list[Glyph], gutter: Optional[tuple[float, float]]) -> list[tuple[int, list[Glyph]]]:
    if gutter is None:
        return [(0, row)]
    g0, g1 = gutter
    center = (g0 + g1) / 2
    visible = [glyph for glyph in row if not glyph.char.isspace()]
    if not visible:
        return []
    size = median(glyph.size for glyph in visible)
    min_x = min(glyph.x0 for glyph in visible)
    max_x = max(glyph.x1 for glyph in visible)
    # 단 사이 여백의 가운데를 글자 덩어리가 덮을 때만 두 단을 가로지르는 줄로 본다.
    crosses = any(start < center < end for start, end in _row_intervals(row))
    if crosses:
        if min_x < g0 - 1.5 * size and max_x > g1 + 1.5 * size:
            return [(-1, row)]
        if min_x >= g0 - 3 * size and max_x <= g1 + 3 * size:
            return [(-1, row)]
    left = [glyph for glyph in row if glyph.cx < center]
    right = [glyph for glyph in row if glyph.cx >= center]
    output: list[tuple[int, list[Glyph]]] = []
    if any(not glyph.char.isspace() for glyph in left):
        output.append((0, left))
    if any(not glyph.char.isspace() for glyph in right):
        output.append((1, right))
    return output


def _make_line(glyphs: list[Glyph], column: int) -> Optional[TextLine]:
    visible = [glyph for glyph in glyphs if not glyph.char.isspace()]
    if not visible:
        return None
    ordered = sorted(glyphs, key=lambda glyph: (glyph.ox, glyph.x0))
    return TextLine(
        glyphs=ordered,
        column=column,
        x0=min(glyph.x0 for glyph in visible),
        x1=max(glyph.x1 for glyph in visible),
        y0=min(glyph.y0 for glyph in visible),
        y1=max(glyph.y1 for glyph in visible),
        baseline=median(glyph.oy for glyph in visible),
        size=median(glyph.size for glyph in visible),
    )


def _merge_small_lines(lines: list[TextLine]) -> list[TextLine]:
    """위첨자·아래첨자처럼 기준선이 살짝 다른 작은 조각을 겹치는 본문 줄에 합친다."""
    if len(lines) < 2:
        return lines
    hosts = [line for line in lines if _visible_count(line) > 3]
    output = list(hosts)
    for line in lines:
        if _visible_count(line) > 3:
            continue
        height = max(0.1, line.y1 - line.y0)
        best: Optional[TextLine] = None
        best_overlap = 0.0
        for host in hosts:
            if host.column != line.column or line.size > 0.8 * host.size:
                continue
            overlap = min(line.y1, host.y1) - max(line.y0, host.y0)
            if overlap < 0.4 * height:
                continue
            if line.x0 < host.x0 - host.size or line.x1 > host.x1 + host.size:
                continue
            if overlap > best_overlap:
                best = host
                best_overlap = overlap
        if best is None:
            output.append(line)
            continue
        best.glyphs = sorted(best.glyphs + line.glyphs, key=lambda glyph: (glyph.ox, glyph.x0))
        best.x0 = min(best.x0, line.x0)
        best.x1 = max(best.x1, line.x1)
        best.y0 = min(best.y0, line.y0)
        best.y1 = max(best.y1, line.y1)
    return output


def _visible_count(line: TextLine) -> int:
    return sum(not glyph.char.isspace() for glyph in line.glyphs)


# ---------------------------------------------------------------------------
# 페이지 준비와 읽기 순서


@dataclass
class _PageContext:
    explicit: bool = False
    base_hh: Optional[float] = None
    base_gap: Optional[float] = None
    hh_threshold: float = 0.18
    other_threshold: float = 0.2


@dataclass
class _Container:
    left: float = 0.0
    right: float = 0.0
    justified: bool = True
    pitch: Optional[float] = None


class _PreparedPage:
    def __init__(self, source: PageInput) -> None:
        self.source = source
        self.flow: list[TextLine] = []
        self.region_glyphs: dict[str, list[Glyph]] = {}
        self.region_texts: dict[str, str] = {}
        self.context = _PageContext()
        self.containers: dict[tuple, _Container] = {}
        self.breaks: list[str] = []
        self.join_spaces: list[bool] = []
        self.cross_break = "hard"
        self.cross_space = False
        self.removed_margin = 0
        self.columns = 1
        self.frames: list[tuple[float, float, float, float]] = []

    @property
    def text_lines(self) -> list[TextLine]:
        return [line for line in self.flow if not line.is_region]

    def compute_containers(self) -> None:
        groups: dict[tuple, list[TextLine]] = defaultdict(list)
        for line in self.text_lines:
            groups[line.container].append(line)
        for key, lines in groups.items():
            container = _Container()
            lefts = [line.x0 for line in lines]
            container.left = _percentile(lefts, 0.1)
            long_lines = [line for line in lines if len(line.chars) >= 5] or lines
            container.right = _percentile([line.x1 for line in long_lines], 0.9)
            flush = sum(
                line.x1 >= container.right - 0.8 * line.size for line in long_lines
            )
            container.justified = flush >= 0.45 * len(long_lines)
            pitches: list[float] = []
            ordered = sorted(lines, key=lambda line: line.baseline)
            for previous, current in zip(ordered, ordered[1:], strict=False):
                dy = current.baseline - previous.baseline
                size = max(previous.size, current.size)
                if 0.9 * size <= dy <= 2.6 * size:
                    pitches.append(dy)
            container.pitch = median(pitches) if len(pitches) >= 2 else None
            frame_index = key[1] if len(key) > 1 else -1
            if 0 <= frame_index < len(self.frames):
                # 테두리 안 글은 왼쪽 안쪽 여백만큼 오른쪽에도 여백이 있다고 본다.
                frame = self.frames[frame_index]
                inset = max(0.0, container.left - frame[0])
                limit = frame[2] - inset
                size = median(line.size for line in lines)
                if limit - container.right > 1.5 * size:
                    container.right = limit
                    container.justified = sum(
                        line.x1 >= limit - 0.8 * line.size for line in long_lines
                    ) >= 0.45 * len(long_lines)
            self.containers[key] = container
        for line in self.text_lines:
            container = self.containers[line.container]
            line.rel = line.x0 - container.left
            if line.hanging_rel is not None:
                line.hanging_rel -= container.left

    def serialize(self) -> PageLayout:
        chars: list[str] = []
        styles: list[Optional[TextStyle]] = []
        for index, line in enumerate(self.flow):
            if index > 0:
                kind = self.breaks[index - 1]
                if kind == "soft":
                    if self.join_spaces[index - 1]:
                        chars.append(" ")
                        styles.append(None)
                elif kind == "blank":
                    chars.extend("\n\n")
                    styles.extend([DEFAULT_STYLE, DEFAULT_STYLE])
                else:
                    chars.append("\n")
                    styles.append(DEFAULT_STYLE)
            for char, style in line.units:
                chars.append(char)
                styles.append(style)
        final_styles = _fill_space_styles(chars, styles)
        return PageLayout(
            page_number=self.source.page_number,
            text="".join(chars),
            styles=final_styles,
            region_texts=dict(self.region_texts),
            join_next=self.cross_break == "soft",
            join_space=self.cross_break == "soft" and self.cross_space,
            removed_margin_lines=self.removed_margin,
            duplicate_glyphs=self.source.duplicate_glyphs,
            columns=self.columns,
        )


def _apply_document_edges(pages: list[_PreparedPage]) -> None:
    """페이지마다 내용이 짧아(시·선택지만 있는 단) 오른쪽 끝선을 잘못 잡지 않도록
    문서 전체에서 같은 단의 대표 오른쪽 끝선을 구해 부족한 쪽을 보정한다."""
    samples: dict[tuple[int, int], list[float]] = defaultdict(list)
    for page in pages:
        for key in page.containers:
            column, frame = key[0], key[1]
            if frame >= 0:
                continue
            long_lines = [line for line in page.text_lines if line.container == key and len(line.chars) >= 10]
            if len(long_lines) >= 3:
                samples[(page.columns, column)].append(_percentile([line.x1 for line in long_lines], 0.9))
    edges = {key: median(values) for key, values in samples.items() if len(values) >= 2}
    for page in pages:
        for key, container in page.containers.items():
            if key[1] >= 0:
                continue
            edge = edges.get((page.columns, key[0]))
            if edge is None or edge <= container.right + 2:
                continue
            lines = [line for line in page.text_lines if line.container == key]
            size = median(line.size for line in lines) if lines else 10.0
            if edge - container.right > 1.5 * size:
                container.right = edge
                container.justified = sum(
                    line.x1 >= edge - 0.8 * line.size for line in lines if len(line.chars) >= 5
                ) >= 0.45 * max(1, sum(len(line.chars) >= 5 for line in lines))


def _prepare_page(page: PageInput) -> _PreparedPage:
    prepared = _PreparedPage(page)
    regions = [region for region in page.regions if _valid_bbox(region.bbox)]
    prepared.region_glyphs = {region.region_id: [] for region in regions}
    remaining: list[Glyph] = []
    for glyph in page.glyphs:
        owner = _region_containing(regions, glyph.cx, glyph.cy)
        if owner is not None:
            prepared.region_glyphs[owner.region_id].append(glyph)
        else:
            remaining.append(glyph)
    gutter = page.gutter if page.gutter_detected else detect_gutter(remaining, page.width, page.height)
    prepared.columns = 2 if gutter else 1
    prepared.context.explicit = _uses_space_glyphs(remaining)
    lines = build_lines(remaining, gutter)
    for line in lines:
        _split_spaces(line)
    items: list[TextLine] = [line for line in lines if line.chars]
    for region in regions:
        items.append(_region_item(region, gutter))
    frames = [frame for frame in page.frames if _valid_bbox(frame)]
    prepared.frames = frames
    for item in items:
        frame_index = _frame_index(frames, item) if not item.is_region else -1
        item.container = (item.column, frame_index)
    prepared.flow = _order_items(items, gutter)
    _page_defaults(prepared)
    return prepared


def _region_item(region: LayoutRegion, gutter: Optional[tuple[float, float]]) -> TextLine:
    x0, y0, x1, y1 = region.bbox
    column = 0
    if gutter is not None:
        g0, g1 = gutter
        if x0 < g0 - 8 and x1 > g1 + 8:
            column = -1
        else:
            column = 0 if (x0 + x1) / 2 < (g0 + g1) / 2 else 1
    token = placeholder(region.region_id)
    line = TextLine(
        glyphs=[], column=column, x0=x0, x1=x1, y0=y0, y1=y1,
        baseline=y1, size=10.0, region_id=region.region_id, region_kind=region.kind,
    )
    line.units = [(char, DEFAULT_STYLE) for char in token]
    line.text = token
    return line


def _order_items(items: list[TextLine], gutter: Optional[tuple[float, float]]) -> list[TextLine]:
    if gutter is None:
        return sorted(items, key=lambda item: (item.y0, item.x0))
    spanning = sorted((item for item in items if item.column == -1), key=lambda item: (item.y0, item.x0))
    columns = [item for item in items if item.column != -1]
    centers = [(item.y0 + item.y1) / 2 for item in spanning]
    bands: dict[int, list[TextLine]] = defaultdict(list)
    for item in columns:
        middle = (item.y0 + item.y1) / 2
        band = sum(center < middle for center in centers)
        bands[band].append(item)
    ordered: list[TextLine] = []
    for band in range(len(spanning) + 1):
        members = bands.get(band, [])
        for column in (0, 1):
            ordered.extend(sorted(
                (item for item in members if item.column == column),
                key=lambda item: (item.y0, item.x0),
            ))
        if band < len(spanning):
            ordered.append(spanning[band])
    return ordered


def _frame_index(frames: list[tuple[float, float, float, float]], line: TextLine) -> int:
    cx = (line.x0 + line.x1) / 2
    cy = (line.y0 + line.y1) / 2
    best = -1
    best_area = float("inf")
    for index, (x0, y0, x1, y1) in enumerate(frames):
        if x0 - 1 <= cx <= x1 + 1 and y0 - 1 <= cy <= y1 + 1 and line.x0 >= x0 - 2 and line.x1 <= x1 + 2:
            area = (x1 - x0) * (y1 - y0)
            if area < best_area:
                best = index
                best_area = area
    return best


def _region_containing(regions: list[LayoutRegion], x: float, y: float) -> Optional[LayoutRegion]:
    found = None
    found_area = float("inf")
    for region in regions:
        x0, y0, x1, y1 = region.bbox
        if x0 <= x <= x1 and y0 <= y <= y1:
            area = (x1 - x0) * (y1 - y0)
            if area < found_area:
                found = region
                found_area = area
    return found


def _valid_bbox(bbox) -> bool:
    try:
        x0, y0, x1, y1 = (float(value) for value in bbox)
    except (TypeError, ValueError):
        return False
    return all(math.isfinite(value) for value in (x0, y0, x1, y1)) and x1 > x0 and y1 > y0


def _page_defaults(page: _PreparedPage) -> None:
    """페이지 기준 자간과 글꼴별 띄어쓰기 폭을 추정해 간격 판정 기준을 보정한다."""
    base_hh_values: list[float] = []
    base_gap_values: list[float] = []
    hh_extras: list[float] = []
    gap_extras: list[float] = []
    for line in page.text_lines:
        chars = line.chars
        size = max(0.1, line.size)
        pairs = range(len(chars) - 1)
        line_hh = [
            (chars[index + 1].ox - chars[index].ox) / size
            for index in pairs
            if _is_hangul(chars[index].char) and _is_hangul(chars[index + 1].char)
            and not line.explicit_after[index]
        ]
        line_gaps = [
            (chars[index + 1].x0 - chars[index].x1) / size
            for index in pairs
            if chars[index].char.isalnum() and chars[index + 1].char.isalnum()
            and not (_is_hangul(chars[index].char) and _is_hangul(chars[index + 1].char))
            and not line.explicit_after[index]
        ]
        if len(line_hh) >= 3:
            base = _percentile(line_hh, 0.3)
            base_hh_values.append(base)
            if not _is_explicit_line(line, page.context):
                hh_extras.extend(value - base for value in line_hh)
        all_gaps = [
            (chars[index + 1].x0 - chars[index].x1) / size
            for index in pairs
            if chars[index].char.isalnum() and chars[index + 1].char.isalnum()
            and not line.explicit_after[index]
        ]
        if len(all_gaps) >= 3:
            base = _percentile(all_gaps, 0.3)
            base_gap_values.append(base)
            if not _is_explicit_line(line, page.context):
                gap_extras.extend(value - base for value in line_gaps)
    page.context.base_hh = median(base_hh_values) if base_hh_values else None
    page.context.base_gap = median(base_gap_values) if base_gap_values else None
    hh_spaces = [value for value in hh_extras if 0.08 < value < 1.2]
    if len(hh_spaces) >= 8:
        page.context.hh_threshold = min(0.24, max(0.1, 0.55 * median(hh_spaces)))
    gap_spaces = [value for value in gap_extras if 0.08 < value < 1.2]
    if len(gap_spaces) >= 8:
        page.context.other_threshold = min(0.26, max(0.12, 0.6 * median(gap_spaces)))


def _is_explicit_line(line: TextLine, context: _PageContext) -> bool:
    count = len(line.chars)
    return (
        (context.explicit and (line.space_count > 0 or count < 12))
        or line.space_count >= max(1.0, 0.06 * count)
    )


def _uses_space_glyphs(glyphs: Sequence[Glyph]) -> bool:
    spaces = sum(glyph.char == " " for glyph in glyphs)
    visible = sum(not glyph.char.isspace() for glyph in glyphs)
    return visible > 0 and spaces >= max(2, 0.04 * visible)


# ---------------------------------------------------------------------------
# 머리말·꼬리말


def _remove_repeated_margins(pages: list[_PreparedPage]) -> None:
    keyed: list[list[tuple[TextLine, str]]] = []
    counts: Counter[str] = Counter()
    for page in pages:
        height = max(1.0, page.source.height)
        candidates: list[tuple[TextLine, str]] = []
        seen: set[str] = set()
        for line in page.text_lines:
            if line.y1 <= 0.12 * height and line.y0 < 0.085 * height:
                zone = "top"
            elif line.y0 >= 0.9 * height:
                zone = "bottom"
            else:
                continue
            compact = re.sub(r"\d", "#", line.compact).lower()
            if not compact or len(compact) > 80 or _looks_structural(line.compact):
                continue
            key = f"{zone}|{compact}"
            candidates.append((line, key))
            if key not in seen:
                counts[key] += 1
                seen.add(key)
        keyed.append(candidates)
    threshold = max(3, math.ceil(len(pages) * 0.5))
    for page, candidates in zip(pages, keyed, strict=True):
        remove: set[int] = set()
        for line, key in candidates:
            compact = key.split("|", 1)[1]
            if _PAGE_NUMBER_RE.fullmatch(compact) or (len(pages) >= 3 and counts[key] >= threshold):
                remove.add(id(line))
        if remove:
            page.flow = [line for line in page.flow if id(line) not in remove]
            page.removed_margin += len(remove)


def _looks_structural(compact: str) -> bool:
    return bool(
        re.search(r"[①②③④⑤]", compact)
        or re.match(r"^\d{1,3}[.)]\S", compact)
        or _GROUP_HEADER_RE.match(compact)
        or ("물음" in compact and ("읽" in compact or "보" in compact))
    )


# ---------------------------------------------------------------------------
# 띄어쓰기


def _is_hangul(char: str) -> bool:
    return "\uac00" <= char <= "\ud7a3"


def _lex_key(char: str) -> str:
    if _is_hangul(char):
        return char
    if char.isdigit():
        return "0"
    if char.isascii() and char.isalpha():
        return "A"
    return char


def _logit(value: float) -> float:
    value = min(0.999, max(0.001, value))
    return math.log(value / (1 - value))


def _sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, value))))


# 문서 통계가 부족할 때 쓰는 기본 추정치(조사·어미). 문서 통계가 쌓이면 통계가 우선한다.
# 이 글자 뒤가 어절 경계일 확률
_WORD_FINAL_PRIOR = {
    **dict.fromkeys("은는을를에께며", 0.85),
    **dict.fromkeys("와과면", 0.7),
    "의": 0.6,  # 조사이지만 '의미·의견·의식'처럼 낱말 첫 글자로도 흔하다.
}
# 이 글자 앞이 어절 경계일 확률(조사·어미는 앞말에 붙는다)
_WORD_INITIAL_PRIOR = {
    **dict.fromkeys("은는을를며께요", 0.05),
    **dict.fromkeys("다의에게니까", 0.15),
    **dict.fromkeys("고서지", 0.3),
}
# 혼자서는 어절이 될 수 없는 조사 음절. 줄 끝에 이 한 글자만 떨어져 있으면 다음 줄 낱말의 첫 글자다.
_LONE_PARTICLES = set("은는을를의에께로와과")
# 한국어 어절 길이(음절 수) 분포 근사치. 줄바꿈이 어절 사이인지 어절 안인지의 사전 비율에 쓴다.
_EOJEOL_LENGTH = {1: 0.1, 2: 0.25, 3: 0.27, 4: 0.18, 5: 0.1, 6: 0.05, 7: 0.03, 8: 0.015}
# 혼자 어절이 되는 일이 흔한 한 음절 낱말(관형사·부사·의존 명사·관형형 어미)
# ('이·그·새·한·할·꼭'처럼 다른 낱말의 첫 음절로도 흔한 것은 제외, '수'는 '수 있다/없다'에서만)
_STANDALONE_SYLLABLES = set("더큰첫된될및왜곧늘좀옛")

# 거의 항상 어절 끝에 오는 두 글자 연결 어미·조사
_WORD_FINAL_ENDINGS = {
    "므로": 0.9, "지만": 0.9, "면서": 0.88, "는데": 0.85, "도록": 0.88, "처럼": 0.85,
    "부터": 0.8, "까지": 0.75, "에게": 0.85, "하여": 0.85, "거나": 0.88, "든지": 0.85,
    "조차": 0.85, "마저": 0.8, "이며": 0.88, "으며": 0.9, "하며": 0.88, "라고": 0.7,
    "다고": 0.7, "에서": 0.75, "으로": 0.75, "과의": 0.9, "와의": 0.9, "들은": 0.9,
    "들이": 0.8, "들을": 0.9, "들의": 0.9, "들에": 0.7,
}


class _SpacingModel:
    """같은 문서에서 확실하게 판정된 글자 쌍으로 애매한 경계를 결정한다.

    왼쪽 글자(와 그 앞 글자), 오른쪽 글자(와 그 다음 글자)가 각각 어절 경계에
    얼마나 자주 놓였는지를 합친 나이브 베이즈 추정값을 log-odds로 돌려준다.
    """

    def __init__(self) -> None:
        self.pair_space: Counter[tuple[str, str]] = Counter()
        self.pair_join: Counter[tuple[str, str]] = Counter()
        self.after_space: Counter[str] = Counter()
        self.after_total: Counter[str] = Counter()
        self.before_space: Counter[str] = Counter()
        self.before_total: Counter[str] = Counter()
        self.after2_space: Counter[tuple[str, str]] = Counter()
        self.after2_total: Counter[tuple[str, str]] = Counter()
        self.before2_space: Counter[tuple[str, str]] = Counter()
        self.before2_total: Counter[tuple[str, str]] = Counter()
        self.space_total = 0
        self.total = 0

    def observe(self, left: str, right: str, spaced: bool, left_prev: str = "", right_next: str = "") -> None:
        if not (left.isalnum() or right.isalnum()):
            return
        a, b = _lex_key(left), _lex_key(right)
        value = 1 if spaced else 0
        if spaced:
            self.pair_space[(a, b)] += 1
            self.space_total += 1
        else:
            self.pair_join[(a, b)] += 1
        self.after_space[a] += value
        self.after_total[a] += 1
        self.before_space[b] += value
        self.before_total[b] += 1
        if left_prev and _is_hangul(left_prev) and _is_hangul(left):
            self.after2_space[(left_prev, left)] += value
            self.after2_total[(left_prev, left)] += 1
        if right_next and _is_hangul(right) and _is_hangul(right_next):
            self.before2_space[(right, right_next)] += value
            self.before2_total[(right, right_next)] += 1
        self.total += 1

    def logodds(self, left: str, right: str, left_prev: str = "", right_next: str = "") -> float:
        a, b = _lex_key(left), _lex_key(right)
        base = (self.space_total + 1) / (self.total + 3)
        prior = _logit(base)
        after_prior = _WORD_FINAL_PRIOR.get(a, base)
        after = (self.after_space[a] + 4 * after_prior) / (self.after_total[a] + 4)
        if left_prev and _is_hangul(left_prev) and _is_hangul(left):
            ending = _WORD_FINAL_ENDINGS.get(left_prev + left)
            anchor = ending if ending is not None else after
            weight = 6 if ending is not None else 3
            key = (left_prev, left)
            after = (self.after2_space[key] + weight * anchor) / (self.after2_total[key] + weight)
        before_prior = _WORD_INITIAL_PRIOR.get(b, base)
        before = (self.before_space[b] + 4 * before_prior) / (self.before_total[b] + 4)
        if right_next and _is_hangul(right) and _is_hangul(right_next):
            key = (right, right_next)
            before = (self.before2_space[key] + 3 * before) / (self.before2_total[key] + 3)
        estimate = _logit(after) + _logit(before) - prior
        spaced = self.pair_space[(a, b)]
        joined = self.pair_join[(a, b)]
        if spaced + joined:
            probability = (spaced + 2 * _sigmoid(estimate)) / (spaced + joined + 2)
            estimate = _logit(probability)
        return max(-6.0, min(6.0, estimate))


def _split_spaces(line: TextLine) -> None:
    chars: list[Glyph] = []
    explicit: list[bool] = []
    leading = False
    spaces = 0
    for glyph in line.glyphs:
        if glyph.char == " ":
            spaces += 1
            if chars:
                explicit[-1] = True
            else:
                leading = True
            continue
        chars.append(glyph)
        explicit.append(False)
    line.chars = chars
    line.trailing_space = bool(explicit and explicit[-1])
    if explicit:
        explicit[-1] = False
    line.explicit_after = explicit
    line.leading_space = leading
    line.space_count = spaces


def _pair_decisions(line: TextLine, context: _PageContext, model: Optional[_SpacingModel], collect: bool) -> list[bool]:
    chars = line.chars
    count = len(chars)
    if count < 2:
        return []
    size = max(0.1, line.size)
    gaps = [chars[index + 1].x0 - chars[index].x1 for index in range(count - 1)]
    advances = [chars[index + 1].ox - chars[index].ox for index in range(count - 1)]
    decisions: list[bool] = []

    if _is_explicit_line(line, context):
        plain = [gaps[index] for index in range(count - 1) if not line.explicit_after[index]]
        typical = median(plain) if plain else 0.0
        for index in range(count - 1):
            local = max(0.1, min(chars[index].size, chars[index + 1].size))
            spaced = line.explicit_after[index] or gaps[index] > max(0.6 * local, typical + 0.45 * local)
            decisions.append(spaced)
            if collect and model is not None:
                model.observe(chars[index].char, chars[index + 1].char, spaced, *_context(chars, index))
        return decisions

    hh = [
        advances[index] / size for index in range(count - 1)
        if _is_hangul(chars[index].char) and _is_hangul(chars[index + 1].char)
    ]
    base_hh = _percentile(hh, 0.3) if len(hh) >= 3 else context.base_hh
    word_gaps = [
        gaps[index] / size for index in range(count - 1)
        if chars[index].char.isalnum() and chars[index + 1].char.isalnum()
    ]
    base_gap = _percentile(word_gaps, 0.3) if len(word_gaps) >= 3 else (context.base_gap or 0.0)
    for index in range(count - 1):
        left = chars[index].char
        right = chars[index + 1].char
        both_hangul = _is_hangul(left) and _is_hangul(right)
        if both_hangul and base_hh is not None:
            extra = advances[index] / size - base_hh
        else:
            extra = gaps[index] / size - base_gap
        if right in _CLOSING_BRACKETS or left in _OPENING:
            # '( ㉠ )'처럼 괄호 안쪽에 실제 공백 폭이 있을 때만 띄운다.
            decisions.append(extra > max(0.25, context.other_threshold + 0.05))
            continue
        if right in _CLOSING or left in _NO_SPACE_AROUND or right in _NO_SPACE_AROUND:
            decisions.append(extra > 0.5)
            continue
        threshold = context.hh_threshold if both_hangul else context.other_threshold
        if left in _SENTENCE_END and right.isalnum():
            threshold = min(threshold, 0.12)
        geometric = (extra - threshold) / 0.04
        if abs(geometric) >= 2.5:
            spaced = geometric > 0
            if collect and model is not None:
                model.observe(left, right, spaced, *_context(chars, index))
        else:
            lexical = (
                model.logodds(left, right, *_context(chars, index))
                if model is not None and not collect else 0.0
            )
            # 기하 근거가 약한 경계에서만 어휘 통계가 결정에 참여한다.
            spaced = geometric + 0.5 * max(-3.0, min(3.0, lexical)) > 0
        decisions.append(spaced)
    return decisions


def _context(chars: list[Glyph], index: int) -> tuple[str, str]:
    left_prev = chars[index - 1].char if index > 0 else ""
    right_next = chars[index + 2].char if index + 2 < len(chars) else ""
    return left_prev, right_next


def _collect_evidence(line: TextLine, context: _PageContext, model: _SpacingModel) -> None:
    _pair_decisions(line, context, model, collect=True)


def _finalize_line(line: TextLine, context: _PageContext, model: _SpacingModel) -> None:
    decisions = _pair_decisions(line, context, model, collect=False)
    units: list[tuple[str, Optional[TextStyle]]] = []
    unit_glyphs: list[Optional[Glyph]] = []
    for index, glyph in enumerate(line.chars):
        units.append((glyph.char, glyph.style))
        unit_glyphs.append(glyph)
        if index < len(decisions) and decisions[index]:
            units.append((" ", None))
            unit_glyphs.append(None)
    units, unit_glyphs = _compose_jamo(units, unit_glyphs)
    line.units = units
    line.unit_glyphs = unit_glyphs
    line.text = "".join(char for char, _style in units)
    match = _MARKER_RE.match(line.text)
    line.marker = bool(match)
    line.hanging_rel = None
    if match:
        for position in range(match.end(), len(unit_glyphs)):
            glyph = unit_glyphs[position]
            if glyph is not None:
                line.hanging_rel = glyph.x0
                break
    first_space = line.text.find(" ")
    if first_space > 0:
        last = next((unit_glyphs[i] for i in range(first_space - 1, -1, -1) if unit_glyphs[i] is not None), None)
        line.first_word_width = (last.x1 - line.x0) if last is not None else 0.0
    else:
        line.first_word_width = min(line.x1 - line.x0, 12 * line.size)


def _compose_jamo(units, glyphs):
    """분해된 한글 자모(ᄒ+ᅡ+ᆫ)를 완성형 음절로 합친다."""
    if not any("\u1100" <= char <= "\u11ff" for char, _style in units):
        return units, glyphs
    output_units = []
    output_glyphs = []
    index = 0
    while index < len(units):
        char, style = units[index]
        if (
            "\u1100" <= char <= "\u1112"
            and index + 1 < len(units)
            and "\u1161" <= units[index + 1][0] <= "\u1175"
        ):
            code = 0xAC00 + ((ord(char) - 0x1100) * 21 + (ord(units[index + 1][0]) - 0x1161)) * 28
            consumed = 2
            if index + 2 < len(units) and "\u11a8" <= units[index + 2][0] <= "\u11c2":
                code += ord(units[index + 2][0]) - 0x11A7
                consumed = 3
            output_units.append((chr(code), style))
            output_glyphs.append(glyphs[index])
            index += consumed
            continue
        output_units.append((char, style))
        output_glyphs.append(glyphs[index])
        index += 1
    return output_units, output_glyphs


def _region_text(glyphs: list[Glyph], context: _PageContext, model: _SpacingModel) -> str:
    if not glyphs:
        return ""
    lines = build_lines(glyphs, None)
    lines.sort(key=lambda line: (line.y0, line.x0))
    local_context = _PageContext(
        explicit=context.explicit or _uses_space_glyphs(glyphs),
        base_hh=context.base_hh,
        base_gap=context.base_gap,
        hh_threshold=context.hh_threshold,
        other_threshold=context.other_threshold,
    )
    output: list[str] = []
    for line in lines:
        _split_spaces(line)
        _finalize_line(line, local_context, model)
        if line.text.strip():
            output.append(line.text)
    return "\n".join(output)


# ---------------------------------------------------------------------------
# 줄바꿈(문단) 판정


def _decide_breaks(pages: list[_PreparedPage]) -> None:
    previous: Optional[TextLine] = None
    previous_page: Optional[_PreparedPage] = None
    for page in pages:
        page.breaks = []
        if not page.flow:
            # 빈 페이지는 앞뒤 페이지의 문단 연결을 끊는다.
            if previous_page is not None:
                previous_page.cross_break = "hard"
            previous = None
            previous_page = None
            continue
        for line in page.flow:
            if previous is None or previous_page is None:
                line.continuation = False
            else:
                same_page = previous_page is page
                kind = _break_kind(
                    previous,
                    line,
                    same_page,
                    previous_page.containers,
                    page.containers,
                )
                line.continuation = kind == "soft"
                if same_page:
                    page.breaks.append(kind)
                else:
                    previous_page.cross_break = kind
            previous = line
            previous_page = page


def _break_kind(
    previous: TextLine,
    current: TextLine,
    same_page: bool,
    previous_containers: dict,
    current_containers: dict,
) -> str:
    if previous.is_region or current.is_region:
        return "hard"
    if not previous.text.strip() or not current.text.strip():
        return "hard"
    if current.marker or _STANDALONE_RE.match(current.text) or _STANDALONE_RE.match(previous.text):
        return "hard"
    if _GROUP_HEADER_RE.match(previous.text) or _INSTRUCTION_RE.search(previous.text):
        return "hard"
    size = max(previous.size, current.size, 0.1)
    if abs(previous.size - current.size) > 0.18 * size:
        return "hard"
    previous_frame = previous.container[1] if len(previous.container) > 1 else -1
    current_frame = current.container[1] if len(current.container) > 1 else -1
    if (previous_frame >= 0) != (current_frame >= 0):
        return "hard"  # 테두리 상자 안팎을 넘나드는 문단은 없다.
    # 서로 다른 상자(예: 단마다 그린 테두리) 사이는 단 전환과 같은 엄격한 규칙으로 판단한다.
    previous_box = previous_containers.get(previous.container) or _Container(previous.x0, previous.x1)
    current_box = current_containers.get(current.container) or _Container(current.x0, current.x1)

    same_container = same_page and previous.container == current.container
    if same_container:
        dy = current.baseline - previous.baseline
        pitch = previous_box.pitch or 1.5 * size
        if dy <= 0.3 * size:
            return "hard"
        if dy > 1.9 * pitch and dy > 2.3 * size:
            return "blank"
        if dy > 1.45 * pitch:
            return "hard"

    room = previous_box.right - previous.x1
    box_width = max(1.0, previous_box.right - previous_box.left)
    if not same_container:
        # 단·페이지를 넘어 이어지는 문단은 앞 줄이 오른쪽 끝까지 차 있어야 한다.
        short = room > max(1.2 * previous.size, 0.04 * box_width)
    elif previous_box.justified:
        short = room > max(1.6 * previous.size, 0.04 * box_width)
    else:
        short = room > max(1.6 * previous.size, current.first_word_width + 0.6 * previous.size)
    if short:
        return "hard"

    current_rel = current.x0 - current_box.left
    if previous.continuation:
        expected = [previous.x0 - previous_box.left]
    else:
        previous_rel = previous.x0 - previous_box.left
        expected = [0.0]
        if previous.marker:
            expected.append(previous_rel)
            if previous.hanging_rel is not None:
                expected.append(previous.hanging_rel)
        elif previous_rel <= 0.6 * previous.size:
            expected.append(previous_rel)
    tolerance = 0.6 * current.size
    if not any(abs(current_rel - value) <= tolerance for value in expected):
        return "hard"
    return "soft"


def _trailing_spaces_kept(pages: list[_PreparedPage]) -> bool:
    kept = 0
    total = 0
    for previous, current, kind in _iter_breaks(pages):
        if kind != "soft":
            continue
        total += 1
        if previous.trailing_space or current.leading_space:
            kept += 1
    return total >= 10 and kept >= 0.1 * total


def _iter_breaks(pages: list[_PreparedPage]):
    for page_index, page in enumerate(pages):
        for index, kind in enumerate(page.breaks):
            yield page.flow[index], page.flow[index + 1], kind
        if page.flow and page.cross_break == "soft":
            following = next((item for item in pages[page_index + 1:] if item.flow), None)
            if following is not None:
                yield page.flow[-1], following.flow[0], "soft"


class _Vocabulary:
    """줄 안에서 양쪽이 띄어쓰기로 확정된 어절 목록. 줄바꿈 이음 판단에 사용한다."""

    _EDGE_PUNCT = "\"'“”‘’()[]{}「」『』<>〈〉《》【】.,!?;:·…~"

    def __init__(self, pages: list[_PreparedPage]) -> None:
        self.counts: Counter[str] = Counter()
        continued_from_previous = False
        for page in pages:
            flow = page.flow
            if not flow:
                continued_from_previous = False
                continue
            for index, line in enumerate(flow):
                if line.is_region or not line.text:
                    continue
                tokens = line.text.split(" ")
                if index == 0:
                    starts_clean = not continued_from_previous
                else:
                    starts_clean = page.breaks[index - 1] != "soft"
                if index >= len(page.breaks):
                    ends_clean = page.cross_break != "soft"
                else:
                    ends_clean = page.breaks[index] != "soft"
                for position, token in enumerate(tokens):
                    if position == 0 and not starts_clean:
                        continue
                    if position == len(tokens) - 1 and not ends_clean:
                        continue
                    core = self.core(token)
                    if len(core) >= 2 or (core and _is_hangul(core)):
                        self.counts[core] += 1
            continued_from_previous = page.cross_break == "soft"
        self.sorted_words = sorted(self.counts)

    @classmethod
    def core(cls, token: str) -> str:
        return token.strip(cls._EDGE_PUNCT)

    def has_prefix(self, prefix: str) -> bool:
        index = bisect_left(self.sorted_words, prefix)
        return index < len(self.sorted_words) and self.sorted_words[index].startswith(prefix)

    def score(self, left_token: str, right_token: str) -> float:
        """양수면 띄어 쓰기, 음수면 붙여 쓰기 근거."""
        left = left_token.lstrip(self._EDGE_PUNCT)
        right = right_token.rstrip(self._EDGE_PUNCT)
        if not left or not right:
            return 0.0
        joined = left + right
        value = 0.0
        if self.counts[joined]:
            value -= 2.5
        elif len(joined) >= 3 and self.has_prefix(joined):
            value -= 1.2
        elif len(left) + 1 >= 2 and self.has_prefix(left + right[0]) and not self.counts[left]:
            # '의|미하였다'처럼 이어 붙인 앞부분이 문서의 다른 어절 시작과 같다.
            value -= 1.0
        left_known = self.counts[left] > 0
        right_known = self.counts[right] > 0
        if not right_known and len(right) >= 3 and self.has_prefix(right[:-1]):
            right_known = True  # '행복의'처럼 끝 조사만 다른 낱말이 문서에 있다.
        if left_known and right_known:
            value += 1.6
        elif left_known or right_known:
            value += 0.5
        return value


def _decide_join_spaces(pages: list[_PreparedPage], model: _SpacingModel, trailing_kept: bool) -> None:
    vocabulary = _Vocabulary(pages)
    for page_index, page in enumerate(pages):
        page.join_spaces = []
        for index, kind in enumerate(page.breaks):
            if kind == "soft":
                page.join_spaces.append(
                    _join_with_space(
                        page.flow[index], page.flow[index + 1], model, vocabulary,
                        trailing_kept, page.flow, index,
                    )
                )
            else:
                page.join_spaces.append(False)
        if page.flow and page.cross_break == "soft":
            following = next((item for item in pages[page_index + 1:] if item.flow), None)
            if following is None:
                page.cross_break = "hard"
            else:
                page.cross_space = _join_with_space(
                    page.flow[-1], following.flow[0], model, vocabulary,
                    trailing_kept, page.flow, len(page.flow) - 1,
                )


def _join_with_space(
    previous: TextLine,
    current: TextLine,
    model: _SpacingModel,
    vocabulary: _Vocabulary,
    trailing_kept: bool,
    flow: list[TextLine],
    previous_index: int,
) -> bool:
    if previous.trailing_space or current.leading_space:
        return True
    if not previous.text or not current.text:
        return False
    left = previous.text[-1]
    right = current.text[0]
    if left in "-‐" and (right.isalnum()):
        return False
    if left in _OPENING or right in _CLOSING or left in _NO_SPACE_AROUND or right in _NO_SPACE_AROUND:
        return False
    if left in _SENTENCE_END:
        return right.isalnum() or right in _OPENING or right in "'\""
    if left in "'\"":
        paragraph = _paragraph_text(flow, previous_index)
        if paragraph.count(left) % 2 == 1:
            return False
    if left.isascii() and left.isalpha() and right.isascii() and right.isalpha():
        return True
    left_token = previous.text.rsplit(" ", 1)[-1]
    right_token = current.text.split(" ", 1)[0]
    left_core = _Vocabulary.core(left_token)
    right_core = _Vocabulary.core(right_token)
    left_prev = previous.text[-2] if len(previous.text) >= 2 and previous.text[-2] != " " else ""
    right_next = current.text[1] if len(current.text) >= 2 and current.text[1] != " " else ""
    score = model.logodds(left, right, left_prev, right_next)
    score += vocabulary.score(left_token, right_token)
    score += _length_prior(left_core, right_core)
    if len(left_core) == 1 and left_core in _LONE_PARTICLES and len(left_token) == 1 and len(previous.text) > 1:
        # '… 의|미하였다'처럼 조사 모양 한 글자는 대개 다음 줄 낱말의 앞부분이다.
        # 단, '갑과 을|사이'처럼 문서에서 홀로 쓰이는 낱말이면 어휘 근거가 이를 뒤집는다.
        score -= 2.5
    if trailing_kept and (_is_hangul(left) or _is_hangul(right)):
        return score > 3.0
    return score > 0.0


def _length_prior(left: str, right: str) -> float:
    """앞 조각·뒤 조각 길이로 본 '어절 사이' 대 '어절 안' 로그 비율(약하게 반영)."""
    if not left or not right or not all(_is_hangul(char) for char in left + right):
        return 0.0
    if len(left) == 1 and left == "수" and right[0] in "있없":
        return 1.2  # '할 수|있다'
    if len(left) == 1 and left in _STANDALONE_SYLLABLES and _WORD_INITIAL_PRIOR.get(right[0], 1.0) >= 0.3:
        return 0.8  # '일상이 된|현대', '더|큰'처럼 한 음절 낱말 뒤의 줄바꿈
    a, b = len(left), len(right)

    def probability(length: int) -> float:
        return _EOJEOL_LENGTH.get(length, 0.008)

    ratio = probability(a) * probability(b) / probability(a + b)
    return max(-1.2, min(1.2, 0.5 * math.log(ratio)))


def _paragraph_text(flow: list[TextLine], index: int) -> str:
    parts = [flow[index].text]
    cursor = index
    while cursor > 0 and flow[cursor].continuation:
        cursor -= 1
        parts.append(flow[cursor].text)
    return "".join(reversed(parts))


# ---------------------------------------------------------------------------
# 공통 유틸


def _fill_space_styles(chars: list[str], styles: list[Optional[TextStyle]]) -> list[TextStyle]:
    """추가한 공백은 양옆 글자 서식이 같을 때만 그 서식을 이어받는다(연속 밑줄 보존)."""
    output: list[TextStyle] = []
    count = len(styles)
    for index, style in enumerate(styles):
        if style is not None:
            output.append(style)
            continue
        left = styles[index - 1] if index > 0 else None
        right = styles[index + 1] if index + 1 < count else None
        if left is not None and left == right and not chars[index - 1].isspace():
            output.append(left)
        else:
            output.append(DEFAULT_STYLE)
    return output


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
