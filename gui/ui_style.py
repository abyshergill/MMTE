"""Application-wide dark stylesheet.

All colours come from ``TOKENS`` so the whole app (main window, docks, dialogs,
message boxes, the AI panel) shares one look.  The stylesheet is applied to the
``QApplication`` in ``main.py`` (and to ``TerminalApp`` itself so the window also
looks right when it is created without ``main.py``).

Semantic object names understood here:

* buttons:  ``AccentBtn`` / ``PrimaryBtn`` (main action), ``DangerBtn``,
  ``GhostBtn`` (quiet), default (secondary)
* labels:   ``SectionTitle``, ``Subtle``, ``Hint``
* frames:   ``Card``
* AI panel: ``AIPanel``, ``AIChat``, ``AIPrompt``, ``ModeBadge[mode=auto|ask]``
"""

from core.theme import TERMINAL_ACCENT, TERMINAL_BG, TERMINAL_FG  # noqa: F401  (re-exported for callers)

TOKENS = {
    "bg0": "#0b0e14",       # window / deepest surface
    "bg1": "#11151d",       # panels, sidebar, menus
    "bg2": "#171c26",       # inputs, cards
    "bg3": "#212838",       # hover
    "line": "#222a38",      # hairlines
    "line2": "#323c52",     # stronger borders
    "text": "#e6edf3",
    "dim": "#9aa8b6",
    "mute": "#6b7a89",
    "accent": "#7c8cff",
    "accent_hi": "#a0acff",
    "accent_fg": "#0a0d1f",
    "info": "#5aa9ff",
    "ok": "#4cc38a",
    "warn": "#e0a93b",
    "err": "#ef6a73",
    "err_bg": "#33191d",
    "err_line": "#6b2f39",
    "select": "#2a3566",
    "term_bg": TERMINAL_BG,
    "term_fg": TERMINAL_FG,
    "mono": "'JetBrains Mono', 'SF Mono', Menlo, 'Cascadia Mono', 'DejaVu Sans Mono', Consolas, monospace",
    "sans": "'Segoe UI Variable', 'Segoe UI', Inter, 'SF Pro Text', 'Helvetica Neue', Arial, sans-serif",
}

# Colours used when the AI panel writes rich text into its chat view.
CHAT_COLORS = {
    "user": TOKENS["info"],
    "ai": TOKENS["ok"],
    "muted": TOKENS["mute"],
    "warn": TOKENS["warn"],
    "err": TOKENS["err"],
    "note": TOKENS["accent"],
}

