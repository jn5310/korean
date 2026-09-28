"""도메인 데이터 모델.

지문(Passage) 1 : N 문제(Question) 구조를 기본 단위로 하며,
시험지(ExamSet)는 지문 ID 의 순서 있는 목록으로 구성된다.
모든 모델은 JSON 직렬화(to_dict / from_dict)를 지원한다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    except (TypeError, ValueError):
        return None
    return max(MIN_DIFFICULTY, min(MAX_DIFFICULTY, level))


@dataclass
class Question:
    id: str = field(default_factory=new_id)
    number: str = ""                      # 원본 문항 번호 (예: "18")
    stem: str = ""                        # 발문
    choices: list[str] = field(default_factory=list)  # 선택지 (①~⑤)
    answer: str = ""
    explanation: str = ""
    question_type: str = ""
    difficulty: Optional[int] = None
    difficulty_source: str = ""           # "ai" | "manual" | ""

    def __post_init__(self) -> None:
        self.difficulty = clamp_difficulty(self.difficulty)

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
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Question":
        return cls(
            id=d.get("id") or new_id(),
            number=str(d.get("number", "")),
            stem=d.get("stem", ""),
            choices=[str(c) for c in d.get("choices", [])],
            answer=str(d.get("answer", "")),
            explanation=d.get("explanation", ""),
            question_type=d.get("question_type", ""),
            difficulty=clamp_difficulty(d.get("difficulty")),
            difficulty_source=d.get("difficulty_source", ""),
        )


@dataclass
class Passage:
    id: str = field(default_factory=new_id)
    title: str = ""
    text: str = ""
    passage_type: str = ""
    difficulty: Optional[int] = None
    difficulty_source: str = ""           # "ai" | "manual" | ""
    difficulty_reason: str = ""           # AI 판별 근거
    tags: list[str] = field(default_factory=list)
    source_file: str = ""
    source_pages: list[int] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        self.difficulty = clamp_difficulty(self.difficulty)

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
            "questions": [q.to_dict() for q in self.questions],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Passage":
        return cls(
            id=d.get("id") or new_id(),
            title=d.get("title", ""),
            text=d.get("text", ""),
            passage_type=d.get("passage_type", ""),
            difficulty=clamp_difficulty(d.get("difficulty")),
            difficulty_source=d.get("difficulty_source", ""),
            difficulty_reason=d.get("difficulty_reason", ""),
            tags=[str(t) for t in d.get("tags", [])],
            source_file=d.get("source_file", ""),
            source_pages=[int(p) for p in d.get("source_pages", [])],
            questions=[Question.from_dict(q) for q in d.get("questions", [])],
            created_at=d.get("created_at") or now_iso(),
            updated_at=d.get("updated_at") or now_iso(),
        )


@dataclass
class ExamSet:
    """커스텀 시험지/문제집 — 지문 ID 순서 목록."""

    id: str = field(default_factory=new_id)
    title: str = "새 시험지"
    passage_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "passage_ids": list(self.passage_ids),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExamSet":
        return cls(
            id=d.get("id") or new_id(),
            title=d.get("title", "새 시험지"),
            passage_ids=[str(p) for p in d.get("passage_ids", [])],
            created_at=d.get("created_at") or now_iso(),
        )


@dataclass
class Library:
    """저장되는 전체 데이터 (지문 라이브러리 + 시험지 목록)."""

    SCHEMA_VERSION = 1

    passages: list[Passage] = field(default_factory=list)
    exam_sets: list[ExamSet] = field(default_factory=list)

    # --- 조회 -------------------------------------------------------
    def get_passage(self, passage_id: str) -> Optional[Passage]:
        return next((p for p in self.passages if p.id == passage_id), None)

    def filter_passages(
        self,
        passage_types: Optional[set[str]] = None,
        question_types: Optional[set[str]] = None,
        difficulties: Optional[set[Optional[int]]] = None,
        keyword: str = "",
    ) -> list[Passage]:
        """유형/난이도/키워드 조건 필터 (조건이 None 이면 해당 조건 무시)."""
        keyword = keyword.strip().lower()
        result = []
        for p in self.passages:
            if passage_types is not None and p.passage_type not in passage_types:
                continue
            if question_types is not None and not any(
                q.question_type in question_types for q in p.questions
            ):
                continue
            if difficulties is not None and p.difficulty not in difficulties:
                continue
            if keyword and keyword not in (p.title + "\n" + p.text).lower():
                continue
            result.append(p)
        return result

    # --- 변경 -------------------------------------------------------
    def add_passage(self, passage: Passage) -> None:
        self.passages.append(passage)

    def remove_passage(self, passage_id: str) -> None:
        self.passages = [p for p in self.passages if p.id != passage_id]
        for exam in self.exam_sets:
            exam.passage_ids = [pid for pid in exam.passage_ids if pid != passage_id]

    # --- 직렬화 -----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "passages": [p.to_dict() for p in self.passages],
            "exam_sets": [e.to_dict() for e in self.exam_sets],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Library":
        return cls(
            passages=[Passage.from_dict(p) for p in d.get("passages", [])],
            exam_sets=[ExamSet.from_dict(e) for e in d.get("exam_sets", [])],
        )
