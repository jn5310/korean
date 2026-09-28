"""시험지 생성 화면 (Test Creator).

왼쪽 : 조건별(지문 유형/문제 유형/난이도/키워드) 지문 탐색기
오른쪽: 시험지 작업 공간 — 클릭 또는 드래그 앤 드롭으로 담고 순서 배치
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from ..models import ExamSet, Passage
from .editor_view import color_icon
from .state import AppState
from .styles import difficulty_color

ROLE_ID = Qt.ItemDataRole.UserRole
_ALL = "__all__"


def _passage_label(p: Passage) -> str:
    lv = f"Lv.{p.difficulty}" if p.difficulty else "미분류"
    ptype = f" · {p.passage_type}" if p.passage_type else ""
    return f"[{lv}] {p.display_title}{ptype}  ({len(p.questions)}문항)"


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

        hint = QLabel("※ 드래그해서 순서를 바꾸거나, 탐색기에서 끌어다 놓을 수 있습니다. 1지문 = 1페이지로 출판됩니다.")
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
        self.export_btn.setEnabled(bool(passages))

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
        QMessageBox.information(self, "PDF 내보내기", "1지문-1페이지 PDF 출판 엔진은 Step 5 에서 구현됩니다.")
