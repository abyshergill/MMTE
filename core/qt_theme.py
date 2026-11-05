from __future__ import annotations
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPalette
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
    """Palette that matches the colour tokens in ``gui.ui_style`` (slate surfaces, teal accent)."""
    p = QPalette()
    R = QPalette.ColorRole
    p.setColor(R.Window, QColor("#0b0e14"))
    p.setColor(R.WindowText, QColor("#e6edf3"))
    p.setColor(R.Base, QColor("#171c26"))
    p.setColor(R.AlternateBase, QColor("#11151d"))
    p.setColor(R.ToolTipBase, QColor("#171c26"))
    p.setColor(R.ToolTipText, QColor("#e6edf3"))
    p.setColor(R.Text, QColor("#e6edf3"))
    p.setColor(R.Button, QColor("#171c26"))
    p.setColor(R.ButtonText, QColor("#e6edf3"))
    p.setColor(R.BrightText, QColor("#ff6b6b"))
    p.setColor(R.Link, QColor("#5aa9ff"))
    p.setColor(R.Highlight, QColor("#2a3566"))
    p.setColor(R.HighlightedText, QColor("#ffffff"))
    p.setColor(R.PlaceholderText, QColor("#6b7a89"))
    p.setColor(R.Mid, QColor("#222a38"))
    p.setColor(R.Dark, QColor("#080b10"))
    p.setColor(R.Light, QColor("#323c52"))
    p.setColor(R.Shadow, QColor("#05070a"))
    disabled = QPalette.ColorGroup.Disabled
    for role in (R.Text, R.ButtonText, R.WindowText):
        p.setColor(disabled, role, QColor("#6b7a89"))
    return p


def _ui_font(app: QApplication) -> QFont:
    """A clean system UI font (the terminal view sets its own monospace font)."""
    families = set(QFontDatabase.families())
    for name in ("Segoe UI Variable", "Segoe UI", "Inter", "SF Pro Text", "Helvetica Neue", "Noto Sans"):
        if name in families:
            font = QFont(name)
            break
    else:
        font = QFont(app.font())
    font.setPointSizeF(max(app.font().pointSizeF(), 10.0))
    return font


def apply_dark_theme(app: QApplication) -> bool:
    """Fusion style + the dark palette + a UI font, **regardless of the OS light/dark setting**.

    The app's stylesheet (``gui.ui_style``) is dark-only, so following a light OS
    palette produced a half-light, half-dark window.  Always returns True.
    """
    app.setStyle("Fusion")
    app.setPalette(_dark_palette())
    app.setFont(_ui_font(app))
    return True


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
