"""메인 윈도우 — 좌측 내비게이션 + 화면 스택 + 메뉴/상태 표시줄."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QAction, QCloseEvent, QKeySequence
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QListWidget, QMainWindow, QMessageBox,
    QStackedWidget, QWidget,
)

from .. import APP_NAME, __version__
from .dashboard import DashboardView
from .editor_view import EditorView
from .settings_dialog import SettingsDialog
from .state import AppState
from .test_creator import TestCreatorView

PAGE_DASHBOARD, PAGE_EDITOR, PAGE_TEST = range(3)


class MainWindow(QMainWindow):
    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self.resize(1360, 860)
        self._build_ui()
        self._build_menu()
        self._build_statusbar()

        state.status_message.connect(self.statusBar().showMessage)
        state.config_changed.connect(self._update_permanent_status)
        state.dirty_changed.connect(self._update_title)
        self._update_title()
        self._update_permanent_status()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setObjectName("NavList")
        self.nav.setFixedWidth(180)
        self.nav.addItems(["🏠  대시보드", "✏️  편집·검수", "📝  시험지 생성"])
        layout.addWidget(self.nav)

        self.stack = QStackedWidget()
        self.dashboard = DashboardView(self.state)
        self.editor = EditorView(self.state)
        self.test_creator = TestCreatorView(self.state)
        self.stack.addWidget(self.dashboard)
        self.stack.addWidget(self.editor)
        self.stack.addWidget(self.test_creator)
        layout.addWidget(self.stack, 1)

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(PAGE_DASHBOARD)

        self.dashboard.open_settings_requested.connect(self.open_settings)
        self.dashboard.analysis_completed.connect(self._show_analyzed_passage)
        self.setCentralWidget(central)

    def _build_menu(self) -> None:
        bar = self.menuBar()

        file_menu = bar.addMenu("파일(&F)")
        save = QAction("라이브러리 저장", self)
        save.setShortcut(QKeySequence.StandardKey.Save)
        save.triggered.connect(self.save_library)
        file_menu.addAction(save)
        file_menu.addSeparator()
        exp = QAction("라이브러리 백업 내보내기 (.koreanlib)…", self)
        exp.triggered.connect(self._export_library)
        imp = QAction("라이브러리 백업 가져오기…", self)
        imp.triggered.connect(self._import_library)
        file_menu.addAction(exp)
        file_menu.addAction(imp)
        file_menu.addSeparator()
        quit_action = QAction("종료", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        settings_menu = bar.addMenu("설정(&S)")
        api = QAction("Gemini API / OCR 설정…", self)
        api.setShortcut(QKeySequence("Ctrl+,"))
        api.triggered.connect(self.open_settings)
        settings_menu.addAction(api)

        help_menu = bar.addMenu("도움말(&H)")
        about = QAction("정보", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _build_statusbar(self) -> None:
        self.api_indicator = QLabel()
        self.api_indicator.setContentsMargins(8, 0, 8, 0)
        self.statusBar().addPermanentWidget(self.api_indicator)

    def _show_analyzed_passage(self, passage_id: str) -> None:
        self.nav.setCurrentRow(PAGE_EDITOR)
        self.editor.select_passage(passage_id)

    # ------------------------------------------------------------------
    def _update_title(self, *_args) -> None:
        mark = " •" if self.state.is_dirty else ""
        self.setWindowTitle(f"{APP_NAME} v{__version__}{mark}")

    def _update_permanent_status(self) -> None:
        if self.state.has_api_key:
            self.api_indicator.setText(f"Gemini: {self.state.config.gemini_model}")
            self.api_indicator.setStyleSheet("color:#2e7d32;")
        else:
            self.api_indicator.setText("Gemini: API Key 미설정")
            self.api_indicator.setStyleSheet("color:#ef6c00;")

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.state, self)
        if dlg.exec():
            self.state.status("설정을 저장했습니다.")

    def save_library(self) -> bool:
        try:
            self.state.save_library()
            return True
        except OSError as exc:
            QMessageBox.critical(self, "저장 실패", f"라이브러리를 저장하지 못했습니다.\n{exc}")
            return False

    def _export_library(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "라이브러리 백업 내보내기",
            "library.koreanlib",
            "지문 스튜디오 백업 (*.koreanlib)",
        )
        if not path:
            return
        try:
            destination, warnings = self.state.repository.export_bundle(
                self.state.library, Path(path)
            )
            self.state.status(f"백업 완료: {destination}")
            if warnings:
                QMessageBox.warning(self, "백업 완료 (일부 그림 누락)", "\n".join(warnings))
        except OSError as exc:
            QMessageBox.critical(self, "내보내기 실패", str(exc))

    def _import_library(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "라이브러리 백업 가져오기",
            "",
            "지문 스튜디오 백업 (*.koreanlib);;구버전 JSON (*.json)",
        )
        if not path:
            return
        warnings = []
        try:
            if Path(path).suffix.lower() == ".koreanlib":
                imported, warnings = self.state.repository.import_bundle(Path(path))
            else:
                imported = self.state.repository.import_from(Path(path))
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            QMessageBox.critical(self, "가져오기 실패", f"올바른 라이브러리 백업이 아닙니다.\n{exc}")
            return
        lib = self.state.library
        existing = {p.id for p in lib.passages}
        new_passages = [p for p in imported.passages if p.id not in existing]
        lib.passages.extend(new_passages)
        existing_exams = {e.id for e in lib.exam_sets}
        lib.exam_sets.extend(e for e in imported.exam_sets if e.id not in existing_exams)
        self.state.mark_dirty()
        self.state.status(f"지문 {len(new_passages)}개를 가져왔습니다.")
        if warnings:
            QMessageBox.warning(self, "가져오기 완료 (일부 그림 누락)", "\n".join(warnings))

    def _about(self) -> None:
        QMessageBox.about(
            self, "정보",
            f"<b>{APP_NAME}</b> v{__version__}<br><br>"
            "PDF 에서 지문·문제를 추출하고 Gemini 로 난이도를 분류하여<br>"
            "1지문-1페이지 교재 및 맞춤형 시험지를 제작합니다.<br><br>"
            f"데이터 폴더: {self.state.config_manager.data_dir}",
        )

    # ------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:
        # 최근 폴더 등 UI 관련 설정은 항상 저장
        try:
            self.state.config_manager.save()
        except OSError:
            pass
        if not self.state.is_dirty:
            event.accept()
            return
        answer = QMessageBox.question(
            self, "종료", "저장하지 않은 변경 사항이 있습니다. 저장할까요?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Save:
            if self.save_library():
                event.accept()
            else:
                event.ignore()
        elif answer == QMessageBox.StandardButton.Discard:
            event.accept()
        else:
            event.ignore()
