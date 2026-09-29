"""PDF 글자 서식·그림을 평문 도메인 모델의 rich sidecar로 투영한다.

페이지 텍스트에는 <보기>·표·그림 자리에 `\\ue000id\\ue001` 자리표시자가 들어 있다.
구조 파서가 만든 지문/발문/선택지 문자열에서 자리표시자를 찾아 해당 칸에 그림을
붙이고, 자리표시자는 평문에서 지운다. 투영은 여러 번 실행해도 결과가 같다.
"""
from __future__ import annotations

import hashlib
import html
import re
from bisect import bisect_left
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional, Sequence

from ..models import MediaAsset, Passage, Question

if TYPE_CHECKING:
    from .pdf_parser import PageText

FIGURE_TOKEN_RE = re.compile(
    r"\ue000([A-Za-z0-9_-]{1,40})\ue001|\[\[\s*그림\s*:\s*([A-Za-z0-9_-]{1,40})\s*\]\]"
)
_CHOICE_PREFIX_RE = re.compile(r"^\s*[①②③④⑤]\s*")


@dataclass(frozen=True)
class RichRun:
    start: int
    end: int
    bold: bool = False
    italic: bool = False
    underline: bool = False
    color: str = ""

    @property
    def has_style(self) -> bool:
        return self.bold or self.italic or self.underline or bool(self.color)


@dataclass(frozen=True)
class _Style:
    bold: bool = False
    italic: bool = False
    underline: bool = False
    color: str = ""

    @property
    def is_default(self) -> bool:
        return not (self.bold or self.italic or self.underline or self.color)


_DEFAULT = _Style()


@dataclass
class _LocatedField:
    start: int
    end: int
    passage: Passage
    question: Optional[Question]
    anchor: str


# ---------------------------------------------------------------------------
# 자리표시자


def extract_figure_tokens(text: str) -> tuple[str, list[str]]:
    """자리표시자·[[그림:id]] 토큰을 지운 텍스트와 등장 순서대로의 id 목록."""
    if not text or ("\ue000" not in text and "[[" not in text):
        return text or "", []
    ids: list[str] = []
    output: list[str] = []
    for line in text.split("\n"):
        found = [match.group(1) or match.group(2) for match in FIGURE_TOKEN_RE.finditer(line)]
        if not found:
            output.append(line)
            continue
        ids.extend(found)
        cleaned = re.sub(r"[ \t]{2,}", " ", FIGURE_TOKEN_RE.sub("", line)).strip()
        if cleaned:
            output.append(cleaned)
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(output)).strip("\n")
    return result, list(dict.fromkeys(ids))


def strip_figure_tokens(text: str) -> str:
    return extract_figure_tokens(text)[0]


def styles_to_runs(styles: Sequence) -> list[RichRun]:
    """문자별 서식(TextStyle/_Style) 목록을 RichRun 구간으로 압축한다."""
    runs: list[RichRun] = []
    start = 0
    count = len(styles)
    while start < count:
        style = styles[start]
        end = start + 1
        while end < count and styles[end] == style:
            end += 1
        if style is not None and (style.bold or style.italic or style.underline or style.color):
            runs.append(RichRun(start, end, bool(style.bold), bool(style.italic), bool(style.underline), _safe_color(style.color)))
        start = end
    return runs


# ---------------------------------------------------------------------------
# 투영


