"""화면들이 공유하는 애플리케이션 상태 (설정, 라이브러리, 스레드 풀)."""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, QThreadPool, pyqtSignal

from ..assets import AssetStore
from ..config import AppConfig, ConfigManager
from ..models import Library
from ..services.gemini_client import GeminiClient
from ..storage import LibraryRepository
from .workers import Worker

logger = logging.getLogger(__name__)


class AppState(QObject):
    library_changed = pyqtSignal()           # 지문 추가/삭제/수정
    config_changed = pyqtSignal()            # API Key/모델 등 설정 변경
    dirty_changed = pyqtSignal(bool)         # 저장 안 된 변경 여부
    status_message = pyqtSignal(str, int)    # 메시지, 표시 시간(ms)

    def __init__(self, config_manager: ConfigManager, repository: LibraryRepository) -> None:
        super().__init__()
        self.config_manager = config_manager
        self.repository = repository
        self.asset_store = AssetStore(config_manager.data_dir)
        self.library: Library = repository.load()
        self.thread_pool = QThreadPool.globalInstance()
        self._dirty = False

    # --- 설정 ---------------------------------------------------------
    @property
    def config(self) -> AppConfig:
        return self.config_manager.config

    @property
    def has_api_key(self) -> bool:
        return bool(self.config_manager.effective_api_key)

    def save_config(self) -> None:
        self.config_manager.save()
        self.config_changed.emit()

    def create_gemini_client(self, api_key: Optional[str] = None, model: Optional[str] = None) -> GeminiClient:
        cfg = self.config
        return GeminiClient(
            api_key=api_key if api_key is not None else self.config_manager.effective_api_key,
            model=model or cfg.gemini_model,
            timeout_sec=cfg.gemini_timeout_sec,
            max_retries=cfg.gemini_max_retries,
            requests_per_minute=cfg.gemini_requests_per_minute,
        )

    # --- 라이브러리 -----------------------------------------------------
    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def mark_dirty(self, notify_library: bool = True) -> None:
        if not self._dirty:
            self._dirty = True
            self.dirty_changed.emit(True)
        if notify_library:
            self.library_changed.emit()

    def save_library(self) -> None:
        self.repository.save(self.library)
        self._dirty = False
        self.dirty_changed.emit(False)
        self.status("라이브러리를 저장했습니다.")

    # --- 공용 유틸 ------------------------------------------------------
    def status(self, message: str, timeout_ms: int = 4000) -> None:
        self.status_message.emit(message, timeout_ms)

    def run_async(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_result: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        on_finished: Optional[Callable[[], None]] = None,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
        **kwargs: Any,
    ) -> Worker:
        worker = Worker(fn, *args, with_progress=on_progress is not None, **kwargs)
        if on_result:
            worker.signals.result.connect(on_result)
        if on_error:
            worker.signals.error.connect(on_error)
        if on_finished:
            worker.signals.finished.connect(on_finished)
        if on_progress:
            worker.signals.progress.connect(on_progress)
        self.thread_pool.start(worker)
        return worker
