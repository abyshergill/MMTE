#!/usr/bin/env python3
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication

from core.config import CONFIG_DIR, LOG_DIR
from core.qt_theme import apply_dark_theme
from gui.main_window import TerminalApp
from gui.ui_style import APP_STYLESHEET


def main():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    apply_dark_theme(app)
    app.setStyleSheet(APP_STYLESHEET)
    window = TerminalApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()