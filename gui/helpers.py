import sys
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QWidget

_MONO_FAMILIES = ("JetBrains Mono", "Cascadia Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono")
_mono_family: str | None = None


def mono_font(point_size: int | None = None) -> QFont:
    """A fixed-width font that exists on this machine (no missing-family lookups on macOS/Linux)."""
    global _mono_family
    if _mono_family is None:
        installed = set(QFontDatabase.families())
        _mono_family = next((f for f in _MONO_FAMILIES if f in installed), "")
    font = QFont(_mono_family) if _mono_family else QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setFixedPitch(True)
    if point_size:
        font.setPointSize(point_size)
    return font

def set_hand_cursor(widget: QWidget) -> None:
    """Safely sets pointing hand cursor across PyQt versions."""
    try:
        widget.setCursor(Qt.CursorShape.PointingHandCursor)
    except Exception:
        pass


class _AfterBridge(QObject):
    """Lets background threads schedule GUI-thread callbacks."""
    request = pyqtSignal(object, tuple, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.request.connect(self._run)

    def _run(self, func, args, kwargs):
        try:
            func(*args, **kwargs)
        except Exception as e:
            print(f"Error executing queued action: {e}", file=sys.stderr)