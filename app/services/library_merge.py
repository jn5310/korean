"""같은 PDF를 다시 분석했을 때 기존 지문을 새 결과로 교체한다.

재분석 결과는 띄어쓰기·중복 글자·<보기> 그림이 개선되어 평문이 달라지므로, 기존
'비슷하면 중복' 규칙으로는 새 결과가 버려진다. 사용자가 교체를 선택하면 이 모듈이
기존 지문을 새 지문으로 바꾸고, 시험지 구성과 사용자가 입력한 정답·해설·수동 난이도·
직접 추가한 그림을 같은 문항으로 옮긴다. Qt에 의존하지 않는다.

사용자 데이터 보호 원칙
-----------------------
* 같은 원본은 파일 해시가 같을 때만 인정한다(해시가 없던 옛 지문만 경로로 판단).
* 옛 문항과 새 문항은 1:1로만 대응시키며, 번호가 같고 **선택지 내용이 일치**해야 같은
  문항으로 본다(발문은 시험지마다 비슷한 문구가 많아 단독 근거로 쓰지 않는다).
  이전 버전의 겹친 번호('111')는 그 지문의 모든 번호가 겹친 번호일 때만 '1'로 본다.
* 입력한 내용을 확실하게 옮기지 못한 옛 지문, 편집 화면에서 직접 고친 옛 지문은 지우지 않고
  '[이전 분석]' 제목으로 보관한다. 새 결과와 전혀 대응하지 않는 옛 지문도 입력한 내용이
  있거나 시험지에 담겨 있거나 수정 여부를 알 수 없으면(이전 버전 기록) 그대로 둔다.
* 시험지는 옛 지문의 문항이 옮겨 간 모든 새 지문으로 문항 순서대로 연결한다.
* plan_replacements()로 같은 계산을 사본에서 미리 해 볼 수 있다.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Optional

from ..models import Library, MediaAsset, Passage, Question

SALVAGE_PREFIX = "[이전 분석] "
_CHOICE_MARK_RE = re.compile(r"^\s*(?:[①②③④⑤]+|[（(]\s*[1-5]\s*[）)]|[1-5]\s*[.)])\s*")


@dataclass
class ReanalysisGroup:
    source_file: str
    new_passages: list[Passage]
    old_passages: list[Passage]


@dataclass
class ReplaceSummary:
    removed: int = 0
    added: int = 0
    remapped_refs: int = 0
    dropped_refs: int = 0
    carried_items: int = 0
    kept_titles: list[str] = field(default_factory=list)      # 새 결과와 대응하지 않아 그대로 둔 지문
    salvaged_titles: list[str] = field(default_factory=list)  # 옮기지 못한 입력·직접 수정이 있어 보관한 지문


def source_key(path: str) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).expanduser().resolve()).casefold()
    except OSError:
        return str(Path(path).expanduser().absolute()).casefold()


def find_reanalysis_groups(library: Library, results: Iterable) -> list[ReanalysisGroup]:
    """새 분석 결과마다 같은 원본에서 나온 기존 지문을 찾는다.

    파일 해시가 같으면 같은 원본이다. 해시가 서로 다르면 같은 경로라도 다른 파일(예:
    같은 이름으로 다시 내려받은 다른 시험지)로 본다. 해시가 없는 옛 지문만 경로로 비교한다.
    """
    results = list(results)
    groups: list[ReanalysisGroup] = []
    claimed: set[str] = set()
    fresh_ids = {passage.id for result in results for passage in getattr(result, "passages", [])}
    old_keys: dict[str, str] = {}
    for result in results:
        passages = list(getattr(result, "passages", []) or [])
        if not passages:
            continue
        digests = {passage.source_digest for passage in passages if passage.source_digest}
        path = source_key(getattr(result, "source_file", ""))
        old: list[Passage] = []
        for passage in library.passages:
            if passage.id in claimed or passage.id in fresh_ids or passage.title.startswith(SALVAGE_PREFIX):
                continue
            if passage.source_digest:
                if passage.source_digest in digests:
                    old.append(passage)
                continue
            if not path or not passage.source_file:
                continue
            key = old_keys.get(passage.id)
            if key is None:
                key = source_key(passage.source_file)
                old_keys[passage.id] = key
            if key == path:
                old.append(passage)
        if not old:
            continue
        claimed.update(passage.id for passage in old)
        groups.append(ReanalysisGroup(str(getattr(result, "source_file", "")), passages, old))
    return groups


def plan_replacements(library: Library, groups: list[ReanalysisGroup]) -> list[ReplaceSummary]:
    """실제 라이브러리를 바꾸지 않고 교체 결과(보관·유지될 지문 등)를 미리 계산한다."""
    trial = copy.deepcopy(library)
    by_id = {passage.id: passage for passage in trial.passages}
    summaries: list[ReplaceSummary] = []
    for group in groups:
        old = [by_id[passage.id] for passage in group.old_passages if passage.id in by_id]
        summaries.append(replace_passages(trial, old, copy.deepcopy(group.new_passages)))
    return summaries


def replace_passages(library: Library, old_passages: list[Passage], new_passages: list[Passage]) -> ReplaceSummary:
    summary = ReplaceSummary()
    mapping = match_passages(old_passages, new_passages)
    related = _RelatedIndex(new_passages)
    counterparts = match_questions(old_passages, new_passages, mapping, related)
    owners = {question.id: passage for passage in new_passages for question in passage.questions}
    referenced = {passage_id for exam in library.exam_sets for passage_id in exam.passage_ids}

    uncarried: set[str] = set()
    claimed_titles: dict[str, str] = {}
    claimed_difficulty: dict[str, int] = {}
    for old in old_passages:
        target = mapping.get(old.id)
        if target is None:
            continue
        carried, conflict = carry_passage_data(old, target, claimed_titles, claimed_difficulty)
        summary.carried_items += carried
        if conflict:
            uncarried.add(old.id)

    for old in old_passages:
        collapse = _all_repeated_digits(old)
        for question in old.questions:
            target = counterparts.get(question.id)
            has_data = question_has_user_data(question)
            if target is None:
                if has_data and not _equivalent_copy_exists(question, related.questions(old), collapse):
                    uncarried.add(old.id)
                continue
            if question.question_type:
                target.question_type = question.question_type  # 사용자가 고쳤을 수 있는 기존 유형 우선
            if not has_data:
                continue
            carried, conflict = _copy_question_data(question, target)
            summary.carried_items += carried
            if conflict:
                uncarried.add(old.id)
            if carried:
                owners[target.id].touch()

    # 옛 지문 → 새 지문 목록(문항이 옮겨 간 순서대로)
    targets: dict[str, list[Passage]] = {}
    for old in old_passages:
        ordered: list[Passage] = []
        for question in old.questions:
            target = counterparts.get(question.id)
            if target is not None and owners[target.id] not in ordered:
                ordered.append(owners[target.id])
        primary = mapping.get(old.id)
        if primary is not None and primary not in ordered:
            ordered.insert(0, primary)
        if ordered:
            targets[old.id] = ordered

    keep_ids: set[str] = set()
    for old in old_passages:
        if old.id not in targets:
            if old.id in referenced or has_user_data(old):
                keep_ids.add(old.id)
                summary.kept_titles.append(old.display_title)
            continue
        if old.id in uncarried or old.edited is True:
            keep_ids.add(old.id)
            summary.salvaged_titles.append(old.display_title)
            if not old.title.startswith(SALVAGE_PREFIX):
                old.title = SALVAGE_PREFIX + old.display_title
            old.touch()

    old_ids = {passage.id for passage in old_passages}
    remove_ids = old_ids - keep_ids
    first_index = next(
        (index for index, passage in enumerate(library.passages) if passage.id in old_ids),
        len(library.passages),
    )
    before = [passage for passage in library.passages[:first_index] if passage.id not in remove_ids]
    after = [passage for passage in library.passages[first_index:] if passage.id not in remove_ids]
    summary.removed = len(library.passages) - len(before) - len(after)
    library.passages = before + list(new_passages) + after
    summary.added = len(new_passages)

    for exam in library.exam_sets:
        updated: list[str] = []
        for passage_id in exam.passage_ids:
            if passage_id in targets:
                # 개선된 새 지문으로 연결(보관본은 라이브러리에만 남긴다).
                for target in targets[passage_id]:
                    if target.id not in updated:
                        updated.append(target.id)
                summary.remapped_refs += 1
                continue
            if passage_id in remove_ids:
                summary.dropped_refs += 1
                continue
            if passage_id not in updated:
                updated.append(passage_id)
        exam.passage_ids = updated
    return summary


# ---------------------------------------------------------------------------
# 대응 찾기


def match_passages(old_passages: list[Passage], new_passages: list[Passage]) -> dict[str, Passage]:
    """기존 지문 → 새 지문 대응(지문 단위 정보와 시험지 연결의 기준).

    문항 번호가 겹치고 쪽 번호나 내용이 조금이라도 맞는 지문을 우선하고, 없으면 본문
    유사도·포함률로 찾는다.
    """
    mapping: dict[str, Passage] = {}
    new_numbers = [{q.number for q in passage.questions if q.number} for passage in new_passages]
    new_pages = [set(passage.source_pages) for passage in new_passages]
    new_compact = [_compact(passage.text)[:400] for passage in new_passages]
    new_grams = [_grams(_compact(_full_text(passage))) for passage in new_passages]
    for old in old_passages:
        collapse = _all_repeated_digits(old)
        numbers = {number for q in old.questions if q.number for number in _number_forms(q.number, collapse)}
        pages = set(old.source_pages)
        old_text = _compact(old.text)[:400]
        old_grams = _grams(_compact(_full_text(old)))

        candidates = []
        for index in range(len(new_passages)):
            overlap = len(numbers & new_numbers[index])
            if not overlap:
                continue
            page_overlap = bool(pages & new_pages[index])
            contained = _containment(old_grams, new_grams[index])
            if page_overlap or contained >= 0.3:
                # 번호 개수보다 같은 쪽·같은 내용이 우선이다(문항 하나가 빠진 경우 다른 선택과목으로 가지 않게).
                candidates.append(((page_overlap, round(contained, 2), overlap), index))
        if candidates:
            mapping[old.id] = new_passages[max(candidates)[1]]
            continue

        best: Optional[tuple[tuple, int]] = None
        for index in range(len(new_passages)):
            page_overlap = bool(pages & new_pages[index])
            needed = 0.5 if (not numbers and page_overlap) else 0.75
            score = _containment(old_grams, new_grams[index]) if page_overlap else 0.0
            if score < needed:
                score = max(score, _similarity(old_text, new_compact[index], needed))
            if score < needed:
                continue
            key = (page_overlap, score)
            if best is None or key > best[0]:
                best = (key, index)
        if best is not None:
            mapping[old.id] = new_passages[best[1]]
    return mapping


class _RelatedIndex:
    """옛 지문의 문항이 옮겨 갔을 수 있는 새 지문(같은 쪽이거나 내용이 겹침)을 찾는다.

    선택과목(화작·언매)처럼 번호와 발문 형식이 같은 다른 지문으로 잘못 옮기지 않도록 범위를 좁힌다.
    새 지문의 3글자 조각은 한 번만 계산한다.
    """

    def __init__(self, new_passages: list[Passage]) -> None:
        self.new_passages = new_passages
        self.new_grams = [_grams(_compact(_full_text(passage))) for passage in new_passages]
        self._cache: dict[str, list[Question]] = {}

    def questions(self, old: Passage) -> list[Question]:
        cached = self._cache.get(old.id)
        if cached is not None:
            return cached
        pages = set(old.source_pages)
        old_grams = _grams(_compact(_full_text(old)))
        output: list[Question] = []
        for passage, grams in zip(self.new_passages, self.new_grams, strict=True):
            related = bool(pages & set(passage.source_pages)) if pages and passage.source_pages else False
            if not related:
                related = _containment(old_grams, grams) >= 0.3
            if related:
                output.extend(passage.questions)
        self._cache[old.id] = output
        return output


def match_questions(
    old_passages: list[Passage],
    new_passages: list[Passage],
    mapping: dict[str, Passage],
    related: Optional[_RelatedIndex] = None,
) -> dict[str, Question]:
    """옛 문항 ID → 새 문항(1:1). 짝지은 지문 안에서 먼저, 나머지는 관련 새 지문에서 찾는다.

    정답·해설 등 사용자 입력이 있는 문항이 먼저 짝을 차지한다. 같은 PDF가 라이브러리에 두 번
    있을 때 입력이 없는 사본이 라이브러리 순서만으로 새 문항을 먼저 가져가지 않게 하기 위해서다.
    """
    related = related or _RelatedIndex(new_passages)
    ordered = [(old, question) for old in old_passages for question in old.questions]
    ordered.sort(key=lambda item: not question_has_user_data(item[1]))  # 안정 정렬: 입력 있는 문항 먼저
    collapse = {old.id: _all_repeated_digits(old) for old in old_passages}
    result: dict[str, Question] = {}
    taken: set[str] = set()
    for old, question in ordered:
        target_passage = mapping.get(old.id)
        if target_passage is None:
            continue
        match = _find_counterpart(question, target_passage.questions, taken, collapse[old.id], 0.5)
        if match is not None:
            result[question.id] = match
            taken.add(match.id)
    for old, question in ordered:
        if question.id in result:
            continue
        match = _find_counterpart(question, related.questions(old), taken, collapse[old.id], 0.6)
        if match is not None:
            result[question.id] = match
            taken.add(match.id)
    return result


def _find_counterpart(
    question: Question,
    candidates: list[Question],
    taken: set[str],
    collapse: bool,
    threshold: float,
    *,
    include_taken: bool = False,
) -> Optional[Question]:
    forms = _number_forms(question.number, collapse)
    best: Optional[tuple[tuple, Question]] = None
    for rank, form in enumerate(forms):
        for candidate in candidates:
            if candidate.number != form or (candidate.id in taken and not include_taken):
                continue
            score = question_match_score(question, candidate)
            if score < threshold:
                continue
            key = (-rank, score)
            if best is None or key > best[0]:
                best = (key, candidate)
    return best[1] if best is not None else None


def question_match_score(old: Question, new: Question) -> float:
    """같은 문항일 가능성(0~1).

    발문은 시험지마다 비슷한 문구가 많아 단독 근거로 쓰지 않는다. 내용이 있는 선택지는 반드시
    일치해야 하고, 'ㄱ, ㄴ'처럼 짧은 선택지뿐이면 발문이 거의 같아야 한다.
    """
    old_choices = _choice_text(old)
    new_choices = _choice_text(new)
    old_stem = _compact(old.stem)
    new_stem = _compact(new.stem)
    stem_score = _overlap_score(old_stem, new_stem) if old_stem and new_stem else 0.0
    substantive = min(len(old_choices), len(new_choices)) >= 15
    if substantive:
        choice_score = _overlap_score(old_choices, new_choices)
        if choice_score < 0.5 or (old_stem and new_stem and stem_score < 0.5):
            return 0.0
        return 0.6 * choice_score + 0.4 * (stem_score if old_stem and new_stem else choice_score)
    if old_stem and new_stem and stem_score >= 0.85:
        if old_choices and new_choices and _overlap_score(old_choices, new_choices) < 0.5:
            return 0.0
        return stem_score
    return 0.0


def _equivalent_copy_exists(question: Question, candidates: list[Question], collapse: bool) -> bool:
    """같은 문항에 이미 같은 정답·해설이 들어가 있으면(라이브러리에 같은 PDF가 두 번 있던 경우) 옮긴 것으로 본다."""
    match = _find_counterpart(question, candidates, set(), collapse, 0.6, include_taken=True)
    if match is None:
        return False
    same_answer = not question.answer.strip() or question.answer.strip() == match.answer.strip()
    same_explanation = not question.explanation.strip() or question.explanation.strip() == match.explanation.strip()
    same_difficulty = question.difficulty_source != "manual" or (
        match.difficulty_source == "manual" and match.difficulty == question.difficulty
    )
    user_images = [asset for asset in question.images if _user_added(asset)]
    same_images = all(_has_asset(match.images, asset) for asset in user_images)
    return same_answer and same_explanation and same_difficulty and same_images


# ---------------------------------------------------------------------------
# 사용자 데이터


def question_has_user_data(question: Question) -> bool:
    return bool(
        question.answer.strip()
        or question.explanation.strip()
        or (question.difficulty_source == "manual" and question.difficulty)
        or any(_user_added(asset) for asset in question.images)
    )


def has_user_data(passage: Passage) -> bool:
    """사용자가 직접 입력·수정했거나 수정 여부를 알 수 없는(이전 버전 기록) 지문."""
    if passage.edited is not False or passage.difficulty_source == "manual" or passage.tags:
        return True
    if passage.title and not _is_auto_title(passage.title, passage.source_file):
        return True
    if any(_user_added(asset) for asset in passage.images):
        return True
    return any(question_has_user_data(question) for question in passage.questions)


def carry_passage_data(
    old: Passage,
    new: Passage,
    claimed_titles: dict[str, str],
    claimed_difficulty: dict[str, int],
) -> tuple[int, bool]:
    """지문 단위 사용자 정보를 옮긴다. (옮긴 수, 다른 옛 지문과 충돌해 못 옮긴 것이 있는지)"""
    carried = 0
    conflict = False
    if old.difficulty_source == "manual" and old.difficulty:
        previous = claimed_difficulty.get(new.id)
        if previous is not None and previous != old.difficulty:
            conflict = True
        else:
            new.difficulty = old.difficulty
            new.difficulty_source = "manual"
            claimed_difficulty[new.id] = old.difficulty
            carried += 1
    elif new.difficulty is None and old.difficulty:
        new.difficulty = old.difficulty
        new.difficulty_source = old.difficulty_source
        new.difficulty_reason = new.difficulty_reason or old.difficulty_reason
    if old.passage_type:
        new.passage_type = old.passage_type  # 사용자가 고쳤을 수 있는 기존 유형을 우선한다.
    for tag in old.tags:
        if tag not in new.tags:
            new.tags.append(tag)
    if old.title and not _is_auto_title(old.title, old.source_file) and not old.title.startswith(SALVAGE_PREFIX):
        previous_title = claimed_titles.get(new.id)
        if previous_title is not None and previous_title != old.title:
            conflict = True
        else:
            new.title = old.title
            claimed_titles[new.id] = old.title
            carried += 1
    for asset in old.images:
        if _user_added(asset) and not _has_asset(new.images, asset):
            new.images.append(MediaAsset.from_dict(asset.to_dict()))
            carried += 1
    if carried:
        new.touch()
    return carried, conflict


def _copy_question_data(source: Question, target: Question) -> tuple[int, bool]:
    """(옮긴 수, 대상에 이미 다른 값이 있어 옮기지 못한 것이 있는지)"""
    carried = 0
    conflict = False
    for name in ("answer", "explanation"):
        value = getattr(source, name).strip()
        if not value:
            continue
        existing = getattr(target, name).strip()
        if not existing:
            setattr(target, name, getattr(source, name))
            carried += 1
        elif existing != value:
            conflict = True
    if source.difficulty_source == "manual" and source.difficulty:
        if target.difficulty_source == "manual" and target.difficulty != source.difficulty:
            conflict = True
        else:
            target.difficulty = source.difficulty
            target.difficulty_source = "manual"
            carried += 1
    elif target.difficulty is None and source.difficulty:
        target.difficulty = source.difficulty
        target.difficulty_source = source.difficulty_source
    for asset in source.images:
        if _user_added(asset) and not _has_asset(target.images, asset):
            target.images.append(MediaAsset.from_dict(asset.to_dict()))
            carried += 1
    return carried, conflict


# ---------------------------------------------------------------------------
# 비교 유틸


def _number_forms(number: str, collapse: bool) -> list[str]:
    """문항 번호 후보. 이전 버전이 겹친 글자로 저장한 번호를 원래 번호로도 본다.

    '111'처럼 같은 숫자 세 개는 실제 문항 번호일 수 없으므로 항상 '1'도 후보로 본다.
    '11'처럼 두 개는 진짜 11번일 수 있어 지문의 모든 번호가 겹친 번호일 때(collapse)만 본다.
    """
    value = (number or "").strip()
    forms = [value] if value else []
    pattern = r"(\d)\1{1,2}" if collapse else r"(\d)\1{2}"
    match = re.fullmatch(pattern, value)
    if match and match.group(1) not in forms:
        forms.append(match.group(1))
    return forms


def _all_repeated_digits(passage: Passage) -> bool:
    """지문의 모든 문항 번호가 '111'·'22'처럼 같은 숫자 반복이면 이전 버전의 겹친 번호로 본다."""
    numbers = [question.number.strip() for question in passage.questions if question.number.strip()]
    return bool(numbers) and all(re.fullmatch(r"(\d)\1{1,2}", number) for number in numbers)


def _choice_text(question: Question) -> str:
    return _compact("".join(_CHOICE_MARK_RE.sub("", choice) for choice in question.choices))


def _overlap_score(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    ratio = SequenceMatcher(None, left[:300], right[:300], autojunk=False).ratio()
    left_grams = _grams(left)
    right_grams = _grams(right)
    if min(len(left_grams), len(right_grams)) < 6:
        return ratio  # 짧은 글은 조각 포함률이 쉽게 높아지므로 전체 비율만 본다.
    shared = len(left_grams & right_grams)
    # 이전 버전 발문·선택지에는 <보기> 글 등이 붙어 있을 수 있어 짧은 쪽이 포함되는지도 본다.
    contained = shared / min(len(left_grams), len(right_grams))
    return max(ratio, contained)


def _containment(old_grams: set[str], new_grams: set[str]) -> float:
    return len(old_grams & new_grams) / len(old_grams) if old_grams else 0.0


def _full_text(passage: Passage) -> str:
    return "\n".join(
        [passage.text]
        + [question.stem + "\n" + "\n".join(question.choices) for question in passage.questions]
    )


def _grams(text: str, size: int = 3) -> set[str]:
    return {text[index:index + size] for index in range(max(0, len(text) - size + 1))}


def _similarity(left: str, right: str, needed: float) -> float:
    if not left or not right:
        return 0.0
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    if needed > 0 and (matcher.real_quick_ratio() < needed or matcher.quick_ratio() < needed):
        return 0.0
    return matcher.ratio()


def _user_added(asset: MediaAsset) -> bool:
    return not asset.kind and asset.source_page == 0


def _has_asset(assets: list[MediaAsset], asset: MediaAsset) -> bool:
    return any(item.relative_path == asset.relative_path and item.anchor == asset.anchor for item in assets)


def _is_auto_title(title: str, source_file: str) -> bool:
    stem = Path(source_file).stem if source_file else ""
    if not stem:
        return bool(re.fullmatch(r"지문 \d+", title.strip()))
    return bool(re.fullmatch(rf"{re.escape(stem)}(?: - 지문 \d+)?", title.strip()))


def _compact(text: str) -> str:
    compact = re.sub(r"\W+", "", text or "", flags=re.UNICODE).lower()
    # 이전 분석의 겹친 글자('윗윗윗글글글')도 비교할 수 있도록 같은 글자 반복을 줄인다.
    return re.sub(r"(.)\1{1,2}", r"\1", compact)
