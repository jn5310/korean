"""[Step 2] PDF 텍스트 추출 + OCR + 지문/문제 파싱 — 인터페이스 정의.

구현 계획:
  1. pdfplumber 로 페이지별 텍스트 추출
  2. 추출 텍스트가 거의 없는 페이지 → 스캔본으로 판단, 이미지 렌더링 후 pytesseract OCR
  3. 문항 번호/선택지 패턴(예: "18.", "①~⑤")을 기준으로 지문 1 : N 문제 구조화
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..models import Passage

ProgressCallback = Callable[[int, int, str], None]  # (현재, 전체, 메시지)


@dataclass
class PageText:
    page_number: int          # 1부터 시작
    text: str
    method: str               # "text" | "ocr"


@dataclass
class ParseResult:
    source_file: str
    pages: list[PageText] = field(default_factory=list)
    passages: list[Passage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class PdfParser:
    def __init__(self, tesseract_cmd: str = "", ocr_languages: str = "kor+eng", ocr_dpi: int = 300) -> None:
        self.tesseract_cmd = tesseract_cmd
        self.ocr_languages = ocr_languages
        self.ocr_dpi = ocr_dpi

    def parse(self, pdf_path: Path, progress: Optional[ProgressCallback] = None) -> ParseResult:
        raise NotImplementedError("PDF 분석 기능은 Step 2 에서 구현됩니다.")
