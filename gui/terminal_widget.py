from __future__ import annotations
from PyQt6.QtCore import Qt, QEvent
from PyQt6.QtGui import QFont, QTextOption
from PyQt6.QtWidgets import QPlainTextEdit
from core.theme import TERMINAL_BG, TERMINAL_FG

class TerminalWidget(QPlainTextEdit):
    """
    Read-only viewport driven entirely by TerminalSession.render(). All key,
    mouse, resize, context-menu and drag/drop events are forwarded to the
    owning TerminalApp (self.app) with the associated TerminalSession
    (self.session), mirroring the per-session <bind> callbacks used in the
    original tkinter implementation.
    """

    def __init__(self, app: object):
        super().__init__()
        self.app = app
        self.session = None  

        self.setReadOnly(False)
        self.setTabChangesFocus(False)
        self.setUndoRedoEnabled(False)


        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)

        self.setAcceptDrops(True)
        self.setCursorWidth(2)

        self.setMaximumBlockCount(10000)

        self.setViewportMargins(0, 0, 0, 0)

        self.setCenterOnScroll(False)

        self.setFont(QFont("Consolas", 13))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)

    def setPlainText(self, text: str) -> None:
        super().setPlainText(text)
        self._invalidate_layout()

    def insertPlainText(self, text: str) -> None:
        super().insertPlainText(text)
        self._invalidate_layout()

    def appendPlainText(self, text: str) -> None:
        super().appendPlainText(text)
        self._invalidate_layout()

    def _invalidate_layout(self) -> None:
        """Force the document layout to recalculate synchronously."""
        doc = self.document()
        doc.markContentsDirty(0, doc.characterCount())
        self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)

    def focusNextPrevChild(self, next_: bool) -> bool:
        """Prevent Qt from switching widget focus on Tab / Shift+Tab."""
        return False

    def event(self, event: QEvent) -> bool:
        """Catch Tab key at Qt's entry event pipeline before focus-navigation takes over."""
        if event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                self.keyPressEvent(event)
                return True
        return super().event(event)

    def canInsertFromMimeData(self, source) -> bool:
        return False

    def insertFromMimeData(self, source) -> None:
        return

    # --- event forwarding -------------------------------------------------
    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if self.session is not None and event.button() == Qt.MouseButton.LeftButton:
            self.app.handle_terminal_click(self.session, event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self.session is not None and event.button() == Qt.MouseButton.LeftButton:
            self.app.handle_terminal_click(self.session, event)

    def keyPressEvent(self, event):
        if self.session is not None:
            self.app.handle_terminal_key(self.session, event)
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event):
        if self.session is not None:
            self.app.handle_mousewheel(self.session, event)
            event.accept()
            return
        super().wheelEvent(event)

    def contextMenuEvent(self, event):
        if self.session is not None:
            self.app.show_context_menu(self.session, event)
            return
        super().contextMenuEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._invalidate_layout()
        if self.session is not None:
            self.app.on_terminal_resize(self.session)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if self.session is None:
            super().dropEvent(event)
            return
        mime = event.mimeData()
        if mime.hasUrls():
            self.app.handle_file_drop(self.session, event)
            event.acceptProposedAction()
        elif mime.hasText():
            self.app.handle_text_drop(self.session, mime.text())
            event.acceptProposedAction()
        else:
            event.ignore()