class _Corpus:
    """모든 페이지 텍스트를 자리표시자 없이 이어 붙이고 그림 위치를 기록한다."""

    def __init__(self, pages: Sequence["PageText"]) -> None:
        chars: list[str] = []
        styles: list[_Style] = []
        self.tokens: list[tuple[int, str]] = []
        self.page_ranges: list[tuple[int, int, int]] = []
        self.assets: dict[str, MediaAsset] = {}
        self.asset_pages: dict[str, int] = {}
        for page in pages:
            for asset in page.images:
                self.assets.setdefault(asset.id, asset)
                self.asset_pages.setdefault(asset.id, page.page_number)
            if chars:
                chars.extend("\n\n")
                styles.extend([_DEFAULT, _DEFAULT])
            start = len(chars)
            page_styles = _expand_runs(page.text, page.runs)
            offset = 0
            for line in page.text.split("\n"):
                line_styles = page_styles[offset: offset + len(line)]
                offset += len(line) + 1
                matches = list(FIGURE_TOKEN_RE.finditer(line))
                if matches:
                    for match in matches:
                        self.tokens.append((len(chars), match.group(1) or match.group(2)))
                    kept_chars: list[str] = []
                    kept_styles: list[_Style] = []
                    cursor = 0
                    for match in matches:
                        kept_chars.extend(line[cursor:match.start()])
                        kept_styles.extend(line_styles[cursor:match.start()])
                        cursor = match.end()
                    kept_chars.extend(line[cursor:])
                    kept_styles.extend(line_styles[cursor:])
                    if not "".join(kept_chars).strip():
                        continue
                    line = "".join(kept_chars)
                    line_styles = kept_styles
                chars.extend(line)
                styles.extend(line_styles)
                chars.append("\n")
                styles.append(_DEFAULT)
            self.page_ranges.append((start, len(chars), page.page_number))
        self.text = "".join(chars)
        self.styles = styles
        self.norm, self.norm_map = _normalized_with_map(self.text)

    def locate(self, fragment: str, cursor: int) -> Optional[tuple[int, int]]:
        fragment_norm, _ = _normalized_with_map(fragment)
        if not fragment_norm or not self.norm:
            return None
        norm_cursor = bisect_left(self.norm_map, max(0, cursor))
        position = self.norm.find(fragment_norm, norm_cursor)
        if position < 0:
            position = self.norm.find(fragment_norm)
        if position < 0:
            return None
        start = self.norm_map[position]
        end = self.norm_map[position + len(fragment_norm) - 1] + 1
        return start, end

    def render(self, text: str, location: tuple[int, int]) -> str:
        start, end = location
        runs = _align_styles(text, self.text[start:end], self.styles[start:end])
        return render_rich_html(text, runs)

    def pages_for(self, location: tuple[int, int]) -> set[int]:
        start, end = location
        return {page for page_start, page_end, page in self.page_ranges if max(start, page_start) < min(end, page_end)}


