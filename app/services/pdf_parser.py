"""PDF 페이지 텍스트·서식·그림 추출과 스캔 페이지 OCR.

분석 전략
---------
1. PyMuPDF로 글자 하나하나의 좌표·글꼴·색과 도형·이미지 위치를 읽는다.
   (공백 자동 추가를 끄고 실제 공백 글자만 받는다)
2. 겹쳐 찍은 가짜 굵게 글자를 하나로 합치고, 2단 편집·문단·띄어쓰기를
   `pdf_text_layout` 엔진으로 복원한다.
3. <보기> 상자·표·그림은 라벨까지 포함해 잘라낸 이미지로 저장하고, 본문에는
   위치를 표시하는 자리표시자만 남긴다(그 안의 글자는 AI 참고용 숨김 텍스트).
4. 텍스트가 없거나 글자가 깨진 페이지에만 Tesseract OCR을 적용한다.
5. PyMuPDF가 없으면 pdfplumber 글자 좌표로 같은 엔진을 사용한다.

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

from ..assets import AssetError, AssetStore
from ..models import MediaAsset, Passage
from .pdf_regions import detect_regions, mark_underlines, parse_drawings
from .pdf_text_layout import (
    Glyph, LayoutRegion, PageInput, PageLayout, build_lines, consensus_gutter,
    detect_gutter, gutter_fits, layout_document, prepare_glyphs, strip_placeholders,
)
from .rich_content import RichContentProjector, RichRun, render_rich_html, styles_to_runs

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]  # (현재, 전체, 메시지)

_REGION_DPI = 200
_MAX_REGIONS_PER_PAGE = 24
_BOLD_FONT_TOKENS = ("bold", "black", "heavy", "semibold", "demibold", "extrabold")


class PdfAnalysisError(Exception):
    """사용자에게 그대로 표시할 수 있는 PDF 분석 오류."""


class PdfDependencyError(PdfAnalysisError):
    """Python 패키지 또는 외부 OCR 엔진이 없는 경우."""


class OcrUnavailableError(PdfDependencyError):
    """Tesseract 실행 파일/언어 데이터가 없는 경우."""


@dataclass
class PageText:
    page_number: int          # 1부터 시작
    text: str                 # 그림 영역은 자리표시자 줄로 들어 있다
    method: str               # "text" | "ocr" | "empty"
    quality: float = 0.0      # 0.0 ~ 1.0 추출 품질 추정치
    rich_html: str = ""
    runs: list[RichRun] = field(default_factory=list)
    images: list[MediaAsset] = field(default_factory=list)
    join_next: bool = False   # 마지막 문단이 다음 페이지 첫 줄로 이어짐
    join_space: bool = False  # 이어질 때 사이에 공백이 필요함


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
        return sum(not strip_placeholders(p.text).strip() and not p.images for p in self.pages)

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
        if not pages or all(not strip_placeholders(page.text).strip() for page in pages):
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
        engine = _load_pymupdf()
        pages: Optional[list[PageText]] = None
        if engine is not None:
            try:
                pages = self._extract_with_pymupdf(engine, path, progress, warning_list)
            except PdfAnalysisError:
                raise
            except Exception as exc:  # noqa: BLE001 - 다른 엔진으로 파일 단위 복구
                if _is_password_error(exc):
                    raise PdfAnalysisError("암호화된 PDF는 분석할 수 없습니다. 암호를 해제해 주세요.") from exc
                logger.exception("PyMuPDF 분석 실패: %s", path)
                warning_list.append(f"PyMuPDF 분석 실패 — 기본 엔진으로 다시 시도합니다: {exc}")
        else:
            warning_list.append(
                "PyMuPDF가 없어 <보기> 그림·글자 서식 없이 기본 엔진으로 분석했습니다. "
                "실행하기.bat를 다시 실행해 주세요."
            )
        if pages is None:
            try:
                pages = self._extract_with_pdfplumber(path, progress, warning_list)
            except PdfAnalysisError:
                raise
            except Exception as exc:  # noqa: BLE001
                if _is_password_error(exc):
                    raise PdfAnalysisError("암호화된 PDF는 분석할 수 없습니다. 암호를 해제해 주세요.") from exc
                raise PdfAnalysisError(f"PDF를 읽지 못했습니다: {exc}") from exc

        removed = remove_repeated_margins(pages)
        if removed:
            warning_list.append(f"스캔 페이지의 반복 머리말/꼬리말 {removed}개를 자동 제거했습니다.")
        _sanitize_page_joins(pages)
        return pages

    # ------------------------------------------------------------------
    # PyMuPDF (기본 엔진)

    def _extract_with_pymupdf(
        self,
        engine,
        path: Path,
        progress: Optional[ProgressCallback],
        warnings: list[str],
    ) -> list[PageText]:
        with engine.open(path) as document:
            if document.needs_pass:
                raise PdfAnalysisError("암호화된 PDF는 분석할 수 없습니다. 암호를 해제해 주세요.")
            total = document.page_count
            if total == 0:
                raise PdfAnalysisError("페이지가 없는 PDF입니다.")
            flags = _text_flags(engine)
            inputs: list[PageInput] = []
            page_assets: list[dict[str, MediaAsset]] = []
            for index in range(total):
                number = index + 1
                _emit(progress, index, total * 2, f"{number}/{total}페이지 글자·그림 위치 분석 중")
                page = document.load_page(index)
                try:
                    page_input, assets = self._pymupdf_page_input(engine, page, number, flags, warnings)
                except Exception as exc:  # noqa: BLE001 - 페이지 단위 부분 성공 보장
                    logger.exception("%d페이지 글자 위치 분석 실패", number)
                    warnings.append(f"{number}페이지 글자 위치 분석 실패 — 보조 엔진 사용: {exc}")
                    page_input, assets = self._plumber_page_input(path, number), {}
                inputs.append(page_input)
                page_assets.append(assets)

            _apply_consensus_gutter(inputs)
            _emit(progress, total, total * 2, "문단·띄어쓰기 정리 중")
            layouts = layout_document(inputs)
            duplicates = sum(page.duplicate_glyphs for page in inputs)
            if duplicates:
                warnings.append(f"겹쳐 인쇄된 중복 글자(가짜 굵게) {duplicates}개를 정리했습니다.")
            margins = sum(layout.removed_margin_lines for layout in layouts)
            if margins:
                warnings.append(f"반복 머리말/꼬리말·쪽 번호 {margins}줄을 자동 제거했습니다.")

            pages: list[PageText] = []
            for index, layout in enumerate(layouts):
                page = document.load_page(index)
                pages.append(
                    self._select_page_text(
                        layout,
                        page_assets[index],
                        render=lambda page=page: self._pymupdf_page_image(page),
                        snapshot=lambda page=page, number=index + 1: self._pymupdf_snapshot(page, number),
                        warnings=warnings,
                        progress=progress,
                        total=total,
                    )
                )
                _emit(progress, total + index + 1, total * 2, f"{index + 1}/{total}페이지 처리 완료")
        return pages

    def _pymupdf_page_input(self, engine, page, number: int, flags: int, warnings: list[str]):
        # PyMuPDF는 회전 전 좌표를 돌려주므로 /Rotate 페이지는 모두 화면(회전 후) 좌표로 바꿔 처리한다.
        transform = _Affine.for_page(page)
        width, height = float(page.rect.width), float(page.rect.height)
        raw = page.get_text("rawdict", flags=flags)
        glyphs, duplicates = prepare_glyphs(_glyphs_from_rawdict(raw, engine, transform))
        gutter = detect_gutter(glyphs, width, height)
        try:
            drawings = page.get_drawings()
        except Exception:  # noqa: BLE001 - 도형이 깨져도 텍스트는 계속 분석
            drawings = []
        try:
            image_boxes = [
                info["bbox"] for info in (page.get_image_info() or [])
                if isinstance(info, dict) and info.get("bbox") is not None
            ]
        except Exception:  # noqa: BLE001
            image_boxes = []
        if transform is not None:
            drawings = [_rotate_drawing(drawing, transform) for drawing in drawings if isinstance(drawing, dict)]
            image_boxes = [box for box in (transform.rect(value) for value in image_boxes) if box is not None]
        prims = parse_drawings(drawings, width, height)
        lines = build_lines(glyphs, gutter)
        detection = detect_regions(lines, glyphs, prims, image_boxes, width, height)
        mark_underlines(glyphs, prims, detection.boxes)

        regions: list[LayoutRegion] = []
        assets: dict[str, MediaAsset] = {}
        if self.asset_store is not None:
            for candidate in detection.regions[:_MAX_REGIONS_PER_PAGE]:
                asset = self._render_region(engine, page, candidate.bbox, candidate.kind, candidate.alt, number, warnings)
                if asset is None:
                    continue
                regions.append(LayoutRegion(asset.id, candidate.bbox, candidate.kind))
                assets[asset.id] = asset
        page_input = PageInput(
            page_number=number,
            width=width,
            height=height,
            glyphs=glyphs,
            regions=regions,
            frames=detection.frames,
            gutter=gutter,
            gutter_detected=True,
            duplicate_glyphs=duplicates,
        )
        return page_input, assets

    def _render_region(self, engine, page, bbox, kind: str, alt: str, number: int, warnings: list[str]) -> Optional[MediaAsset]:
        if self.asset_store is None:
            return None
        try:
            # bbox는 이미 화면(회전 후) 좌표이며 get_pixmap의 clip도 같은 좌표계를 쓴다.
            clip = engine.Rect(*bbox)
            pixmap = page.get_pixmap(clip=clip, dpi=_REGION_DPI, alpha=False)
            data = pixmap.tobytes("png")
            asset = self.asset_store.import_bytes(
                data,
                source_page=number,
                bbox=[float(value) for value in bbox],
                alt=f"{number}페이지 {alt}",
                kind=kind,
            )
        except AssetError as exc:
            warnings.append(f"{number}페이지 {alt} 그림 저장 실패 — 글자로 유지합니다: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001 - 렌더링 실패 시 해당 영역을 글자로 유지
            warnings.append(f"{number}페이지 {alt} 그림 추출 실패 — 글자로 유지합니다: {exc}")
            return None
        return asset

    def _pymupdf_page_image(self, page):
        from PIL import Image

        pixmap = page.get_pixmap(dpi=self.ocr_dpi, alpha=False)
        mode = "RGB" if pixmap.n < 4 else "RGBA"
        return Image.frombytes(mode, (pixmap.width, pixmap.height), pixmap.samples).convert("RGB")

    def _pymupdf_snapshot(self, page, number: int) -> Optional[MediaAsset]:
        if self.asset_store is None:
            return None
        pixmap = page.get_pixmap(dpi=120, alpha=False)
        rect = page.rect
        return self.asset_store.import_bytes(
            pixmap.tobytes("png"),
            source_page=number,
            bbox=[0.0, 0.0, float(rect.width), float(rect.height)],
            anchor="passage",
            alt=f"{number}페이지 원본(스캔)",
            kind="page",
        )

    # ------------------------------------------------------------------
    # pdfplumber (보조 엔진)

    def _extract_with_pdfplumber(
        self,
        path: Path,
        progress: Optional[ProgressCallback],
        warnings: list[str],
    ) -> list[PageText]:
        try:
            import pdfplumber
        except ImportError as exc:
            raise PdfDependencyError(
                "PDF 분석 패키지가 없습니다. 실행하기.bat를 다시 실행해 주세요."
            ) from exc

        with pdfplumber.open(path) as pdf:
            total = len(pdf.pages)
            if total == 0:
                raise PdfAnalysisError("페이지가 없는 PDF입니다.")
            inputs: list[PageInput] = []
            for index, page in enumerate(pdf.pages, start=1):
                _emit(progress, index - 1, total * 2, f"{index}/{total}페이지 텍스트 확인 중")
                try:
                    inputs.append(_plumber_input_from_page(page, index))
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"{index}페이지 내장 텍스트 추출 실패: {exc}")
                    inputs.append(PageInput(index, float(page.width or 595), float(page.height or 842)))
            _apply_consensus_gutter(inputs)
            layouts = layout_document(inputs)
            pages: list[PageText] = []
            for index, (page, layout) in enumerate(zip(pdf.pages, layouts, strict=True), start=1):
                pages.append(
                    self._select_page_text(
                        layout,
                        {},
                        render=lambda page=page, index=index: self._render_plumber_page(page, path, index),
                        snapshot=None,
                        warnings=warnings,
                        progress=progress,
                        total=total,
                    )
                )
                _emit(progress, total + index, total * 2, f"{index}/{total}페이지 처리 완료")
        return pages

    @staticmethod
    def _plumber_page_input(path: Path, number: int) -> PageInput:
        try:
            import pdfplumber

            with pdfplumber.open(path) as pdf:
                return _plumber_input_from_page(pdf.pages[number - 1], number)
        except Exception:  # noqa: BLE001 - 보조 엔진도 실패하면 OCR 경로로 넘어간다
            return PageInput(number, 595.0, 842.0)

    def _render_plumber_page(self, plumber_page, path: Path, page_number: int):
        """pdfplumber 렌더링을 우선하고 PyMuPDF를 예비 렌더러로 사용한다."""
        first_error: Optional[Exception] = None
        try:
            page_image = plumber_page.to_image(resolution=self.ocr_dpi, antialias=True)
            return page_image.original.convert("RGB")
        except Exception as exc:  # noqa: BLE001
            first_error = exc
        engine = _load_pymupdf()
        if engine is None:
            raise PdfDependencyError("스캔 PDF 렌더러가 없습니다. 실행하기.bat를 다시 실행해 주세요.")
        try:
            with engine.open(path) as document:
                return self._pymupdf_page_image(document.load_page(page_number - 1))
        except Exception as exc:  # noqa: BLE001
            raise PdfAnalysisError(
                f"{page_number}페이지를 OCR 이미지로 변환하지 못했습니다: {exc or first_error}"
            ) from exc

    # ------------------------------------------------------------------
    # 페이지 결과 선택(내장 텍스트 vs OCR)

    def _select_page_text(
        self,
        layout: PageLayout,
        assets: dict[str, MediaAsset],
        *,
        render: Callable[[], object],
        snapshot: Optional[Callable[[], Optional[MediaAsset]]],
        warnings: list[str],
        progress: Optional[ProgressCallback],
        total: int,
    ) -> PageText:
        page_number = layout.page_number
        content = layout.content_text
        direct_quality = text_quality(content)

        if self._should_ocr(content, direct_quality):
            try:
                self._tesseract_command()  # 설치되지 않았으면 페이지를 렌더링하기 전에 건너뛴다.
                _emit(progress, total + page_number - 1, total * 2, f"{page_number}/{total}페이지 OCR 인식 중")
                ocr_text = self._run_ocr(render())
                ocr_quality = text_quality(ocr_text)
                if self._prefer_ocr(content, direct_quality, ocr_text, ocr_quality):
                    images: list[MediaAsset] = []
                    if snapshot is not None:
                        try:
                            shot = snapshot()
                            if shot is not None:
                                images.append(shot)
                        except Exception as exc:  # noqa: BLE001
                            warnings.append(f"{page_number}페이지 OCR 원본 이미지 보존 실패: {exc}")
                    return PageText(
                        page_number=page_number,
                        text=ocr_text,
                        method="ocr" if ocr_text.strip() else "empty",
                        quality=ocr_quality if ocr_text.strip() else 0.0,
                        images=images,
                    )
            except OcrUnavailableError as exc:
                warnings.append(f"{page_number}페이지 OCR 생략: {exc}")
            except PdfAnalysisError as exc:
                warnings.append(f"{page_number}페이지 OCR 실패: {exc}")
            except Exception as exc:  # noqa: BLE001 - 페이지 단위 부분 성공 보장
                logger.exception("%d페이지 OCR 실패", page_number)
                warnings.append(f"{page_number}페이지 OCR 실패: {exc}")

        for asset_id, hidden in layout.region_texts.items():
            asset = assets.get(asset_id)
            if asset is not None:
                asset.text = hidden[:20_000]
        runs = styles_to_runs(layout.styles)
        has_content = bool(content.strip()) or bool(assets)
        return PageText(
            page_number=page_number,
            text=layout.text if has_content else "",
            method="text" if has_content else "empty",
            quality=direct_quality if has_content else 0.0,
            rich_html=render_rich_html(layout.text, runs) if has_content else "",
            runs=runs if has_content else [],
            images=list(assets.values()) if has_content else [],
            join_next=layout.join_next,
            join_space=layout.join_space,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _validate_path(pdf_path: Path) -> Path:
        path = Path(pdf_path).expanduser()
        if not path.exists() or not path.is_file():
            raise PdfAnalysisError(f"PDF 파일을 찾을 수 없습니다: {path}")
        if path.suffix.lower() != ".pdf":
            raise PdfAnalysisError("PDF 파일만 분석할 수 있습니다.")
        return path.resolve()

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

    def _tesseract_command(self) -> str:
        if self.tesseract_cmd:
            if not Path(self.tesseract_cmd).expanduser().exists():
                raise OcrUnavailableError("설정된 Tesseract 경로가 올바르지 않습니다.")
            return self.tesseract_cmd
        command = shutil.which("tesseract") or ""
        if not command:
            raise OcrUnavailableError(
                "Tesseract가 설치되어 있지 않아 스캔 글자를 읽지 못했습니다."
            )
        return command

    def _run_ocr(self, image) -> str:
        try:
            import pytesseract
            from PIL import ImageOps
        except ImportError as exc:
            raise PdfDependencyError(
                "OCR Python 패키지가 없습니다. 실행하기.bat를 다시 실행해 주세요."
            ) from exc

        command = self._tesseract_command()
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


# ---------------------------------------------------------------------------
# PDF 엔진 어댑터


def _load_pymupdf():
    try:
        import pymupdf

        return pymupdf
    except ImportError:
        pass
    try:
        import fitz

        return fitz if hasattr(fitz, "open") else None
    except ImportError:
        return None


def _text_flags(engine) -> int:
    """공백 자동 추가(TEXT_INHIBIT_SPACES)를 끄고 실제 글자만 받는 추출 옵션."""
    flags = (
        int(getattr(engine, "TEXT_PRESERVE_LIGATURES", 1))
        | int(getattr(engine, "TEXT_PRESERVE_WHITESPACE", 2))
        | int(getattr(engine, "TEXT_INHIBIT_SPACES", 8))
        | int(getattr(engine, "TEXT_MEDIABOX_CLIP", 64))
    )
    # 신버전은 같은 위치에 겹쳐 찍은 가짜 굵게·밑줄을 스스로 판별한다.
    flags |= int(getattr(engine, "TEXT_COLLECT_STYLES", 0) or 0)
    return flags


class _Affine:
    """PyMuPDF Matrix(a, b, c, d, e, f) 좌표 변환(회전 전 → 화면 좌표)."""

    def __init__(self, a: float, b: float, c: float, d: float, e: float, f: float) -> None:
        self.a, self.b, self.c, self.d, self.e, self.f = a, b, c, d, e, f

    @classmethod
    def for_page(cls, page) -> Optional["_Affine"]:
        if int(getattr(page, "rotation", 0) or 0) % 360 == 0:
            return None
        matrix = getattr(page, "rotation_matrix", None)
        try:
            values = [float(getattr(matrix, name)) for name in "abcdef"]
        except (TypeError, ValueError, AttributeError):
            return None
        return cls(*values)

    def point(self, x: float, y: float) -> tuple[float, float]:
        return self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f

    def direction(self, dx: float, dy: float) -> tuple[float, float]:
        return self.a * dx + self.c * dy, self.b * dx + self.d * dy

    def rect(self, value) -> Optional[tuple[float, float, float, float]]:
        try:
            if hasattr(value, "x0"):
                x0, y0, x1, y1 = float(value.x0), float(value.y0), float(value.x1), float(value.y1)
            else:
                x0, y0, x1, y1 = (float(item) for item in value)
        except (TypeError, ValueError):
            return None
        corners = [self.point(x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
        xs = [x for x, _y in corners]
        ys = [y for _x, y in corners]
        return (min(xs), min(ys), max(xs), max(ys))


def _rotate_drawing(drawing: dict, transform: _Affine) -> dict:
    def point(value):
        try:
            if hasattr(value, "x"):
                return transform.point(float(value.x), float(value.y))
            return transform.point(float(value[0]), float(value[1]))
        except (TypeError, ValueError, IndexError):
            return (float("nan"), float("nan"))

    items = []
    for item in drawing.get("items", []) or []:
        if not item:
            continue
        operator = item[0]
        if operator in ("l", "c"):
            items.append((operator, *[point(value) for value in item[1:]]))
        elif operator == "re" and len(item) >= 2:
            rect = transform.rect(item[1])
            if rect is not None:
                items.append(("re", rect, *item[2:]))
        elif operator == "qu" and len(item) >= 2:
            quad = item[1]
            corners = [getattr(quad, name, None) for name in ("ul", "ur", "ll", "lr")]
            if all(corner is not None for corner in corners):
                items.append(("qu", [point(corner) for corner in corners]))
            else:
                try:
                    items.append(("qu", [point(corner) for corner in list(quad)[:4]]))
                except TypeError:
                    continue
    rotated = dict(drawing)
    rotated["items"] = items
    rect = transform.rect(drawing.get("rect", (0, 0, 0, 0)))
    if rect is not None:
        rotated["rect"] = rect
    return rotated


def _glyphs_from_rawdict(raw, engine, transform: Optional["_Affine"] = None) -> list[Glyph]:
    low_level = getattr(engine, "mupdf", None)
    bold_bit = int(getattr(low_level, "FZ_STEXT_BOLD", 0) or 0)
    filled_bit = int(getattr(low_level, "FZ_STEXT_FILLED", 0) or 0)
    stroked_bit = int(getattr(low_level, "FZ_STEXT_STROKED", 0) or 0)
    underline_bit = int(getattr(low_level, "FZ_STEXT_UNDERLINE", 0) or 0)
    glyphs: list[Glyph] = []
    blocks = raw.get("blocks", []) if isinstance(raw, dict) else []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []) or []:
            direction = line.get("dir") or (1.0, 0.0)
            try:
                dx, dy = float(direction[0]), float(direction[1])
            except (TypeError, ValueError, IndexError):
                dx, dy = 1.0, 0.0
            if transform is not None:
                dx, dy = transform.direction(dx, dy)
            if line.get("wmode", 0) or abs(dy) > 0.2 or dx < 0.8:
                continue  # 세로쓰기·회전 글자(여백의 저작권 문구 등)는 본문이 아니다.
            for span in line.get("spans", []) or []:
                size = _finite(span.get("size"), 0.0)
                flags = int(span.get("flags") or 0)
                font = str(span.get("font") or "")
                lower_font = font.lower()
                bold = bool(flags & 16) or any(token in lower_font for token in _BOLD_FONT_TOKENS)
                italic = bool(flags & 2) or "italic" in lower_font or "oblique" in lower_font
                underline = False
                char_flags = span.get("char_flags")
                if isinstance(char_flags, int) and char_flags >= 0:
                    if bold_bit and char_flags & bold_bit:
                        bold = True
                    if filled_bit and stroked_bit and char_flags & filled_bit and char_flags & stroked_bit:
                        bold = True
                    if underline_bit and char_flags & underline_bit:
                        underline = True
                color = _color_hex(span.get("color"))
                for char in span.get("chars", []) or []:
                    if not isinstance(char, dict) or char.get("synthetic"):
                        continue
                    value = str(char.get("c") or "")
                    try:
                        x0, y0, x1, y1 = (float(item) for item in char["bbox"])
                        origin = char["origin"]
                        ox, oy = float(origin[0]), float(origin[1])
                    except (KeyError, TypeError, ValueError, IndexError):
                        continue
                    if transform is not None:
                        rotated = transform.rect((x0, y0, x1, y1))
                        if rotated is None:
                            continue
                        x0, y0, x1, y1 = rotated
                        ox, oy = transform.point(ox, oy)
                    glyph_size = size if size > 0 else max(1.0, y1 - y0)
                    for offset, piece in enumerate(value):
                        if len(value) > 1:
                            step = (x1 - x0) / len(value)
                            px0 = x0 + step * offset
                            glyphs.append(Glyph(piece, px0, y0, px0 + step, y1, glyph_size, px0, oy,
                                                bold, italic, underline, color))
                        else:
                            glyphs.append(Glyph(piece, x0, y0, x1, y1, glyph_size, ox, oy,
                                                bold, italic, underline, color))
    return glyphs


def _plumber_input_from_page(page, number: int) -> PageInput:
    try:
        deduped = page.dedupe_chars(tolerance=1)
    except Exception:  # noqa: BLE001 - 구버전 pdfplumber
        deduped = page
    glyphs: list[Glyph] = []
    for char in getattr(deduped, "chars", []) or []:
        if char.get("upright") is False:
            continue
        value = str(char.get("text") or "")
        try:
            x0 = float(char["x0"])
            x1 = float(char["x1"])
            top = float(char["top"])
            bottom = float(char["bottom"])
        except (KeyError, TypeError, ValueError):
            continue
        size = _finite(char.get("size"), bottom - top) or max(1.0, bottom - top)
        font = str(char.get("fontname") or "")
        lower_font = font.lower()
        bold = any(token in lower_font for token in _BOLD_FONT_TOKENS)
        italic = "italic" in lower_font or "oblique" in lower_font
        color = _plumber_color(char.get("non_stroking_color"))
        baseline = bottom - 0.21 * size
        if not value:
            continue
        step = (x1 - x0) / len(value)
        for offset, piece in enumerate(value):
            px0 = x0 + step * offset
            glyphs.append(Glyph(piece, px0, top, px0 + step, bottom, size, px0, baseline,
                                bold, italic, False, color))
    prepared, duplicates = prepare_glyphs(glyphs)
    width = float(getattr(page, "width", 595) or 595)
    height = float(getattr(page, "height", 842) or 842)
    return PageInput(
        number, width, height, prepared,
        gutter=detect_gutter(prepared, width, height),
        gutter_detected=True,
        duplicate_glyphs=duplicates,
    )


def _sanitize_page_joins(pages: list[PageText]) -> None:
    """페이지 사이 문단 연결은 양쪽 모두 좌표로 배치한 텍스트 페이지일 때만 유지한다.

    다음 페이지가 OCR 결과로 바뀌면 레이아웃이 판단한 '첫 줄'이 더 이상 존재하지 않는다.
    """
    for index, page in enumerate(pages):
        following = pages[index + 1] if index + 1 < len(pages) else None
        if following is None or page.method != "text" or following.method != "text":
            page.join_next = False
            page.join_space = False


def _apply_consensus_gutter(inputs: list[PageInput]) -> None:
    """단 판정이 애매한 페이지에 문서 공통 단 여백을 적용한다."""
    text_pages = [page for page in inputs if page.glyphs]
    common = consensus_gutter([page.gutter for page in text_pages], len(text_pages))
    if common is None:
        return
    for page in text_pages:
        if page.gutter is None and gutter_fits(page.glyphs, common):
            page.gutter = common
            page.gutter_detected = True


def _color_hex(value) -> str:
    try:
        number = int(value) & 0xFFFFFF
    except (TypeError, ValueError, OverflowError):
        return ""
    red, green, blue = (number >> 16) & 255, (number >> 8) & 255, number & 255
    if max(red, green, blue) <= 0x40 and max(red, green, blue) - min(red, green, blue) <= 0x20:
        return ""  # 검정에 가까운 인쇄용 색은 기본 글자색으로 본다.
    return f"#{number:06x}"


def _plumber_color(value) -> str:
    if value is None:
        return ""
    try:
        components = [float(item) for item in (value if isinstance(value, (list, tuple)) else [value])]
    except (TypeError, ValueError):
        return ""
    if len(components) == 1:
        red = green = blue = components[0]
    elif len(components) == 3:
        red, green, blue = components
    elif len(components) == 4:
        cyan, magenta, yellow, black = components
        red = (1 - cyan) * (1 - black)
        green = (1 - magenta) * (1 - black)
        blue = (1 - yellow) * (1 - black)
    else:
        return ""
    number = (int(max(0, min(1, red)) * 255) << 16) | (int(max(0, min(1, green)) * 255) << 8) | int(max(0, min(1, blue)) * 255)
    return _color_hex(number)


def _finite(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


# ---------------------------------------------------------------------------
# 텍스트 유틸


def normalize_page_text(text: str) -> str:
    """분석에 방해되는 제어문자/과도한 공백만 정리하고 원문 줄 구조는 보존한다."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    # 자리표시자와 겹치지 않도록 사용자 영역 문자를 치환한다.
    text = text.replace("\ue000", "□").replace("\ue001", "□")
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


def remove_repeated_margins(pages: list[PageText]) -> int:
    """스캔(OCR) 페이지에서 같은 위치에 반복되는 머리말/꼬리말만 제거한다.

    텍스트 PDF 페이지의 머리말·쪽 번호는 레이아웃 엔진이 좌표로 이미 제거한다.
    """
    ocr_pages = [page for page in pages if page.method == "ocr"]
    if len(ocr_pages) < 3:
        return 0
    candidates: Counter[str] = Counter()
    positions: list[list[tuple[int, str]]] = []
    for page in ocr_pages:
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

    threshold = max(3, math.ceil(len(ocr_pages) * 0.6))
    repeated = {key for key, count in candidates.items() if count >= threshold}
    if not repeated:
        return 0

    removed = 0
    for page, page_positions in zip(ocr_pages, positions, strict=True):
        remove_indices = {index for index, key in page_positions if key in repeated}
        if not remove_indices:
            continue
        lines = page.text.splitlines()
        removed += len(remove_indices)
        page.text = "\n".join(line for index, line in enumerate(lines) if index not in remove_indices).strip()
        page.quality = text_quality(page.text)
    return removed


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
