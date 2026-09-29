"""Gemini API Key / 모델 / OCR 설정 다이얼로그."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from ..config import ENV_API_KEY, SUGGESTED_GEMINI_MODELS
from ..services.gemini_client import ConnectionTestResult, GeminiError
from .state import AppState

API_KEY_URL = "https://aistudio.google.com/apikey"


class SettingsDialog(QDialog):
    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.setWindowTitle("설정")
        self.setMinimumWidth(560)
        self._busy = False
        self._build_ui()
        self._load_values()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # --- Gemini ---
        gemini_box = QGroupBox("Google Gemini API")
        form = QFormLayout(gemini_box)

        key_row = QHBoxLayout()
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText(f"AIza...  (비워 두면 환경변수 {ENV_API_KEY} 사용)")
        self.show_key_btn = QPushButton("보기")
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.toggled.connect(self._toggle_key_visibility)
        key_row.addWidget(self.api_key_edit, 1)
        key_row.addWidget(self.show_key_btn)
        form.addRow("API Key", key_row)

        link = QLabel(f'<a href="{API_KEY_URL}">Google AI Studio 에서 무료 API Key 발급받기</a>')
        link.setOpenExternalLinks(True)
        link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        form.addRow("", link)

        model_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItems(SUGGESTED_GEMINI_MODELS)
        self.refresh_models_btn = QPushButton("모델 목록 불러오기")
        self.refresh_models_btn.clicked.connect(self._refresh_models)
        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(self.refresh_models_btn)
        form.addRow("모델", model_row)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 600)
        self.timeout_spin.setSuffix(" 초")
        form.addRow("요청 제한 시간", self.timeout_spin)

        self.retries_spin = QSpinBox()
        self.retries_spin.setRange(0, 10)
        self.retries_spin.setSuffix(" 회")
        form.addRow("재시도 횟수", self.retries_spin)

        self.rpm_spin = QSpinBox()
        self.rpm_spin.setRange(1, 60)
        self.rpm_spin.setSuffix(" 회/분")
        self.rpm_spin.setToolTip("무료 tier 한도에 맞춰 보수적으로 설정하세요. 기본값은 10회/분입니다.")
        form.addRow("분당 최대 요청", self.rpm_spin)

        test_row = QHBoxLayout()
        self.test_btn = QPushButton("연결 테스트")
        self.test_btn.clicked.connect(self._test_connection)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_result, 1)
        form.addRow("", test_row)
        layout.addWidget(gemini_box)

        # --- OCR (스캔본 PDF 로컬 인식) ---
        ocr_box = QGroupBox("OCR (스캔본 PDF)")
        ocr_form = QFormLayout(ocr_box)
        tess_row = QHBoxLayout()
        self.tesseract_edit = QLineEdit()
        self.tesseract_edit.setPlaceholderText("비워 두면 PATH 에서 tesseract 를 찾습니다")
        browse = QPushButton("찾아보기…")
        browse.clicked.connect(self._browse_tesseract)
        tess_row.addWidget(self.tesseract_edit, 1)
        tess_row.addWidget(browse)
        ocr_form.addRow("Tesseract 경로", tess_row)
        self.ocr_lang_edit = QLineEdit()
        ocr_form.addRow("OCR 언어", self.ocr_lang_edit)
        self.ocr_dpi_spin = QSpinBox()
        self.ocr_dpi_spin.setRange(150, 600)
        self.ocr_dpi_spin.setSingleStep(50)
        self.ocr_dpi_spin.setSuffix(" dpi")
        ocr_form.addRow("렌더링 해상도", self.ocr_dpi_spin)
        self.ocr_timeout_spin = QSpinBox()
        self.ocr_timeout_spin.setRange(10, 600)
        self.ocr_timeout_spin.setSuffix(" 초/페이지")
        ocr_form.addRow("OCR 제한 시간", self.ocr_timeout_spin)
        ocr_note = QLabel("Tesseract가 없어도 API Key가 설정되어 있으면 Gemini 문서 비전으로 스캔 PDF를 분석합니다.")
        ocr_note.setWordWrap(True)
        ocr_note.setObjectName("Muted")
        ocr_form.addRow("", ocr_note)
        layout.addWidget(ocr_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_values(self) -> None:
        cfg = self.state.config
        self.api_key_edit.setText(cfg.gemini_api_key)
        if self.model_combo.findText(cfg.gemini_model) < 0:
            self.model_combo.addItem(cfg.gemini_model)
        self.model_combo.setCurrentText(cfg.gemini_model)
        self.timeout_spin.setValue(cfg.gemini_timeout_sec)
        self.retries_spin.setValue(cfg.gemini_max_retries)
        self.rpm_spin.setValue(cfg.gemini_requests_per_minute)
        self.tesseract_edit.setText(cfg.tesseract_cmd)
        self.ocr_lang_edit.setText(cfg.ocr_languages)
        self.ocr_dpi_spin.setValue(cfg.ocr_dpi)
        self.ocr_timeout_spin.setValue(cfg.ocr_timeout_sec)

    # ------------------------------------------------------------------
    def _toggle_key_visibility(self, visible: bool) -> None:
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        self.show_key_btn.setText("숨기기" if visible else "보기")

    def _browse_tesseract(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "tesseract 실행 파일 선택")
        if path:
            self.tesseract_edit.setText(path)

    def _entered_key(self) -> str:
        return self.api_key_edit.text().strip() or self.state.config_manager.effective_api_key

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.test_btn.setEnabled(not busy)
        self.refresh_models_btn.setEnabled(not busy)

    def _make_client(self):
        """입력 중인(아직 저장 전) 값으로 클라이언트 생성."""
        return self.state.create_gemini_client(
            api_key=self._entered_key(), model=self.model_combo.currentText().strip()
        )

    def _test_connection(self) -> None:
        try:
            client = self._make_client()
        except GeminiError as exc:
            self._show_result(False, str(exc))
            return
        self._set_busy(True)
        self.test_result.setObjectName("Muted")
        self.test_result.setText("테스트 중…")
        self.state.run_async(
            client.test_connection,
            on_result=self._on_test_result,
            on_error=lambda e: self._show_result(False, str(e)),
            on_finished=lambda: self._set_busy(False),
        )

    def _on_test_result(self, result: ConnectionTestResult) -> None:
        self._show_result(result.ok, result.message)

    def _show_result(self, ok: bool, message: str) -> None:
        self.test_result.setObjectName("StatusOk" if ok else "StatusError")
        self.test_result.setText(("✔ " if ok else "✖ ") + message)
        self.test_result.style().unpolish(self.test_result)
        self.test_result.style().polish(self.test_result)

    def _refresh_models(self) -> None:
        try:
            client = self._make_client()
        except GeminiError as exc:
            QMessageBox.warning(self, "모델 목록", str(exc))
            return
        self._set_busy(True)
        self.state.run_async(
            client.list_models,
            on_result=self._on_models_loaded,
            on_error=lambda e: QMessageBox.warning(self, "모델 목록", str(e)),
            on_finished=lambda: self._set_busy(False),
        )

    def _on_models_loaded(self, models: list[str]) -> None:
        if not models:
            QMessageBox.information(self, "모델 목록", "사용 가능한 Gemini 모델이 없습니다.")
            return
        current = self.model_combo.currentText()
        self.model_combo.clear()
        self.model_combo.addItems(models)
        self.model_combo.setCurrentText(current if current in models else models[0])
        self.state.status(f"모델 {len(models)}개를 불러왔습니다.")

    def _save(self) -> None:
        cfg = self.state.config
        cfg.gemini_api_key = self.api_key_edit.text().strip()
        cfg.gemini_model = self.model_combo.currentText().strip() or cfg.gemini_model
        cfg.gemini_timeout_sec = self.timeout_spin.value()
        cfg.gemini_max_retries = self.retries_spin.value()
        cfg.gemini_requests_per_minute = self.rpm_spin.value()
        cfg.tesseract_cmd = self.tesseract_edit.text().strip()
        cfg.ocr_languages = self.ocr_lang_edit.text().strip() or "kor+eng"
        cfg.ocr_dpi = self.ocr_dpi_spin.value()
        cfg.ocr_timeout_sec = self.ocr_timeout_spin.value()
        self.state.save_config()
        self.accept()
