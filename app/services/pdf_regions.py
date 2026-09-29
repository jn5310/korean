"""PDF 벡터 도형·이미지 정보로 <보기> 상자, 표, 그림 영역을 찾는다.

PyMuPDF `page.get_drawings()` 결과를 단순 기하 요소(가로선·세로선·사각형·기타 도형)로
바꾼 뒤, 줄 텍스트와 함께 영역 종류를 판정한다. PDF 라이브러리에 직접 의존하지 않는다.

판정 원칙
---------
* <보기>: 상자 윗변 근처에 '<보기>' 라벨 줄이 있거나, 바로 위 발문이 <보기>를 언급하는 상자.
  라벨이 상자 테두리 위에 걸쳐 있어도 잘리지 않도록 라벨 영역을 합쳐 자른다.
* 표: 내부 격자선이 있는 상자.
* 그림: 도형이 모인 영역 또는 래스터 이미지.
* 그 밖의 상자(지문 테두리 등)는 그림으로 바꾸지 않고 텍스트 흐름의 틀(frame)로만 쓴다.
"""
from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .pdf_text_layout import Glyph, TextLine

Rect = tuple[float, float, float, float]

LABEL_RE = re.compile(r"^[<〈＜《\[【(（［「『]?보기[>〉＞》\]】)）］」』]?$")
_CHOICE_START_RE = re.compile(r"^[①②③④⑤⑴⑵⑶⑷⑸]")
_QUESTION_START_RE = re.compile(r"^\d{1,3}[.)]\S")
_GROUP_START_RE = re.compile(r"^[\[【(（]\d{1,3}[~～\-–—]\d{1,3}[\]】)）]")

REGION_ALT = {"bogi": "<보기>", "table": "표", "figure": "그림", "image": "그림"}
_PRIORITY = {"bogi": 3, "table": 2, "figure": 1, "image": 0}


@dataclass(slots=True)
class HSeg:
    y: float
    x0: float
    x1: float
    drawing: int = -1


@dataclass(slots=True)
class VSeg:
    x: float
    y0: float
    y1: float
    drawing: int = -1


@dataclass(slots=True)
class Shape:
    bbox: Rect
    kind: str
    drawing: int = -1


@dataclass(slots=True)
class Box:
    bbox: Rect
    source: str = "rect"
    filled: bool = False


@dataclass
class Primitives:
    hsegs: list[HSeg] = field(default_factory=list)
    vsegs: list[VSeg] = field(default_factory=list)
    boxes: list[Box] = field(default_factory=list)
    shapes: list[Shape] = field(default_factory=list)


@dataclass
class RegionCandidate:
    kind: str
    bbox: Rect

    @property
    def alt(self) -> str:
        return REGION_ALT.get(self.kind, "그림")


@dataclass
class RegionDetection:
    regions: list[RegionCandidate] = field(default_factory=list)
    frames: list[Rect] = field(default_factory=list)
    boxes: list[Box] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 도형 해석


def parse_drawings(drawings: Iterable, page_width: float, page_height: float) -> Primitives:
    """get_drawings() 결과를 가로선·세로선·사각형·기타 도형으로 정리한다."""
    prims = Primitives()
    page_area = max(1.0, page_width * page_height)
    for index, drawing in enumerate(drawings or []):
        if not isinstance(drawing, dict):
            continue
        kind = str(drawing.get("type") or "")
        stroked = drawing.get("color") is not None and (not kind or "s" in kind)
        filled = drawing.get("fill") is not None and (not kind or "f" in kind)
        if kind == "clip" or (not stroked and not filled):
            continue
        white_fill = filled and _is_white(drawing.get("fill"))
        for item in drawing.get("items", []) or []:
            if not item:
                continue
            operator = item[0]
            if operator == "l" and len(item) >= 3:
                (x1, y1), (x2, y2) = _point(item[1]), _point(item[2])
                _add_line(prims, x1, y1, x2, y2, index)
            elif operator == "re" and len(item) >= 2:
                rect = _rect(item[1])
                if rect:
                    _add_rect(prims, rect, index, stroked, filled, white_fill, page_area)
            elif operator == "qu" and len(item) >= 2:
                points = _quad_points(item[1])
                if not points:
                    continue
                xs = [x for x, _y in points]
                ys = [y for _x, y in points]
                rect = (min(xs), min(ys), max(xs), max(ys))
                if _axis_aligned(points):
                    _add_rect(prims, rect, index, stroked, filled, white_fill, page_area)
                else:
                    prims.shapes.append(Shape(rect, "quad", index))
            elif operator == "c" and len(item) >= 5:
                points = [_point(value) for value in item[1:5]]
                xs = [x for x, _y in points]
                ys = [y for _x, y in points]
                prims.shapes.append(Shape((min(xs), min(ys), max(xs), max(ys)), "curve", index))
    return prims


