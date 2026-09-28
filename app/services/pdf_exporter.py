"""[Step 5] ReportLab 기반 1지문-1페이지 PDF 출판 엔진 — 인터페이스 정의.

규칙:
  - 각 지문은 항상 새 페이지에서 시작
  - 지문/문제가 길면 다음 페이지로 자연스럽게 이어짐 (문항 중간 분리 최소화)
  - 한글 TTF 폰트 등록 필요 (예: NanumGothic, Noto Sans KR)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..models import Passage


@dataclass
class ExportOptions:
    title: str = ""
    include_answers: bool = False
    font_path: str = ""
    font_size: float = 10.5


class PdfExporter:
    def __init__(self, options: ExportOptions | None = None) -> None:
        self.options = options or ExportOptions()

    def export(self, passages: list[Passage], output_path: Path) -> Path:
        raise NotImplementedError("PDF 내보내기는 Step 5 에서 구현됩니다.")
