"""애플리케이션 진입점.

실행:  python main.py
"""
from __future__ import annotations

import logging
import sys

from PyQt6.QtWidgets import QApplication

from app import APP_NAME, __version__
from app.config import ConfigManager
from app.storage import LibraryRepository
from app.ui.main_window import MainWindow
from app.ui.state import AppState
from app.ui.styles import APP_STYLESHEET


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName(APP_NAME)
    qt_app.setApplicationVersion(__version__)
    qt_app.setStyleSheet(APP_STYLESHEET)

    config_manager = ConfigManager()
    repository = LibraryRepository(config_manager.data_dir / "library.json")
    state = AppState(config_manager, repository)

    window = MainWindow(state)
    window.show()
    return qt_app.exec()


if __name__ == "__main__":
    sys.exit(main())