def _add_line(prims: Primitives, x1: float, y1: float, x2: float, y2: float, drawing: int) -> None:
    if not all(math.isfinite(value) for value in (x1, y1, x2, y2)):
        return
    if abs(y1 - y2) <= 1.0 and abs(x2 - x1) >= 3:
        prims.hsegs.append(HSeg((y1 + y2) / 2, min(x1, x2), max(x1, x2), drawing))
    elif abs(x1 - x2) <= 1.0 and abs(y2 - y1) >= 3:
        prims.vsegs.append(VSeg((x1 + x2) / 2, min(y1, y2), max(y1, y2), drawing))
    elif abs(x2 - x1) >= 1 or abs(y2 - y1) >= 1:
        prims.shapes.append(Shape((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)), "line", drawing))


def _add_rect(prims: Primitives, rect: Rect, drawing: int, stroked: bool, filled: bool,
              white_fill: bool, page_area: float) -> None:
    x0, y0, x1, y1 = rect
    width, height = x1 - x0, y1 - y0
    if width <= 0 and height <= 0:
        return
    if height <= 2.2 and width >= 3:
        prims.hsegs.append(HSeg((y0 + y1) / 2, x0, x1, drawing))
        return
    if width <= 2.2 and height >= 3:
        prims.vsegs.append(VSeg((x0 + x1) / 2, y0, y1, drawing))
        return
    if width * height / page_area > 0.6:
        return  # 페이지 배경·전체 테두리
    if stroked:
        prims.boxes.append(Box(rect, "rect", filled and not white_fill))
        # 칸(cell)으로 그린 표의 격자 판정을 위해 변도 선으로 기록한다.
        prims.hsegs.extend([HSeg(y0, x0, x1, drawing), HSeg(y1, x0, x1, drawing)])
        prims.vsegs.extend([VSeg(x0, y0, y1, drawing), VSeg(x1, y0, y1, drawing)])
    elif filled and not white_fill:
        if width >= 30 and height >= 14:
            prims.boxes.append(Box(rect, "fill", True))
        else:
            prims.shapes.append(Shape(rect, "rect", drawing))


# ---------------------------------------------------------------------------
# 영역 판정


