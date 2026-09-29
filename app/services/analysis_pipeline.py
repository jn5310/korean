"""PDF 추출 → 구조 복원 → Gemini 교차 검토 → 난이도 분류 통합 파이프라인."""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Optional

from ..models import Passage
from .difficulty import DifficultyClassifier
from .document_analyzer import GeminiDocumentAnalyzer
from .gemini_client import GeminiError
from .pdf_parser import ParseResult, PdfAnalysisError, PdfParser
from .rich_content import rich_html_to_plain

logger = logging.getLogger(__name__)

PipelineProgress = Callable[[int, int, str], None]
_FILE_UNITS = 1000
_FILE_HASH_CACHE: dict[tuple[str, int, int], str] = {}


@dataclass
class FileAnalysisFailure:
    path: str
    message: str


@dataclass
class AnalysisBatchResult:
    results: list[ParseResult] = field(default_factory=list)
    failures: list[FileAnalysisFailure] = field(default_factory=list)
    general_warnings: list[str] = field(default_factory=list)
    selected_model: str = ""
    original_model: str = ""
    model_changed: bool = False

    @property
    def passages(self) -> list[Passage]:
        return [passage for result in self.results for passage in result.passages]

    @property
    def successful_paths(self) -> list[str]:
        return [result.source_file for result in self.results if result.passages]

    @property
    def warnings(self) -> list[str]:
        output: list[str] = list(self.general_warnings)
        for result in self.results:
            name = Path(result.source_file).name
            output.extend(f"[{name}] {warning}" for warning in result.warnings)
        output.extend(f"[{Path(item.path).name}] 실패: {item.message}" for item in self.failures)
        return list(dict.fromkeys(output))


class AnalysisPipeline:
    """한 Worker 안에서 파일들을 순차 분석하여 무료 API 제한과 데이터 일관성을 지킨다."""

    def __init__(
        self,
        parser: PdfParser,
        *,
        document_analyzer: Optional[GeminiDocumentAnalyzer] = None,
        classifier: Optional[DifficultyClassifier] = None,
    ) -> None:
        self.parser = parser
        self.document_analyzer = document_analyzer
        self.classifier = classifier

    def analyze_files(
        self,
        paths: list[Path],
        progress: Optional[PipelineProgress] = None,
    ) -> AnalysisBatchResult:
        batch = AnalysisBatchResult()
        total_units = max(1, len(paths) * _FILE_UNITS)
        last_progress = 0

        def monotonic_progress(current: int, total: int, message: str) -> None:
            nonlocal last_progress
            last_progress = max(last_progress, min(current, total))
            _emit(progress, last_progress, total, message)

        for file_index, path in enumerate(paths):
            base = file_index * _FILE_UNITS
            _emit(monotonic_progress, base, total_units, f"{path.name}: 분석 준비")
            try:
                result = self._analyze_file(path, base, total_units, monotonic_progress)
            except Exception as exc:  # noqa: BLE001 - 파일별 실패 후 다음 파일 계속
                logger.exception("PDF 분석 실패: %s", path)
                batch.failures.append(FileAnalysisFailure(str(path), _friendly_message(exc)))
            else:
                if result.passages:
                    batch.results.append(result)
                else:
                    message = "지문·문항을 찾지 못했습니다."
                    if result.warnings:
                        message += " " + result.warnings[-1]
                    batch.failures.append(FileAnalysisFailure(str(path), message))
            _emit(monotonic_progress, base + _FILE_UNITS, total_units, f"{path.name}: 분석 완료")
        return batch

    def _analyze_file(
        self,
        path: Path,
        base: int,
        total_units: int,
        progress: Optional[PipelineProgress],
    ) -> ParseResult:
        local_error: Optional[Exception] = None
        try:
            result = self.parser.parse(
                path,
                progress=lambda current, total, message: _map_progress(
                    progress, base, 0, 450, current, total, total_units, f"{path.name}: {message}"
                ),
            )
        except PdfAnalysisError as exc:
            local_error = exc
            result = ParseResult(source_file=str(path), warnings=[f"로컬 PDF 분석 실패: {exc}"])

        if self.document_analyzer is not None:
            _emit(progress, base + 460, total_units, f"{path.name}: Gemini 교차 분석 시작")
            try:
                has_unread_pages = bool(result.pages) and any(
                    not page.text.strip() or page.quality < 0.30 for page in result.pages
                )
                if result.pages and not has_unread_pages:
                    ai_result = self.document_analyzer.analyze_pages(
                        result.pages,
                        result.source_file,
                        progress=lambda current, total, message: _map_progress(
                            progress, base, 460, 370, current, total, total_units, f"{path.name}: {message}"
                        ),
                    )
                else:
                    _emit(progress, base + 520, total_units, f"{path.name}: Gemini PDF 비전 분석 중")
                    ai_result = self.document_analyzer.analyze_pdf(path)
                    _emit(progress, base + 830, total_units, f"{path.name}: Gemini PDF 비전 분석 완료")

                reconciled = self.document_analyzer.reconcile(
                    result.passages,
                    result.confidence,
                    ai_result,
                )
                result.passages = reconciled.passages
                result.warnings.extend(reconciled.warnings)
                result.confidence = max(result.confidence, ai_result.confidence)
                result.used_ai = True
            except GeminiError as exc:
                if result.passages:
                    result.warnings.append(f"Gemini 교차 분석 실패 — 로컬 결과를 유지합니다: {exc}")
                else:
                    if local_error:
                        raise PdfAnalysisError(
                            f"로컬 분석({local_error}) 및 Gemini 분석({exc})이 모두 실패했습니다."
                        ) from exc
                    raise
            else:
                if reconciled.needs_classification and self.classifier and result.passages:
                    try:
                        classified = self.classifier.classify_many(
                            result.passages,
                            progress=lambda current, total, message: _map_progress(
                                progress, base, 840, 140, current, total, total_units, f"{path.name}: {message}"
                            ),
                        )
                        result.warnings.extend(classified.warnings)
                    except GeminiError as exc:
                        result.warnings.append(f"AI 난이도·유형 추가 판별 실패: {exc}")
        elif result.passages:
            result.warnings.append("Gemini API Key가 없어 난이도·유형 자동 판별을 건너뛰었습니다.")

        if result.passages and result.pages:
            result.warnings.extend(
                self.parser.project_rich_content(result.passages, result.pages, result.source_file)
            )
        result.warnings = list(dict.fromkeys(result.warnings))
        if not result.passages and local_error:
            raise local_error
        return result


