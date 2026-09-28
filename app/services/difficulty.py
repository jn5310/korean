"""[Step 3] Gemini 기반 지문/문제 난이도(1~5) 자동 판별 — 인터페이스 정의.

GeminiClient.generate_json() 에 JSON 스키마를 넘겨 구조화된 응답
{passage_difficulty, reason, passage_type, questions: [{id, difficulty, question_type}]}
을 받아 Passage/Question 에 반영할 예정.
"""
from __future__ import annotations

from ..models import Passage
from .gemini_client import GeminiClient


class DifficultyClassifier:
    def __init__(self, client: GeminiClient) -> None:
        self.client = client

    def classify(self, passage: Passage) -> Passage:
        raise NotImplementedError("AI 난이도 판별은 Step 3 에서 구현됩니다.")