def detect_regions(
    lines: Sequence[TextLine],
    glyphs: Sequence[Glyph],
    prims: Primitives,
    image_boxes: Sequence[Rect],
    width: float,
    height: float,
) -> RegionDetection:
    visible = [glyph for glyph in glyphs if not glyph.char.isspace()]
    median_size = _median([glyph.size for glyph in visible]) or 10.0
    page_area = max(1.0, width * height)
    boxes = _dedupe_boxes(
        [box for box in prims.boxes if _box_ok(box.bbox, page_area)]
        + [box for box in _boxes_from_segments(prims) if _box_ok(box.bbox, page_area)]
        + [box for box in _cell_cluster_boxes(prims.boxes) if _box_ok(box.bbox, page_area)]
    )
    detection = RegionDetection(boxes=boxes)
    candidates: list[RegionCandidate] = []
    used_labels: set[int] = set()
    label_lines = [line for line in lines if LABEL_RE.fullmatch(line.compact)]

    for box in sorted(boxes, key=lambda item: _area(item.bbox)):
        inside = [glyph for glyph in visible if _contains(box.bbox, glyph.cx, glyph.cy)]
        label = _label_for_box(label_lines, box.bbox)
        if label is not None:
            used_labels.add(id(label))
            candidates.append(RegionCandidate("bogi", _pad(_union(box.bbox, _line_rect(label)), 3, width, height)))
            continue
        if inside and len(inside) <= 700 and _mentions_bogi_above(lines, box.bbox, median_size):
            candidates.append(RegionCandidate("bogi", _pad(box.bbox, 3, width, height)))
            continue
        if len(inside) <= 450 and _has_grid(box.bbox, prims):
            candidates.append(RegionCandidate("table", _pad(box.bbox, 3, width, height)))
            continue
        if _figure_like(box.bbox, prims, inside, image_boxes):
            candidates.append(RegionCandidate("figure", _pad(box.bbox, 3, width, height)))
            continue
        if inside:
            detection.frames.append(box.bbox)

    for label in label_lines:
        if id(label) in used_labels:
            continue
        rect = _unboxed_bogi(label, lines)
        if rect is not None:
            candidates.append(RegionCandidate("bogi", _pad(rect, 3, width, height)))

    box_edges = _box_edge_filter(boxes)
    for cluster in _shape_clusters(prims, box_edges, visible, width):
        candidates.append(RegionCandidate("figure", _pad(cluster, 2, width, height)))

    for rect in image_boxes:
        clean = _rect(rect)
        if not clean:
            continue
        area = _area(clean)
        if area / page_area > 0.68 or area / page_area < 0.001:
            continue
        if clean[2] - clean[0] < 12 or clean[3] - clean[1] < 12:
            continue
        overlaid = [glyph for glyph in visible if _contains(clean, glyph.cx, glyph.cy)]
        overlaid_area = sum(max(0.0, (glyph.x1 - glyph.x0) * (glyph.y1 - glyph.y0)) for glyph in overlaid)
        if len(overlaid) > 40 or overlaid_area > 0.15 * area:
            continue  # 본문 뒤에 깔린 배경·워터마크 이미지
        candidates.append(RegionCandidate("image", _pad(clean, 1, width, height)))

    merged = _merge_candidates(candidates)
    detection.regions = [
        region for region in merged
        if not _swallows_structure(region, lines) and not _in_page_margin(region.bbox, height)
    ]
    detection.regions.sort(key=lambda region: (region.bbox[1], region.bbox[0]))
    return detection


def mark_underlines(glyphs: Sequence[Glyph], prims: Primitives, boxes: Sequence[Box]) -> int:
    """글자 기준선 바로 아래의 가로선을 밑줄로 표시한다(상자 테두리는 제외)."""
    edges = _box_edge_filter(boxes)
    index = _UnderlineIndex(glyphs)
    if not index.glyphs:
        return 0
    marked = 0
    for seg in prims.hsegs:
        if seg.x1 - seg.x0 < 3 or edges(seg):
            continue
        for glyph in index.candidates(seg):
            glyph_width = max(0.1, glyph.x1 - glyph.x0)
            overlap = min(glyph.x1, seg.x1) - max(glyph.x0, seg.x0)
            if overlap >= 0.5 * glyph_width and not glyph.underline:
                glyph.underline = True
                marked += 1
    return marked


# ---------------------------------------------------------------------------
# 상자 찾기


