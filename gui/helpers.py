import sys
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QWidget

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