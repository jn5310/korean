"""메인 화면 (Dashboard): PDF 업로드 / 분석 시작 / Gemini 상태 / 라이브러리 현황."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QAbstractItemView, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from ..services.analysis_pipeline import (
    AnalysisBatchResult, AnalysisPipeline, merge_rich_content, passages_equivalent,
)
from ..services.difficulty import DifficultyClassifier
from ..services.document_analyzer import GeminiDocumentAnalyzer
from ..services.library_merge import (
    ReanalysisGroup, find_reanalysis_groups, plan_replacements, replace_passages, source_key,
)
from ..services.gemini_client import ConnectionTestResult, GeminiClient, GeminiError
from ..services.pdf_parser import PdfParser
from .state import AppState
from .styles import difficulty_color
from .widgets import Card


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _normalized_path(path: str) -> str:
    try:
        return str(Path(path).resolve()).casefold()
    except OSError:
        return str(Path(path).absolute()).casefold()


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
        if self.isEnabled() and self._pdf_paths(event):
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
    analysis_completed = pyqtSignal(str)    # 새로 추가한 첫 지문 ID

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._pending_files: list[str] = []
        self._busy = False
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
        self.add_btn = QPushButton("PDF 추가…")
        self.add_btn.clicked.connect(self._browse_files)
        self.remove_btn = QPushButton("선택 제거")
        self.remove_btn.clicked.connect(self._remove_selected)
        self.analyze_btn = QPushButton("분석 시작")
        self.analyze_btn.setObjectName("Primary")
        self.analyze_btn.clicked.connect(self._start_analysis)
        row.addWidget(self.add_btn)
        row.addWidget(self.remove_btn)
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
            if (
                result.model_changed
                and result.model
                and self.state.config.gemini_model == result.original_model
            ):
                self.state.config.gemini_model = result.model
                try:
                    self.state.save_config()
                except OSError as exc:
                    QMessageBox.warning(self, "모델 저장 실패", str(exc))
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
        if self._busy:
            return
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
        self.analyze_btn.setEnabled(bool(self._pending_files) and not self._busy)

    # --- PDF/OCR/Gemini 통합 분석 -----------------------------------------
    def _start_analysis(self) -> None:
        if self._busy or not self._pending_files:
            return
        cfg = deepcopy(self.state.config)  # 분석 중 설정 변경의 영향을 받지 않는 snapshot
        api_key = self.state.config_manager.effective_api_key
        files = [Path(path) for path in self._pending_files]

        def job(progress):
            parser = PdfParser(
                cfg.tesseract_cmd,
                cfg.ocr_languages,
                cfg.ocr_dpi,
                ocr_timeout_sec=getattr(cfg, "ocr_timeout_sec", 90),
                asset_store=self.state.asset_store,
            )
            analyzer = None
            classifier = None
            client = None
            setup_warning = ""
            if api_key:
                try:
                    client = GeminiClient(
                        api_key=api_key,
                        model=cfg.gemini_model,
                        timeout_sec=cfg.gemini_timeout_sec,
                        max_retries=cfg.gemini_max_retries,
                        requests_per_minute=getattr(cfg, "gemini_requests_per_minute", 10),
                    )
                    analyzer = GeminiDocumentAnalyzer(client)
                    classifier = DifficultyClassifier(client)
                except GeminiError as exc:
                    setup_warning = f"Gemini 초기화 실패로 로컬 분석만 수행했습니다: {exc}"
            pipeline = AnalysisPipeline(
                parser,
                document_analyzer=analyzer,
                classifier=classifier,
            )
            batch = pipeline.analyze_files(files, progress=progress)
            if setup_warning:
                batch.general_warnings.append(setup_warning)
            if client is not None:
                batch.selected_model = client.model
                batch.original_model = cfg.gemini_model
                batch.model_changed = client.model != cfg.gemini_model
            return batch

        self._set_analysis_busy(True, len(files) * 1000)
        self.state.run_async(
            job,
            on_progress=self._on_progress,
            on_result=self._on_analysis_done,
            on_error=self._on_analysis_error,
            on_finished=self._on_analysis_finished,
        )

    def _set_analysis_busy(self, busy: bool, maximum: int = 1) -> None:
        self._busy = busy
        self.drop_area.setEnabled(not busy)
        self.file_list.setEnabled(not busy)
        self.add_btn.setEnabled(not busy)
        self.remove_btn.setEnabled(not busy)
        self.analyze_btn.setText("분석 중…" if busy else "분석 시작")
        self.progress.setVisible(busy)
        self.progress_label.setVisible(busy)
        if busy:
            self.progress.setRange(0, max(1, maximum))
            self.progress.setValue(0)
            self.progress_label.setText("분석 준비 중…")
        self._update_analyze_enabled()

    def _on_progress(self, current: int, total: int, message: str) -> None:
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(max(0, min(current, total)))
        self.progress_label.setText(message)

    def _confirm_replace(self, groups: list[ReanalysisGroup]) -> bool:
        names = ", ".join(f"'{Path(group.source_file).name}'" for group in groups[:3])
        if len(groups) > 3:
            names += f" 외 {len(groups) - 3}개"
        old_count = sum(len(group.old_passages) for group in groups)
        new_count = sum(len(group.new_passages) for group in groups)
        try:
            plans = plan_replacements(self.state.library, groups)
        except Exception as exc:  # noqa: BLE001 - 미리 계산이 실패하면 교체하지 않는다.
            QMessageBox.warning(
                self,
                "이전 분석 결과 교체",
                f"교체 결과를 미리 계산하지 못해 기존 결과를 그대로 유지합니다.\n{exc}",
            )
            return False
        kept = [title for plan in plans for title in plan.kept_titles]
        salvaged = [title for plan in plans for title in plan.salvaged_titles]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("이전 분석 결과가 있습니다")
        box.setText(
            f"{names} 파일을 이전에 분석한 지문 {old_count}개가 라이브러리에 있습니다.\n"
            f"새로 분석한 지문 {new_count}개로 교체할까요?"
        )
        details = [
            "교체하면 띄어쓰기·겹친 글자·<보기> 그림이 개선된 새 결과가 들어갑니다.",
            "입력한 정답·해설, 직접 정한 난이도·유형·제목, 직접 추가한 그림은 번호와 선택지가 같은 "
            "새 문항으로 옮겨지고, 시험지는 그 문항들이 들어간 새 지문으로 연결됩니다.",
        ]
        if salvaged:
            details.append(
                f"옮길 곳을 확실히 찾지 못한 입력이 있거나 직접 고친 이전 지문 {len(salvaged)}개는 "
                "지우지 않고 '[이전 분석]' 제목으로 보관합니다."
            )
        if kept:
            details.append(
                f"새 결과와 짝이 맞지 않는 이전 지문 {len(kept)}개는 입력한 내용·시험지 구성이 있거나 "
                "이전 버전에서 고쳤을 수 있어 그대로 남겨 둡니다."
            )
        details.append("'기존 결과 유지'를 누르면 라이브러리를 그대로 두고 이 파일의 새 결과는 추가하지 않습니다.")
        box.setInformativeText("\n".join(details))
        preserved = [*salvaged, *kept]
        if preserved:
            box.setDetailedText("보관·유지되는 이전 지문:\n" + "\n".join(f"• {title}" for title in preserved))
        replace_button = box.addButton("기존 결과 교체", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("기존 결과 유지", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(replace_button)
        box.exec()
        return box.clickedButton() is replace_button

    def _on_analysis_done(self, batch: AnalysisBatchResult) -> None:
        added_passages = []
        enriched_passages = []
        duplicate_count = 0
        replaced_old = 0
        replaced_new: list = []
        replace_notes: list[str] = []
        replaced_sources: set[str] = set()
        skipped_sources: set[str] = set()
        groups = find_reanalysis_groups(self.state.library, batch.results)
        if groups and not self._confirm_replace(groups):
            for group in groups:
                skipped_sources.add(source_key(group.source_file))
                replace_notes.append(
                    f"[{Path(group.source_file).name}] 기존 결과를 유지하여 새 분석 결과는 추가하지 않았습니다."
                )
            groups = []
        if groups:
            for group in groups:
                summary = replace_passages(self.state.library, group.old_passages, group.new_passages)
                replaced_sources.add(source_key(group.source_file))
                replaced_old += summary.removed
                added_passages.extend(group.new_passages)
                replaced_new.extend(group.new_passages)
                name = Path(group.source_file).name
                if summary.remapped_refs:
                    replace_notes.append(
                        f"[{name}] 시험지에 담긴 지문 {summary.remapped_refs}개를 새 지문으로 연결했습니다."
                    )
                if summary.dropped_refs:
                    replace_notes.append(
                        f"[{name}] 새 결과에서 대응 지문을 찾지 못해 시험지에서 {summary.dropped_refs}개를 뺐습니다."
                    )
                if summary.carried_items:
                    replace_notes.append(
                        f"[{name}] 정답·해설·수동 난이도 등 {summary.carried_items}개 항목을 옮겼습니다."
                    )
                if summary.kept_titles:
                    shown = ", ".join(summary.kept_titles[:5])
                    more = f" 외 {len(summary.kept_titles) - 5}개" if len(summary.kept_titles) > 5 else ""
                    replace_notes.append(
                        f"[{name}] 새 결과와 짝이 맞지 않아 이전 지문을 그대로 남겼습니다(입력한 내용 보존): {shown}{more}"
                    )
                if summary.salvaged_titles:
                    shown = ", ".join(summary.salvaged_titles[:5])
                    more = f" 외 {len(summary.salvaged_titles) - 5}개" if len(summary.salvaged_titles) > 5 else ""
                    replace_notes.append(
                        f"[{name}] 옮기지 못한 입력이나 직접 고친 내용이 있어 이전 지문을 '[이전 분석]' 제목으로 "
                        f"보관했습니다: {shown}{more}"
                    )

        known_passages = list(self.state.library.passages)
        for result in batch.results:
            key = source_key(result.source_file)
            if key in replaced_sources or key in skipped_sources:
                self.state.config_manager.add_recent_file(result.source_file)
                continue
            for passage in result.passages:
                existing = next(
                    (item for item in known_passages if passages_equivalent(item, passage)),
                    None,
                )
                if existing is not None:
                    if merge_rich_content(existing, passage):
                        enriched_passages.append(existing)
                    else:
                        duplicate_count += 1
                    continue
                known_passages.append(passage)
                self.state.library.add_passage(passage)
                added_passages.append(passage)
            self.state.config_manager.add_recent_file(result.source_file)

        # 성공한 파일만 대기 목록에서 제거하고 실패 파일은 재시도할 수 있게 남긴다.
        successful = {_normalized_path(path) for path in batch.successful_paths}
        self._pending_files = [
            path for path in self._pending_files if _normalized_path(path) not in successful
        ]
        self._rebuild_file_list()

        warnings = [*replace_notes, *batch.warnings]
        model_changed = bool(batch.model_changed and batch.selected_model)
        if model_changed and self.state.config.gemini_model == batch.original_model:
            previous = self.state.config.gemini_model
            self.state.config.gemini_model = batch.selected_model
            warnings.append(
                f"기존 Gemini 모델({previous})을 사용할 수 없어 {batch.selected_model}로 자동 변경했습니다."
            )
        elif model_changed and self.state.config.gemini_model != batch.original_model:
            warnings.append("분석 중 사용자가 변경한 Gemini 모델 설정을 유지했습니다.")
            model_changed = False
        if duplicate_count:
            warnings.append(f"이미 라이브러리에 있는 중복 지문 {duplicate_count}개는 추가하지 않았습니다.")

        if batch.results or model_changed:
            try:
                self.state.save_config()
            except OSError as exc:
                warnings.append(f"최근 파일/모델 설정 저장 실패: {exc}")

        changed_passages = [*added_passages, *enriched_passages]
        if changed_passages or replaced_old:
            self.state.mark_dirty()
            try:
                self.state.save_library()  # 분석 직후 자동 저장하여 결과 유실 방지
            except OSError as exc:
                warnings.append(f"자동 저장 실패: {exc}. Ctrl+S로 다시 저장해 주세요.")
            question_count = sum(len(passage.questions) for passage in added_passages)
            replaced_text = f", 이전 지문 {replaced_old}개 교체" if replaced_old else ""
            self.state.status(
                f"분석 완료: 새 지문 {len(added_passages)}개, 서식 보강 {len(enriched_passages)}개, "
                f"문제 {question_count}개{replaced_text}",
                7000,
            )
        elif batch.failures:
            self.state.status("PDF 분석에 실패했습니다. 상세 내용을 확인해 주세요.", 7000)
        else:
            self.state.status("새로 추가할 지문이 없습니다.", 5000)

        self._show_analysis_summary(
            batch, added_passages, len(enriched_passages), duplicate_count, warnings,
            replaced_old, replaced_new,
        )
        if changed_passages:
            self.analysis_completed.emit(changed_passages[0].id)

    def _rebuild_file_list(self) -> None:
        self.file_list.clear()
        for path in self._pending_files:
            item = QListWidgetItem(Path(path).name)
            item.setToolTip(path)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.file_list.addItem(item)

    def _show_analysis_summary(
        self,
        batch: AnalysisBatchResult,
        added_passages: list,
        enriched_count: int,
        duplicate_count: int,
        warnings: list[str],
        replaced_old: int = 0,
        replaced_new: list | None = None,
    ) -> None:
        question_count = sum(len(passage.questions) for passage in added_passages)
        replaced_new = replaced_new or []
        ocr_pages = sum(result.ocr_page_count for result in batch.results)
        ai_files = sum(result.used_ai for result in batch.results)
        box = QMessageBox(self)
        box.setWindowTitle("PDF 분석 결과")
        box.setIcon(
            QMessageBox.Icon.Warning if batch.failures or warnings else QMessageBox.Icon.Information
        )
        if replaced_old:
            replaced_questions = sum(len(passage.questions) for passage in replaced_new)
            text = (
                f"이전 지문 {replaced_old}개를 새 분석 결과(지문 {len(replaced_new)}개·문제 "
                f"{replaced_questions}개)로 교체했습니다."
            )
            other = len(added_passages) - len(replaced_new)
            if other > 0:
                text += f"\n다른 파일에서 새 지문 {other}개도 추가했습니다."
            box.setText(text)
        else:
            box.setText(
                f"새 지문 {len(added_passages)}개·문제 {question_count}개를 추가하고, "
                f"기존 지문 {enriched_count}개의 서식·그림을 보강했습니다."
            )
        details = [
            f"성공 파일: {len(batch.results)}개",
            f"실패 파일: {len(batch.failures)}개",
            f"OCR 사용 페이지: {ocr_pages}개",
            f"Gemini 교차 분석 파일: {ai_files}개",
            f"서식·그림 보강 지문: {enriched_count}개",
            f"중복 제외 지문: {duplicate_count}개",
        ]
        box.setInformativeText("\n".join(details))
        if warnings:
            box.setDetailedText("\n".join(f"• {warning}" for warning in warnings))
        box.exec()

    def _on_analysis_error(self, exc: Exception) -> None:
        QMessageBox.critical(
            self,
            "분석 오류",
            f"분석 작업을 시작하지 못했습니다.\n\n{exc}\n\n오류 화면을 캡처해 보내 주세요.",
        )

    def _on_analysis_finished(self) -> None:
        self._set_analysis_busy(False)