def _boxes_from_segments(prims: Primitives) -> list[Box]:
    # 칸이 아주 많은 표가 있어도 다른 상자의 세로변을 놓치지 않도록 긴 선부터 고른다.
    tall = sorted((seg for seg in prims.vsegs if seg.y1 - seg.y0 >= 12), key=lambda seg: -(seg.y1 - seg.y0))
    verticals = sorted(_distinct_verticals(tall)[:400], key=lambda seg: seg.x)
    horizontals = sorted(prims.hsegs, key=lambda seg: seg.y)
    ys = [seg.y for seg in horizontals]
    tolerance = 12.0

    def near(y: float, x0: float, x1: float) -> list[HSeg]:
        low = bisect_left(ys, y - tolerance)
        high = bisect_right(ys, y + tolerance)
        return [seg for seg in horizontals[low:high] if seg.x1 > x0 - tolerance and seg.x0 < x1 + tolerance]

    boxes: list[Box] = []
    for index, left in enumerate(verticals):
        left_height = left.y1 - left.y0
        for right in verticals[index + 1:]:
            span = right.x - left.x
            if span < 30:
                continue
            if span > 700:
                break
            right_height = right.y1 - right.y0
            overlap = min(left.y1, right.y1) - max(left.y0, right.y0)
            if overlap < 0.8 * min(left_height, right_height):
                continue
            if abs(left_height - right_height) > 0.25 * max(left_height, right_height):
                continue
            top = min(left.y0, right.y0)
            bottom = max(left.y1, right.y1)
            bottom_segments = near(bottom, left.x, right.x)
            if _coverage(bottom_segments, left.x, right.x) < 0.6 * span:
                continue
            top_segments = near(top, left.x, right.x)
            if _coverage(top_segments, left.x, right.x) < 0.3 * span:
                continue
            y0 = min([top] + [seg.y for seg in top_segments])
            y1 = max([bottom] + [seg.y for seg in bottom_segments])
            boxes.append(Box((left.x, y0, right.x, y1), "lines"))
    return boxes


def _distinct_verticals(segments: list[VSeg]) -> list[VSeg]:
    """같은 자리에 겹쳐 그린 세로선(표 칸의 공유 변)은 하나만 남긴다."""
    seen: set[tuple[int, int, int]] = set()
    output: list[VSeg] = []
    for seg in segments:
        key = (round(seg.x), round(seg.y0 / 2), round(seg.y1 / 2))
        if key in seen:
            continue
        seen.add(key)
        output.append(seg)
    return output


def _cell_cluster_boxes(boxes: Sequence[Box]) -> list[Box]:
    """칸 사각형들이 맞닿아 표를 이룬 경우 전체 영역을 하나의 상자로 만든다."""
    cells = [box.bbox for box in boxes if box.source == "rect"]
    if len(cells) < 2:
        return []
    output: list[Box] = []
    for component in _connected_components(cells, 1.5):
        if len(component) >= 2:
            union = cells[component[0]]
            for index in component[1:]:
                union = _union(union, cells[index])
            output.append(Box(union, "cells"))
    return output


