from __future__ import annotations
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPalette, QColor
from PyQt6.QtWidgets import QApplication


def is_dark_mode(app: QApplication) -> bool:
    """Best-effort detection of the OS light/dark preference."""
    try:
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
    except Exception:
        pass
    # Fallback for older Qt/platform combos: infer from the default palette.
    return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


def _dark_palette() -> QPalette:
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(30, 34, 39))
    p.setColor(QPalette.ColorRole.WindowText, QColor(222, 227, 230))
    p.setColor(QPalette.ColorRole.Base, QColor(21, 24, 28))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(38, 43, 49))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(222, 227, 230))
    p.setColor(QPalette.ColorRole.ToolTipText, QColor(222, 227, 230))
    p.setColor(QPalette.ColorRole.Text, QColor(222, 227, 230))
    p.setColor(QPalette.ColorRole.Button, QColor(42, 47, 54))
    p.setColor(QPalette.ColorRole.ButtonText, QColor(222, 227, 230))
    p.setColor(QPalette.ColorRole.BrightText, QColor(255, 90, 90))
    p.setColor(QPalette.ColorRole.Link, QColor(93, 173, 236))
    p.setColor(QPalette.ColorRole.Highlight, QColor(47, 111, 149))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(123, 139, 153))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(110, 116, 122))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(110, 116, 122))
    return p


def apply_system_theme(app: QApplication) -> bool:
    """
    Applies Fusion (for consistent cross-platform widget rendering) plus a
    palette matching the OS light/dark preference. Individual widgets should
    prefer the stylesheet `palette(...)` function (e.g. `palette(base)`,
    `palette(highlight)`) over literal hex colors so they keep tracking this
    palette automatically. Returns True if dark mode was applied.
    """
    app.setStyle("Fusion")
    dark = is_dark_mode(app)
    if dark:
        app.setPalette(_dark_palette())
    else:
        app.setPalette(app.style().standardPalette())
    return dark