class RichContentProjector:
    """페이지 sidecar를 최종 Passage/Question 필드에 안전하게 투영한다."""

    def project(self, passages: list[Passage], pages: list["PageText"], source_file: str) -> list[str]:
        warnings: list[str] = []
        corpus = _Corpus(pages)
        digest = _file_digest(source_file) if source_file else ""
        attached = {asset.id for passage in passages for asset in _passage_assets(passage)}
        fields: list[_LocatedField] = []
        cursor = 0

        for passage in passages:
            text, ids = extract_figure_tokens(passage.text)
            passage.text = text
            location = corpus.locate(text, cursor) if text.strip() else None
            if location:
                passage.text_html = corpus.render(text, location)
                fields.append(_LocatedField(location[0], location[1], passage, None, "passage"))
                cursor = location[1]
            self._attach(ids, corpus, passage, None, "passage", attached)

            for question in passage.questions:
                question_pages: set[int] = set()
                stem, ids = extract_figure_tokens(question.stem)
                question.stem = stem
                location = corpus.locate(stem, cursor) if stem.strip() else None
                if location:
                    question.stem_html = corpus.render(stem, location)
                    fields.append(_LocatedField(location[0], location[1], passage, question, "stem"))
                    question_pages |= corpus.pages_for(location)
                    cursor = location[1]
                self._attach(ids, corpus, passage, question, "stem", attached)

                html_choices: list[str] = []
                for choice_index, choice in enumerate(question.choices):
                    clean, ids = extract_figure_tokens(choice)
                    question.choices[choice_index] = clean
                    location = corpus.locate(clean, cursor) if clean.strip() else None
                    if location is None and _CHOICE_PREFIX_RE.match(clean):
                        body = _CHOICE_PREFIX_RE.sub("", clean)
                        location = corpus.locate(body, cursor) if body.strip() else None
                    anchor = f"choice:{min(choice_index, 4)}"
                    if location:
                        html_choices.append(corpus.render(clean, location))
                        fields.append(_LocatedField(location[0], location[1], passage, question, anchor))
                        question_pages |= corpus.pages_for(location)
                        cursor = location[1]
                    else:
                        html_choices.append("")
                    self._attach(ids, corpus, passage, question, anchor, attached)
                question.choices_html = html_choices
                if question_pages:
                    question.source_pages = sorted(question_pages)
            if digest:
                passage.source_digest = digest

        missing = self._attach_by_position(corpus, fields, attached)
        self._attach_page_snapshots(passages, pages, attached)
        if missing:
            warnings.append(
                f"그림 {missing}개의 정확한 지문·문항 위치를 특정하지 못해 자동 배치하지 않았습니다."
            )
        return warnings

    @staticmethod
    def _attach(ids: Iterable[str], corpus: _Corpus, passage: Passage, question: Optional[Question],
                anchor: str, attached: set[str]) -> None:
        for asset_id in ids:
            source = corpus.assets.get(asset_id)
            if source is None or asset_id in attached:
                continue
            asset = MediaAsset.from_dict(source.to_dict())
            asset.anchor = anchor
            (passage.images if question is None else question.images).append(asset)
            attached.add(asset_id)

    def _attach_by_position(self, corpus: _Corpus, fields: list[_LocatedField], attached: set[str]) -> int:
        """텍스트 대조로 붙이지 못한 그림을 원문 위치상 바로 앞 칸에 붙인다."""
        ordered = sorted(fields, key=lambda item: item.start)
        starts = [item.start for item in ordered]
        missing = 0
        for position, asset_id in corpus.tokens:
            if asset_id in attached or asset_id not in corpus.assets:
                continue
            index = bisect_left(starts, position + 1) - 1
            target = ordered[index] if index >= 0 else None
            page = corpus.asset_pages.get(asset_id, 0)
            if target is not None and target.passage.source_pages and page not in target.passage.source_pages:
                target = None
            if target is None:
                missing += 1
                continue
            self._attach([asset_id], corpus, target.passage, target.question, target.anchor, attached)
        return missing

    @staticmethod
    def _attach_page_snapshots(passages: list[Passage], pages: Sequence["PageText"], attached: set[str]) -> None:
        for page in pages:
            for source in page.images:
                if source.kind != "page" or source.id in attached:
                    continue
                owner = next((passage for passage in passages if page.page_number in passage.source_pages), None)
                if owner is None:
                    continue
                asset = MediaAsset.from_dict(source.to_dict())
                asset.anchor = "passage"
                owner.images.append(asset)
                attached.add(source.id)


def _passage_assets(passage: Passage):
    yield from passage.images
    for question in passage.questions:
        yield from question.images


# ---------------------------------------------------------------------------
# HTML 변환


def render_rich_html(text: str, runs: list[RichRun]) -> str:
    """평문과 검증된 run을 Qt/ReportLab 공통 subset HTML로 변환."""
    if not text:
        return ""
    styles = [_DEFAULT for _ in text]
    for run in runs:
        style = _Style(run.bold, run.italic, run.underline, _safe_color(run.color))
        for index in range(max(0, run.start), min(len(text), run.end)):
            styles[index] = style
    if all(style.is_default for style in styles):
        return ""

    output: list[str] = []
    current = _DEFAULT
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        content = html.escape("".join(buffer)).replace("\n", "<br/>")
        output.append(_wrap_style(content, current))
        buffer.clear()

    for char, style in zip(text, styles, strict=True):
        if style != current:
            flush()
            current = style
        buffer.append(char)
    flush()
    return "".join(output)