def _connected_components(rects: Sequence[Rect], gap: float, cell: float = 32.0) -> list[list[int]]:
    """가까이 붙은 사각형 묶음(격자 색인으로 O(n) 근처 탐색)."""
    grid: dict[tuple[int, int], list[int]] = {}
    for index, rect in enumerate(rects):
        for key in _grid_keys(rect, gap, cell):
            grid.setdefault(key, []).append(index)
    parent = list(range(len(rects)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for members in grid.values():
        if len(members) > 400:
            members = members[:400]
        for position, first in enumerate(members):
            for second in members[position + 1:]:
                root_a, root_b = find(first), find(second)
                if root_a != root_b and _touching(rects[first], rects[second], gap):
                    parent[root_a] = root_b
    groups: dict[int, list[int]] = {}
    for index in range(len(rects)):
        groups.setdefault(find(index), []).append(index)
    return list(groups.values())


def _grid_keys(rect: Rect, gap: float, cell: float):
    x0 = int((rect[0] - gap) // cell)
    x1 = int((rect[2] + gap) // cell)
    y0 = int((rect[1] - gap) // cell)
    y1 = int((rect[3] + gap) // cell)
    if (x1 - x0 + 1) * (y1 - y0 + 1) > 900:
        x1 = x0 + 29
        y1 = y0 + 29
    for gx in range(x0, x1 + 1):
        for gy in range(y0, y1 + 1):
            yield (gx, gy)


def _dedupe_boxes(boxes: list[Box]) -> list[Box]:
    output: list[Box] = []
    for box in sorted(boxes, key=lambda item: -_area(item.bbox)):
        if any(_iou(box.bbox, other.bbox) > 0.9 for other in output):
            continue
        output.append(box)
    return output


def _box_ok(rect: Rect, page_area: float) -> bool:
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    return width >= 30 and height >= 14 and width * height / page_area <= 0.6


# ---------------------------------------------------------------------------
# 판정 도우미


def _label_for_box(labels: Sequence[TextLine], box: Rect) -> Optional[TextLine]:
    best = None
    best_distance = float("inf")
    for line in labels:
        center_x = (line.x0 + line.x1) / 2
        center_y = (line.y0 + line.y1) / 2
        size = max(1.0, line.size)
        if not box[0] - 2 <= center_x <= box[2] + 2:
            continue
        if not box[1] - 1.2 * size <= center_y <= box[1] + 1.8 * size:
            continue
        distance = abs(center_y - box[1])
        if distance < best_distance:
            best = line
            best_distance = distance
    return best


def _mentions_bogi_above(lines: Sequence[TextLine], box: Rect, size: float) -> bool:
    """라벨이 없어도 바로 위 발문이 <보기>를 언급하면 <보기> 상자로 본다.

    '[1~3] 다음 글과 <보기>를 읽고…' 같은 묶음 지시문 아래의 지문 테두리는 제외한다.
    """
    above = [
        line for line in lines
        if line.y1 <= box[1] + 2
        and line.y1 >= box[1] - 5 * size
        and line.x0 < box[2]
        and line.x1 > box[0]
        and not _contains(box, (line.x0 + line.x1) / 2, (line.y0 + line.y1) / 2)
    ]
    above.sort(key=lambda line: -line.y1)
    nearest = above[:3]
    if not any("보기" in line.compact for line in nearest):
        return False
    if any(_GROUP_START_RE.match(line.compact) or "물음에" in line.compact for line in nearest):
        return False
    return any(
        _QUESTION_START_RE.match(line.compact) or "?" in line.compact or "것은" in line.compact
        for line in nearest
    )


def _has_grid(box: Rect, prims: Primitives) -> bool:
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    horizontal: dict[int, list[HSeg]] = {}
    for seg in prims.hsegs:
        if y0 + 3 < seg.y < y1 - 3 and seg.x1 > x0 - 2 and seg.x0 < x1 + 2:
            horizontal.setdefault(round(seg.y), []).append(seg)
    vertical: dict[int, list[VSeg]] = {}
    for seg in prims.vsegs:
        if x0 + 3 < seg.x < x1 - 3 and seg.y1 > y0 - 2 and seg.y0 < y1 + 2:
            vertical.setdefault(round(seg.x), []).append(seg)
    h_lines = sum(_coverage(group, x0, x1) >= 0.6 * width for group in _merge_keys(horizontal).values())
    v_lines = sum(
        _vertical_coverage(group, y0, y1) >= 0.6 * height for group in _merge_keys(vertical).values()
    )
    cells = sum(
        1 for other in prims.boxes
        if other.bbox != box and _contains_rect(box, other.bbox, 1.5) and _area(other.bbox) < 0.8 * _area(box)
    )
    return (v_lines >= 1 and h_lines + v_lines >= 2) or h_lines >= 2 or cells >= 2


def _figure_like(box: Rect, prims: Primitives, inside: Sequence[Glyph], image_boxes: Sequence[Rect]) -> bool:
    text_area = sum(max(0.0, (glyph.x1 - glyph.x0) * (glyph.y1 - glyph.y0)) for glyph in inside)
    if len(inside) > 80 and text_area > 0.2 * _area(box):
        return False  # 글이 많은 테두리(지문 틀)는 안의 그림만 따로 잘라 낸다.
    shapes = [
        shape for shape in prims.shapes
        if _contains_rect(box, shape.bbox, 2)
        and _area(shape.bbox) < 0.9 * _area(box)
        and not _corner_decoration(box, shape.bbox)
    ]
    images = [rect for rect in (_rect(value) for value in image_boxes) if rect and _contains_rect(box, rect, 2)]
    return bool(images) or len(shapes) >= 3


def _corner_decoration(box: Rect, shape: Rect) -> bool:
    """둥근 모서리 상자의 모서리 곡선은 그림 요소가 아니다."""
    small = shape[2] - shape[0] <= 16 and shape[3] - shape[1] <= 16
    near_x = min(abs(shape[0] - box[0]), abs(shape[2] - box[2])) <= 3
    near_y = min(abs(shape[1] - box[1]), abs(shape[3] - box[3])) <= 3
    return small and near_x and near_y


def _unboxed_bogi(label: TextLine, lines: Sequence[TextLine]) -> Optional[Rect]:
    """상자 없이 '<보기>' 라벨만 있는 경우 다음 선택지·문항 전까지를 영역으로 본다."""
    following = sorted(
        (line for line in lines if line.column == label.column and line.y0 > label.y1 - 1),
        key=lambda line: line.y0,
    )
    selected: list[TextLine] = []
    previous = label
    limit = 25
    for line in following:
        compact = line.compact
        if _CHOICE_START_RE.match(compact) or _QUESTION_START_RE.match(compact) or _GROUP_START_RE.match(compact):
            break
        if line.baseline - previous.baseline > 2.6 * max(line.size, previous.size):
            break
        selected.append(line)
        previous = line
        if len(selected) >= limit:
            return None
    if not selected:
        return None
    rect = _line_rect(label)
    for line in selected:
        rect = _union(rect, _line_rect(line))
    return rect


def _shape_clusters(prims: Primitives, is_box_edge, glyphs: Sequence[Glyph], width: float) -> list[Rect]:
    underline = _UnderlineIndex(glyphs)
    items: list[Rect] = [shape.bbox for shape in prims.shapes]
    for seg in prims.hsegs:
        if not is_box_edge(seg) and seg.x1 - seg.x0 < 0.5 * width and not underline.matches(seg):
            items.append((seg.x0, seg.y - 0.5, seg.x1, seg.y + 0.5))
    for seg in prims.vsegs:
        if not is_box_edge(seg) and seg.y1 - seg.y0 < 300:
            items.append((seg.x - 0.5, seg.y0, seg.x + 0.5, seg.y1))
    if len(items) < 3:
        return []
    clusters: list[Rect] = []
    for component in _connected_components(items, 6.0):
        if len(component) < 3:
            continue
        rect = items[component[0]]
        for index in component[1:]:
            rect = _union(rect, items[index])
        if rect[2] - rect[0] < 24 or rect[3] - rect[1] < 18:
            continue
        inside = [glyph for glyph in glyphs if _contains(rect, glyph.cx, glyph.cy)]
        text_area = sum(max(0.0, (glyph.x1 - glyph.x0) * (glyph.y1 - glyph.y0)) for glyph in inside)
        if text_area > 0.3 * _area(rect):
            continue
        clusters.append(rect)
    return clusters


class _UnderlineIndex:
    """가로선이 어떤 글자의 밑줄 위치에 있는지 빠르게 확인한다."""

    def __init__(self, glyphs: Sequence[Glyph]) -> None:
        self.glyphs = sorted((glyph for glyph in glyphs if not glyph.char.isspace()), key=lambda glyph: glyph.oy)
        self.baselines = [glyph.oy for glyph in self.glyphs]

    def candidates(self, seg: HSeg) -> list[Glyph]:
        low = bisect_left(self.baselines, seg.y - 12.0)
        high = bisect_right(self.baselines, seg.y + 5.0)
        return [
            glyph for glyph in self.glyphs[low:high]
            if -0.12 * glyph.size <= seg.y - glyph.oy <= 0.35 * glyph.size
        ]

    def matches(self, seg: HSeg) -> bool:
        return any(glyph.x1 > seg.x0 and glyph.x0 < seg.x1 for glyph in self.candidates(seg))


def _box_edge_filter(boxes: Sequence[Box]):
    rects = [box.bbox for box in boxes]

    def is_edge(seg) -> bool:
        for x0, y0, x1, y1 in rects:
            if isinstance(seg, HSeg):
                if (abs(seg.y - y0) <= 1.5 or abs(seg.y - y1) <= 1.5) and seg.x0 >= x0 - 13 and seg.x1 <= x1 + 13:
                    return True
                if y0 + 1.5 < seg.y < y1 - 1.5 and seg.x0 >= x0 - 2 and seg.x1 <= x1 + 2 and seg.x1 - seg.x0 >= 0.6 * (x1 - x0):
                    return True  # 표 내부 격자선
            else:
                if (abs(seg.x - x0) <= 1.5 or abs(seg.x - x1) <= 1.5) and seg.y0 >= y0 - 13 and seg.y1 <= y1 + 13:
                    return True
                if x0 + 1.5 < seg.x < x1 - 1.5 and seg.y0 >= y0 - 2 and seg.y1 <= y1 + 2 and seg.y1 - seg.y0 >= 0.6 * (y1 - y0):
                    return True
        return False

    return is_edge


def _swallows_structure(region: RegionCandidate, lines: Sequence[TextLine]) -> bool:
    """문항 번호나 선택지 여러 개를 삼키는 영역은 잘못 잡은 것이므로 버린다."""
    choices = 0
    for line in lines:
        if not _contains(region.bbox, (line.x0 + line.x1) / 2, (line.y0 + line.y1) / 2):
            continue
        compact = line.compact
        if _QUESTION_START_RE.match(compact) and len(compact) >= 8 and "?" in compact:
            return True
        if _GROUP_START_RE.match(compact):
            return True
        if _CHOICE_START_RE.match(compact):
            choices += 1
    return choices >= 3


def _in_page_margin(rect: Rect, height: float) -> bool:
    """머리말·꼬리말 띠 안의 장식(제목 틀, 쪽 번호 상자)은 문제 그림이 아니다."""
    return rect[3] <= 0.1 * height or rect[1] >= 0.92 * height


def _merge_candidates(candidates: list[RegionCandidate]) -> list[RegionCandidate]:
    items = sorted(candidates, key=lambda region: -_area(region.bbox))
    changed = True
    while changed:
        changed = False
        output: list[RegionCandidate] = []
        for region in items:
            merged = False
            for index, existing in enumerate(output):
                inter = _intersection_area(region.bbox, existing.bbox)
                smaller = min(_area(region.bbox), _area(existing.bbox))
                if smaller <= 0:
                    continue
                if inter >= 0.85 * _area(region.bbox):
                    # 작은 후보가 큰 후보 안에 있어도 합집합을 써야 <보기> 라벨처럼
                    # 한쪽에만 포함된 부분이 잘리지 않는다.
                    kind = region.kind if _PRIORITY[region.kind] > _PRIORITY[existing.kind] else existing.kind
                    union = _union(region.bbox, existing.bbox)
                    if union != existing.bbox:
                        changed = True  # 커진 영역이 다른 후보와 새로 겹칠 수 있다.
                    output[index] = RegionCandidate(kind, union)
                    merged = True
                    break
                if inter > 0.3 * smaller:
                    kind = region.kind if _PRIORITY[region.kind] > _PRIORITY[existing.kind] else existing.kind
                    output[index] = RegionCandidate(kind, _union(region.bbox, existing.bbox))
                    merged = True
                    changed = True
                    break
            if not merged:
                output.append(region)
        items = sorted(output, key=lambda region: -_area(region.bbox))
    return items


# ---------------------------------------------------------------------------
# 기하 유틸


def _point(value) -> tuple[float, float]:
    try:
        if hasattr(value, "x") and hasattr(value, "y"):
            return float(value.x), float(value.y)
        return float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError):
        return (float("nan"), float("nan"))


def _rect(value) -> Optional[Rect]:
    try:
        if hasattr(value, "x0"):
            numbers = (float(value.x0), float(value.y0), float(value.x1), float(value.y1))
        else:
            items = list(value)
            if len(items) != 4:
                return None
            numbers = tuple(float(item) for item in items)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(number) for number in numbers):
        return None
    x0, y0, x1, y1 = numbers
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _quad_points(value) -> list[tuple[float, float]]:
    try:
        if hasattr(value, "ul"):
            points = [value.ul, value.ur, value.ll, value.lr]
        else:
            points = list(value)[:4]
        result = [_point(point) for point in points]
    except (TypeError, ValueError):
        return []
    return result if len(result) == 4 and all(math.isfinite(x) and math.isfinite(y) for x, y in result) else []


def _axis_aligned(points: list[tuple[float, float]]) -> bool:
    xs = sorted(x for x, _y in points)
    ys = sorted(y for _x, y in points)
    return abs(xs[0] - xs[1]) <= 0.5 and abs(xs[2] - xs[3]) <= 0.5 and abs(ys[0] - ys[1]) <= 0.5 and abs(ys[2] - ys[3]) <= 0.5


def _is_white(color) -> bool:
    try:
        values = [float(value) for value in color]
    except (TypeError, ValueError):
        return False
    return bool(values) and all(value >= 0.97 for value in values[:3])


def _coverage(segments: Sequence[HSeg], start: float, end: float) -> float:
    intervals = sorted((max(start, seg.x0), min(end, seg.x1)) for seg in segments)
    return _interval_length(intervals)


def _vertical_coverage(segments: Sequence[VSeg], start: float, end: float) -> float:
    intervals = sorted((max(start, seg.y0), min(end, seg.y1)) for seg in segments)
    return _interval_length(intervals)


def _interval_length(intervals: list[tuple[float, float]]) -> float:
    total = 0.0
    current_start: Optional[float] = None
    current_end = 0.0
    for start, end in intervals:
        if end <= start:
            continue
        if current_start is None or start > current_end:
            if current_start is not None:
                total += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_start is not None:
        total += current_end - current_start
    return total


def _merge_keys(groups: dict[int, list]) -> dict[int, list]:
    """1pt 차이로 나뉜 같은 선 묶음을 합친다."""
    merged: dict[int, list] = {}
    for key in sorted(groups):
        target = next((existing for existing in merged if abs(existing - key) <= 1), None)
        if target is None:
            merged[key] = list(groups[key])
        else:
            merged[target].extend(groups[key])
    return merged


def _line_rect(line: TextLine) -> Rect:
    return (line.x0, line.y0, line.x1, line.y1)


def _contains(rect: Rect, x: float, y: float) -> bool:
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


def _contains_rect(outer: Rect, inner: Rect, tolerance: float = 0.0) -> bool:
    return (
        inner[0] >= outer[0] - tolerance and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance and inner[3] <= outer[3] + tolerance
    )


def _touching(first: Rect, second: Rect, gap: float) -> bool:
    return not (
        first[2] + gap < second[0] or second[2] + gap < first[0]
        or first[3] + gap < second[1] or second[3] + gap < first[1]
    )


def _union(first: Rect, second: Rect) -> Rect:
    return (min(first[0], second[0]), min(first[1], second[1]), max(first[2], second[2]), max(first[3], second[3]))


def _pad(rect: Rect, padding: float, width: float, height: float) -> Rect:
    return (
        max(0.0, rect[0] - padding),
        max(0.0, rect[1] - padding),
        min(width, rect[2] + padding),
        min(height, rect[3] + padding),
    )


def _area(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _intersection_area(first: Rect, second: Rect) -> float:
    width = min(first[2], second[2]) - max(first[0], second[0])
    height = min(first[3], second[3]) - max(first[1], second[1])
    return max(0.0, width) * max(0.0, height)


def _iou(first: Rect, second: Rect) -> float:
    inter = _intersection_area(first, second)
    union = _area(first) + _area(second) - inter
    return inter / union if union > 0 else 0.0


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
