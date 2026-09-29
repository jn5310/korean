"""PDF 글자 서식·이미지를 평문 도메인 모델의 rich sidecar로 투영한다."""
from __future__ import annotations

import hashlib
import html
import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..assets import AssetError, AssetStore
from ..models import MediaAsset, Passage, Question

if TYPE_CHECKING:
    from .pdf_parser import PageText


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


@dataclass
class _LocatedField:
    start: int
    end: int
    question: Optional[Question]
    anchor: str


class PdfRichExtractor:
    """PyMuPDF span/drawing/image 정보를 이미 선택된 PageText 평문에 정렬한다."""

    def __init__(self, asset_store: Optional[AssetStore]) -> None:
        self.asset_store = asset_store

    def enrich(self, pdf_path: Path, pages: list["PageText"], warnings: list[str]) -> None:
        try:
            import pymupdf
        except ImportError:
            warnings.append("PyMuPDF가 없어 글자 서식과 그림 추출을 건너뛰었습니다.")
            return
        try:
            with pymupdf.open(pdf_path) as document:
                for page_text in pages:
                    if not 1 <= page_text.page_number <= document.page_count:
                        continue
                    page = document.load_page(page_text.page_number - 1)
                    try:
                        self._enrich_page(page, page_text, warnings)
                    except Exception as exc:  # noqa: BLE001 - 서식 실패가 평문 분석을 막지 않음
                        warnings.append(f"{page_text.page_number}페이지 서식/그림 추출 실패: {exc}")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"PDF 서식/그림 추출을 건너뛰었습니다: {exc}")

    def _enrich_page(self, page, page_text: "PageText", warnings: list[str]) -> None:
        layout = page.get_text("dict", sort=True)
        drawings = page.get_drawings()
        underline_segments = _horizontal_segments(drawings)
        source_chars: list[str] = []
        source_styles: list[_Style] = []
        source_images: list[tuple[int, bytes, list[float], int, int]] = []
        text_anchors: list[tuple[int, list[float]]] = []
        page_area = max(1.0, float(page.rect.width * page.rect.height))

        for block in layout.get("blocks", []):
            block_type = block.get("type", 0)
            if block_type == 0:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = str(span.get("text", ""))
                        bbox = _float_bbox(span.get("bbox", []))
                        if bbox:
                            text_anchors.append((len(source_chars), bbox))
                        style = _span_style(span, bbox, underline_segments)
                        source_chars.extend(text)
                        source_styles.extend([style] * len(text))
                    source_chars.append("\n")
                    source_styles.append(_Style())
            elif block_type == 1 and self.asset_store is not None:
                image_data = block.get("image")
                bbox = _float_bbox(block.get("bbox", []))
                width = int(block.get("width", 0) or 0)
                height = int(block.get("height", 0) or 0)
                if not isinstance(image_data, bytes) or not bbox or width < 20 or height < 20:
                    continue
                area = max(0.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
                # 스캔 페이지 전체 배경과 작은 로고/아이콘은 문제 그림으로 중복 저장하지 않는다.
                if area / page_area > 0.68 or area / page_area < 0.001:
                    continue
                source_images.append((len(source_chars), image_data, bbox, width, height))

        if self.asset_store is not None:
            existing_boxes = [item[2] for item in source_images]
            for bbox in _vector_regions(drawings, page_area):
                if any(_bbox_overlap_ratio(bbox, existing) > 0.75 for existing in existing_boxes):
                    continue
                try:
                    pixmap = page.get_pixmap(clip=bbox, dpi=180, alpha=False)
                    source_images.append(
                        (
                            _offset_for_bbox(bbox, text_anchors),
                            pixmap.tobytes("png"),
                            bbox,
                            pixmap.width,
                            pixmap.height,
                        )
                    )
                    existing_boxes.append(bbox)
                except Exception:  # noqa: BLE001 - 선택적 벡터 그림 fallback
                    continue

            if page_text.method == "ocr":
                # 스캔본은 개별 그림 분리가 불가능하므로 정확한 검수를 위한 저해상도 원본 페이지를 보존한다.
                try:
                    pixmap = page.get_pixmap(dpi=120, alpha=False)
                    source_images.append(
                        (0, pixmap.tobytes("png"), _float_bbox(page.rect), pixmap.width, pixmap.height)
                    )
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"{page_text.page_number}페이지 OCR 원본 이미지 보존 실패: {exc}")

        source_text = "".join(source_chars)
        page_text.runs = _align_styles(page_text.text, source_text, source_styles)
        page_text.rich_html = render_rich_html(page_text.text, page_text.runs)
        source_to_target = _alignment_map(source_text, page_text.text)

        assets: list[MediaAsset] = []
        for source_offset, image_data, bbox, _width, _height in source_images:
            try:
                target_offset = _nearest_mapped_offset(source_offset, source_to_target)
                asset = self.asset_store.import_bytes(
                    image_data,
                    source_page=page_text.page_number,
                    bbox=bbox,
                    offset=target_offset,
                    alt=f"{page_text.page_number}페이지 문제 그림",
                )
                if all(existing.id != asset.id for existing in assets):
                    assets.append(asset)
            except AssetError as exc:
                warnings.append(f"{page_text.page_number}페이지 그림 추출 생략: {exc}")
        page_text.images = assets


