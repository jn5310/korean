"""편집 및 검수 화면 (Editor View).

왼쪽  : 지문 리스트 (난이도 색상 태그, 필터/검색)
가운데: 지문·문제 상세 편집
오른쪽: 미리보기 (Step 5 에서 실제 PDF 레이아웃 미리보기로 교체 예정)
"""
from __future__ import annotations

import html
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QTextBrowser, QVBoxLayout, QWidget,
)

from ..config import DEFAULT_PASSAGE_TYPES, DEFAULT_QUESTION_TYPES, DIFFICULTY_LEVELS
from ..models import Passage, Question
from .state import AppState
from .styles import difficulty_color
from .widgets import DifficultyCombo, difficulty_badge_html, make_type_combo

ROLE_ID = Qt.ItemDataRole.UserRole
_ALL = "__all__"


def color_icon(color: str, size: int = 12) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(QColor(color))
    return QIcon(pix)


def passage_preview_html(p: Passage) -> str:
    """간이 HTML 미리보기 — 1지문 1페이지 구성을 흉내낸다."""
    esc = lambda s: html.escape(s or "").replace("\n", "<br>")  # noqa: E731
    parts = [
        f"<h3>{esc(p.display_title)} {difficulty_badge_html(p.difficulty)}</h3>",
        f'<div style="color:#607d8b;">{esc(p.passage_type)}</div>' if p.passage_type else "",
        f'<div style="border:1px solid #cfd8dc;padding:8px;margin:6px 0;line-height:150%;">{esc(p.text)}</div>',
    ]
    for i, q in enumerate(p.questions, start=1):
        number = q.number or str(i)
        parts.append(f'<p style="margin-top:10px;"><b>{esc(number)}.</b> {esc(q.stem)} '
                     f'{difficulty_badge_html(q.difficulty) if q.difficulty else ""}</p>')
        if q.choices:
            parts.append("<div style='margin-left:14px;'>" +
                         "<br>".join(esc(c) for c in q.choices) + "</div>")
    return "".join(parts)


