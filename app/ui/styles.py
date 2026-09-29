"""공용 스타일시트 및 난이도 색상."""

DIFFICULTY_COLORS = {
    None: "#9e9e9e",
    1: "#43a047",
    2: "#7cb342",
    3: "#fbc02d",
    4: "#fb8c00",
    5: "#e53935",
}


def difficulty_color(level) -> str:
    return DIFFICULTY_COLORS.get(level, DIFFICULTY_COLORS[None])


APP_STYLESHEET = """
QWidget { font-size: 13px; }
QMainWindow { background: #f5f6f8; }

#NavList {
    background: #263238; color: #eceff1; border: none;
    font-size: 14px; padding-top: 12px;
}
#NavList::item { padding: 14px 18px; }
#NavList::item:selected { background: #37474f; border-left: 4px solid #4fc3f7; color: white; }

#Card {
    background: white; border: 1px solid #dfe3e8; border-radius: 8px;
}
#CardTitle { font-size: 15px; font-weight: bold; color: #263238; }
#PageTitle { font-size: 20px; font-weight: bold; color: #263238; }
#Muted { color: #78909c; }

#DropArea {
    border: 2px dashed #90a4ae; border-radius: 8px; background: #fafbfc;
    color: #607d8b; font-size: 14px; min-height: 90px;
}
#DropArea[dragActive="true"] { border-color: #0288d1; background: #e1f5fe; }

#StatusOk { color: #2e7d32; font-weight: bold; }
#StatusWarn { color: #ef6c00; font-weight: bold; }
#StatusError { color: #c62828; font-weight: bold; }

QPushButton {
    padding: 6px 14px; border-radius: 4px; border: 1px solid #b0bec5; background: white;
}
QPushButton:hover { background: #eceff1; }
QPushButton:disabled { color: #b0bec5; }
QPushButton#Primary { background: #0288d1; color: white; border: none; }
QPushButton#Primary:hover { background: #0277bd; }
QPushButton#Primary:disabled { background: #90caf9; }

QTextEdit#RichTextEdit {
    background: white; border: 1px solid #cfd8dc; border-radius: 3px; padding: 4px;
}
QTextEdit#RichTextEdit:focus { border: 1px solid #0288d1; }
#RichTextToolbar { background: #f5f7f8; border-radius: 3px; }
#RichTextToolbar QToolButton { padding: 3px 7px; border: 1px solid #cfd8dc; background: white; }
#RichTextToolbar QToolButton:checked { background: #b3e5fc; border-color: #0288d1; }
"""