_TEMPLATE = """
QWidget { color: @text@; }
QMainWindow, QDialog { background-color: @bg0@; }
QDialog QLabel { background: transparent; }
QToolTip {
    background-color: @bg2@; color: @text@;
    border: 1px solid @line2@; padding: 5px 8px; border-radius: 4px;
}

/* --- Menus --- */
QMenuBar {
    background-color: @bg1@; color: @text@; padding: 2px 8px;
    border-bottom: 1px solid @line@;
}
QMenuBar::item { background: transparent; padding: 7px 14px; margin: 2px 1px; border-radius: 5px; }
QMenuBar::item:selected { background-color: @bg3@; color: @accent@; }
QMenuBar::item:pressed { background-color: @bg3@; }
QMenu {
    background-color: @bg1@; color: @text@; border: 1px solid @line2@;
    padding: 6px; border-radius: 10px;
}
QMenu::item { padding: 7px 30px 7px 16px; border-radius: 5px; }
QMenu::item:selected { background-color: @bg3@; color: @accent@; }
QMenu::item:disabled { color: @mute@; }
QMenu::separator { height: 1px; background: @line@; margin: 6px 8px; }

/* --- Sidebar --- */
QFrame#Sidebar { background-color: @bg1@; border-right: 1px solid @line@; }
QLabel#SidebarTitle { color: @dim@; letter-spacing: 1.5px; background: transparent; }
QLabel#SidebarSubtitle {
    color: @mute@; font-size: 11px; letter-spacing: 1.5px; padding-top: 2px; background: transparent;
}
QPushButton#CollapseBtn {
    background: transparent; color: @dim@; border: none; border-radius: 5px; font-weight: bold; padding: 0;
}
QPushButton#CollapseBtn:hover { background-color: @bg3@; color: @accent@; }
QListWidget#SessionList { background-color: transparent; border: none; outline: none; }
QListWidget#SessionList::item { padding: 0; margin: 2px 0; }
QListWidget#SessionList::item:selected { background-color: @select@; border: 1px solid @accent@; }

/* --- Buttons --- */
QPushButton {
    background-color: @bg2@; color: @text@; border: 1px solid @line2@;
    border-radius: 8px; padding: 7px 16px; min-height: 20px;
}
QPushButton:hover { background-color: @bg3@; border-color: @accent@; }
QPushButton:pressed { background-color: @bg1@; }
QPushButton:focus { border-color: @accent@; }
QPushButton:disabled { color: @mute@; border-color: @line@; background-color: @bg1@; }
QPushButton#AccentBtn, QPushButton#PrimaryBtn {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 @accent@, stop:1 #5fd0c4); color: @accent_fg@; border: 1px solid @accent@; font-weight: 600;
}
QPushButton#AccentBtn:hover, QPushButton#PrimaryBtn:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 @accent_hi@, stop:1 #7fe0d5); border-color: @accent_hi@; }
QPushButton#AccentBtn:pressed, QPushButton#PrimaryBtn:pressed { background: @accent@; }
QPushButton#AccentBtn:disabled, QPushButton#PrimaryBtn:disabled {
    background-color: @bg2@; color: @mute@; border-color: @line@;
}
QPushButton#DangerBtn { background-color: @err_bg@; border-color: @err_line@; color: #f4b9be; }
QPushButton#DangerBtn:hover { background-color: #4a222a; border-color: @err@; }
QPushButton#GhostBtn { background: transparent; border: 1px solid transparent; color: @dim@; }
QPushButton#GhostBtn:hover { background-color: @bg3@; color: @text@; border-color: @line2@; }

/* --- Inputs --- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit {
    background-color: @bg2@; color: @text@; border: 1px solid @line2@;
    border-radius: 8px; padding: 6px 10px;
    selection-background-color: @select@; selection-color: @text@;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QTextEdit:focus, QPlainTextEdit:focus { border-color: @accent@; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QTextEdit:disabled, QPlainTextEdit:disabled {
    color: @mute@; background-color: @bg1@;
}
QComboBox QAbstractItemView {
    background-color: @bg1@; border: 1px solid @line2@; selection-background-color: @bg3@;
    selection-color: @accent@; outline: none; padding: 3px;
}

/* The live terminal must keep exact geometry (columns/rows are computed from its
   viewport and font metrics), so undo every generic text-input rule for it. */
QPlainTextEdit#TerminalView, QPlainTextEdit#TerminalView:focus {
    background-color: @term_bg@; color: @term_fg@; border: none; border-radius: 0;
    padding: 0; selection-background-color: @term_fg@; selection-color: @term_bg@;
}

/* --- Check / radio --- */
QCheckBox, QRadioButton { spacing: 8px; background: transparent; }
QCheckBox::indicator, QRadioButton::indicator {
    width: 15px; height: 15px; border: 1px solid @line2@; background: @bg2@; border-radius: 4px;
}
QRadioButton::indicator { border-radius: 8px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: @accent@; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background-color: @accent@; border-color: @accent@;
}
QCheckBox::indicator:disabled { background: @bg1@; border-color: @line@; }
QCheckBox:disabled { color: @mute@; }

/* --- Lists, tables, trees --- */
QListWidget, QListView, QTreeView, QTreeWidget, QTableView, QTableWidget {
    background-color: @bg2@; border: 1px solid @line@; border-radius: 6px; outline: none;
    alternate-background-color: @bg1@; gridline-color: @line@;
    selection-background-color: @select@; selection-color: @text@;
}
QListWidget::item { padding: 6px 8px; border-radius: 6px; }
QListWidget::item:hover { background-color: @bg3@; }
QListWidget::item:selected { background-color: @select@; color: @text@; }
QHeaderView::section {
    background-color: @bg1@; color: @dim@; padding: 6px 8px; border: none;
    border-bottom: 1px solid @line2@; border-right: 1px solid @line@;
}

/* --- Group boxes / tabs --- */
QGroupBox {
    border: 1px solid @line@; border-radius: 8px; margin-top: 12px; padding: 12px 8px 8px 8px;
    background-color: transparent;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: @dim@; }
QTabWidget::pane { border: 1px solid @line@; border-radius: 6px; top: -1px; }

QTabBar { background: transparent; }
QTabBar::tab {
    background: @bg1@; color: @dim@; padding: 7px 10px 7px 16px; margin-right: 2px;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
    border: 1px solid @line@; border-bottom: none;
}
QTabBar::tab:selected {
    background: @bg0@; color: @accent@; border-color: @line2@; border-bottom: 2px solid @accent@;
}
QTabBar::tab:hover:!selected { color: @text@; background-color: @bg2@; }
QTabBar::close-button { subcontrol-position: right; margin: 0 2px 0 6px; }

/* --- Dock widget (AI panel container) --- */
QDockWidget { color: @text@; titlebar-close-icon: none; }
QDockWidget::title {
    background-color: @bg1@; color: @dim@; padding: 8px 12px; text-align: left;
    border-bottom: 1px solid @line@; letter-spacing: 1px;
}
QMainWindow::separator { background: @line@; width: 1px; height: 1px; }
QMainWindow::separator:hover { background: @accent@; }
QSplitter::handle { background: @line@; }

/* --- Scrollbars --- */
QScrollBar:vertical { background: transparent; width: 11px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 2px; }
QScrollBar::handle:vertical { background: @line2@; border-radius: 4px; min-height: 28px; }
QScrollBar::handle:horizontal { background: @line2@; border-radius: 4px; min-width: 28px; }
QScrollBar::handle:hover { background: @mute@; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QProgressBar {
    background-color: @bg2@; border: 1px solid @line@; border-radius: 5px; text-align: center; height: 14px;
}
QProgressBar::chunk { background-color: @accent@; border-radius: 4px; }

/* --- Status bar / status text --- */
QLabel#StatusBar {
    background-color: @bg1@; color: @dim@; padding: 6px 14px;
    border-top: 1px solid @line@; font-size: 12px;
}

/* --- Reusable label / frame roles --- */
QLabel#SectionTitle { color: @text@; font-weight: 600; letter-spacing: 0.5px; background: transparent; }
QLabel#Subtle { color: @dim@; font-size: 12px; background: transparent; }
QLabel#Hint { color: @mute@; font-size: 11px; background: transparent; }
QFrame#Card { background-color: @bg2@; border: 1px solid @line@; border-radius: 8px; }

/* --- AI Test Engineer panel --- */
QWidget#AIPanel { background-color: @bg1@; }
QLabel#AITitle { color: @text@; font-size: 13px; font-weight: 700; letter-spacing: 1.5px; background: transparent; }
QFrame#AICard { background-color: @bg2@; border: 1px solid @line@; border-radius: 8px; }
QFrame#AICard QLabel { background: transparent; border: none; }
QFrame#AIModeBar { background-color: @bg2@; border: 1px solid @line@; border-radius: 8px; }
QFrame#AIModeBar QLabel, QFrame#AIModeBar QCheckBox { background: transparent; }
QTextEdit#AIChat {
    background-color: @bg0@; border: 1px solid @line@; border-radius: 8px; padding: 8px;
    font-family: @mono@; font-size: 12px;
}
QTextEdit#AIPrompt { background-color: @bg2@; border-radius: 8px; padding: 8px; font-size: 13px; }
QLabel#AIStatus { color: @warn@; font-size: 11px; background: transparent; }
QLabel#ModeBadge { border-radius: 9px; padding: 2px 9px; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
QLabel#ModeBadge[mode="auto"] { background-color: #1c2250; color: @accent@; border: 1px solid #3a4590; }
QLabel#ModeBadge[mode="ask"]  { background-color: #3a2f12; color: @warn@;   border: 1px solid #5c4a1d; }

/* --- Run-in-terminal dialog --- */
QPlainTextEdit#CommandEditor {
    font-family: @mono@; font-size: 12px; background-color: @bg0@;
}
QListWidget#TerminalPicker::item { padding: 8px 8px; }

/* --- Square icon buttons (DAW rows, tab "+") --- */
QPushButton#IconBtn, QToolButton#IconBtn {
    background-color: @bg2@; border: 1px solid @line2@; border-radius: 7px; padding: 0; min-height: 0;
}
QPushButton#IconBtn:hover, QToolButton#IconBtn:hover { background-color: @bg3@; border-color: @accent@; }
QPushButton#IconBtn:disabled { background-color: @bg1@; border-color: @line@; }
QPushButton#RunBtn {
    background-color: #173a2c; border: 1px solid #2d6b52; color: #bdf0d6;
    border-radius: 7px; padding: 0 12px 0 8px; min-height: 0; font-weight: 600;
}
QPushButton#RunBtn:hover { background-color: #1d4a38; border-color: @ok@; color: #ffffff; }
QPushButton#RunBtn:disabled { background-color: @bg1@; border-color: @line@; color: @mute@; }
QPushButton#RemoveBtn {
    background-color: transparent; border: 1px solid transparent; border-radius: 7px; padding: 0; min-height: 0;
}
QPushButton#RemoveBtn:hover { background-color: @err_bg@; border-color: @err@; }
QToolButton#NewTabBtn {
    background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 0; margin: 0 0 2px 4px;
}
QToolButton#NewTabBtn:hover { background-color: @bg3@; border-color: @line2@; }
QFrame#DawRow { background-color: @bg2@; border: 1px solid @line@; border-radius: 8px; }
QFrame#DawRow QLabel { background: transparent; border: none; }
QLabel#RecBadge {
    background-color: #3a1218; color: #ff8a93; border: 1px solid #7a2a35;
    border-radius: 9px; padding: 2px 10px; font-size: 11px; font-weight: 700; letter-spacing: 1px;
}

/* --- Built-in text editor --- */
QPlainTextEdit#CodeEditor, QPlainTextEdit#CodeEditor:focus {
    background-color: #0f131a; color: #d7dde5; border: none; border-radius: 0; padding: 0;
    selection-background-color: #264f78; selection-color: #ffffff;
}
QFrame#FindBar { background-color: @bg1@; border-top: 1px solid @line@; }
QStatusBar { background-color: @bg1@; color: @dim@; border-top: 1px solid @line@; }
QStatusBar QLabel { color: @dim@; padding: 0 10px; background: transparent; }
QToolBar { background-color: @bg1@; border: none; border-bottom: 1px solid @line@; spacing: 2px; padding: 3px 6px; }
QToolBar QToolButton { background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 4px 8px; color: @text@; }
QToolBar QToolButton:hover { background-color: @bg3@; border-color: @line2@; }
QTabWidget#EditorTabs::pane { border: none; }
"""


def build_stylesheet(tokens: dict[str, str] | None = None) -> str:
    values = dict(TOKENS)
    values.update(tokens or {})
    sheet = _TEMPLATE
    # Longest keys first so e.g. ``accent_hi`` is never clobbered by ``accent``.
    for key in sorted(values, key=len, reverse=True):
        sheet = sheet.replace(f"@{key}@", values[key])
    return sheet


APP_STYLESHEET = build_stylesheet()
