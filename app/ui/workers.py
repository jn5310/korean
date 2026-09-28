"""백그라운드 작업 실행 (Gemini 호출, PDF 분석 등) — GUI 멈춤 방지."""
from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal, pyqtSlot

logger = logging.getLogger(__name__)


class WorkerSignals(QObject):
    result = pyqtSignal(object)
    error = pyqtSignal(object)            # Exception 인스턴스
    progress = pyqtSignal(int, int, str)  # 현재, 전체, 메시지
    finished = pyqtSignal()


class Worker(QRunnable):
    """임의의 함수를 QThreadPool 에서 실행한다.

    fn 이 `progress` 키워드 인자를 받으면 진행률 콜백을 넘겨준다:
        def job(path, progress): progress(1, 10, "1페이지 분석 중")
    """

    def __init__(self, fn: Callable[..., Any], *args: Any, with_progress: bool = False, **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        if with_progress:
            self.kwargs["progress"] = self.signals.progress.emit

    @pyqtSlot()
    def run(self) -> None:
        try:
            value = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Worker 오류:\n%s", traceback.format_exc())
            self.signals.error.emit(exc)
        else:
            self.signals.result.emit(value)
        finally:
            self.signals.finished.emit()
