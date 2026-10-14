from core.theme import TERMINAL_ACCENT, TERMINAL_BG, TERMINAL_FG

APP_STYLESHEET = f"""
QMainWindow {{
    background-color: #12161b;
}}

/* --- Top menu bar --- */
QMenuBar {{
    background-color: #171c22;
    color: #dbe6e2;
    padding: 3px 8px;
    font-size: 13px;
    border-bottom: 1px solid #262d35;
}}
QMenuBar::item {{
    background: transparent;
    padding: 7px 16px;
    margin: 1px 2px;
    border-radius: 5px;
}}
QMenuBar::item:selected {{
    background-color: #212a32;
    color: {TERMINAL_ACCENT};
}}
QMenuBar::item:pressed {{
    background-color: #26313b;
}}
QMenu {{
    background-color: #171c22;
    color: #dbe6e2;
    border: 1px solid #2a323c;
    padding: 6px;
    border-radius: 6px;
}}
QMenu::item {{
    padding: 7px 28px 7px 16px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: #223138;
    color: {TERMINAL_ACCENT};
}}
QMenu::item:disabled {{
    color: #56626b;
}}
QMenu::separator {{
    height: 1px;
    background: #2a323c;
    margin: 5px 8px;
}}

/* --- Sidebar --- */
QFrame#Sidebar {{
    background-color: #171c22;
    border-right: 1px solid #262d35;
}}
QLabel#SidebarTitle {{
    color: #8aa0ac;
    letter-spacing: 1px;
}}
QLabel#SidebarSubtitle {{
    color: #5c6b74;
    font-size: 11px;
    letter-spacing: 1px;
    padding-top: 2px;
}}
QPushButton#CollapseBtn {{
    background: transparent;
    color: #8aa0ac;
    border: none;
    border-radius: 4px;
    font-weight: bold;
}}
QPushButton#CollapseBtn:hover {{
    background-color: #222a32;
    color: {TERMINAL_ACCENT};
}}
QListWidget#SessionList {{
    background-color: transparent;
    border: none;
}}

/* --- Generic buttons --- */
QPushButton {{
    background-color: #232b33;
    color: #dbe6e2;
    border: 1px solid #2e3841;
    border-radius: 6px;
    padding: 7px 12px;
}}
QPushButton:hover {{
    background-color: #2b3540;
    border-color: {TERMINAL_ACCENT};
}}
QPushButton:pressed {{
    background-color: #1c232a;
}}
QPushButton:disabled {{
    color: #55626b;
    border-color: #262d35;
    background-color: #1b2127;
}}
QPushButton#AccentBtn {{
    background-color: {TERMINAL_ACCENT};
    color: #06231d;
    border: none;
    font-weight: bold;
}}
QPushButton#AccentBtn:hover {{
    background-color: #55d9c8;
}}
QPushButton#DangerBtn {{
    background-color: #3a2126;
    border-color: #6b2f39;
    color: #f0b7bd;
}}
QPushButton#DangerBtn:hover {{
    background-color: #5c2530;
}}

/* --- Tab bar --- */
QTabBar::tab {{
    background: #171c22;
    color: #8aa0ac;
    padding: 7px 20px;
    margin-right: 2px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    border: 1px solid #262d35;
    border-bottom: none;
}}
QTabBar::tab:selected {{
    background: {TERMINAL_BG};
    color: {TERMINAL_ACCENT};
    border-color: #2e3841;
    border-bottom: 2px solid {TERMINAL_ACCENT};
}}
QTabBar::tab:hover {{
    color: #dbe6e2;
}}

/* --- Status bar --- */
QLabel#StatusBar {{
    background-color: #171c22;
    color: #8aa0ac;
    padding: 5px 12px;
    border-top: 1px solid #262d35;
    font-size: 12px;
}}

QLineEdit, QComboBox {{
    background-color: #171c22;
    border: 1px solid #2e3841;
    border-radius: 5px;
    padding: 5px 8px;
    color: #dbe6e2;
}}
QLineEdit:focus, QComboBox:focus {{
    border-color: {TERMINAL_ACCENT};
}}
"""