class RichContentProjector:
    """페이지 sidecar를 최종 Passage/Question 필드에 안전하게 투영한다."""

    def project(self, passages: list[Passage], pages: list["PageText"], source_file: str) -> list[str]:
        warnings: list[str] = []
        page_map = {page.page_number: page for page in pages}
        digest = _file_digest(source_file)
        assigned_assets: set[str] = set()
        all_asset_ids = {asset.id for page in pages for asset in page.images}
        for passage in passages:
            selected = [page_map[number] for number in passage.source_pages if number in page_map]
            if not selected:
                selected = list(pages)
            if not selected:
                continue
            corpus, runs, images, page_ranges = _page_corpus(selected)
            fields: list[_LocatedField] = []
            search_from = 0

            location = _locate_fragment(passage.text, corpus, search_from)
            if location:
                start, end, projected_runs = _project_runs(passage.text, corpus, runs, location)
                passage.text_html = render_rich_html(passage.text, projected_runs)
                fields.append(_LocatedField(start, end, None, "passage"))
                search_from = end

            for question in passage.questions:
                question_pages: set[int] = set()
                stem_location = _locate_fragment(question.stem, corpus, search_from)
                if stem_location:
                    start, end, projected_runs = _project_runs(question.stem, corpus, runs, stem_location)
                    question.stem_html = render_rich_html(question.stem, projected_runs)
                    fields.append(_LocatedField(start, end, question, "stem"))
                    question_pages.update(_pages_for_interval(start, end, page_ranges))
                    search_from = end
                html_choices: list[str] = []
                for choice_index, choice in enumerate(question.choices):
                    choice_location = _locate_fragment(choice, corpus, search_from)
                    if choice_location:
                        start, end, projected_runs = _project_runs(choice, corpus, runs, choice_location)
                        html_choices.append(render_rich_html(choice, projected_runs))
                        fields.append(_LocatedField(start, end, question, f"choice:{choice_index}"))
                        question_pages.update(_pages_for_interval(start, end, page_ranges))
                        search_from = end
                    else:
                        html_choices.append("")
                question.choices_html = html_choices
                question.source_pages = sorted(question_pages)

            passage.source_digest = digest
            _assign_images(passage, images, fields, assigned_assets)
        missing = all_asset_ids - assigned_assets
        if missing:
            warnings.append(f"그림 {len(missing)}개의 정확한 지문·문항 위치를 특정하지 못해 자동 배치하지 않았습니다.")
        return warnings


def render_rich_html(text: str, runs: list[RichRun]) -> str:
    """평문과 검증된 run을 Qt/ReportLab 공통 subset HTML로 변환."""
    if not text:
        return ""
    styles = [_Style() for _ in text]
    for run in runs:
        style = _Style(run.bold, run.italic, run.underline, _safe_color(run.color))
        for index in range(max(0, run.start), min(len(text), run.end)):
            styles[index] = style
    if all(style.is_default for style in styles):
        return ""

    output: list[str] = []
    current = _Style()
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


def _span_style(span: dict, bbox: list[float], lines: list[tuple[float, float, float]]) -> _Style:
    flags = int(span.get("flags", 0) or 0)
    font = str(span.get("font", "")).lower()
    color_value = span.get("color", 0)
    try:
        color_number = int(color_value) & 0xFFFFFF
    except (TypeError, ValueError, OverflowError):
        color_number = 0
    color = f"#{color_number:06x}" if color_number not in {0, 0x111111} else ""
    return _Style(
        bold=bool(flags & 16) or any(token in font for token in ("bold", "black", "semibold", "demi")),
        italic=bool(flags & 2) or "italic" in font or "oblique" in font,
        underline=_is_underlined(bbox, lines),
        color=color,
    )