def passage_fingerprint(passage: Passage) -> str:
    """원본 위치와 전체 문항 내용을 함께 포함한 정확한 식별값."""
    question_numbers = ",".join(question.number for question in passage.questions)
    page_key = ",".join(map(str, passage.source_pages))
    content_key = _normalized_passage_content(passage)
    if passage.source_file and (page_key or question_numbers):
        source_key = _source_file_digest(passage.source_file)
        locator = f"pages={page_key}|questions={question_numbers}"
        return hashlib.sha256(
            f"source={source_key}|{locator}|content={content_key}".encode("utf-8")
        ).hexdigest()
    return hashlib.sha256(content_key.encode("utf-8")).hexdigest()


def passages_equivalent(left: Passage, right: Passage) -> bool:
    """같은 원본 위치의 작은 OCR 차이는 중복, 실제 내용 차이는 별도 지문으로 판단."""
    if passage_fingerprint(left) == passage_fingerprint(right):
        return True
    if not left.source_file or not right.source_file:
        return False
    if _source_file_digest(left.source_file) != _source_file_digest(right.source_file):
        return False
    if left.source_pages != right.source_pages:
        return False
    if [q.number for q in left.questions] != [q.number for q in right.questions]:
        return False
    similarity = SequenceMatcher(
        None,
        _normalized_passage_content(left),
        _normalized_passage_content(right),
        autojunk=False,
    ).ratio()
    return similarity >= 0.92


def _normalized_passage_content(passage: Passage) -> str:
    text = passage.text + "|" + "|".join(
        question.number + question.stem + "".join(question.choices)
        for question in passage.questions
    )
    return re.sub(r"\W+", "", text, flags=re.UNICODE).lower()


