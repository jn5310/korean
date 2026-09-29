"""PDF 페이지 텍스트 추출과 스캔 페이지 OCR.

분석 전략
---------
1. pdfplumber로 각 페이지의 내장 텍스트를 먼저 읽는다.
2. 텍스트가 없거나 글자가 깨진 페이지에만 Tesseract OCR을 적용한다.
3. 내장 텍스트와 OCR 결과의 품질을 비교하여 더 나은 결과를 선택한다.
4. 여러 페이지에서 반복되는 머리말·꼬리말을 제거한다.
5. 페이지 번호를 보존한 채 structure_parser로 지문 1:N 오지선다 문제를 구성한다.

이 모듈은 Qt에 의존하지 않으므로 백그라운드 Worker와 독립적으로 검증할 수 있다.
"""
from __future__ import annotations

import logging
import math
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..assets import AssetStore
from ..models import MediaAsset, Passage
from .rich_content import PdfRichExtractor, RichContentProjector, RichRun

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]  # (현재, 전체, 메시지)


class PdfAnalysisError(Exception):
    """사용자에게 그대로 표시할 수 있는 PDF 분석 오류."""


class PdfDependencyError(PdfAnalysisError):
    """Python 패키지 또는 외부 OCR 엔진이 없는 경우."""


class OcrUnavailableError(PdfDependencyError):
    """Tesseract 실행 파일/언어 데이터가 없는 경우."""


@dataclass
class PageText:
    page_number: int          # 1부터 시작
    text: str
    method: str               # "text" | "ocr" | "empty"
    quality: float = 0.0      # 0.0 ~ 1.0 추출 품질 추정치
    rich_html: str = ""
    runs: list[RichRun] = field(default_factory=list)
    images: list[MediaAsset] = field(default_factory=list)


