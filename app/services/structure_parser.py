"""지문 뒤에 이어지는 오지선다 문제를 1:N 구조로 복원한다.

PDF 텍스트/OCR 결과는 선택지들이 한 줄에 붙거나, 문항 번호와 선택지 기호가
다르게 인식될 수 있다. 이 파서는 다음 순서로 보수적으로 구조를 찾는다.

* '[1~3] 다음 글을 읽고 물음에 답하시오' 같은 지문 묶음 경계를 우선 사용
* 줄 시작의 문항 번호 후보를 찾은 뒤 선택지 ①~⑤가 실제로 이어질 때만 문항으로 인정
* ➀/❶ 및 (1)~(5), 1)~5) OCR 변형도 순서가 완전할 때 선택지로 인정
* 확실하지 않은 내용은 버리지 않고 경고와 함께 지문/문항에 남김

AI가 없어도 흔한 시험지 형식은 분석할 수 있고, 결과의 confidence와 warnings는
상위 파이프라인이 Gemini 보정 필요성을 판단하는 데 사용한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..models import Passage, Question

if TYPE_CHECKING:
    from .pdf_parser import PageText

CIRCLED = "①②③④⑤"
CHOICE_PREFIX = {index + 1: char for index, char in enumerate(CIRCLED)}

_GROUP_RANGE_RE = re.compile(r"[\[【(（]\s*\d{1,3}\s*[~～\-–—]\s*\d{1,3}\s*[\]】)）]")
_GROUP_INSTRUCTION_RE = re.compile(
    r"(?:다음|아래(?:의)?)\s*(?:글|지문|자료)(?:을|를)?\s*"
    r"(?:읽고|보고|참고하여|바탕으로).{0,35}(?:물음|문제).{0,20}(?:답|풀)",
    re.IGNORECASE,
)
_ENGLISH_INSTRUCTION_RE = re.compile(
    r"(?:read|refer to)\s+(?:the\s+)?(?:following\s+)?(?:passage|text).{0,50}questions?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SourceLine:
    text: str
    page_number: int


@dataclass
class StructureResult:
    passages: list[Passage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confidence: float = 0.0
    question_candidates: int = 0
    complete_questions: int = 0


@dataclass
class _ParsedQuestion:
    question: Question
    complete: bool
    warning: str = ""


@dataclass
class _Section:
    lines: list[SourceLine]
    label: str = ""

    @property
    def pages(self) -> list[int]:
        return sorted({line.page_number for line in self.lines if line.text.strip()})


@dataclass(frozen=True)
class _ChoiceToken:
    number: int
    start: int
    end: int


def parse_page_structure(pages: list["PageText"], source_file: str = "") -> StructureResult:
    per_page: list[list[SourceLine]] = [
        [SourceLine(line.rstrip(), page.page_number) for line in (page.text.splitlines() if page.text else [])]
        for page in pages
    ]
    # 레이아웃 엔진이 '다음 페이지로 이어지는 문단'이라고 판단한 경우 두 줄을 하나로 합친다.
    for index in range(len(pages) - 1):
        if not getattr(pages[index], "join_next", False):
            continue
        current = per_page[index]
        following = per_page[index + 1]
        last = next((i for i in range(len(current) - 1, -1, -1) if current[i].text.strip()), None)
        first = next((i for i, line in enumerate(following) if line.text.strip()), None)
        if last is None or first is None:
            continue
        if _is_placeholder_line(current[last].text) or _is_placeholder_line(following[first].text):
            continue
        joiner = " " if getattr(pages[index], "join_space", False) else ""
        merged = current[last].text.rstrip() + joiner + following[first].text.lstrip()
        current[last] = SourceLine(merged, current[last].page_number)
        del following[first]

    lines: list[SourceLine] = []
    for index, page in enumerate(pages):
        lines.extend(per_page[index])
        if getattr(page, "join_next", False) and index + 1 < len(pages):
            continue
        # 페이지 경계를 빈 줄 두 개로 보존하면 지문/선택지 문단 분리에 도움이 된다.
        lines.extend((SourceLine("", page.page_number), SourceLine("", page.page_number)))
    return parse_source_lines(lines, source_file=source_file)


_PLACEHOLDER_RE = re.compile("\ue000[A-Za-z0-9_-]{1,40}\ue001")


def _is_placeholder_line(text: str) -> bool:
    return bool(text.strip()) and not _PLACEHOLDER_RE.sub("", text).strip()


def _content_length(text: str) -> int:
    """자리표시자(그림 위치)는 본문 길이로 세지 않는다."""
    return len(re.sub(r"\s+", "", _PLACEHOLDER_RE.sub("", text)))


def parse_text_structure(text: str, source_file: str = "", page_number: int = 1) -> StructureResult:
    """테스트·붙여넣기 분석용 편의 API."""
    lines = [SourceLine(line.rstrip(), page_number) for line in text.replace("\r", "").split("\n")]
    return parse_source_lines(lines, source_file=source_file)


def parse_source_lines(lines: list[SourceLine], source_file: str = "") -> StructureResult:
    cleaned = _trim_blank_lines(lines)
    if not cleaned:
        return StructureResult(warnings=["분석할 텍스트가 없습니다."])

    sections, preamble_lines = _split_sections(cleaned)
    warnings: list[str] = []
    if preamble_lines and len(_join_lines(preamble_lines).strip()) >= 80:
        warnings.append("첫 지문 앞의 표지/안내로 보이는 텍스트는 분석에서 제외했습니다.")

    passages: list[Passage] = []
    candidates = 0
    complete = 0
    for section_number, section in enumerate(sections, start=1):
        passage, section_warnings, section_candidates, section_complete = _parse_section(
            section,
            source_file=source_file,
            section_number=section_number,
        )
        warnings.extend(section_warnings)
        candidates += section_candidates
        complete += section_complete
        if passage:
            passages.append(passage)

    if not passages:
        warnings.append("지문과 오지선다 문제 묶음을 찾지 못했습니다.")
        confidence = 0.0
    else:
        question_count = sum(len(p.questions) for p in passages)
        completion_ratio = complete / question_count if question_count else 0.0
        passage_ratio = sum(bool(p.text.strip()) for p in passages) / len(passages)
        candidate_precision = complete / candidates if candidates else 0.0
        confidence = min(
            1.0,
            0.15
            + 0.45 * completion_ratio
            + 0.25 * passage_ratio
            + 0.15 * candidate_precision,
        )
        if question_count == 0:
            confidence = min(confidence, 0.35)

    return StructureResult(
        passages=passages,
        warnings=list(dict.fromkeys(warnings)),
        confidence=round(confidence, 3),
        question_candidates=candidates,
        complete_questions=complete,
    )


# ---------------------------------------------------------------------------
# 문서 → 지문 섹션

def _split_sections(lines: list[SourceLine]) -> tuple[list[_Section], list[SourceLine]]:
    header_indices = [index for index, line in enumerate(lines) if _is_group_header(line.text)]
    if not header_indices:
        boundaries = _implicit_section_boundaries(lines)
        if not boundaries:
            return [_Section(_trim_blank_lines(lines))], []
        starts = [0, *boundaries]
        sections = [
            _Section(_trim_blank_lines(lines[start:end]), label="__implicit__")
            for start, end in zip(starts, [*boundaries, len(lines)], strict=True)
            if _trim_blank_lines(lines[start:end])
        ]
        return sections, []

    preamble = _trim_blank_lines(lines[: header_indices[0]])
    preamble_sections: list[_Section] = []
    if _contains_question_evidence(preamble):
        boundaries = _implicit_section_boundaries(preamble)
        starts = [0, *boundaries]
        preamble_sections = [
            _Section(_trim_blank_lines(preamble[start:end]), label="__implicit__")
            for start, end in zip(starts, [*boundaries, len(preamble)], strict=True)
            if _trim_blank_lines(preamble[start:end])
        ]
        preamble = []
    sections: list[_Section] = []
    for position, header_index in enumerate(header_indices):
        end = header_indices[position + 1] if position + 1 < len(header_indices) else len(lines)
        header = lines[header_index].text.strip()
        body = _trim_blank_lines(lines[header_index + 1 : end])
        if body:
            sections.append(_Section(body, label=header))
    return [*preamble_sections, *sections], preamble


def _contains_question_evidence(lines: list[SourceLine]) -> bool:
    if not lines:
        return False
    numeric_choices = _numeric_choice_line_indices(lines)
    candidates = [
        (index, match)
        for index, line in enumerate(lines)
        if index not in numeric_choices and (match := _match_question_start(line.text))
    ]
    for position, (start, match) in enumerate(candidates):
        end = candidates[position + 1][0] if position + 1 < len(candidates) else len(lines)
        token_count, complete = _choice_evidence(_question_body(lines[start:end], match[1]))
        if complete or token_count >= 3:
            return True
    return False


def _is_group_header(text: str) -> bool:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact or len(compact) > 140:
        return False
    if _GROUP_INSTRUCTION_RE.search(compact) or _ENGLISH_INSTRUCTION_RE.search(compact):
        return True
    # '[1~3]'만 한 줄에 있고 바로 다음 줄부터 지문이 시작되는 형식
    return bool(_GROUP_RANGE_RE.fullmatch(compact))


def _implicit_section_boundaries(lines: list[SourceLine]) -> list[int]:
    """명시적 범위 머리말이 없을 때 ⑤ 뒤 문단/페이지 전환을 새 지문 경계로 추정."""
    numeric_choices = _numeric_choice_line_indices(lines)
    candidates = [
        index for index, line in enumerate(lines)
        if index not in numeric_choices and _match_question_start(line.text)
    ]
    boundaries: list[int] = []
    for position, start in enumerate(candidates[:-1]):
        end = candidates[position + 1]
        match = _match_question_start(lines[start].text)
        if match is None:
            continue
        body = _question_body(lines[start:end], match[1])
        token_count, complete = _choice_evidence(body)
        if not complete and token_count < 3:
            continue
        fifth_line = _last_fifth_choice_line(lines, start, end)
        if fifth_line is None:
            continue

        # 빈 줄 뒤에 충분한 본문이 있고 그 다음 문항이 시작되면 가장 강한 경계 신호다.
        # 새 지문이 그림으로 시작하면(자리표시자 줄) 그림부터 새 지문에 포함한다.
        saw_blank = False
        figure_start: Optional[int] = None
        for index in range(fifth_line + 1, end):
            if not lines[index].text.strip():
                saw_blank = True
                continue
            if saw_blank:
                if _is_placeholder_line(lines[index].text):
                    if figure_start is None:
                        figure_start = index
                    continue
                trailing = _join_lines(_trim_blank_lines(lines[index:end])).strip()
                if _content_length(trailing) >= 5:
                    boundaries.append(figure_start if figure_start is not None else index)
                break

        if boundaries and boundaries[-1] > fifth_line:
            continue

        # 빈 줄이 사라진 OCR 결과에서는 새 페이지의 긴 본문만 보수적으로 경계로 본다.
        fifth_page = lines[fifth_line].page_number
        figure_start = None
        for index in range(fifth_line + 1, end):
            if lines[index].page_number == fifth_page or not lines[index].text.strip():
                continue
            if _is_placeholder_line(lines[index].text):
                if figure_start is None:
                    figure_start = index
                continue
            trailing = _join_lines(_trim_blank_lines(lines[index:end])).strip()
            if _content_length(trailing) >= 5:
                boundaries.append(figure_start if figure_start is not None else index)
            break
    return sorted(set(boundaries))


def _last_fifth_choice_line(lines: list[SourceLine], start: int, end: int) -> Optional[int]:
    marker = re.compile(r"⑤|➄|❺|(?:^|\s)5\s*[.)](?=\s|$)")
    found = [index for index in range(start, end) if marker.search(lines[index].text)]
    return found[-1] if found else None


# ---------------------------------------------------------------------------
# 지문 섹션 → 문항

def _parse_section(
    section: _Section,
    *,
    source_file: str,
    section_number: int,
) -> tuple[Optional[Passage], list[str], int, int]:
    warnings: list[str] = []
    if section.label == "__implicit__":
        warnings.append(f"{section_number}번째 지문 경계를 ⑤ 뒤 문단/페이지 전환으로 추정했습니다.")
    numeric_choice_lines = _numeric_choice_line_indices(section.lines)
    candidate_positions: list[tuple[int, str, str]] = []
    for index, line in enumerate(section.lines):
        if index in numeric_choice_lines:
            continue
        match = _match_question_start(line.text)
        if match:
            candidate_positions.append((index, match[0], match[1]))

    # 1차로 다음 '모든 번호 후보' 전까지 선택지를 확인해 실제 문항 시작을 가린다.
    valid_starts: list[tuple[int, str, str]] = []
    partial_starts: list[tuple[int, str, str]] = []
    for position, candidate in enumerate(candidate_positions):
        start = candidate[0]
        end = candidate_positions[position + 1][0] if position + 1 < len(candidate_positions) else len(section.lines)
        body = _question_body(section.lines[start:end], first_remainder=candidate[2])
        token_count, has_complete = _choice_evidence(body)
        if has_complete:
            valid_starts.append(candidate)
        elif token_count >= 3:
            partial_starts.append(candidate)

    # 완전/부분 문항을 원래 순서대로 모두 보존한다. 완전 문항이 하나 있다고
    # 부분 문항을 버리면 그 내용이 앞 문항 ⑤에 합쳐지는 데이터 손상이 생긴다.
    starts_by_index = {item[0]: item for item in [*valid_starts, *partial_starts]}
    starts = [starts_by_index[index] for index in sorted(starts_by_index)]
    candidate_count = len(candidate_positions)
    if not starts:
        text = _clean_passage_text(_join_lines(section.lines))
        if not text:
            return None, warnings, candidate_count, 0
        warnings.append(f"{section_number}번째 지문에서 오지선다 문항을 찾지 못했습니다.")
        return (
            Passage(
                title=_source_title(source_file, section_number),
                text=text,
                source_file=source_file,
                source_pages=section.pages,
            ),
            warnings,
            candidate_count,
            0,
        )

    first_start = starts[0][0]
    passage_lines = _trim_blank_lines(section.lines[:first_start])
    passage_text = _clean_passage_text(_join_lines(passage_lines))
    if not passage_text:
        warnings.append(f"{section_number}번째 묶음의 지문 본문이 비어 있습니다.")

    questions: list[Question] = []
    complete_count = 0
    for position, (start, number, first_remainder) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(section.lines)
        parsed = _parse_question(
            section.lines[start:end],
            number=number,
            first_remainder=first_remainder,
        )
        if not parsed:
            continue
        questions.append(parsed.question)
        complete_count += int(parsed.complete)
        if parsed.warning:
            warnings.append(f"{parsed.question.number or len(questions)}번: {parsed.warning}")

    if not questions:
        warnings.append(f"{section_number}번째 지문에서 문항 구조 복원에 실패했습니다.")

    passage = Passage(
        title=_source_title(source_file, section_number),
        text=passage_text,
        source_file=source_file,
        source_pages=section.pages,
        questions=questions,
    )
    return passage, warnings, candidate_count, complete_count


def _match_question_start(text: str) -> Optional[tuple[str, str]]:
    line = text.strip()
    if not line or _GROUP_RANGE_RE.match(line):
        return None
    patterns = (
        r"^\[\s*(\d{1,3})\s*\]\s*(.*)$",
        r"^(?:문제|문항|question|q)\s*(\d{1,3})\s*(?:번|[.)])?\s+(.*)$",
        r"^(\d{1,3})\s*(?:[.)]|번(?:\s|$))\s*(.*)$",
    )
    for pattern in patterns:
        match = re.match(pattern, line, re.IGNORECASE)
        if match:
            return match.group(1), match.group(2).strip()
    return None


def _numeric_choice_line_indices(lines: list[SourceLine]) -> set[int]:
    """문항 번호처럼 보이는 줄 단위 1.～5. 선택지를 문항 후보에서 제외한다."""
    nonblank = [index for index, line in enumerate(lines) if line.text.strip()]
    result: set[int] = set()

    for offset, index in enumerate(nonblank):
        match = re.match(r"^\s*1\s*[.)]\s+\S", lines[index].text)
        if not match or offset == 0:
            continue
        # PDF 줄바꿈으로 발문이 여러 줄이어도 앞쪽의 실제 문항 시작까지 되짚는다.
        has_parent_question = False
        for previous_index in reversed(nonblank[max(0, offset - 12) : offset]):
            if _match_question_start(lines[previous_index].text) is not None:
                has_parent_question = True
                break
            if re.search(r"[①②③④⑤]", lines[previous_index].text):
                break
        if not has_parent_question:
            continue

        # 한 줄에 1.～5.가 모두 있는 형식
        if _find_complete_sequence(_choice_tokens(lines[index].text)) is not None:
            result.add(index)
            continue

        # 다섯 줄에 1.～5.가 순서대로 있는 형식
        sequence: list[int] = []
        for expected, candidate_index in enumerate(nonblank[offset : offset + 5], start=1):
            candidate = re.match(r"^\s*([1-5])\s*[.)]\s+\S", lines[candidate_index].text)
            if not candidate or int(candidate.group(1)) != expected:
                sequence = []
                break
            sequence.append(candidate_index)
        if len(sequence) == 5:
            result.update(sequence)
    return result


def _question_body(lines: list[SourceLine], first_remainder: str) -> str:
    following = [line.text for line in lines[1:]]
    parts = [first_remainder, *following]
    return "\n".join(parts).strip()


def _choice_evidence(text: str) -> tuple[int, bool]:
    tokens = _choice_tokens(text)
    numbers = [token.number for token in tokens]
    return len(set(numbers)), _find_complete_sequence(tokens) is not None


def _parse_question(
    lines: list[SourceLine],
    *,
    number: str,
    first_remainder: str,
) -> Optional[_ParsedQuestion]:
    body = _question_body(lines, first_remainder)
    tokens = _choice_tokens(body)
    sequence = _find_complete_sequence(tokens)
    complete = sequence is not None

    if sequence is None:
        # OCR 누락 시에도 3개 이상이면 확인·수정 가능한 부분 결과로 보존한다.
        sequence = _best_partial_sequence(tokens)
    if not sequence:
        return None

    first = sequence[0]
    stem = _clean_fragment(body[: first.start])
    choices: list[str] = []
    for index, token in enumerate(sequence):
        next_start = sequence[index + 1].start if index + 1 < len(sequence) else len(body)
        content = _clean_fragment(body[token.end : next_start])
        choices.append(f"{CHOICE_PREFIX[token.number]} {content}".rstrip())

    warning = ""
    if not complete:
        found = ", ".join(CHOICE_PREFIX[token.number] for token in sequence)
        warning = f"선택지 5개 중 일부만 인식했습니다({found}). 원문 확인이 필요합니다."
    elif any(not _choice_content(choice) for choice in choices):
        warning = "내용이 비어 있는 선택지가 있어 원문 확인이 필요합니다."
        complete = False

    question = Question(number=number, stem=stem, choices=choices)
    return _ParsedQuestion(question=question, complete=complete, warning=warning)


# ---------------------------------------------------------------------------
# 선택지 토큰화

def _choice_tokens(text: str) -> list[_ChoiceToken]:
    # parse_text_structure로 직접 들어온 OCR 텍스트도 동일하게 처리한다.
    normalized = text.translate(str.maketrans("➀➁➂➃➄❶❷❸❹❺", "①②③④⑤①②③④⑤"))
    circled = [
        _ChoiceToken(CIRCLED.index(match.group(1)) + 1, match.start(), match.end())
        for match in re.finditer(r"([①②③④⑤])", normalized)
    ]
    if _find_complete_sequence(circled) is not None:
        return circled

    # 괄호형/숫자형은 문장 속 숫자와 혼동될 수 있으므로 완전한 1→5 순서가 있을 때 우선 사용한다.
    numeric: list[_ChoiceToken] = []
    pattern = re.compile(
        r"(?:[（(\[]\s*([1-5])\s*[）)\]]|(?:^|(?<=\s))([1-5])\s*[.)](?=\s|$))",
        re.MULTILINE,
    )
    for match in pattern.finditer(text):
        value = match.group(1) or match.group(2)
        numeric.append(_ChoiceToken(int(value), match.start(), match.end()))
    if _find_complete_sequence(numeric) is not None:
        return numeric
    # 완전하지 않은 경우 원문자 쪽이 더 신뢰할 만하다.
    return circled if len(circled) >= len(numeric) else numeric


def _find_complete_sequence(tokens: list[_ChoiceToken]) -> Optional[list[_ChoiceToken]]:
    for start, token in enumerate(tokens):
        if token.number != 1:
            continue
        expected = 1
        sequence: list[_ChoiceToken] = []
        for current in tokens[start:]:
            if current.number == expected:
                sequence.append(current)
                expected += 1
                if expected == 6:
                    return sequence
            elif current.number == 1:
                sequence = [current]
                expected = 2
            elif current.number < expected:
                continue
            else:
                break
    return None


def _best_partial_sequence(tokens: list[_ChoiceToken]) -> list[_ChoiceToken]:
    best: list[_ChoiceToken] = []
    current: list[_ChoiceToken] = []
    last = 0
    for token in tokens:
        if token.number == 1:
            current = [token]
            last = 1
        elif current and token.number > last and token.number <= 5:
            current.append(token)
            last = token.number
        if len(current) > len(best):
            best = list(current)
    return best if len(best) >= 3 else []


# ---------------------------------------------------------------------------
# 정리 유틸

def _clean_passage_text(text: str) -> str:
    lines = text.splitlines()
    while lines and (_is_group_header(lines[0]) or _GROUP_RANGE_RE.fullmatch(lines[0].strip())):
        lines.pop(0)
    return _clean_fragment("\n".join(lines))


def _clean_fragment(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    output: list[str] = []
    blank = False
    for line in lines:
        if line:
            output.append(line)
            blank = False
        elif output and not blank:
            output.append("")
            blank = True
    return "\n".join(output).strip()


def _choice_content(choice: str) -> str:
    return re.sub(r"^[①②③④⑤]\s*", "", choice).strip()


def _join_lines(lines: list[SourceLine]) -> str:
    return "\n".join(line.text for line in lines)


def _trim_blank_lines(lines: list[SourceLine]) -> list[SourceLine]:
    start = 0
    end = len(lines)
    while start < end and not lines[start].text.strip():
        start += 1
    while end > start and not lines[end - 1].text.strip():
        end -= 1
    return lines[start:end]


def _source_title(source_file: str, section_number: int) -> str:
    if not source_file:
        return f"지문 {section_number}"
    stem = Path(source_file).stem
    return stem if section_number == 1 else f"{stem} - 지문 {section_number}"
