"""메인 화면 (Dashboard): PDF 업로드 / 분석 시작 / Gemini 상태 / 라이브러리 현황."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QAbstractItemView, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from ..services.gemini_client import ConnectionTestResult, GeminiError
from ..services.pdf_parser import ParseResult, PdfParser
from .state import AppState
from .styles import difficulty_color
from .widgets import Card


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class PdfDropArea(QLabel):
    files_dropped = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("여기에 PDF 파일을 끌어다 놓으세요", parent)
        self.setObjectName("DropArea")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAcceptDrops(True)

    @staticmethod
    def _pdf_paths(event) -> list[str]:
        if not event.mimeData().hasUrls():
            return []
        return [u.toLocalFile() for u in event.mimeData().urls()
                if u.isLocalFile() and u.toLocalFile().lower().endswith(".pdf")]

    def _set_active(self, active: bool) -> None:
        self.setProperty("dragActive", "true" if active else "false")
        _repolish(self)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._pdf_paths(event):
            event.acceptProposedAction()
            self._set_active(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._set_active(False)

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_active(False)
        paths = self._pdf_paths(event)
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()


class DashboardView(QWidget):
    open_settings_requested = pyqtSignal()
    analysis_completed = pyqtSignal()        # 분석 후 편집 화면으로 이동할 때 사용

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._pending_files: list[str] = []
        self._build_ui()
        state.config_changed.connect(self.refresh_api_status)
        state.library_changed.connect(self.refresh_stats)
        self.refresh_api_status()
        self.refresh_stats()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)

        title = QLabel("대시보드")
        title.setObjectName("PageTitle")
        root.addWidget(title)

        top = QHBoxLayout()
        top.setSpacing(16)

        # --- Gemini 카드 ---
        api_card = Card("Gemini API")
        self.api_status = QLabel()
        self.api_status.setWordWrap(True)
        self.api_model = QLabel()
        self.api_model.setObjectName("Muted")
        api_card.body.addWidget(self.api_status)
        api_card.body.addWidget(self.api_model)
        btns = QHBoxLayout()
        settings_btn = QPushButton("API Key 설정…")
        settings_btn.setObjectName("Primary")
        settings_btn.clicked.connect(self.open_settings_requested.emit)
        self.test_btn = QPushButton("연결 테스트")
        self.test_btn.clicked.connect(self._test_connection)
        btns.addWidget(settings_btn)
        btns.addWidget(self.test_btn)
        btns.addStretch()
        api_card.body.addLayout(btns)
        api_card.body.addStretch()
        top.addWidget(api_card, 1)

        # --- 라이브러리 현황 카드 ---
        stats_card = Card("라이브러리 현황")
        grid = QGridLayout()
        self.stat_passages = QLabel("0")
        self.stat_questions = QLabel("0")
        for lbl in (self.stat_passages, self.stat_questions):
            lbl.setStyleSheet("font-size: 22px; font-weight: bold; color: #0288d1;")
        grid.addWidget(QLabel("지문"), 0, 0)
        grid.addWidget(self.stat_passages, 1, 0)
        grid.addWidget(QLabel("문제"), 0, 1)
        grid.addWidget(self.stat_questions, 1, 1)
        stats_card.body.addLayout(grid)
        self.stat_difficulty = QLabel()
        self.stat_difficulty.setTextFormat(Qt.TextFormat.RichText)
        stats_card.body.addWidget(self.stat_difficulty)
        stats_card.body.addStretch()
        top.addWidget(stats_card, 1)
        root.addLayout(top)

        # --- PDF 업로드 카드 ---
        pdf_card = Card("PDF 불러오기 및 분석")
        hint = QLabel("텍스트 PDF 는 직접 추출하고, 스캔본은 OCR 로 인식합니다. "
                      "추출된 지문·문제는 Gemini 로 난이도(1~5단계)가 자동 태깅됩니다.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        pdf_card.body.addWidget(hint)

        self.drop_area = PdfDropArea()
        self.drop_area.files_dropped.connect(self.add_files)
        pdf_card.body.addWidget(self.drop_area)

        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        pdf_card.body.addWidget(self.file_list, 1)

        row = QHBoxLayout()
        add_btn = QPushButton("PDF 추가…")
        add_btn.clicked.connect(self._browse_files)
        remove_btn = QPushButton("선택 제거")
        remove_btn.clicked.connect(self._remove_selected)
        self.analyze_btn = QPushButton("분석 시작")
        self.analyze_btn.setObjectName("Primary")
        self.analyze_btn.clicked.connect(self._start_analysis)
        row.addWidget(add_btn)
        row.addWidget(remove_btn)
        row.addStretch()
        row.addWidget(self.analyze_btn)
        pdf_card.body.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress_label = QLabel()
        self.progress_label.setObjectName("Muted")
        self.progress_label.setVisible(False)
        pdf_card.body.addWidget(self.progress)
        pdf_card.body.addWidget(self.progress_label)
        root.addWidget(pdf_card, 1)

        self._update_analyze_enabled()

    def showEvent(self, event) -> None:
        # 편집 화면에서 바뀐 난이도 등을 반영
        self.refresh_stats()
        super().showEvent(event)

    # --- Gemini 상태 -----------------------------------------------------
    def refresh_api_status(self) -> None:
        cfg = self.state.config
        if self.state.has_api_key:
            source = "설정 파일" if cfg.gemini_api_key.strip() else "환경변수"
            self.api_status.setText(f"● API Key 설정됨 ({source})")
            self.api_status.setObjectName("StatusOk")
        else:
            self.api_status.setText("● API Key 가 설정되지 않았습니다. 난이도 자동 판별을 쓰려면 설정해 주세요.")
            self.api_status.setObjectName("StatusWarn")
        _repolish(self.api_status)
        self.api_model.setText(f"모델: {cfg.gemini_model}")
        self.test_btn.setEnabled(self.state.has_api_key)

    def _test_connection(self) -> None:
        try:
            client = self.state.create_gemini_client()
        except GeminiError as exc:
            QMessageBox.warning(self, "연결 테스트", str(exc))
            return
        self.test_btn.setEnabled(False)
        self.test_btn.setText("테스트 중…")

        def done():
            self.test_btn.setText("연결 테스트")
            self.test_btn.setEnabled(self.state.has_api_key)

        self.state.run_async(
            client.test_connection,
            on_result=self._on_test_result,
            on_error=lambda e: QMessageBox.warning(self, "연결 테스트", str(e)),
            on_finished=done,
        )

    def _on_test_result(self, result: ConnectionTestResult) -> None:
        if result.ok:
            self.state.status(result.message, 6000)
            QMessageBox.information(self, "연결 테스트", result.message)
        else:
            QMessageBox.warning(self, "연결 테스트", result.message)

    # --- 라이브러리 현황 --------------------------------------------------
    def refresh_stats(self) -> None:
        passages = self.state.library.passages
        self.stat_passages.setText(str(len(passages)))
        self.stat_questions.setText(str(sum(len(p.questions) for p in passages)))
        counts = Counter(p.difficulty for p in passages)
        parts = []
        for level in (1, 2, 3, 4, 5, None):
            label = f"Lv.{level}" if level else "미분류"
            parts.append(f'<span style="color:{difficulty_color(level)};font-weight:bold;">'
                         f'{label}</span> {counts.get(level, 0)}')
        self.stat_difficulty.setText(" &nbsp;·&nbsp; ".join(parts))

    # --- 파일 목록 -------------------------------------------------------
    def add_files(self, paths: list[str]) -> None:
        added = 0
        for path in paths:
            if path in self._pending_files:
                continue
            self._pending_files.append(path)
            item = QListWidgetItem(Path(path).name)
            item.setToolTip(path)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.file_list.addItem(item)
            added += 1
        if paths:
            self.state.config.last_open_dir = str(Path(paths[0]).parent)
        if added:
            self.state.status(f"PDF {added}개를 추가했습니다.")
        self._update_analyze_enabled()

    def _browse_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "PDF 파일 선택", self.state.config.last_open_dir, "PDF 파일 (*.pdf)"
        )
        if paths:
            self.add_files(paths)

    def _remove_selected(self) -> None:
        for item in self.file_list.selectedItems():
            self._pending_files.remove(item.data(Qt.ItemDataRole.UserRole))
            self.file_list.takeItem(self.file_list.row(item))
        self._update_analyze_enabled()

    def _update_analyze_enabled(self) -> None:
        self.analyze_btn.setEnabled(bool(self._pending_files))

    # --- 분석 (Step 2 에서 실제 구현) -------------------------------------
    def _start_analysis(self) -> None:
        cfg = self.state.config
        parser = PdfParser(cfg.tesseract_cmd, cfg.ocr_languages, cfg.ocr_dpi)
        files = list(self._pending_files)

        def job(progress):
            results = []
            for i, path in enumerate(files, start=1):
                progress(i - 1, len(files), f"{Path(path).name} 분석 중…")
                results.append(parser.parse(Path(path)))
            progress(len(files), len(files), "완료")
            return results

        self.analyze_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress_label.setVisible(True)
        self.progress.setRange(0, len(files))
        self.progress.setValue(0)
        self.state.run_async(
            job,
            on_progress=self._on_progress,
            on_result=self._on_analysis_done,
            on_error=self._on_analysis_error,
            on_finished=self._on_analysis_finished,
        )

    def _on_progress(self, current: int, total: int, message: str) -> None:
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(current)
        self.progress_label.setText(message)

    def _on_analysis_done(self, results: list[ParseResult]) -> None:
        count = 0
        for result in results:
            for passage in result.passages:
                self.state.library.add_passage(passage)
                count += 1
            self.state.config_manager.add_recent_file(result.source_file)
        self._pending_files.clear()
        self.file_list.clear()
        self.state.mark_dirty()
        self.state.status(f"지문 {count}개를 추출했습니다.")
        self.analysis_completed.emit()

    def _on_analysis_error(self, exc: Exception) -> None:
        if isinstance(exc, NotImplementedError):
            QMessageBox.information(self, "분석", f"{exc}\n\n지금은 '편집·검수' 화면에서 지문을 직접 입력할 수 있습니다.")
        else:
            QMessageBox.critical(self, "분석 오류", str(exc))

    def _on_analysis_finished(self) -> None:
        self.progress.setVisible(False)
        self.progress_label.setVisible(False)
        self._update_analyze_enabled()