@dataclass
class ParseResult:
    source_file: str
    pages: list[PageText] = field(default_factory=list)
    passages: list[Passage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confidence: float = 0.0
    used_ai: bool = False

    @property
    def ocr_page_count(self) -> int:
        return sum(p.method == "ocr" for p in self.pages)

    @property
    def empty_page_count(self) -> int:
        return sum(not p.text.strip() for p in self.pages)

    @property
    def needs_ai_vision(self) -> bool:
        """로컬 추출로 구조 분석이 어려워 PDF 원본 비전 분석이 필요한지 여부."""
        return not self.passages or self.empty_page_count == len(self.pages)


class PdfParser:
    """텍스트 PDF와 스캔 PDF를 같은 PageText 형태로 변환한다."""

    def __init__(
        self,
        tesseract_cmd: str = "",
        ocr_languages: str = "kor+eng",
        ocr_dpi: int = 300,
        *,
        min_text_chars: int = 35,
        text_quality_threshold: float = 0.48,
        ocr_timeout_sec: int = 90,
        asset_store: Optional[AssetStore] = None,
    ) -> None:
        self.tesseract_cmd = tesseract_cmd.strip()
        self.ocr_languages = ocr_languages.strip() or "kor+eng"
        self.ocr_dpi = max(150, min(600, int(ocr_dpi)))
        self.min_text_chars = max(10, int(min_text_chars))
        self.text_quality_threshold = max(0.1, min(0.95, float(text_quality_threshold)))
        self.ocr_timeout_sec = max(10, int(ocr_timeout_sec))
        self.asset_store = asset_store
        self._rich_extractor = PdfRichExtractor(asset_store)
        self._rich_projector = RichContentProjector()

    # ------------------------------------------------------------------
    def parse(self, pdf_path: Path, progress: Optional[ProgressCallback] = None) -> ParseResult:
        path = self._validate_path(pdf_path)
        warnings: list[str] = []
        pages = self.extract_pages(path, progress=progress, warnings=warnings)

        # 순환 결합을 피하고 추출 계층을 단독 사용할 수 있도록 지연 import한다.
        from .structure_parser import parse_page_structure

        structured = parse_page_structure(pages, source_file=str(path))
        warnings.extend(structured.warnings)
        warnings.extend(self.project_rich_content(structured.passages, pages, str(path)))
        if not pages or all(not page.text.strip() for page in pages):
            warnings.append(
                "문서에서 읽을 수 있는 텍스트를 찾지 못했습니다. "
                "Gemini 문서 비전 분석 또는 Tesseract OCR이 필요합니다."
            )
        return ParseResult(
            source_file=str(path),
            pages=pages,
            passages=structured.passages,
            warnings=_deduplicate(warnings),
            confidence=structured.confidence,
        )

    def project_rich_content(
        self,
        passages: list[Passage],
        pages: list[PageText],
        source_file: str,
    ) -> list[str]:
        return self._rich_projector.project(passages, pages, source_file)

    def extract_pages(
        self,
        pdf_path: Path,
        *,
        progress: Optional[ProgressCallback] = None,
        warnings: Optional[list[str]] = None,
    ) -> list[PageText]:
        """페이지별 최적 텍스트를 추출한다. 구조 파싱 없이도 독립 사용 가능하다."""
        path = self._validate_path(pdf_path)
        warning_list = warnings if warnings is not None else []
        try:
            pages = self._extract_with_pdfplumber(path, progress, warning_list)
        except Exception as exc:  # noqa: BLE001 - 다른 PDF 엔진으로 파일 단위 복구
            if _is_password_error(exc):
                raise PdfAnalysisError("암호화된 PDF는 분석할 수 없습니다. 암호를 해제해 주세요.") from exc
            warning_list.append(f"pdfplumber 분석 실패 — PyMuPDF로 다시 시도합니다: {exc}")
            try:
                pages = self._extract_with_pymupdf(path, progress, warning_list)
            except Exception as fallback_exc:  # noqa: BLE001
                if _is_password_error(fallback_exc):
                    raise PdfAnalysisError("암호화된 PDF는 분석할 수 없습니다. 암호를 해제해 주세요.") from fallback_exc
                raise PdfAnalysisError(
                    f"PDF를 두 분석 엔진 모두 읽지 못했습니다: {fallback_exc}"
                ) from fallback_exc

        cleaned, removed = remove_repeated_margins(pages)
        if removed:
            warning_list.append(f"반복 머리말/꼬리말 {removed}개를 자동 제거했습니다.")
        self._rich_extractor.enrich(path, cleaned, warning_list)
        return cleaned

    def _extract_with_pdfplumber(
        self,
        path: Path,
        progress: Optional[ProgressCallback],
        warnings: list[str],
    ) -> list[PageText]:
        try:
            import pdfplumber
        except ImportError as exc:
            raise PdfDependencyError("pdfplumber가 설치되어 있지 않습니다.") from exc

        pages: list[PageText] = []
        with pdfplumber.open(path) as pdf:
            total = len(pdf.pages)
            if total == 0:
                raise PdfAnalysisError("페이지가 없는 PDF입니다.")
            for index, page in enumerate(pdf.pages, start=1):
                _emit(progress, index - 1, total, f"{index}/{total}페이지 텍스트 확인 중")
                try:
                    direct = self._extract_direct_text(page)
                except Exception as exc:  # noqa: BLE001 - 페이지별 대체 엔진 사용
                    warnings.append(f"{index}페이지 내장 텍스트 추출 실패 — 보조 엔진 사용: {exc}")
                    try:
                        direct = self._extract_pymupdf_text(path, index)
                    except Exception as fallback_exc:  # noqa: BLE001
                        warnings.append(f"{index}페이지 보조 텍스트 추출 실패: {fallback_exc}")
                        direct = ""
                # pdfplumber가 예외 없이 빈 문자열/cid 깨짐을 반환하는 경우도
                # PyMuPDF 텍스트 후보를 먼저 비교한 뒤에만 OCR로 넘어간다.
                if self._should_ocr(direct, text_quality(direct)):
                    try:
                        alternate = self._extract_pymupdf_text(path, index)
                        if (
                            text_quality(alternate) > text_quality(direct) + 0.03
                            or (not direct.strip() and alternate.strip())
                        ):
                            direct = alternate
                    except Exception:  # noqa: BLE001 - 선택적 보조 후보 실패는 OCR에서 복구
                        pass
                pages.append(
                    self._select_page_text(
                        direct,
                        render=lambda page=page, index=index: self._render_page(page, path, index),
                        page_number=index,
                        warnings=warnings,
                        progress=progress,
                        total=total,
                    )
                )
                _emit(progress, index, total, f"{index}/{total}페이지 처리 완료")
        return pages

    def _extract_with_pymupdf(
        self,
        path: Path,
        progress: Optional[ProgressCallback],
        warnings: list[str],
    ) -> list[PageText]:
        try:
            import pymupdf
        except ImportError as exc:
            raise PdfDependencyError(
                "PDF 분석 패키지가 없습니다. 실행하기.bat를 다시 실행해 주세요."
            ) from exc

        pages: list[PageText] = []
        with pymupdf.open(path) as document:
            if document.needs_pass:
                raise PdfAnalysisError("암호화된 PDF입니다.")
            total = document.page_count
            if total == 0:
                raise PdfAnalysisError("페이지가 없는 PDF입니다.")
            for index in range(1, total + 1):
                _emit(progress, index - 1, total, f"{index}/{total}페이지 보조 엔진 분석 중")
                page = document.load_page(index - 1)
                try:
                    direct = normalize_page_text(page.get_text("text", sort=True) or "")
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"{index}페이지 보조 텍스트 추출 실패: {exc}")
                    direct = ""
                pages.append(
                    self._select_page_text(
                        direct,
                        render=lambda page=page: self._pymupdf_page_image(page),
                        page_number=index,
                        warnings=warnings,
                        progress=progress,
                        total=total,
                    )
                )
                _emit(progress, index, total, f"{index}/{total}페이지 처리 완료")
        return pages

    def _select_page_text(
        self,
        direct: str,
        *,
        render: Callable[[], object],
        page_number: int,
        warnings: list[str],
        progress: Optional[ProgressCallback],
        total: int,
    ) -> PageText:
        direct = normalize_page_text(direct)
        direct_quality = text_quality(direct)
        selected_text = direct
        method = "text" if direct.strip() else "empty"
        selected_quality = direct_quality

        if self._should_ocr(direct, direct_quality):
            try:
                _emit(progress, page_number - 1, total, f"{page_number}/{total}페이지 OCR 인식 중")
                ocr_text = self._run_ocr(render())
                ocr_quality = text_quality(ocr_text)
                if self._prefer_ocr(direct, direct_quality, ocr_text, ocr_quality):
                    selected_text = ocr_text
                    selected_quality = ocr_quality
                    method = "ocr"
            except OcrUnavailableError as exc:
                warnings.append(f"{page_number}페이지 OCR 생략: {exc}")
            except Exception as exc:  # noqa: BLE001 - 페이지 단위 부분 성공 보장
                logger.exception("%d페이지 OCR 실패", page_number)
                warnings.append(f"{page_number}페이지 OCR 실패: {exc}")

        selected_text = normalize_page_text(selected_text)
        return PageText(
            page_number=page_number,
            text=selected_text,
            method=method if selected_text.strip() else "empty",
            quality=selected_quality if selected_text.strip() else 0.0,
        )

    @staticmethod
    def _extract_pymupdf_text(path: Path, page_number: int) -> str:
        import pymupdf

        with pymupdf.open(path) as document:
            page = document.load_page(page_number - 1)
            return normalize_page_text(page.get_text("text", sort=True) or "")

    def _pymupdf_page_image(self, page):
        from PIL import Image

        pixmap = page.get_pixmap(dpi=self.ocr_dpi, alpha=False)
        mode = "RGB" if pixmap.n < 4 else "RGBA"
        return Image.frombytes(mode, (pixmap.width, pixmap.height), pixmap.samples).convert("RGB")

    # ------------------------------------------------------------------
    @staticmethod
    def _validate_path(pdf_path: Path) -> Path:
        path = Path(pdf_path).expanduser()
        if not path.exists() or not path.is_file():
            raise PdfAnalysisError(f"PDF 파일을 찾을 수 없습니다: {path}")
        if path.suffix.lower() != ".pdf":
            raise PdfAnalysisError("PDF 파일만 분석할 수 있습니다.")
        return path.resolve()

    @staticmethod
    def _extract_direct_text(page) -> str:
        """일반 추출을 우선하고, 결과가 깨지면 레이아웃 보존 추출도 비교한다."""
        try:
            plain = page.extract_text(
                x_tolerance=2,
                y_tolerance=3,
                layout=False,
                use_text_flow=False,
            ) or ""
        except TypeError:
            # 구버전 pdfplumber 호환
            plain = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
        plain = normalize_page_text(plain)
        if text_quality(plain) >= 0.45:
            return plain
        try:
            layout = normalize_page_text(page.extract_text(layout=True) or "")
        except Exception:  # noqa: BLE001 - 보조 후보이므로 실패 시 일반 결과 사용
            return plain
        return layout if text_quality(layout) > text_quality(plain) else plain

    def _should_ocr(self, text: str, quality: float) -> bool:
        compact = re.sub(r"\s+", "", text)
        return len(compact) < self.min_text_chars or quality < self.text_quality_threshold

    @staticmethod
    def _prefer_ocr(direct: str, direct_quality: float, ocr: str, ocr_quality: float) -> bool:
        direct_len = len(re.sub(r"\s+", "", direct))
        ocr_len = len(re.sub(r"\s+", "", ocr))
        if not direct.strip():
            return bool(ocr.strip())
        if ocr_len < 10:
            return False
        # OCR이 확실히 더 읽기 좋거나, 내장 텍스트보다 유효 글자를 훨씬 많이 얻은 경우만 교체한다.
        return ocr_quality >= direct_quality + 0.08 or (
            ocr_quality >= direct_quality - 0.03 and ocr_len >= direct_len * 1.8
        )

    def _render_page(self, plumber_page, path: Path, page_number: int):
        """pdfplumber 렌더링을 우선하고 PyMuPDF를 예비 렌더러로 사용한다."""
        first_error: Optional[Exception] = None
        try:
            page_image = plumber_page.to_image(resolution=self.ocr_dpi, antialias=True)
            return page_image.original.convert("RGB")
        except Exception as exc:  # noqa: BLE001
            first_error = exc

        try:
            import pymupdf
            from PIL import Image

            with pymupdf.open(path) as document:
                page = document.load_page(page_number - 1)
                pixmap = page.get_pixmap(dpi=self.ocr_dpi, alpha=False)
                mode = "RGB" if pixmap.n < 4 else "RGBA"
                return Image.frombytes(mode, (pixmap.width, pixmap.height), pixmap.samples).convert("RGB")
        except ImportError as exc:
            raise PdfDependencyError(
                "스캔 PDF 렌더러가 없습니다. 실행하기.bat를 다시 실행해 주세요."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise PdfAnalysisError(
                f"{page_number}페이지를 OCR 이미지로 변환하지 못했습니다: {exc or first_error}"
            ) from exc

    def _run_ocr(self, image) -> str:
        try:
            import pytesseract
            from PIL import ImageOps
        except ImportError as exc:
            raise PdfDependencyError(
                "OCR Python 패키지가 없습니다. 실행하기.bat를 다시 실행해 주세요."
            ) from exc

        command = self.tesseract_cmd or shutil.which("tesseract") or ""
        if not command:
            raise OcrUnavailableError(
                "Tesseract가 설치되어 있지 않아 스캔 글자를 읽지 못했습니다."
            )
        if self.tesseract_cmd and not Path(self.tesseract_cmd).expanduser().exists():
            raise OcrUnavailableError("설정된 Tesseract 경로가 올바르지 않습니다.")
        pytesseract.pytesseract.tesseract_cmd = command

        prepared = ImageOps.autocontrast(image.convert("L"))
        candidates: list[str] = []
        for psm in (3, 6):
            try:
                text = pytesseract.image_to_string(
                    prepared,
                    lang=self.ocr_languages,
                    config=f"--oem 3 --psm {psm} -c preserve_interword_spaces=1",
                    timeout=self.ocr_timeout_sec,
                )
                candidates.append(normalize_page_text(text))
                if text_quality(text) >= 0.58:
                    break
            except pytesseract.TesseractNotFoundError as exc:
                raise OcrUnavailableError("Tesseract 실행 파일을 찾을 수 없습니다.") from exc
            except pytesseract.TesseractError as exc:
                detail = str(exc)
                if "Failed loading language" in detail or "Error opening data file" in detail:
                    raise OcrUnavailableError(
                        f"OCR 언어 데이터({self.ocr_languages})가 설치되어 있지 않습니다."
                    ) from exc
                raise PdfAnalysisError(f"Tesseract OCR 오류: {detail}") from exc
            except RuntimeError as exc:
                raise PdfAnalysisError(
                    f"OCR 제한 시간({self.ocr_timeout_sec}초)을 초과했습니다."
                ) from exc
        return max(candidates, key=text_quality, default="")


def normalize_page_text(text: str) -> str:
    """분석에 방해되는 제어문자/과도한 공백만 정리하고 원문 줄 구조는 보존한다."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    # PDF 폰트에서 자주 생기는 원문자 변형을 표준 선택지 문자로 통일한다.
    text = text.translate(str.maketrans("➀➁➂➃➄❶❷❸❹❺", "①②③④⑤①②③④⑤"))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
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


def text_quality(text: str) -> float:
    """OCR 여부 결정을 위한 언어 비종속적 텍스트 품질 휴리스틱."""
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return 0.0
    length_score = min(1.0, len(compact) / 80.0)
    readable = sum(
        char.isalnum()
        or "가" <= char <= "힣"
        or char in "①②③④⑤.,?!:;-'\"()[]{}<>~%+/=·…※"
        for char in compact
    )
    readable_ratio = readable / len(compact)
    control_count = sum(ord(char) < 32 for char in text if char not in "\n\t")
    garbage = text.count("�") + text.lower().count("(cid:") * 4 + control_count * 2
    garbage_penalty = min(0.65, garbage / max(1, len(compact)) * 4)
    return max(0.0, min(1.0, length_score * 0.28 + readable_ratio * 0.72 - garbage_penalty))


def remove_repeated_margins(pages: list[PageText]) -> tuple[list[PageText], int]:
    """충분히 긴 3페이지 이상 문서에서 같은 위치의 머리말/꼬리말만 제거한다."""
    if len(pages) < 3:
        return pages, 0
    candidates: Counter[str] = Counter()
    positions: list[list[tuple[int, str]]] = []
    for page in pages:
        lines = page.text.splitlines()
        if len(lines) < 8:
            positions.append([])
            continue
        indexed_positions = [
            (0, "top-0"),
            (1, "top-1"),
            (len(lines) - 2, "bottom-1"),
            (len(lines) - 1, "bottom-0"),
        ]
        page_candidates: list[tuple[int, str]] = []
        seen_on_page: set[str] = set()
        for index, relative_position in indexed_positions:
            line = lines[index]
            key = _margin_key(line)
            if not key or len(key) > 90 or _looks_structural(line):
                continue
            positioned_key = f"{relative_position}|{key}"
            page_candidates.append((index, positioned_key))
            if positioned_key not in seen_on_page:
                candidates[positioned_key] += 1
                seen_on_page.add(positioned_key)
        positions.append(page_candidates)

    threshold = max(3, math.ceil(len(pages) * 0.6))
    repeated = {key for key, count in candidates.items() if count >= threshold}
    if not repeated:
        return pages, 0

    removed = 0
    cleaned: list[PageText] = []
    for page, page_positions in zip(pages, positions, strict=True):
        remove_indices = {index for index, key in page_positions if key in repeated}
        lines = page.text.splitlines()
        removed += len(remove_indices)
        text = "\n".join(line for index, line in enumerate(lines) if index not in remove_indices).strip()
        cleaned.append(PageText(page.page_number, text, page.method, text_quality(text)))
    return cleaned, removed


def _looks_structural(line: str) -> bool:
    """문항/선택지/지문 지시문은 반복되어도 본문 구조이므로 제거하지 않는다."""
    text = line.strip()
    return bool(
        re.search(r"[①②③④⑤➀➁➂➃➄❶❷❸❹❺]", text)
        or re.match(r"^(?:문제\s*)?\d{1,3}\s*[.)번]", text)
        or ("글" in text and "물음" in text and ("읽" in text or "보" in text))
    )


def _margin_key(line: str) -> str:
    key = re.sub(r"\s+", " ", line).strip().lower()
    if re.fullmatch(r"[-–—]?\s*\d+\s*[-–—]?", key):
        return "<page-number>"
    return key


def _is_password_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(token in message for token in ("password", "encrypted", "authenticate", "암호"))


def _emit(progress: Optional[ProgressCallback], current: int, total: int, message: str) -> None:
    if progress:
        progress(current, total, message)


def _deduplicate(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item.strip()))
