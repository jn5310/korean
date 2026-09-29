"""시험지 생성 화면 (Test Creator).

왼쪽 : 조건별(지문 유형/문제 유형/난이도/키워드) 지문 탐색기
오른쪽: 시험지 작업 공간 — 클릭 또는 드래그 앤 드롭으로 담고 순서 배치
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Optional

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from ..models import ExamSet, Passage
from ..services.pdf_exporter import ExportOptions, ExportResult, PdfExportError, PdfExporter
from .editor_view import color_icon
from .state import AppState
from .styles import difficulty_color

ROLE_ID = Qt.ItemDataRole.UserRole
_ALL = "__all__"


def _passage_label(p: Passage) -> str:
    lv = f"Lv.{p.difficulty}" if p.difficulty else "미분류"
    ptype = f" · {p.passage_type}" if p.passage_type else ""
    return f"[{lv}] {p.display_title}{ptype}  ({len(p.questions)}문항)"


class ExportOptionsDialog(QDialog):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("PDF 출판 설정")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        info = QLabel(
            "각 지문은 새 페이지에서 시작합니다. 지문과 문제가 한 페이지보다 길면 "
            "읽기 쉬운 크기를 유지하며 다음 페이지로 이어집니다."
        )
        info.setWordWrap(True)
        info.setObjectName("Muted")
        layout.addWidget(info)
        form = QFormLayout()
        self.title_edit = QLineEdit(title)
        form.addRow("문서 제목", self.title_edit)
        self.font_size = QDoubleSpinBox()
        self.font_size.setRange(8.0, 18.0)
        self.font_size.setSingleStep(0.5)
        self.font_size.setValue(10.5)
        self.font_size.setSuffix(" pt")
        form.addRow("본문 글자 크기", self.font_size)
        self.include_answers = QCheckBox("마지막에 정답지 추가")
        self.include_explanations = QCheckBox("해설도 함께 출력")
        self.include_explanations.setEnabled(False)
        self.include_answers.toggled.connect(self.include_explanations.setEnabled)
        form.addRow("정답", self.include_answers)
        form.addRow("", self.include_explanations)
        font_row = QHBoxLayout()
        self.font_path = QLineEdit()
        self.font_path.setPlaceholderText("비워 두면 Windows 맑은 고딕을 자동 사용합니다")
        font_btn = QPushButton("찾아보기…")
        font_btn.clicked.connect(self._browse_font)
        font_row.addWidget(self.font_path, 1)
        font_row.addWidget(font_btn)
        form.addRow("한글 TTF 글꼴", font_row)
        bold_font_row = QHBoxLayout()
        self.bold_font_path = QLineEdit()
        self.bold_font_path.setPlaceholderText("비워 두면 맑은 고딕 Bold를 자동 탐색합니다")
        bold_font_btn = QPushButton("찾아보기…")
        bold_font_btn.clicked.connect(self._browse_bold_font)
        bold_font_row.addWidget(self.bold_font_path, 1)
        bold_font_row.addWidget(bold_font_btn)
        form.addRow("굵은 TTF 글꼴", bold_font_row)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("PDF 만들기")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_font(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "한글 글꼴 선택",
            "",
            "TrueType 글꼴 (*.ttf *.ttc);;모든 파일 (*)",
        )
        if path:
            self.font_path.setText(path)

    def _browse_bold_font(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "굵은 한글 글꼴 선택",
            "",
            "TrueType 글꼴 (*.ttf *.ttc);;모든 파일 (*)",
        )
        if path:
            self.bold_font_path.setText(path)

    def options(self) -> ExportOptions:
        return ExportOptions(
            title=self.title_edit.text().strip(),
            include_answers=self.include_answers.isChecked(),
            include_explanations=(
                self.include_answers.isChecked() and self.include_explanations.isChecked()
            ),
            font_path=self.font_path.text().strip(),
            bold_font_path=self.bold_font_path.text().strip(),
            font_size=self.font_size.value(),
        )


class ExamWorkspaceList(QListWidget):
    """내부 드래그로 순서 변경 + 탐색기에서 드래그해 온 지문 받기."""

    passages_dropped = pyqtSignal(list, int)   # 지문 ID 목록, 삽입 위치(-1 = 끝)
    order_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAcceptDrops(True)

    def _accepts(self, event) -> bool:
        return isinstance(event.source(), QListWidget)

    def dragEnterEvent(self, event) -> None:
        if self._accepts(event):
            super().dragEnterEvent(event)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._accepts(event):
            super().dragMoveEvent(event)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        source = event.source()
        if source is self:
            super().dropEvent(event)
            self.order_changed.emit()
            return
        if isinstance(source, QListWidget):
            ids = [it.data(ROLE_ID) for it in source.selectedItems()]
            row = self.indexAt(event.position().toPoint()).row()
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.passages_dropped.emit(ids, row)
        else:
            event.ignore()


class TestCreatorView(QWidget):
    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._exam: Optional[ExamSet] = None
        self._export_busy = False
        self._build_ui()
        state.library_changed.connect(self.refresh_all)
        self.refresh_all()

    # ================================================================ UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_browser())
        splitter.addWidget(self._build_workspace())
        splitter.setSizes([520, 520])
        root.addWidget(splitter)

    def _build_browser(self) -> QWidget:
        box = QGroupBox("지문 탐색기")
        lay = QVBoxLayout(box)

        row1 = QHBoxLayout()
        self.ptype_filter = QComboBox()
        self.qtype_filter = QComboBox()
        row1.addWidget(QLabel("지문 유형"))
        row1.addWidget(self.ptype_filter, 1)
        row1.addWidget(QLabel("문제 유형"))
        row1.addWidget(self.qtype_filter, 1)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("난이도"))
        self.diff_checks: dict[Optional[int], QCheckBox] = {}
        for level in (1, 2, 3, 4, 5, None):
            cb = QCheckBox(f"Lv.{level}" if level else "미분류")
            cb.setChecked(True)
            cb.setStyleSheet(f"QCheckBox {{ color: {difficulty_color(level)}; font-weight: bold; }}")
            cb.toggled.connect(self.refresh_browser)
            self.diff_checks[level] = cb
            row2.addWidget(cb)
        row2.addStretch()
        lay.addLayout(row2)

        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("키워드 검색")
        self.keyword_edit.textChanged.connect(self.refresh_browser)
        lay.addWidget(self.keyword_edit)

        self.ptype_filter.currentIndexChanged.connect(self.refresh_browser)
        self.qtype_filter.currentIndexChanged.connect(self.refresh_browser)

        self.browser_list = QListWidget()
        self.browser_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.browser_list.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.browser_list.itemDoubleClicked.connect(lambda _it: self._add_selected())
        lay.addWidget(self.browser_list, 1)

        row3 = QHBoxLayout()
        self.result_label = QLabel()
        self.result_label.setObjectName("Muted")
        add_btn = QPushButton("선택 지문 담기 →")
        add_btn.setObjectName("Primary")
        add_btn.clicked.connect(self._add_selected)
        row3.addWidget(self.result_label, 1)
        row3.addWidget(add_btn)
        lay.addLayout(row3)
        return box

    def _build_workspace(self) -> QWidget:
        box = QGroupBox("시험지 작업 공간")
        lay = QVBoxLayout(box)

        row1 = QHBoxLayout()
        self.exam_combo = QComboBox()
        self.exam_combo.currentIndexChanged.connect(self._on_exam_selected)
        new_btn = QPushButton("새 시험지")
        new_btn.clicked.connect(self._new_exam)
        del_btn = QPushButton("시험지 삭제")
        del_btn.clicked.connect(self._delete_exam)
        row1.addWidget(self.exam_combo, 1)
        row1.addWidget(new_btn)
        row1.addWidget(del_btn)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("제목"))
        self.exam_title = QLineEdit()
        self.exam_title.textEdited.connect(self._on_title_edited)
        row2.addWidget(self.exam_title, 1)
        lay.addLayout(row2)

        self.workspace = ExamWorkspaceList()
        self.workspace.passages_dropped.connect(self._add_passages)
        self.workspace.order_changed.connect(self._sync_order_from_list)
        lay.addWidget(self.workspace, 1)

        hint = QLabel("※ 각 지문은 새 페이지에서 시작하며, 긴 지문·문제는 잘리지 않고 다음 페이지로 이어집니다.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        row3 = QHBoxLayout()
        for text, slot in (("▲", lambda: self._move(-1)), ("▼", lambda: self._move(1)),
                           ("빼기", self._remove_selected)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row3.addWidget(b)
        self.summary_label = QLabel()
        row3.addWidget(self.summary_label, 1)
        self.export_btn = QPushButton("PDF 내보내기")
        self.export_btn.setObjectName("Primary")
        self.export_btn.clicked.connect(self._export_pdf)
        row3.addWidget(self.export_btn)
        lay.addLayout(row3)
        return box

    # ========================================================= 새로고침
    def showEvent(self, event) -> None:
        self.refresh_all()  # 편집 화면의 변경 사항(유형/난이도) 반영
        super().showEvent(event)

    def refresh_all(self) -> None:
        self._refresh_filter_options()
        self.refresh_browser()
        self._refresh_exam_combo()

    @staticmethod
    def _reset_combo(combo: QComboBox, label_all: str, values: list[str]) -> None:
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(label_all, _ALL)
        for v in values:
            combo.addItem(v or "(미지정)", v)
        idx = combo.findData(current)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _refresh_filter_options(self) -> None:
        passages = self.state.library.passages
        ptypes = sorted({p.passage_type for p in passages})
        qtypes = sorted({q.question_type for p in passages for q in p.questions})
        self._reset_combo(self.ptype_filter, "전체", ptypes)
        self._reset_combo(self.qtype_filter, "전체", qtypes)

    def refresh_browser(self) -> None:
        ptype = self.ptype_filter.currentData()
        qtype = self.qtype_filter.currentData()
        difficulties = {lv for lv, cb in self.diff_checks.items() if cb.isChecked()}
        passages = self.state.library.filter_passages(
            passage_types=None if ptype == _ALL else {ptype},
            question_types=None if qtype == _ALL else {qtype},
            difficulties=difficulties,
            keyword=self.keyword_edit.text(),
        )
        in_exam = set(self._exam.passage_ids) if self._exam else set()
        self.browser_list.clear()
        for p in passages:
            item = QListWidgetItem(("✔ " if p.id in in_exam else "") + _passage_label(p))
            item.setData(ROLE_ID, p.id)
            item.setIcon(color_icon(difficulty_color(p.difficulty)))
            self.browser_list.addItem(item)
        self.result_label.setText(f"{len(passages)}개 지문")

    def _refresh_exam_combo(self) -> None:
        exams = self.state.library.exam_sets
        keep_id = self._exam.id if self._exam else None
        self.exam_combo.blockSignals(True)
        self.exam_combo.clear()
        for e in exams:
            self.exam_combo.addItem(e.title or "(제목 없음)", e.id)
        idx = self.exam_combo.findData(keep_id) if keep_id else -1
        if idx < 0 and exams:
            idx = 0
        self.exam_combo.setCurrentIndex(idx)
        self.exam_combo.blockSignals(False)
        self._on_exam_selected(idx)

    # ========================================================= 시험지
    def _on_exam_selected(self, idx: int) -> None:
        exams = self.state.library.exam_sets
        exam_id = self.exam_combo.itemData(idx) if idx >= 0 else None
        self._exam = next((e for e in exams if e.id == exam_id), None)
        self.exam_title.setEnabled(self._exam is not None)
        self.exam_title.setText(self._exam.title if self._exam else "")
        self._refresh_workspace()
        self.refresh_browser()

    def _ensure_exam(self) -> ExamSet:
        if self._exam is None:
            self._new_exam()
        assert self._exam is not None
        return self._exam

    def _new_exam(self) -> None:
        exam = ExamSet(title=f"시험지 {len(self.state.library.exam_sets) + 1}")
        self.state.library.exam_sets.append(exam)
        self._exam = exam
        self.state.mark_dirty(notify_library=False)
        self._refresh_exam_combo()
        self.exam_title.setFocus()
        self.exam_title.selectAll()

    def _delete_exam(self) -> None:
        if not self._exam:
            return
        if QMessageBox.question(self, "시험지 삭제", f"'{self._exam.title}' 시험지를 삭제할까요?\n"
                                "(지문 라이브러리에는 영향이 없습니다)") != QMessageBox.StandardButton.Yes:
            return
        self.state.library.exam_sets = [e for e in self.state.library.exam_sets if e.id != self._exam.id]
        self._exam = None
        self.state.mark_dirty(notify_library=False)
        self._refresh_exam_combo()

    def _on_title_edited(self, text: str) -> None:
        if self._exam:
            self._exam.title = text
            self.exam_combo.setItemText(self.exam_combo.currentIndex(), text or "(제목 없음)")
            self.state.mark_dirty(notify_library=False)

    # ========================================================= 작업 공간
    def _refresh_workspace(self, select_rows: Optional[list[int]] = None) -> None:
        self.workspace.clear()
        lib = self.state.library
        if self._exam:
            # 삭제된 지문 ID 정리 → 화면 행 번호와 passage_ids 인덱스를 항상 일치시킨다
            self._exam.passage_ids = [pid for pid in self._exam.passage_ids if lib.get_passage(pid)]
        passages = [lib.get_passage(pid) for pid in (self._exam.passage_ids if self._exam else [])]
        for i, p in enumerate(passages, start=1):
            item = QListWidgetItem(f"{i}. {_passage_label(p)}")
            item.setData(ROLE_ID, p.id)
            item.setIcon(color_icon(difficulty_color(p.difficulty)))
            self.workspace.addItem(item)
        for r in select_rows or []:
            if 0 <= r < self.workspace.count():
                self.workspace.item(r).setSelected(True)
        n_q = sum(len(p.questions) for p in passages)
        levels = [p.difficulty for p in passages if p.difficulty]
        avg = f"{sum(levels) / len(levels):.1f}" if levels else "-"
        self.summary_label.setText(f"지문 {len(passages)} · 문제 {n_q} · 평균 난이도 {avg}")
        self.export_btn.setEnabled(bool(passages) and not self._export_busy)

    def _add_selected(self) -> None:
        ids = [it.data(ROLE_ID) for it in self.browser_list.selectedItems()]
        self._add_passages(ids, -1)

    def _add_passages(self, ids: list[str], row: int) -> None:
        if not ids:
            return
        exam = self._ensure_exam()
        new_ids = [pid for pid in ids if pid not in exam.passage_ids]
        skipped = len(ids) - len(new_ids)
        insert_at = row if 0 <= row <= len(exam.passage_ids) else len(exam.passage_ids)
        exam.passage_ids[insert_at:insert_at] = new_ids
        self.state.mark_dirty(notify_library=False)
        self._refresh_workspace()
        self.refresh_browser()
        msg = f"지문 {len(new_ids)}개를 담았습니다."
        if skipped:
            msg += f" (이미 담긴 {skipped}개 제외)"
        self.state.status(msg)

    def _sync_order_from_list(self) -> None:
        if not self._exam:
            return
        self._exam.passage_ids = [self.workspace.item(i).data(ROLE_ID) for i in range(self.workspace.count())]
        self.state.mark_dirty(notify_library=False)
        self._refresh_workspace()

    def _selected_rows(self) -> list[int]:
        return sorted(self.workspace.row(it) for it in self.workspace.selectedItems())

    def _remove_selected(self) -> None:
        if not self._exam:
            return
        rows = set(self._selected_rows())
        self._exam.passage_ids = [pid for i, pid in enumerate(self._exam.passage_ids) if i not in rows]
        self.state.mark_dirty(notify_library=False)
        self._refresh_workspace()
        self.refresh_browser()

    def _move(self, delta: int) -> None:
        rows = self._selected_rows()
        if not self._exam or len(rows) != 1:
            return
        ids = self._exam.passage_ids
        r, t = rows[0], rows[0] + delta
        if 0 <= t < len(ids):
            ids[r], ids[t] = ids[t], ids[r]
            self.state.mark_dirty(notify_library=False)
            self._refresh_workspace(select_rows=[t])

    def _export_pdf(self) -> None:
        if not self._exam or self._export_busy:
            return
        passages = [
            self.state.library.get_passage(passage_id) for passage_id in self._exam.passage_ids
        ]
        passages = [deepcopy(passage) for passage in passages if passage is not None]
        if not passages:
            QMessageBox.warning(self, "PDF 내보내기", "시험지에 지문을 먼저 담아 주세요.")
            return

        dialog = ExportOptionsDialog(self._exam.title, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        options = dialog.options()
        safe_name = re.sub(r'[<>:"/\\|?*]+', "_", options.title or self._exam.title or "시험지")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "시험지 PDF 저장",
            f"{safe_name}.pdf",
            "PDF 파일 (*.pdf)",
        )
        if not path:
            return

        exporter = PdfExporter(options, asset_store=self.state.asset_store)
        self._set_export_busy(True)
        self.state.run_async(
            exporter.export,
            passages,
            Path(path),
            on_result=self._on_export_success,
            on_error=self._on_export_error,
            on_finished=lambda: self._set_export_busy(False),
        )

    def _set_export_busy(self, busy: bool) -> None:
        self._export_busy = busy
        self.export_btn.setText("PDF 생성 중…" if busy else "PDF 내보내기")
        self.export_btn.setEnabled(not busy and bool(self._exam and self._exam.passage_ids))

    def _on_export_success(self, result: ExportResult) -> None:
        self.state.status(f"PDF 저장 완료: {result.path}", 8000)
        box = QMessageBox(self)
        box.setWindowTitle("PDF 출판 완료")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            f"지문 {result.passage_count}개·문제 {result.question_count}개를 "
            f"{result.page_count}페이지 PDF로 만들었습니다."
        )
        box.setInformativeText(f"저장 위치:\n{result.path}\n\n지금 PDF를 열까요?")
        if result.warnings:
            box.setDetailedText("\n".join(f"• {warning}" for warning in result.warnings))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if box.exec() == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.path)))

    def _on_export_error(self, exc: Exception) -> None:
        message = str(exc) if isinstance(exc, PdfExportError) else f"예상하지 못한 오류: {exc}"
        QMessageBox.critical(
            self,
            "PDF 내보내기 실패",
            f"{message}\n\n글꼴 또는 그림 파일을 확인한 뒤 다시 시도해 주세요.",
        )
