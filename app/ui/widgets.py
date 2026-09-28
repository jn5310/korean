"""여러 화면에서 재사용하는 작은 위젯들."""
from __future__ import annotations

from PyQt6.QtWidgets import QComboBox, QFrame, QLabel, QVBoxLayout, QWidget

from ..config import DIFFICULTY_LEVELS
from .styles import difficulty_color


class Card(QFrame):
    """제목이 있는 흰색 카드 컨테이너."""

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 16)
        self.body.setSpacing(10)
        if title:
            label = QLabel(title)
            label.setObjectName("CardTitle")
            self.body.addWidget(label)


class DifficultyCombo(QComboBox):
    """미분류(None) + 1~5단계 선택 콤보."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.addItem("미분류", None)
        for level, label in DIFFICULTY_LEVELS.items():
            self.addItem(label, level)

    def value(self):
        return self.currentData()

    def set_value(self, level) -> None:
        # findData(None) 은 Qt 버전에 따라 동작이 달라 직접 비교한다
        idx = next((i for i in range(self.count()) if self.itemData(i) == level), 0)
        self.setCurrentIndex(idx)


def difficulty_badge_html(level) -> str:
    text = f"Lv.{level}" if level else "미분류"
    return (f'<span style="background:{difficulty_color(level)};color:white;'
            f'padding:1px 6px;border-radius:3px;">{text}</span>')


def make_type_combo(options: list[str], parent: QWidget | None = None) -> QComboBox:
    """제안 목록 + 자유 입력 가능한 유형 콤보."""
    combo = QComboBox(parent)
    combo.setEditable(True)
    combo.addItem("")
    combo.addItems(options)
    return combo
