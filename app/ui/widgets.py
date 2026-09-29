"""여러 화면에서 재사용하는 작은 위젯들."""
from __future__ import annotations

import re
from html import escape as _escape_html
from html.parser import HTMLParser

from PyQt6.QtCore import QSignalBlocker, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QTextCharFormat
from PyQt6.QtWidgets import (
    QColorDialog, QComboBox, QFrame, QHBoxLayout, QLabel, QTextEdit, QToolButton,
    QVBoxLayout, QWidget,
)

from ..config import DIFFICULTY_LEVELS
from .styles import difficulty_color


class Card(QFrame):
    """제목이 있는 흰색 카드 컨테이너."""

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 16)
        self.body.setSpacing(10)
        if title:
            label = QLabel(title)
            label.setObjectName("CardTitle")
            self.body.addWidget(label)


class DifficultyCombo(QComboBox):
    """미분류(None) + 1~5단계 선택 콤보."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.addItem("미분류", None)
        for level, label in DIFFICULTY_LEVELS.items():
            self.addItem(label, level)

    def value(self):
        return self.currentData()

    def set_value(self, level) -> None:
        # findData(None) 은 Qt 버전에 따라 동작이 달라 직접 비교한다
        idx = next((i for i in range(self.count()) if self.itemData(i) == level), 0)
        self.setCurrentIndex(idx)


def difficulty_badge_html(level) -> str:
    text = f"Lv.{level}" if level else "미분류"
    return (f'<span style="background:{difficulty_color(level)};color:white;'
            f'padding:1px 6px;border-radius:3px;">{text}</span>')


def make_type_combo(options: list[str], parent: QWidget | None = None) -> QComboBox:
    """제안 목록 + 자유 입력 가능한 유형 콤보."""
    combo = QComboBox(parent)
    combo.setEditable(True)
    combo.addItem("")
    combo.addItems(options)
    return combo


# ---------------------------------------------------------------------------
# 제한된 리치 텍스트 편집기


class _RichHtmlSanitizer(HTMLParser):
    """Qt/ReportLab 공통 subset만 보존하고 외부 리소스·스크립트를 제거."""

    INLINE = {"strong", "b", "em", "i", "u", "span"}
    SKIP = {"script", "style", "head", "object", "iframe", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self.stack: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self.SKIP:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in {"br"}:
            self.output.append("<br/>")
        elif tag in {"p", "div", "li"}:
            if self.output and not self.output[-1].endswith("<br/>"):
                self.output.append("<br/>")
        elif tag in self.INLINE:
            normalized = {"b": "strong", "i": "em"}.get(tag, tag)
            if normalized == "span":
                style = _safe_inline_style(dict(attrs).get("style", ""))
                if not style:
                    self.stack.append("")
                    return
                self.output.append(f'<span style="{style}">')
            else:
                self.output.append(f"<{normalized}>")
            self.stack.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth or tag not in self.INLINE or not self.stack:
            return
        normalized = self.stack.pop()
        if normalized:
            self.output.append(f"</{normalized}>")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.output.append(_escape_html(data))

    def result(self) -> str:
        while self.stack:
            tag = self.stack.pop()
            if tag:
                self.output.append(f"</{tag}>")
        return "".join(self.output)


def _safe_inline_style(style: str) -> str:
    allowed: list[str] = []
    for declaration in str(style).split(";"):
        if ":" not in declaration:
            continue
        name, value = (part.strip().lower() for part in declaration.split(":", 1))
        if name == "color" and re.fullmatch(r"#[0-9a-f]{6}", value):
            allowed.append(f"color:{value}")
        elif name == "font-weight" and value in {"600", "700", "bold"}:
            allowed.append("font-weight:700")
        elif name == "font-style" and value == "italic":
            allowed.append("font-style:italic")
        elif name == "text-decoration" and value == "underline":
            allowed.append("text-decoration:underline")
    return ";".join(dict.fromkeys(allowed))


def sanitize_rich_html(value: str) -> str:
    if not value:
        return ""
    parser = _RichHtmlSanitizer()
    parser.feed(str(value))
    parser.close()
    return parser.result()


def _char_format_html(text: str, fmt: QTextCharFormat) -> str:
    content = _escape_html(text).replace("\u2028", "<br/>").replace("\n", "<br/>")
    styles: list[str] = []
    if fmt.fontWeight() >= QFont.Weight.DemiBold:
        styles.append("font-weight:700")
    if fmt.fontItalic():
        styles.append("font-style:italic")
    if fmt.fontUnderline():
        styles.append("text-decoration:underline")
    color = fmt.foreground().color()
    if color.isValid() and color.name().lower() not in {"#000000", "#111111"}:
        styles.append(f"color:{color.name().lower()}")
    return f'<span style="{";".join(styles)}">{content}</span>' if styles else content


def _block_html(block) -> str:
    output: list[str] = []
    iterator = block.begin()
    while not iterator.atEnd():
        fragment = iterator.fragment()
        if fragment.isValid():
            output.append(_char_format_html(fragment.text(), fragment.charFormat()))
        iterator += 1
    return "".join(output)


class RichTextEdit(QTextEdit):
    """평문 projection과 제한 HTML fragment를 동시에 다루는 편집기."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptRichText(True)
        self.setObjectName("RichTextEdit")

    def set_content(self, plain: str, rich_html: str = "") -> None:
        safe = sanitize_rich_html(rich_html)
        if safe:
            self.setHtml(f"<div>{safe}</div>")
        else:
            self.setPlainText(plain or "")

    def set_blocks(self, plain_blocks: list[str], html_blocks: list[str]) -> None:
        blocks: list[str] = []
        for index, plain in enumerate(plain_blocks):
            rich = html_blocks[index] if index < len(html_blocks) else ""
            blocks.append(sanitize_rich_html(rich) or _escape_html(plain).replace("\n", "<br/>"))
        self.setHtml("".join(f"<div>{block}</div>" for block in blocks))

    def safe_html(self) -> str:
        blocks = [_block_html(block) for block in _iter_blocks(self.document())]
        value = "<br/>".join(blocks)
        plain_html = _escape_html(self.plain_projection()).replace("\n", "<br/>")
        return "" if value == plain_html else value

    def block_contents(self, *, include_empty: bool = False) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        for block in _iter_blocks(self.document()):
            plain = block.text().replace("\u2028", "\n").replace("\ufffc", "")
            rich = sanitize_rich_html(_block_html(block))
            if include_empty or plain or rich:
                plain_html = _escape_html(plain)
                result.append((plain, "" if rich == plain_html else rich))
        return result

    def plain_projection(self) -> str:
        return self.toPlainText().replace("\ufffc", "")

    def insertFromMimeData(self, source) -> None:  # noqa: N802 - Qt override
        # 외부 HTML/file URL을 그대로 받아 로컬 파일을 노출하지 않도록 평문만 붙여넣는다.
        if source.hasText():
            self.insertPlainText(source.text())


