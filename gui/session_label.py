from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QLabel, QLineEdit

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


class InlineRenameEdit(QLineEdit):
    """Line edit laid over a tab for in-place renaming.

    Enter or clicking elsewhere commits (``committed`` is emitted once); Escape cancels.
    """

    committed = pyqtSignal(str)

    def __init__(self, parent, text: str):
        super().__init__(text, parent)
        self._done = False
        self.setStyleSheet("QLineEdit { padding: 0 6px; border-radius: 4px; min-height: 0; }")
        self.editingFinished.connect(self._commit)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancel()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._commit()

    def cancel(self):
        if not self._done:
            self._done = True
            self.hide()
            self.deleteLater()

    def _commit(self):
        if self._done:
            return
        self._done = True
        self.hide()
        self.committed.emit(self.text().strip())
        self.deleteLater()
