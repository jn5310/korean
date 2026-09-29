"""ReportLab 기반 1지문-새 페이지 PDF 출판 엔진.

각 지문은 반드시 새 페이지에서 시작하며, 한 지문이 길면 읽을 수 있는 글자 크기를
유지한 채 다음 페이지로 자연스럽게 이어진다. 굵게·기울임·밑줄·색상과 관리되는
이미지 자산을 ReportLab Platypus flowable로 변환한다.
"""
from __future__ import annotations

import html
import os
import re
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

from ..assets import AssetStore, asset_display_size
from ..models import MediaAsset, Passage, Question


class PdfExportError(Exception):
    """사용자에게 보여 줄 수 있는 PDF 출판 오류."""


@dataclass
class ExportOptions:
    title: str = ""
    include_answers: bool = False
    include_explanations: bool = False
    font_path: str = ""
    bold_font_path: str = ""
    font_size: float = 10.5
    page_size: str = "A4"
    show_difficulty: bool = True


@dataclass
class ExportResult:
    path: Path
    page_count: int
    passage_count: int
    question_count: int
    warnings: list[str] = field(default_factory=list)


class PdfExporter:
    def __init__(
        self,
        options: ExportOptions | None = None,
        *,
        asset_store: Optional[AssetStore] = None,
    ) -> None:
        self.options = options or ExportOptions()
        self.asset_store = asset_store

    def export(self, passages: list[Passage], output_path: Path) -> ExportResult:
        if not passages:
            raise PdfExportError("내보낼 지문이 없습니다.")
        target = Path(output_path).expanduser()
        if target.suffix.lower() != ".pdf":
            target = target.with_suffix(".pdf")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PdfExportError(f"PDF 저장 폴더를 만들 수 없습니다: {exc}") from exc
        temporary = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.tmp.pdf")
        warnings: list[str] = []

        try:
            reportlab = _reportlab_modules()
            font_name, bold_name = _register_fonts(
                reportlab,
                self.options.font_path,
                self.options.bold_font_path,
                warnings,
            )
            page_size = reportlab["pagesizes"].A4 if self.options.page_size.upper() == "A4" else reportlab["pagesizes"].LETTER
            doc = reportlab["SimpleDocTemplate"](
                str(temporary),
                pagesize=page_size,
                rightMargin=42,
                leftMargin=42,
                topMargin=46,
                bottomMargin=44,
                title=self.options.title or "맞춤형 시험지",
                author="지문·문제 재구성 스튜디오",
            )
            styles = _build_styles(reportlab, font_name, bold_name, self.options.font_size)
            story = self._build_story(passages, doc, styles, reportlab, warnings)
            page_count = [0]

            def draw_page(canvas, document) -> None:
                page_count[0] = max(page_count[0], canvas.getPageNumber())
                canvas.saveState()
                canvas.setFont(font_name, 8)
                canvas.setFillColor(reportlab["colors"].HexColor("#607D8B"))
                canvas.drawCentredString(page_size[0] / 2, 22, str(canvas.getPageNumber()))
                canvas.restoreState()

            doc.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
            os.replace(temporary, target)
            return ExportResult(
                path=target,
                page_count=page_count[0],
                passage_count=len(passages),
                question_count=sum(len(passage.questions) for passage in passages),
                warnings=list(dict.fromkeys(warnings)),
            )
        except PdfExportError:
            _remove_file(temporary)
            raise
        except Exception as exc:  # noqa: BLE001 - ReportLab/Pillow 예외를 사용자 메시지로 통합
            _remove_file(temporary)
            raise PdfExportError(f"PDF를 만들지 못했습니다: {exc}") from exc

    def _build_story(self, passages, doc, styles, rl, warnings):
        story = []
        Paragraph = rl["Paragraph"]
        Spacer = rl["Spacer"]
        PageBreak = rl["PageBreak"]
        CondPageBreak = rl["CondPageBreak"]

        if self.options.title:
            story.append(Paragraph(_plain_markup(self.options.title), styles["document_title"]))
            story.append(Spacer(1, 10))

        for passage_index, passage in enumerate(passages, start=1):
            if passage_index > 1:
                story.append(PageBreak())
            heading = f"{passage_index}. {passage.display_title}"
            if self.options.show_difficulty and passage.difficulty:
                heading += f"  [난이도 {passage.difficulty}]"
            story.append(Paragraph(_plain_markup(heading), styles["passage_title"]))
            if passage.passage_type:
                story.append(Paragraph(_plain_markup(f"유형: {passage.passage_type}"), styles["metadata"]))
            story.append(Spacer(1, 5))
            story.append(Paragraph(_rich_markup(passage.text, passage.text_html), styles["passage"]))
            story.extend(self._image_flowables(passage.images, doc, rl, warnings))
            story.append(Spacer(1, 8))

            for question_index, question in enumerate(passage.questions, start=1):
                story.append(CondPageBreak(92))
                number = question.number or str(question_index)
                suffix = f"  [난이도 {question.difficulty}]" if self.options.show_difficulty and question.difficulty else ""
                stem = _rich_markup(question.stem, question.stem_html)
                story.append(Paragraph(f"<b>{html.escape(number)}.</b> {stem}{html.escape(suffix)}", styles["question"]))
                story.extend(self._images_for_anchor(question, "stem", doc, rl, warnings))
                for choice_index, choice in enumerate(question.choices):
                    rich = question.choices_html[choice_index] if choice_index < len(question.choices_html) else ""
                    story.append(Paragraph(_rich_markup(choice, rich), styles["choice"]))
                    story.extend(self._images_for_anchor(question, f"choice:{choice_index}", doc, rl, warnings))
                story.extend(self._images_for_anchor(question, "after", doc, rl, warnings))
                story.append(Spacer(1, 7))

        if self.options.include_answers:
            story.append(PageBreak())
            story.append(Paragraph("정답 및 해설", styles["passage_title"]))
            for passage_index, passage in enumerate(passages, start=1):
                story.append(Paragraph(_plain_markup(f"{passage_index}. {passage.display_title}"), styles["answer_heading"]))
                for question_index, question in enumerate(passage.questions, start=1):
                    number = question.number or str(question_index)
                    answer = question.answer.strip() or "미입력"
                    answer_markup = f"{html.escape(number)}. 정답: {_plain_markup(answer)}"
                    if self.options.include_explanations and question.explanation.strip():
                        answer_markup += f"<br/>해설: {_plain_markup(question.explanation)}"
                    story.append(Paragraph(answer_markup, styles["answer"]))
        return story

    def _images_for_anchor(self, question: Question, anchor: str, doc, rl, warnings):
        return self._image_flowables(
            [asset for asset in question.images if asset.anchor == anchor],
            doc,
            rl,
            warnings,
        )

    def _image_flowables(self, assets: list[MediaAsset], doc, rl, warnings):
        output = []
        for asset in assets:
            path = self.asset_store.resolve(asset) if self.asset_store else None
            if path is None:
                warnings.append(f"그림 파일을 찾지 못해 제외했습니다: {asset.alt}")
                continue
            try:
                image = rl["Image"](str(path))
                # 원본 PDF에서 차지하던 실제 크기에 맞춘다(본문 글자 크기와 비슷한 비율).
                width, height = asset_display_size(
                    asset,
                    image.imageWidth,
                    image.imageHeight,
                    points_scale=1.15,
                    pixel_scale=0.75,
                    max_width=doc.width,
                    max_height=doc.height * 0.8,
                )
                if width <= 0 or height <= 0:
                    raise ValueError("그림 크기를 알 수 없습니다.")
                image.drawWidth = width
                image.drawHeight = height
                image.hAlign = "CENTER"
                output.extend([rl["Spacer"](1, 5), image, rl["Spacer"](1, 5)])
            except Exception as exc:  # noqa: BLE001 - 손상 그림 하나가 전체 PDF를 막지 않음
                warnings.append(f"그림을 출력하지 못해 제외했습니다({asset.alt}): {exc}")
        return output


