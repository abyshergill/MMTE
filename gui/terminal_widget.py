from __future__ import annotations
from PyQt6.QtCore import Qt, QEvent
from PyQt6.QtGui import QFont
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
        self.session = None  # assigned by TerminalApp right after construction

        # Keep setReadOnly(False) so Qt renders the native blinking cursor.
        # Direct user edits are completely blocked by swallowing key/mouse events.
        self.setReadOnly(False)
        self.setTabChangesFocus(False)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setAcceptDrops(True)
        self.setCursorWidth(2)
        # self.setStyleSheet(
        #     f"QPlainTextEdit {{ background-color: {TERMINAL_BG}; color: {TERMINAL_FG}; "
        #     f"border: 1px solid #183248; border-radius: 8px; }}"
        # )
        self.setFont(QFont("Consolas", 13))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)

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
        # setReadOnly(False) is needed purely for cursor blinking; content is
        # otherwise driven entirely by TerminalSession.render(). Block every
        # Qt-native insertion path (paste, IME, internal drag) so nothing can
        # write into the document outside that pipeline.
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
            handled = self.app.handle_terminal_key(self.session, event)
            if handled:
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