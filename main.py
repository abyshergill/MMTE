#!/usr/bin/env python3
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QSplashScreen

from core.config import CONFIG_DIR, LOG_DIR
from core.qt_theme import apply_dark_theme
from gui.ui_style import APP_STYLESHEET

ICON_PATH = Path(__file__).resolve().parent / "assets" / "icon.svg"


def __getattr__(name):
    # ``main.TerminalApp`` still works, but the heavy GUI import only happens on first use
    # (after the splash screen is visible when launched normally).
    if name == "TerminalApp":
        from gui.main_window import TerminalApp

        return TerminalApp
    raise AttributeError(name)


def build_splash_pixmap(icon: QIcon | None = None) -> QPixmap:
    """Draw the launch splash: dark card, app icon, name and a progress track."""
    width, height = 560, 300
    app = QApplication.instance()
    ratio = max(app.devicePixelRatio() if app else 1.0, 1.0)
    pixmap = QPixmap(int(width * ratio), int(height * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)

    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    card = QRectF(0.5, 0.5, width - 1, height - 1)
    gradient = QLinearGradient(0, 0, width, height)
    gradient.setColorAt(0.0, QColor("#11151d"))
    gradient.setColorAt(1.0, QColor("#0b0e14"))
    p.setBrush(gradient)
    p.setPen(QPen(QColor("#323c52"), 1))
    p.drawRoundedRect(card, 16, 16)

    accent = QLinearGradient(0, 0, width, 0)
    accent.setColorAt(0.0, QColor("#7c8cff"))
    accent.setColorAt(1.0, QColor("#5fd0c4"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(accent)
    p.drawRoundedRect(QRectF(32, 268, width - 64, 4), 2, 2)

    if icon is not None and not icon.isNull():
        icon.paint(p, 32, 40, 72, 72)
    else:
        p.setBrush(accent)
        p.drawRoundedRect(QRectF(32, 40, 72, 72), 16, 16)

    title_font = QFont()
    title_font.setPointSize(22)
    title_font.setBold(True)
    p.setFont(title_font)
    p.setPen(QColor("#e6edf3"))
    p.drawText(QRectF(124, 42, width - 150, 36), Qt.AlignmentFlag.AlignVCenter, "Modern Multi Terminal")

    sub_font = QFont()
    sub_font.setPointSize(12)
    p.setFont(sub_font)
    p.setPen(QColor("#9aa8b6"))
    p.drawText(QRectF(124, 80, width - 150, 26), Qt.AlignmentFlag.AlignVCenter, "Emulator & DevOps Sequencer Suite")

    p.setPen(QColor("#6b7a89"))
    p.drawText(
        QRectF(32, 140, width - 64, 60),
        Qt.TextFlag.TextWordWrap,
        "SSH · Telnet · Serial · SFTP · Editor · Macros · Script Suite · AI",
    )
    p.end()
    return pixmap


class Splash(QSplashScreen):
    def __init__(self, icon: QIcon | None = None):
        super().__init__(build_splash_pixmap(icon))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def step(self, message: str) -> None:
        self.showMessage(
            message, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, QColor("#9aa8b6")
        )
        QApplication.processEvents()

    def drawContents(self, painter: QPainter) -> None:
        # Message sits just above the progress track, inside the card padding.
        painter.setPen(QColor("#9aa8b6"))
        rect = self.rect().adjusted(32, 0, -32, -42)
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom), self.message())


def main():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    apply_dark_theme(app)
    icon = QIcon(str(ICON_PATH)) if ICON_PATH.exists() else None
    if icon is not None:
        app.setWindowIcon(icon)

    splash = Splash(icon)
    splash.show()
    splash.step("Loading interface ...")

    app.setStyleSheet(APP_STYLESHEET)
    splash.step("Loading terminal engine and AI assistant ...")
    from gui.main_window import TerminalApp  # the heavy import: keep it behind the splash

    splash.step("Starting ...")
    window = TerminalApp()
    window.show()
    splash.finish(window)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