def merge_rich_content(existing: Passage, incoming: Passage) -> bool:
    """동등 레코드에서 평문과 정확히 대응하는 서식·그림만 비파괴적으로 보강."""
    changed = False
    passage_matches = _same_plain(existing.text, incoming.text)
    if (
        passage_matches
        and not existing.text_html
        and incoming.text_html
        and _html_matches(existing.text, incoming.text_html)
    ):
        existing.text_html = incoming.text_html
        changed = True
    if passage_matches:
        changed |= _merge_assets(existing.images, incoming.images)
    if not existing.source_digest and incoming.source_digest:
        existing.source_digest = incoming.source_digest
        changed = True

    existing_counts = _number_counts(existing.questions)
    incoming_counts = _number_counts(incoming.questions)
    for index, question in enumerate(existing.questions):
        source = None
        if (
            question.number
            and existing_counts.get(question.number) == 1
            and incoming_counts.get(question.number) == 1
        ):
            source = next(item for item in incoming.questions if item.number == question.number)
        elif index < len(incoming.questions):
            candidate = incoming.questions[index]
            if _question_plain_matches(question, candidate):
                source = candidate
        if source is None or not _question_plain_matches(question, source):
            continue
        if (
            not question.stem_html
            and source.stem_html
            and _html_matches(question.stem, source.stem_html)
        ):
            question.stem_html = source.stem_html
            changed = True
        if len(question.choices_html) < len(question.choices):
            question.choices_html.extend([""] * (len(question.choices) - len(question.choices_html)))
        for choice_index in range(min(len(question.choices_html), len(source.choices_html))):
            incoming_html = source.choices_html[choice_index]
            if (
                not question.choices_html[choice_index]
                and incoming_html
                and _html_matches(question.choices[choice_index], incoming_html)
            ):
                question.choices_html[choice_index] = incoming_html
                changed = True
        changed |= _merge_assets(question.images, source.images)
        if not question.source_pages and source.source_pages:
            question.source_pages = list(source.source_pages)
            changed = True
    if changed:
        existing.touch()
    return changed


def _number_counts(questions) -> dict[str, int]:
    counts: dict[str, int] = {}
    for question in questions:
        if question.number:
            counts[question.number] = counts.get(question.number, 0) + 1
    return counts


def _same_plain(left: str, right: str) -> bool:
    def canonical(value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n")
    return canonical(left) == canonical(right)


def _html_matches(plain: str, rich_html: str) -> bool:
    return _same_plain(plain, rich_html_to_plain(rich_html))


def _question_plain_matches(left, right) -> bool:
    return (
        _same_plain(left.stem, right.stem)
        and len(left.choices) == len(right.choices)
        and all(_same_plain(a, b) for a, b in zip(left.choices, right.choices, strict=True))
    )


def _merge_assets(existing, incoming) -> bool:
    keys = {
        (asset.relative_path, asset.source_page, tuple(asset.bbox), asset.anchor, asset.offset)
        for asset in existing
    }
    changed = False
    for asset in incoming:
        key = (asset.relative_path, asset.source_page, tuple(asset.bbox), asset.anchor, asset.offset)
        if key not in keys:
            existing.append(asset)
            keys.add(key)
            changed = True
    return changed


def _source_file_digest(source_file: str) -> str:
    path = Path(source_file).expanduser()
    try:
        resolved = path.resolve()
        stat = resolved.stat()
        cache_key = (str(resolved).casefold(), stat.st_size, stat.st_mtime_ns)
        cached = _FILE_HASH_CACHE.get(cache_key)
        if cached:
            return cached
        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        value = digest.hexdigest()
        _FILE_HASH_CACHE[cache_key] = value
        return value
    except OSError:
        return str(path.absolute()).casefold()


def _map_progress(
    callback: Optional[PipelineProgress],
    base: int,
    stage_start: int,
    stage_units: int,
    current: int,
    total: int,
    global_total: int,
    message: str,
) -> None:
    ratio = current / max(1, total)
    value = base + stage_start + int(stage_units * max(0.0, min(1.0, ratio)))
    _emit(callback, value, global_total, message)


def _emit(callback: Optional[PipelineProgress], current: int, total: int, message: str) -> None:
    if callback:
        callback(current, total, message)


def _friendly_message(exc: Exception) -> str:
    if isinstance(exc, (PdfAnalysisError, GeminiError)):
        return str(exc)
    return f"예상하지 못한 오류: {exc}"