def _horizontal_segments(drawings) -> list[tuple[float, float, float]]:
    result: list[tuple[float, float, float]] = []
    for drawing in drawings or []:
        if float(drawing.get("width", 1) or 1) > 3:
            continue
        for item in drawing.get("items", []):
            if not item:
                continue
            if item[0] == "l" and len(item) >= 3:
                first, second = item[1], item[2]
                x1, y1 = float(first.x), float(first.y)
                x2, y2 = float(second.x), float(second.y)
                if abs(y1 - y2) <= 1.5 and abs(x2 - x1) >= 4:
                    result.append((min(x1, x2), max(x1, x2), (y1 + y2) / 2))
            elif item[0] == "re" and len(item) >= 2:
                bbox = _float_bbox(item[1])
                if bbox and bbox[3] - bbox[1] <= 3 and bbox[2] - bbox[0] >= 4:
                    result.append((bbox[0], bbox[2], (bbox[1] + bbox[3]) / 2))
    return result


def _vector_regions(drawings, page_area: float) -> list[list[float]]:
    """여러 개의 단순 path로 나뉜 도표도 인접 bbox를 묶어 하나의 clip으로 만든다."""
    raw: list[tuple[list[float], int]] = []
    for drawing in drawings or []:
        bbox = _float_bbox(drawing.get("rect", []))
        if bbox:
            raw.append((bbox, len(drawing.get("items", []))))

    clusters: list[tuple[list[float], int, int]] = []  # bbox, path 수, item 수
    remaining = set(range(len(raw)))
    while remaining:
        seed = remaining.pop()
        component = [seed]
        stack = [seed]
        while stack:
            current = stack.pop()
            neighbors = [
                index for index in remaining
                if _rects_near(raw[current][0], raw[index][0], gap=10)
            ]
            for index in neighbors:
                remaining.remove(index)
                component.append(index)
                stack.append(index)
        union = list(raw[component[0]][0])
        for index in component[1:]:
            union = _union_bbox(union, raw[index][0])
        clusters.append(
            (union, len(component), sum(raw[index][1] for index in component))
        )

    regions: list[list[float]] = []
    for bbox, path_count, item_count in clusters:
        width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        ratio = width * height / max(1.0, page_area)
        if width < 35 or height < 24 or not 0.003 <= ratio <= 0.55:
            continue
        if path_count < 2 and item_count < 2:
            continue
        if any(_bbox_overlap_ratio(bbox, existing) > 0.82 for existing in regions):
            continue
        regions.append(bbox)
        if len(regions) >= 12:
            break
    return regions


def _rects_near(first: list[float], second: list[float], gap: float) -> bool:
    return not (
        first[2] + gap < second[0]
        or second[2] + gap < first[0]
        or first[3] + gap < second[1]
        or second[3] + gap < first[1]
    )


def _union_bbox(first: list[float], second: list[float]) -> list[float]:
    return [
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    ]


def _bbox_overlap_ratio(first: list[float], second: list[float]) -> float:
    if not first or not second:
        return 0.0
    overlap_width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    overlap_height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    overlap = overlap_width * overlap_height
    smaller = min(
        max(1.0, (first[2] - first[0]) * (first[3] - first[1])),
        max(1.0, (second[2] - second[0]) * (second[3] - second[1])),
    )
    return overlap / smaller


def _offset_for_bbox(bbox: list[float], anchors: list[tuple[int, list[float]]]) -> int:
    if not anchors:
        return 0
    center_y = (bbox[1] + bbox[3]) / 2
    preceding = [item for item in anchors if item[1][1] <= center_y]
    candidates = preceding or anchors
    return min(candidates, key=lambda item: abs(item[1][3] - center_y))[0]


def _is_underlined(bbox: list[float], lines: list[tuple[float, float, float]]) -> bool:
    if not bbox:
        return False
    x0, _y0, x1, y1 = bbox
    width = max(1.0, x1 - x0)
    for line_x0, line_x1, line_y in lines:
        overlap = max(0.0, min(x1, line_x1) - max(x0, line_x0))
        if overlap / width >= 0.55 and y1 - 2.5 <= line_y <= y1 + 3.5:
            return True
    return False


def _align_styles(target: str, source: str, source_styles: list[_Style]) -> list[RichRun]:
    if target == source and len(source_styles) >= len(target):
        return _compress_styles(list(source_styles[: len(target)]))
    source_norm, source_map = _normalized_with_map(source)
    target_norm, target_map = _normalized_with_map(target)
    styles = [_Style() for _ in target]
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
        if not char.isspace() or not styles[index].is_default:
            continue
        left = index - 1
        while left >= 0 and text[left].isspace():
            left -= 1
        right = index + 1
        while right < len(text) and text[right].isspace():
            right += 1
        if left >= 0 and right < len(text) and styles[left] == styles[right] and not styles[left].is_default:
            styles[index] = styles[left]


