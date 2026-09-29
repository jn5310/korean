"""도메인 데이터 모델.

평문 필드는 검색·AI 분석·호환성을 위한 기준 데이터로 유지하고, PDF 원본의
굵게/기울임/밑줄/색상은 제한된 HTML sidecar에, 그림은 MediaAsset에 보존한다.
지문(Passage) 1 : N 문제(Question) 구조와 JSON v1의 역호환을 유지한다.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Optional

MIN_DIFFICULTY = 1
MAX_DIFFICULTY = 5


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clamp_difficulty(value) -> Optional[int]:
    """None(미분류) 또는 1~5 범위 정수로 정규화."""
    if value is None or value == "":
        return None
    try:
        level = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(MIN_DIFFICULTY, min(MAX_DIFFICULTY, level))


def _safe_relative_path(value) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or ":" in normalized:
        return ""
    return path.as_posix()


def _nonnegative_int(value, maximum: int = 20_000) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(maximum, number))


def _bbox(value) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return []
    try:
        result = [round(float(item), 3) for item in value]
    except (TypeError, ValueError, OverflowError):
        return []
    if not all(math.isfinite(item) for item in result):
        return []
    return result if result[2] >= result[0] and result[3] >= result[1] else []


@dataclass
class MediaAsset:
    """앱 자산 폴더에 보관되는 래스터 그림과 원본 위치."""

    id: str = field(default_factory=new_id)
    relative_path: str = ""
    mime_type: str = "image/png"
    width: int = 0
    height: int = 0
    source_page: int = 0
    bbox: list[float] = field(default_factory=list)
    anchor: str = "after"       # passage | stem | choice:0..4 | after
    offset: int = 0              # 대응 평문 내 근사 문자 위치
    alt: str = "문제 그림"

    def __post_init__(self) -> None:
        self.relative_path = _safe_relative_path(self.relative_path)
        self.mime_type = self.mime_type if isinstance(self.mime_type, str) and self.mime_type in {"image/png", "image/jpeg", "image/webp"} else "image/png"
        self.width = _nonnegative_int(self.width)
        self.height = _nonnegative_int(self.height)
        self.source_page = _nonnegative_int(self.source_page, 1_000_000)
        self.bbox = _bbox(self.bbox)
        self.anchor = self.anchor if isinstance(self.anchor, str) and self.anchor in {"passage", "stem", "after", "choice:0", "choice:1", "choice:2", "choice:3", "choice:4"} else "after"
        self.offset = _nonnegative_int(self.offset, 10_000_000)
        self.alt = str(self.alt or "문제 그림")[:200]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "relative_path": self.relative_path,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "source_page": self.source_page,
            "bbox": list(self.bbox),
            "anchor": self.anchor,
            "offset": self.offset,
            "alt": self.alt,
        }

    @classmethod
    def from_dict(cls, data) -> "MediaAsset":
        if not isinstance(data, dict):
            return cls()
        return cls(
            id=str(data.get("id") or new_id()),
            relative_path=data.get("relative_path", ""),
            mime_type=data.get("mime_type", "image/png"),
            width=data.get("width", 0),
            height=data.get("height", 0),
            source_page=data.get("source_page", 0),
            bbox=data.get("bbox", []),
            anchor=data.get("anchor", "after"),
            offset=data.get("offset", 0),
            alt=data.get("alt", "문제 그림"),
        )


@dataclass
class Question:
    id: str = field(default_factory=new_id)
    number: str = ""
    stem: str = ""
    choices: list[str] = field(default_factory=list)
    answer: str = ""
    explanation: str = ""
    question_type: str = ""
    difficulty: Optional[int] = None
    difficulty_source: str = ""
    # v2 rich sidecar — 비어 있으면 평문을 사용한다.
    stem_html: str = ""
    choices_html: list[str] = field(default_factory=list)
    images: list[MediaAsset] = field(default_factory=list)
    source_pages: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.difficulty = clamp_difficulty(self.difficulty)
        self.stem_html = self.stem_html if isinstance(self.stem_html, str) else ""
        self.choices_html = [str(item) for item in self.choices_html] if isinstance(self.choices_html, list) else []
        if len(self.choices_html) < len(self.choices):
            self.choices_html.extend([""] * (len(self.choices) - len(self.choices_html)))
        self.choices_html = self.choices_html[: len(self.choices)]
        self.images = [asset if isinstance(asset, MediaAsset) else MediaAsset.from_dict(asset) for asset in self.images] if isinstance(self.images, list) else []
        self.source_pages = sorted({int(page) for page in self.source_pages if _positive_int(page)}) if isinstance(self.source_pages, list) else []

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "number": self.number,
            "stem": self.stem,
            "choices": list(self.choices),
            "answer": self.answer,
            "explanation": self.explanation,
            "question_type": self.question_type,
            "difficulty": self.difficulty,
            "difficulty_source": self.difficulty_source,
            "stem_html": self.stem_html,
            "choices_html": list(self.choices_html),
            "images": [asset.to_dict() for asset in self.images if asset.relative_path],
            "source_pages": list(self.source_pages),
        }

    @classmethod
    def from_dict(cls, data) -> "Question":
        if not isinstance(data, dict):
            return cls()
        choices = [str(choice) for choice in data.get("choices", [])] if isinstance(data.get("choices", []), list) else []
        return cls(
            id=data.get("id") or new_id(),
            number=str(data.get("number", "")),
            stem=str(data.get("stem", "")),
            choices=choices,
            answer=str(data.get("answer", "")),
            explanation=str(data.get("explanation", "")),
            question_type=str(data.get("question_type", "")),
            difficulty=clamp_difficulty(data.get("difficulty")),
            difficulty_source=str(data.get("difficulty_source", "")),
            stem_html=data.get("stem_html", ""),
            choices_html=data.get("choices_html", []),
            images=data.get("images", []),
            source_pages=data.get("source_pages", []),
        )


@dataclass
class Passage:
    id: str = field(default_factory=new_id)
    title: str = ""
    text: str = ""
    passage_type: str = ""
    difficulty: Optional[int] = None
    difficulty_source: str = ""
    difficulty_reason: str = ""
    tags: list[str] = field(default_factory=list)
    source_file: str = ""
    source_pages: list[int] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    # v2 rich sidecar
    text_html: str = ""
    images: list[MediaAsset] = field(default_factory=list)
    source_digest: str = ""

    def __post_init__(self) -> None:
        self.difficulty = clamp_difficulty(self.difficulty)
        self.text_html = self.text_html if isinstance(self.text_html, str) else ""
        self.images = [asset if isinstance(asset, MediaAsset) else MediaAsset.from_dict(asset) for asset in self.images] if isinstance(self.images, list) else []
        self.source_digest = str(self.source_digest or "")[:128]

    @property
    def display_title(self) -> str:
        if self.title.strip():
            return self.title.strip()
        first_line = self.text.strip().splitlines()[0] if self.text.strip() else ""
        return (first_line[:30] + "…") if len(first_line) > 30 else (first_line or "(제목 없음)")

    def touch(self) -> None:
        self.updated_at = now_iso()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "text": self.text,
            "passage_type": self.passage_type,
            "difficulty": self.difficulty,
            "difficulty_source": self.difficulty_source,
            "difficulty_reason": self.difficulty_reason,
            "tags": list(self.tags),
            "source_file": self.source_file,
            "source_pages": list(self.source_pages),
            "questions": [question.to_dict() for question in self.questions],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "text_html": self.text_html,
            "images": [asset.to_dict() for asset in self.images if asset.relative_path],
            "source_digest": self.source_digest,
        }

    @classmethod
    def from_dict(cls, data) -> "Passage":
        if not isinstance(data, dict):
            return cls()
        return cls(
            id=data.get("id") or new_id(),
            title=str(data.get("title", "")),
            text=str(data.get("text", "")),
            passage_type=str(data.get("passage_type", "")),
            difficulty=clamp_difficulty(data.get("difficulty")),
            difficulty_source=str(data.get("difficulty_source", "")),
            difficulty_reason=str(data.get("difficulty_reason", "")),
            tags=[str(tag) for tag in data.get("tags", [])] if isinstance(data.get("tags", []), list) else [],
            source_file=str(data.get("source_file", "")),
            source_pages=[int(page) for page in data.get("source_pages", []) if _positive_int(page)] if isinstance(data.get("source_pages", []), list) else [],
            questions=[Question.from_dict(question) for question in data.get("questions", [])] if isinstance(data.get("questions", []), list) else [],
            created_at=data.get("created_at") or now_iso(),
            updated_at=data.get("updated_at") or now_iso(),
            text_html=data.get("text_html", ""),
            images=data.get("images", []),
            source_digest=data.get("source_digest", ""),
        )


@dataclass
class ExamSet:
    """커스텀 시험지/문제집 — 지문 ID 순서 목록."""

    id: str = field(default_factory=new_id)
    title: str = "새 시험지"
    passage_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "passage_ids": list(self.passage_ids), "created_at": self.created_at}

    @classmethod
    def from_dict(cls, data) -> "ExamSet":
        if not isinstance(data, dict):
            return cls()
        return cls(
            id=data.get("id") or new_id(),
            title=str(data.get("title", "새 시험지")),
            passage_ids=[str(passage_id) for passage_id in data.get("passage_ids", [])] if isinstance(data.get("passage_ids", []), list) else [],
            created_at=data.get("created_at") or now_iso(),
        )


@dataclass
class Library:
    """저장되는 전체 데이터 (지문 라이브러리 + 시험지 목록)."""

    SCHEMA_VERSION = 2

    passages: list[Passage] = field(default_factory=list)
    exam_sets: list[ExamSet] = field(default_factory=list)

    def get_passage(self, passage_id: str) -> Optional[Passage]:
        return next((passage for passage in self.passages if passage.id == passage_id), None)

    def filter_passages(
        self,
        passage_types: Optional[set[str]] = None,
        question_types: Optional[set[str]] = None,
        difficulties: Optional[set[Optional[int]]] = None,
        keyword: str = "",
    ) -> list[Passage]:
        keyword = keyword.strip().lower()
        result = []
        for passage in self.passages:
            if passage_types is not None and passage.passage_type not in passage_types:
                continue
            if question_types is not None and not any(question.question_type in question_types for question in passage.questions):
                continue
            if difficulties is not None and passage.difficulty not in difficulties:
                continue
            if keyword and keyword not in (passage.title + "\n" + passage.text).lower():
                continue
            result.append(passage)
        return result

    def add_passage(self, passage: Passage) -> None:
        self.passages.append(passage)

    def remove_passage(self, passage_id: str) -> None:
        self.passages = [passage for passage in self.passages if passage.id != passage_id]
        for exam in self.exam_sets:
            exam.passage_ids = [pid for pid in exam.passage_ids if pid != passage_id]

    def to_dict(self) -> dict:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "passages": [passage.to_dict() for passage in self.passages],
            "exam_sets": [exam.to_dict() for exam in self.exam_sets],
        }

    @classmethod
    def from_dict(cls, data) -> "Library":
        if not isinstance(data, dict):
            return cls()
        passages = data.get("passages", [])
        exam_sets = data.get("exam_sets", [])
        return cls(
            passages=[Passage.from_dict(passage) for passage in passages] if isinstance(passages, list) else [],
            exam_sets=[ExamSet.from_dict(exam) for exam in exam_sets] if isinstance(exam_sets, list) else [],
        )


def _positive_int(value) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return int(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False