def _iter_blocks(document):
    block = document.begin()
    while block.isValid():
        yield block
        block = block.next()


class RichTextToolbar(QWidget):
    image_requested = pyqtSignal()
    remove_image_requested = pyqtSignal()

    def __init__(self, editor: RichTextEdit, *, allow_image: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.editor = editor
        self.setObjectName("RichTextToolbar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.bold_btn = self._button("B", "굵게", self._toggle_bold)
        self.bold_btn.setStyleSheet("font-weight:bold;")
        self.underline_btn = self._button("U", "밑줄", self._toggle_underline)
        self.underline_btn.setStyleSheet("text-decoration:underline;")
        self.color_btn = self._button("색", "글자색", self._choose_color, checkable=False)
        clear_btn = self._button("지우기", "선택 영역 서식 지우기", self._clear_format, checkable=False)
        layout.addWidget(self.bold_btn)
        layout.addWidget(self.underline_btn)
        layout.addWidget(self.color_btn)
        layout.addWidget(clear_btn)
        if allow_image:
            image_btn = self._button("그림 추가", "현재 항목에 그림 추가", self.image_requested.emit, checkable=False)
            manage_btn = self._button("그림 삭제", "현재 항목의 그림 선택 삭제", self.remove_image_requested.emit, checkable=False)
            layout.addWidget(image_btn)
            layout.addWidget(manage_btn)
        layout.addStretch()
        editor.currentCharFormatChanged.connect(self._sync_format)

    def _button(self, text, tooltip, slot, checkable=True):
        button = QToolButton(self)
        button.setText(text)
        button.setToolTip(tooltip)
        button.setCheckable(checkable)
        button.clicked.connect(slot)
        return button

    def _merge(self, fmt: QTextCharFormat) -> None:
        cursor = self.editor.textCursor()
        cursor.mergeCharFormat(fmt)
        self.editor.mergeCurrentCharFormat(fmt)
        self.editor.setFocus()

    def _toggle_bold(self, checked: bool) -> None:
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Bold if checked else QFont.Weight.Normal)
        self._merge(fmt)

    def _toggle_underline(self, checked: bool) -> None:
        fmt = QTextCharFormat()
        fmt.setFontUnderline(checked)
        self._merge(fmt)

    def _choose_color(self) -> None:
        color = QColorDialog.getColor(self.editor.textColor(), self, "글자색 선택")
        if color.isValid():
            fmt = QTextCharFormat()
            fmt.setForeground(color)
            self._merge(fmt)
            self.color_btn.setStyleSheet(f"color:{color.name()};")

    def _clear_format(self) -> None:
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Normal)
        fmt.setFontItalic(False)
        fmt.setFontUnderline(False)
        fmt.setForeground(QColor("#000000"))
        self._merge(fmt)

    def _sync_format(self, fmt: QTextCharFormat) -> None:
        bold_blocker = QSignalBlocker(self.bold_btn)
        underline_blocker = QSignalBlocker(self.underline_btn)
        self.bold_btn.setChecked(fmt.fontWeight() >= QFont.Weight.DemiBold)
        self.underline_btn.setChecked(fmt.fontUnderline())
        del bold_blocker, underline_blocker
        color = fmt.foreground().color()
        self.color_btn.setStyleSheet(f"color:{color.name()};" if color.isValid() else "")