def _alignment_map(source: str, target: str) -> dict[int, int]:
    source_norm, source_map = _normalized_with_map(source)
    target_norm, target_map = _normalized_with_map(target)
    mapping: dict[int, int] = {}
    matcher = SequenceMatcher(None, source_norm, target_norm, autojunk=False)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            mapping[source_map[block.a + offset]] = target_map[block.b + offset]
    return mapping


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


def _nearest_mapped_offset(source_offset: int, mapping: dict[int, int]) -> int:
    if not mapping:
        return 0
    candidates = [source for source in mapping if source >= source_offset]
    source = min(candidates) if candidates else max(mapping)
    return mapping[source]


def _page_corpus(pages: list["PageText"]):
    text_parts: list[str] = []
    runs: list[RichRun] = []
    images: list[tuple[int, MediaAsset]] = []
    page_ranges: list[tuple[int, int, int]] = []
    cursor = 0
    for page in pages:
        if text_parts:
            text_parts.append("\n\n")
            cursor += 2
        start = cursor
        text_parts.append(page.text)
        for run in page.runs:
            runs.append(RichRun(start + run.start, start + run.end, run.bold, run.italic, run.underline, run.color))
        for asset in page.images:
            images.append((start + asset.offset, asset))
        cursor += len(page.text)
        page_ranges.append((start, cursor, page.page_number))
    return "".join(text_parts), runs, images, page_ranges


def _locate_fragment(fragment: str, corpus: str, start_hint: int) -> Optional[tuple[int, int]]:
    fragment_norm, _fragment_map = _normalized_with_map(fragment)
    corpus_norm, corpus_map = _normalized_with_map(corpus)
    if not fragment_norm or not corpus_norm:
        return None
    normalized_hint = sum(not char.isspace() for char in corpus[: max(0, start_hint)])
    position = corpus_norm.find(fragment_norm, normalized_hint)
    if position < 0:
        position = corpus_norm.find(fragment_norm)
    if position < 0:
        return None
    start = corpus_map[position]
    end = corpus_map[position + len(fragment_norm) - 1] + 1
    return start, end


def _project_runs(text: str, corpus: str, corpus_runs: list[RichRun], location: tuple[int, int]):
    start, end = location
    source_fragment = corpus[start:end]
    source_styles = [_Style() for _ in source_fragment]
    for run in corpus_runs:
        overlap_start = max(start, run.start)
        overlap_end = min(end, run.end)
        if overlap_start >= overlap_end:
            continue
        style = _Style(run.bold, run.italic, run.underline, run.color)
        for index in range(overlap_start - start, overlap_end - start):
            source_styles[index] = style
    projected = _align_styles(text, source_fragment, source_styles)
    return start, end, projected


def _pages_for_interval(start: int, end: int, ranges: list[tuple[int, int, int]]) -> set[int]:
    return {page for page_start, page_end, page in ranges if max(start, page_start) < min(end, page_end)}


def _assign_images(
    passage: Passage,
    images: list[tuple[int, MediaAsset]],
    fields: list[_LocatedField],
    assigned_assets: set[str],
) -> None:
    passage.images = []
    for question in passage.questions:
        question.images = []
    ordered_fields = sorted(fields, key=lambda item: item.start)
    if not ordered_fields:
        return
    content_start = ordered_fields[0].start
    content_end = ordered_fields[-1].end
    for offset, source_asset in sorted(images, key=lambda item: item[0]):
        if source_asset.id in assigned_assets or not content_start <= offset < content_end:
            continue
        assigned_assets.add(source_asset.id)
        asset = MediaAsset.from_dict(source_asset.to_dict())
        previous = [field for field in ordered_fields if field.start <= offset]
        field = previous[-1] if previous else None
        if field is None or field.question is None:
            asset.anchor = "passage"
            passage.images.append(asset)
        else:
            asset.anchor = field.anchor
            field.question.images.append(asset)


def _float_bbox(value) -> list[float]:
    try:
        items = list(value)
    except (TypeError, ValueError, OverflowError):
        return []
    if len(items) != 4:
        return []
    try:
        result = [float(item) for item in items]
    except (TypeError, ValueError, OverflowError):
        return []
    return result if all(math.isfinite(item) for item in result) else []


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