def plain_to_html(text: str) -> str:
    return html.escape(text or "").replace("\n", "<br/>")


class _PlainExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        tag = tag.lower()
        if tag == "br":
            self.parts.append("\n")
        elif tag in {"p", "div", "li"} and self.parts:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def rich_html_to_plain(value: str) -> str:
    parser = _PlainExtractor()
    parser.feed(value or "")
    parser.close()
    return "".join(parser.parts)


def _wrap_style(content: str, style: _Style) -> str:
    if style.is_default:
        return content
    css: list[str] = []
    if style.bold:
        css.append("font-weight:700")
    if style.italic:
        css.append("font-style:italic")
    if style.underline:
        css.append("text-decoration:underline")
    if style.color:
        css.append(f"color:{style.color}")
    return f'<span style="{";".join(css)}">{content}</span>'


# ---------------------------------------------------------------------------
# 서식 정렬


def _expand_runs(text: str, runs: Sequence[RichRun]) -> list[_Style]:
    styles = [_DEFAULT] * len(text)
    for run in runs:
        style = _Style(run.bold, run.italic, run.underline, _safe_color(run.color))
        for index in range(max(0, run.start), min(len(text), run.end)):
            styles[index] = style
    return styles


def _align_styles(target: str, source: str, source_styles: Sequence[_Style]) -> list[RichRun]:
    if target == source and len(source_styles) >= len(target):
        return _compress_styles(list(source_styles[: len(target)]))
    source_norm, source_map = _normalized_with_map(source)
    target_norm, target_map = _normalized_with_map(target)
    styles = [_DEFAULT for _ in target]
    if source_norm == target_norm:
        for source_index, target_index in zip(source_map, target_map, strict=True):
            if source_index < len(source_styles):
                styles[target_index] = source_styles[source_index]
    else:
        matcher = SequenceMatcher(None, source_norm, target_norm, autojunk=False)
        for block in matcher.get_matching_blocks():
            for offset in range(block.size):
                source_index = source_map[block.a + offset]
                target_index = target_map[block.b + offset]
                if source_index < len(source_styles):
                    styles[target_index] = source_styles[source_index]
    _interpolate_whitespace_styles(target, styles)
    return _compress_styles(styles)


def _interpolate_whitespace_styles(text: str, styles: list[_Style]) -> None:
    """같은 서식의 인접 문자 사이 공백에도 서식을 이어 연속 밑줄을 보존."""
    for index, char in enumerate(text):
        if not char.isspace() or not styles[index].is_default or char == "\n":
            continue
        left = index - 1
        while left >= 0 and text[left].isspace():
            left -= 1
        right = index + 1
        while right < len(text) and text[right].isspace():
            right += 1
        if left >= 0 and right < len(text) and styles[left] == styles[right] and not styles[left].is_default:
            styles[index] = styles[left]


def _compress_styles(styles: list[_Style]) -> list[RichRun]:
    runs: list[RichRun] = []
    start = 0
    while start < len(styles):
        style = styles[start]
        end = start + 1
        while end < len(styles) and styles[end] == style:
            end += 1
        if not style.is_default:
            runs.append(RichRun(start, end, style.bold, style.italic, style.underline, style.color))
        start = end
    return runs


def _normalized_with_map(text: str) -> tuple[str, list[int]]:
    translated = text.translate(str.maketrans("➀➁➂➃➄❶❷❸❹❺", "①②③④⑤①②③④⑤"))
    chars: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(translated):
        if char.isspace():
            continue
        chars.append(char.lower())
        positions.append(index)
    return "".join(chars), positions


def _safe_color(value: str) -> str:
    return value.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", value or "") else ""


def _file_digest(source_file: str) -> str:
    try:
        digest = hashlib.sha256()
        with Path(source_file).open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""