class _MarkupConverter(HTMLParser):
    """제한 HTML을 ReportLab Paragraph XML subset으로 변환."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self.stack: list[list[str]] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "head", "object", "iframe", "svg"}:
            self.skip_depth += 1
            return
        if tag == "img":
            return
        if self.skip_depth:
            return
        if tag in {"br", "p", "div", "li"}:
            self.output.append("<br/>")
            return
        opened: list[str] = []
        if tag in {"b", "strong"}:
            opened = ["b"]
        elif tag in {"i", "em"}:
            opened = ["i"]
        elif tag == "u":
            opened = ["u"]
        elif tag == "span":
            style = dict(attrs).get("style") or ""
            declarations = _style_declarations(style)
            if declarations.get("font-weight") in {"600", "700", "bold"}:
                opened.append("b")
            if declarations.get("font-style") == "italic":
                opened.append("i")
            if declarations.get("text-decoration") == "underline":
                opened.append("u")
            color = declarations.get("color", "")
            if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                opened.append(f'font color="{color}"')
        for item in opened:
            self.output.append(f"<{item}>")
        self.stack.append(opened)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "head", "object", "iframe", "svg"}:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag == "img":
            return
        if self.skip_depth or tag not in {"b", "strong", "i", "em", "u", "span"} or not self.stack:
            return
        for item in reversed(self.stack.pop()):
            self.output.append(f"</{item.split()[0]}>")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.output.append(html.escape(_clean_controls(data)))

    def result(self) -> str:
        while self.stack:
            for item in reversed(self.stack.pop()):
                self.output.append(f"</{item.split()[0]}>")
        return "".join(self.output)


def _rich_markup(plain: str, rich: str) -> str:
    if not rich:
        return _plain_markup(plain)
    parser = _MarkupConverter()
    parser.feed(rich)
    parser.close()
    result = parser.result()
    return result or _plain_markup(plain)


def _plain_markup(value: str) -> str:
    clean = _clean_controls(value or "")
    return html.escape(clean).replace("\n", "<br/>")


def _style_declarations(style: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for declaration in str(style).split(";"):
        if ":" in declaration:
            name, value = declaration.split(":", 1)
            result[name.strip().lower()] = value.strip().lower()
    return result


def _clean_controls(value: str) -> str:
    return "".join(char for char in value if char in "\n\t" or ord(char) >= 32)


def _reportlab_modules() -> dict:
    try:
        from reportlab.lib import colors, pagesizes
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import CondPageBreak, Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer
    except ImportError as exc:
        raise PdfExportError("ReportLab이 설치되어 있지 않습니다. 실행하기.bat를 다시 실행해 주세요.") from exc
    return {
        "colors": colors,
        "pagesizes": pagesizes,
        "TA_CENTER": TA_CENTER,
        "TA_LEFT": TA_LEFT,
        "ParagraphStyle": ParagraphStyle,
        "pdfmetrics": pdfmetrics,
        "UnicodeCIDFont": UnicodeCIDFont,
        "TTFont": TTFont,
        "CondPageBreak": CondPageBreak,
        "Image": Image,
        "PageBreak": PageBreak,
        "Paragraph": Paragraph,
        "SimpleDocTemplate": SimpleDocTemplate,
        "Spacer": Spacer,
    }


def _register_fonts(rl: dict, regular_path: str, bold_path: str, warnings: list[str]) -> tuple[str, str]:
    pdfmetrics = rl["pdfmetrics"]
    TTFont = rl["TTFont"]
    candidates = [
        regular_path,
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "malgun.ttf"),
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "NanumGothic.ttf"),
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf",
        "/Library/Fonts/AppleGothic.ttf",
    ]
    regular = next((Path(path) for path in candidates if path and Path(path).is_file()), None)
    if regular:
        regular_name = "KoreanStudioRegular"
        bold_name = "KoreanStudioBold"
        pdfmetrics.registerFont(TTFont(regular_name, str(regular)))
        bold_candidates = [
            bold_path,
            str(regular).replace("malgun.ttf", "malgunbd.ttf"),
            str(regular).replace("NanumGothic.ttf", "NanumGothicBold.ttf"),
            str(regular).replace("Regular.ttf", "Bold.ttf"),
        ]
        bold = next((Path(path) for path in bold_candidates if path and Path(path).is_file()), regular)
        if bold == regular:
            warnings.append("별도 굵은 글꼴을 찾지 못해 PDF의 굵은 글씨가 일반 글꼴로 표시될 수 있습니다.")
        pdfmetrics.registerFont(TTFont(bold_name, str(bold)))
        pdfmetrics.registerFontFamily(regular_name, normal=regular_name, bold=bold_name, italic=regular_name, boldItalic=bold_name)
        return regular_name, bold_name

    # OS 폰트를 찾지 못해도 PDF 생성을 막지 않는 ReportLab 한국어 CID fallback.
    cid_name = "HYSMyeongJo-Medium"
    try:
        pdfmetrics.registerFont(rl["UnicodeCIDFont"](cid_name))
        pdfmetrics.registerFontFamily(cid_name, normal=cid_name, bold=cid_name, italic=cid_name, boldItalic=cid_name)
        warnings.append("시스템 TTF를 찾지 못해 CID 대체 글꼴을 사용했습니다. 굵은 글씨가 약하게 보일 수 있습니다.")
    except Exception as exc:  # noqa: BLE001
        raise PdfExportError(
            "한글 글꼴을 찾지 못했습니다. 설정에서 malgun.ttf 또는 NanumGothic.ttf를 지정해 주세요."
        ) from exc
    return cid_name, cid_name


def _build_styles(rl: dict, font_name: str, bold_name: str, font_size: float) -> dict:
    ParagraphStyle = rl["ParagraphStyle"]
    TA_CENTER = rl["TA_CENTER"]
    TA_LEFT = rl["TA_LEFT"]
    size = max(8.0, min(18.0, float(font_size)))
    base = {
        "fontName": font_name,
        "fontSize": size,
        "leading": size * 1.58,
        "wordWrap": "CJK",
        "splitLongWords": True,
    }

    def body_style(name: str, **updates):
        values = {**base, **updates}
        return ParagraphStyle(name, **values)

    return {
        "document_title": ParagraphStyle("DocumentTitle", fontName=bold_name, fontSize=size + 7, leading=size + 11, alignment=TA_CENTER, spaceAfter=8),
        "passage_title": ParagraphStyle("PassageTitle", fontName=bold_name, fontSize=size + 3, leading=size + 7, spaceAfter=5, keepWithNext=True),
        "metadata": body_style("Metadata", fontSize=max(8, size - 1), textColor=rl["colors"].HexColor("#607D8B"), spaceAfter=3),
        "passage": body_style("Passage", alignment=TA_LEFT, borderColor=rl["colors"].HexColor("#CFD8DC"), borderWidth=0.7, borderPadding=8, spaceAfter=7),
        "question": body_style("Question", spaceBefore=5, spaceAfter=4, keepWithNext=True),
        "choice": body_style("Choice", leftIndent=15, firstLineIndent=0, spaceAfter=2),
        "answer_heading": ParagraphStyle("AnswerHeading", fontName=bold_name, fontSize=size + 1, leading=size + 5, spaceBefore=8, spaceAfter=3),
        "answer": body_style("Answer", leftIndent=10, spaceAfter=3),
    }


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