class EditorView(QWidget):
    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._loading = False
        self._current: Optional[Passage] = None
        self._current_q: Optional[Question] = None

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(300)
        self._preview_timer.timeout.connect(self._update_preview)

        self._build_ui()
        state.library_changed.connect(self.reload_list)
        self.reload_list()

    # ================================================================ UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_center())
        splitter.addWidget(self._build_right())
        splitter.setSizes([260, 620, 420])
        root.addWidget(splitter)

    def _build_left(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("<b>지문 목록</b>"))

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("검색 (제목/본문)")
        self.search_edit.textChanged.connect(self.reload_list)
        lay.addWidget(self.search_edit)

        self.diff_filter = QComboBox()
        self.diff_filter.addItem("전체 난이도", _ALL)
        self.diff_filter.addItem("미분류", None)
        for level, label in DIFFICULTY_LEVELS.items():
            self.diff_filter.addItem(label, level)
        self.diff_filter.currentIndexChanged.connect(self.reload_list)
        lay.addWidget(self.diff_filter)

        self.passage_list = QListWidget()
        self.passage_list.currentItemChanged.connect(self._on_passage_selected)
        lay.addWidget(self.passage_list, 1)

        row = QHBoxLayout()
        add_btn = QPushButton("+ 새 지문")
        add_btn.clicked.connect(self._add_passage)
        self.del_btn = QPushButton("삭제")
        self.del_btn.clicked.connect(self._delete_passage)
        row.addWidget(add_btn)
        row.addWidget(self.del_btn)
        lay.addLayout(row)
        return w

    def _build_center(self) -> QWidget:
        self.center = QSplitter(Qt.Orientation.Vertical)

        # --- 지문 편집 ---
        pbox = QGroupBox("지문")
        pl = QVBoxLayout(pbox)
        form = QFormLayout()
        self.title_edit = QLineEdit()
        self.title_edit.textEdited.connect(self._on_passage_edited)
        form.addRow("제목", self.title_edit)

        meta = QHBoxLayout()
        self.ptype_combo = make_type_combo(DEFAULT_PASSAGE_TYPES)
        self.ptype_combo.currentTextChanged.connect(self._on_passage_edited)
        self.pdiff_combo = DifficultyCombo()
        self.pdiff_combo.currentIndexChanged.connect(self._on_passage_difficulty_changed)
        self.ai_btn = QPushButton("AI 난이도 판별")
        self.ai_btn.setEnabled(False)
        self.ai_btn.setToolTip("Step 3 에서 구현 예정")
        meta.addWidget(QLabel("유형"))
        meta.addWidget(self.ptype_combo, 1)
        meta.addWidget(QLabel("난이도"))
        meta.addWidget(self.pdiff_combo)
        meta.addWidget(self.ai_btn)
        form.addRow(meta)
        self.source_label = QLabel()
        self.source_label.setObjectName("Muted")
        self.source_label.setWordWrap(True)
        form.addRow("출처", self.source_label)
        pl.addLayout(form)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText("지문 본문을 입력/수정하세요.")
        self.text_edit.textChanged.connect(self._on_passage_edited)
        pl.addWidget(self.text_edit, 1)
        self.center.addWidget(pbox)

        # --- 문제 편집 ---
        qbox = QGroupBox("문제")
        ql = QHBoxLayout(qbox)
        left = QVBoxLayout()
        self.question_list = QListWidget()
        self.question_list.currentRowChanged.connect(self._on_question_selected)
        left.addWidget(self.question_list, 1)
        qbtns = QHBoxLayout()
        for text, slot in (("+", self._add_question), ("−", self._delete_question),
                           ("▲", lambda: self._move_question(-1)), ("▼", lambda: self._move_question(1))):
            b = QPushButton(text)
            b.setFixedWidth(36)
            b.clicked.connect(slot)
            qbtns.addWidget(b)
        left.addLayout(qbtns)
        ql.addLayout(left, 1)

        self.q_form_widget = QWidget()
        qf = QFormLayout(self.q_form_widget)
        qmeta = QHBoxLayout()
        self.q_number = QLineEdit()
        self.q_number.setFixedWidth(60)
        self.q_type = make_type_combo(DEFAULT_QUESTION_TYPES)
        self.q_diff = DifficultyCombo()
        qmeta.addWidget(self.q_number)
        qmeta.addWidget(QLabel("유형"))
        qmeta.addWidget(self.q_type, 1)
        qmeta.addWidget(QLabel("난이도"))
        qmeta.addWidget(self.q_diff)
        qf.addRow("번호", qmeta)
        self.q_stem = QPlainTextEdit()
        self.q_stem.setMaximumHeight(70)
        qf.addRow("발문", self.q_stem)
        self.q_choices = QPlainTextEdit()
        self.q_choices.setPlaceholderText("한 줄에 선택지 하나 (예: ① …)")
        self.q_choices.setMaximumHeight(110)
        qf.addRow("선택지", self.q_choices)
        self.q_answer = QLineEdit()
        qf.addRow("정답", self.q_answer)
        self.q_explanation = QPlainTextEdit()
        self.q_explanation.setMaximumHeight(70)
        qf.addRow("해설", self.q_explanation)
        ql.addWidget(self.q_form_widget, 2)

        self.q_number.textEdited.connect(self._on_question_edited)
        self.q_type.currentTextChanged.connect(self._on_question_edited)
        self.q_diff.currentIndexChanged.connect(self._on_question_difficulty_changed)
        self.q_stem.textChanged.connect(self._on_question_edited)
        self.q_choices.textChanged.connect(self._on_question_edited)
        self.q_answer.textEdited.connect(self._on_question_edited)
        self.q_explanation.textChanged.connect(self._on_question_edited)

        self.center.addWidget(qbox)
        self.center.setSizes([380, 320])
        return self.center

    def _build_right(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("<b>미리보기</b>"))
        self.preview = QTextBrowser()
        lay.addWidget(self.preview, 1)
        note = QLabel("※ 실제 PDF 페이지 레이아웃 미리보기는 Step 5 에서 제공됩니다.")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        return w

    # ========================================================= 목록
    def _passage_item_text(self, p: Passage) -> str:
        lv = f"Lv.{p.difficulty}" if p.difficulty else "미분류"
        return f"[{lv}] {p.display_title}  ({len(p.questions)}문항)"

    def _style_item(self, item: QListWidgetItem, p: Passage) -> None:
        item.setText(self._passage_item_text(p))
        item.setIcon(color_icon(difficulty_color(p.difficulty)))

    def reload_list(self) -> None:
        selected_id = self._current.id if self._current else None
        diff = self.diff_filter.currentData()
        passages = self.state.library.filter_passages(
            difficulties=None if diff == _ALL else {diff},
            keyword=self.search_edit.text(),
        )
        self.passage_list.blockSignals(True)
        self.passage_list.clear()
        reselect_row = -1
        for row, p in enumerate(passages):
            item = QListWidgetItem()
            item.setData(ROLE_ID, p.id)
            self._style_item(item, p)
            self.passage_list.addItem(item)
            if p.id == selected_id:
                reselect_row = row
        self.passage_list.blockSignals(False)

        if reselect_row >= 0:
            self.passage_list.setCurrentRow(reselect_row)
        elif passages:
            self.passage_list.setCurrentRow(0)
        else:
            self._show_passage(None)

    def _refresh_current_item(self) -> None:
        item = self.passage_list.currentItem()
        if item and self._current:
            self._style_item(item, self._current)

    def _on_passage_selected(self, current: Optional[QListWidgetItem], _prev=None) -> None:
        p = self.state.library.get_passage(current.data(ROLE_ID)) if current else None
        self._show_passage(p)

    # ========================================================= 지문 편집
    def _show_passage(self, p: Optional[Passage]) -> None:
        self._loading = True
        self._current = p
        enabled = p is not None
        for w in (self.title_edit, self.ptype_combo, self.pdiff_combo, self.text_edit,
                  self.question_list, self.del_btn):
            w.setEnabled(enabled)
        self.title_edit.setText(p.title if p else "")
        self.ptype_combo.setCurrentText(p.passage_type if p else "")
        self.pdiff_combo.set_value(p.difficulty if p else None)
        self.text_edit.setPlainText(p.text if p else "")
        if p and p.source_file:
            pages = ", ".join(map(str, p.source_pages))
            self.source_label.setText(f"{p.source_file}" + (f" (p.{pages})" if pages else ""))
        else:
            self.source_label.setText("직접 입력")
        if p and p.difficulty_reason:
            self.pdiff_combo.setToolTip(f"AI 판별 근거: {p.difficulty_reason}")
        else:
            self.pdiff_combo.setToolTip("")
        self._reload_questions()
        self._loading = False
        self._update_preview()

    def _on_passage_edited(self, *_args) -> None:
        if self._loading or not self._current:
            return
        p = self._current
        p.title = self.title_edit.text()
        p.passage_type = self.ptype_combo.currentText().strip()
        p.text = self.text_edit.toPlainText()
        p.touch()
        self._after_edit()

    def _on_passage_difficulty_changed(self, *_args) -> None:
        if self._loading or not self._current:
            return
        self._current.difficulty = self.pdiff_combo.value()
        self._current.difficulty_source = "manual"
        self._current.touch()
        self._after_edit()

    def _after_edit(self) -> None:
        self.state.mark_dirty(notify_library=False)
        self._refresh_current_item()
        self._preview_timer.start()

    def _add_passage(self) -> None:
        p = Passage(title="새 지문")
        self.state.library.add_passage(p)
        self._current = p
        # 필터 때문에 새 지문이 안 보이는 일이 없도록 초기화
        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)
        self.diff_filter.blockSignals(True)
        self.diff_filter.setCurrentIndex(0)
        self.diff_filter.blockSignals(False)
        self.state.mark_dirty()   # → reload_list 가 self._current 를 다시 선택
        self.title_edit.setFocus()
        self.title_edit.selectAll()

    def _delete_passage(self) -> None:
        if not self._current:
            return
        answer = QMessageBox.question(
            self, "지문 삭제",
            f"'{self._current.display_title}' 지문과 딸린 문제 {len(self._current.questions)}개를 삭제할까요?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.state.library.remove_passage(self._current.id)
        self._current = None
        self.state.mark_dirty()

    # ========================================================= 문제 편집
    def _question_item_text(self, idx: int, q: Question) -> str:
        head = q.number or str(idx + 1)
        stem = q.stem.strip().replace("\n", " ")
        return f"{head}. {stem[:24]}{'…' if len(stem) > 24 else ''}" if stem else f"{head}. (발문 없음)"

    def _reload_questions(self, select: int = 0) -> None:
        was_loading = self._loading
        self._loading = True
        self.question_list.clear()
        qs = self._current.questions if self._current else []
        for i, q in enumerate(qs):
            self.question_list.addItem(self._question_item_text(i, q))
        self._loading = was_loading
        if qs:
            self.question_list.setCurrentRow(max(0, min(select, len(qs) - 1)))
        else:
            self._show_question(None)

    def _on_question_selected(self, row: int) -> None:
        qs = self._current.questions if self._current else []
        self._show_question(qs[row] if 0 <= row < len(qs) else None)

    def _show_question(self, q: Optional[Question]) -> None:
        was_loading = self._loading
        self._loading = True
        self._current_q = q
        self.q_form_widget.setEnabled(q is not None)
        self.q_number.setText(q.number if q else "")
        self.q_type.setCurrentText(q.question_type if q else "")
        self.q_diff.set_value(q.difficulty if q else None)
        self.q_stem.setPlainText(q.stem if q else "")
        self.q_choices.setPlainText("\n".join(q.choices) if q else "")
        self.q_answer.setText(q.answer if q else "")
        self.q_explanation.setPlainText(q.explanation if q else "")
        self._loading = was_loading

    def _on_question_edited(self, *_args) -> None:
        if self._loading or not self._current_q:
            return
        q = self._current_q
        q.number = self.q_number.text().strip()
        q.question_type = self.q_type.currentText().strip()
        q.stem = self.q_stem.toPlainText()
        q.choices = [c.strip() for c in self.q_choices.toPlainText().splitlines() if c.strip()]
        q.answer = self.q_answer.text().strip()
        q.explanation = self.q_explanation.toPlainText()
        self._after_question_edit()

    def _on_question_difficulty_changed(self, *_args) -> None:
        if self._loading or not self._current_q:
            return
        self._current_q.difficulty = self.q_diff.value()
        self._current_q.difficulty_source = "manual"
        self._after_question_edit()

    def _after_question_edit(self) -> None:
        row = self.question_list.currentRow()
        if row >= 0 and self._current_q:
            self.question_list.item(row).setText(self._question_item_text(row, self._current_q))
        if self._current:
            self._current.touch()
        self._after_edit()

    def _add_question(self) -> None:
        if not self._current:
            return
        qs = self._current.questions
        qs.append(Question(number=str(len(qs) + 1)))
        self._reload_questions(select=len(qs) - 1)
        self._after_edit()
        self.q_stem.setFocus()

    def _delete_question(self) -> None:
        row = self.question_list.currentRow()
        if not self._current or row < 0:
            return
        del self._current.questions[row]
        self._reload_questions(select=row)
        self._after_edit()

    def _move_question(self, delta: int) -> None:
        row = self.question_list.currentRow()
        if not self._current or row < 0:
            return
        qs = self._current.questions
        target = row + delta
        if not 0 <= target < len(qs):
            return
        qs[row], qs[target] = qs[target], qs[row]
        self._reload_questions(select=target)
        self._after_edit()

    # ========================================================= 미리보기
    def _update_preview(self) -> None:
        if self._current:
            self.preview.setHtml(passage_preview_html(self._current))
        else:
            self.preview.setHtml("<p style='color:#90a4ae;'>지문을 선택하거나 '+ 새 지문'으로 추가하세요.</p>")
