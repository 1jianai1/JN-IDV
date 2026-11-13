"""深色专业开发工具主题（纯 QSS + QPalette）。"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

# 全局颜色（绘图代码也引用这里，保证页面风格一致）
COLORS = {
    "bg": "#14171c",
    "panel": "#1b1f26",
    "panel_alt": "#1d222b",
    "panel_hi": "#232a34",
    "border": "#2b323d",
    "border_hi": "#3a4352",
    "text": "#d6dde8",
    "text_hi": "#f0f4fa",
    "muted": "#8b97a8",
    "dim": "#5c6675",
    "accent": "#4fc3f7",
    "accent_strong": "#2f81f7",
    "ok": "#66bb6a",
    "warn": "#ffb74d",
    "error": "#ef5350",
    # 像素分量颜色
    "U": "#4fc3f7",
    "Y": "#e8e8e8",
    "V": "#f06292",
    "R": "#ef5350",
    "G": "#66bb6a",
    "B": "#42a5f5",
    "B0": "#90a4ae",
    "B1": "#b0bec5",
    "SAV": "#ffb74d",
    "EAV": "#ff8a65",
    "raw": "#9aa5b7",
}

MONOMER_FONT = "Consolas"


def mono_font(size: int = 9, bold: bool = False) -> QFont:
    font = QFont(MONOMER_FONT, size)
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setBold(bold)
    return font


def component_color(component: str | None) -> QColor:
    if not component:
        return QColor(COLORS["raw"])
    key = component
    if key in ("Y0", "Y1", "Luma"):
        key = "Y"
    if key in ("SAV", "EAV"):
        return QColor(COLORS[key])
    return QColor(COLORS.get(key, COLORS["raw"]))


STYLESHEET = f"""
QWidget {{
    background-color: {COLORS['bg']};
    color: {COLORS['text']};
    font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
    font-size: 9pt;
}}
QMainWindow, QDialog {{ background-color: {COLORS['bg']}; }}
QToolTip {{
    background-color: {COLORS['panel_hi']};
    color: {COLORS['text_hi']};
    border: 1px solid {COLORS['border_hi']};
    padding: 4px 6px;
}}
QGroupBox {{
    background-color: {COLORS['panel']};
    border: 1px solid {COLORS['border']};
    border-radius: 6px;
    margin-top: 15px;
    padding: 10px 8px 8px 8px;
    font-weight: 600;
    color: #aeb9c9;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 4px;
    color: #9fb0c6;
}}
QLabel {{ background: transparent; }}
QLabel#muted {{ color: {COLORS['muted']}; }}
QLabel#hint {{ color: {COLORS['dim']}; font-size: 8pt; }}
QLabel#value {{ color: {COLORS['accent']}; font-family: {MONOMER_FONT}; }}
QLabel#statusOk {{ color: {COLORS['ok']}; font-weight: 600; }}
QLabel#statusErr {{ color: {COLORS['error']}; font-weight: 600; }}
QLabel#statusWarn {{ color: {COLORS['warn']}; font-weight: 600; }}
QLabel#sectionTitle {{ color: {COLORS['text_hi']}; font-size: 10pt; font-weight: 600; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: {COLORS['panel_alt']};
    border: 1px solid {COLORS['border']};
    border-radius: 4px;
    padding: 3px 6px;
    min-height: 18px;
    selection-background-color: {COLORS['accent_strong']};
    selection-color: white;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {COLORS['accent']};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {COLORS['dim']};
    background-color: #191d24;
}}
QComboBox QAbstractItemView {{
    background-color: {COLORS['panel_alt']};
    border: 1px solid {COLORS['border_hi']};
    selection-background-color: {COLORS['accent_strong']};
    selection-color: white;
    outline: none;
}}
QPushButton {{
    background-color: {COLORS['panel_hi']};
    border: 1px solid #333c49;
    border-radius: 4px;
    padding: 5px 10px;
    color: {COLORS['text']};
}}
QPushButton:hover {{ background-color: #2b3441; border-color: #4a5768; }}
QPushButton:pressed {{ background-color: #1a2029; }}
QPushButton:disabled {{ color: {COLORS['dim']}; background-color: #1a1e25; border-color: #252b34; }}
QPushButton#primary {{
    background-color: #2563eb;
    border: 1px solid #3b82f6;
    color: white;
    font-weight: 600;
    padding: 8px;
}}
QPushButton#primary:hover {{ background-color: #3b82f6; }}
QPushButton#primary:disabled {{ background-color: #1e2a40; border-color: #2a3a54; color: #6b7c95; }}
QPushButton#stop {{ background-color: #3a2226; border-color: #5c2a30; color: #ff9a9a; }}
QPushButton#stop:hover {{ background-color: #4a2a2f; }}
QPushButton#stop:disabled {{ color: #6b4a4f; background-color: #24191c; border-color: #352226; }}
QPushButton#mini {{ padding: 2px 6px; font-size: 8pt; }}
QToolButton {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 2px 6px;
    color: {COLORS['muted']};
}}
QToolButton:hover {{ background-color: {COLORS['panel_hi']}; color: {COLORS['text_hi']}; border-color: {COLORS['border']}; }}
QToolButton:pressed {{ background-color: #1a2029; }}
QToolButton#refresh {{ font-size: 12pt; padding: 0px; color: {COLORS['muted']}; }}
QToolButton#refresh:hover {{ color: {COLORS['accent']}; }}
QToolButton#more {{
    background-color: {COLORS['panel_hi']};
    border: 1px solid {COLORS['border']};
    color: {COLORS['text']};
    padding: 0px;
    margin: 2px 6px 2px 2px;
}}
QToolButton#more::menu-indicator {{
    image: none;
    width: 0px;
}}
QToolButton#more:hover {{ border-color: {COLORS['accent']}; }}
QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{
    width: 14px; height: 14px;
    border-radius: 3px;
    border: 1px solid {COLORS['border_hi']};
    background-color: {COLORS['panel_alt']};
}}
QCheckBox::indicator:checked {{ background-color: {COLORS['accent_strong']}; border-color: {COLORS['accent_strong']}; }}
QCheckBox::indicator:disabled {{ border-color: #2a313b; }}
QTabWidget::pane {{
    border: 1px solid {COLORS['border']};
    border-radius: 0px 6px 6px 6px;
    background-color: {COLORS['bg']};
    top: -1px;
}}
QTabBar::tab {{
    background-color: #181c23;
    border: 1px solid {COLORS['border']};
    border-bottom: none;
    padding: 6px 20px;
    margin-right: 2px;
    color: {COLORS['muted']};
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
}}
QTabBar::tab:selected {{
    background-color: {COLORS['panel']};
    color: {COLORS['text_hi']};
    border-top: 2px solid {COLORS['accent']};
    padding-top: 5px;
}}
QTabBar::tab:hover {{ color: {COLORS['text']}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0px; }}
QScrollBar::handle:vertical {{ background: #39424f; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: #4a5566; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0px; }}
QScrollBar::handle:horizontal {{ background: #39424f; border-radius: 5px; min-width: 28px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
QStatusBar {{
    background-color: #12151a;
    color: {COLORS['muted']};
    border-top: 1px solid #232a34;
}}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {COLORS['muted']}; }}
QMenu {{
    background-color: {COLORS['panel_alt']};
    border: 1px solid {COLORS['border_hi']};
    padding: 5px;
}}
QMenu::item {{ padding: 5px 26px 5px 12px; border-radius: 3px; }}
QMenu::item:selected {{ background-color: {COLORS['accent_strong']}; color: white; }}
QMenu::item:disabled {{ color: {COLORS['dim']}; }}
QMenu::separator {{ height: 1px; background-color: {COLORS['border']}; margin: 5px 8px; }}
QMenu::icon {{ padding-left: 8px; }}
QProgressBar {{
    background-color: {COLORS['panel_alt']};
    border: 1px solid {COLORS['border']};
    border-radius: 3px;
    text-align: center;
    max-height: 8px;
    color: transparent;
}}
QProgressBar::chunk {{ background-color: {COLORS['accent']}; border-radius: 2px; }}
QSplitter::handle {{ background-color: {COLORS['border']}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 5px; }}
QSplitter::handle:hover {{ background-color: {COLORS['accent']}; }}
QPlainTextEdit, QTextEdit {{
    background-color: #101318;
    border: 1px solid {COLORS['border']};
    border-radius: 4px;
    selection-background-color: #2f4f7f;
    selection-color: white;
}}
QScrollArea {{ border: none; background: transparent; }}
QFrame#card {{
    background-color: {COLORS['panel']};
    border: 1px solid {COLORS['border']};
    border-radius: 6px;
}}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(COLORS["bg"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(COLORS["panel_alt"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(COLORS["panel"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(COLORS["panel_hi"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(COLORS["accent_strong"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(COLORS["panel_hi"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(COLORS["dim"]))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)
