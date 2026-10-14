from PyQt6.QtWidgets import QLabel
from gui.helpers import set_hand_cursor

class _SessionLabel(QLabel):
    """Sidebar row label: single-click selects, double-click renames."""
    def __init__(self, app, session, text: str):
        super().__init__(text)
        self.app = app
        self.session = session
        set_hand_cursor(self)
        self.setContentsMargins(8, 6, 8, 6)

    def mousePressEvent(self, event):
        self.app.select_session(self.session)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.app.select_session(self.session)
        self.app.rename_terminal()
        super().mouseDoubleClickEvent(event)