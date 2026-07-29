from __future__ import annotations
from unittest.mock import MagicMock

from PyQt6.QtCore import Qt, QPoint, QEvent
from PyQt6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent, QDropEvent
from PyQt6.QtCore import QPointF
from PyQt6.QtTest import QTest


class FakeAppRecorder:
    """Stands in for TerminalApp; records what the widget forwards to it."""
    def __init__(self, key_handled=True):
        self.key_handled = key_handled
        self.key_calls = []
        self.click_calls = []
        self.wheel_calls = []
        self.menu_calls = []
        self.resize_calls = []
        self.file_drop_calls = []
        self.text_drop_calls = []

    def handle_terminal_key(self, session, event):
        self.key_calls.append(event.key())
        return self.key_handled

    def handle_terminal_click(self, session, event):
        self.click_calls.append(event)

    def handle_mousewheel(self, session, event):
        self.wheel_calls.append(event)

    def show_context_menu(self, session, event):
        self.menu_calls.append(event)

    def on_terminal_resize(self, session):
        self.resize_calls.append(True)

    def handle_file_drop(self, session, event):
        self.file_drop_calls.append(event)

    def handle_text_drop(self, session, text):
        self.text_drop_calls.append(text)


def _make_widget(qapp, key_handled=True):
    from gui.terminal_widget import TerminalWidget
    app = FakeAppRecorder(key_handled=key_handled)
    w = TerminalWidget(app=app)
    w.session = object()  # any truthy session marker; app is faked out
    return w, app


def test_key_press_forwarded_and_consumed_when_handled(qapp):
    w, app = _make_widget(qapp, key_handled=True)
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, "a")
    w.keyPressEvent(ev)
    assert app.key_calls == [Qt.Key.Key_A]
    assert ev.isAccepted()


def test_key_press_falls_through_to_qt_when_not_handled(qapp):
    w, app = _make_widget(qapp, key_handled=False)
    before = w.toPlainText()
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier, "")
    w.keyPressEvent(ev)
    assert app.key_calls == [Qt.Key.Key_Left]
    # Falling through to super() shouldn't have modified document content.
    assert w.toPlainText() == before


def test_wheel_event_forwarded(qapp):
    w, app = _make_widget(qapp)
    ev = QWheelEvent(
        QPointF(10, 10), QPointF(10, 10), QPoint(0, 0), QPoint(0, 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    w.wheelEvent(ev)
    assert len(app.wheel_calls) == 1


def test_mouse_release_forwards_click_on_left_button(qapp):
    w, app = _make_widget(qapp)
    ev = QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(5, 5), QPointF(5, 5), QPointF(5, 5),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    w.mouseReleaseEvent(ev)
    assert len(app.click_calls) == 1


def test_mouse_release_ignores_right_button(qapp):
    w, app = _make_widget(qapp)
    ev = QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(5, 5), QPointF(5, 5), QPointF(5, 5),
        Qt.MouseButton.RightButton, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier,
    )
    w.mouseReleaseEvent(ev)
    assert app.click_calls == []


# --- regression: mousePressEvent must still let Qt set its selection anchor ---

def test_click_drag_selects_text(qapp):
    """
    Regression test for the bug where mousePressEvent skipped super(),
    leaving Qt without a selection anchor, so click-drag selection silently
    did nothing. QTest simulates a real press-move-release sequence.
    """
    w, app = _make_widget(qapp)
    w.resize(400, 200)
    w.show()
    w.setPlainText("select this line of text\nsecond line")
    w.repaint()

    doc = w.document()
    start_block = doc.findBlockByNumber(0)
    start_rect = w.cursorRect(_cursor_at(w, start_block.position()))
    end_rect = w.cursorRect(_cursor_at(w, start_block.position() + len("select this")))

    QTest.mousePress(w.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start_rect.center())
    QTest.mouseMove(w.viewport(), end_rect.center())
    QTest.mouseRelease(w.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end_rect.center())

    assert w.textCursor().hasSelection(), "click-drag should produce a text selection"
    # The click handler should still have fired (session-switch-on-click behavior).
    assert len(app.click_calls) >= 1


def _cursor_at(widget, position):
    c = widget.textCursor()
    c.setPosition(position)
    return c


# --- regression: readOnly(False) must not allow bypassing render() -----------

def test_paste_is_blocked_despite_read_write_mode(qapp):
    """
    The widget is setReadOnly(False) so the native cursor blinks, but content
    must still be fully controlled by TerminalSession.render(). Qt's paste
    path (insertFromMimeData) must be a no-op.
    """
    w, app = _make_widget(qapp)
    w.setPlainText("original")
    from PyQt6.QtGui import QClipboard
    from PyQt6.QtWidgets import QApplication
    QApplication.clipboard().setText("INJECTED")
    w.paste()
    assert w.toPlainText() == "original"


def test_can_insert_from_mime_data_is_false(qapp):
    w, app = _make_widget(qapp)
    from PyQt6.QtCore import QMimeData
    md = QMimeData()
    md.setText("anything")
    assert w.canInsertFromMimeData(md) is False


def test_drop_event_with_urls_forwards_file_drop(qapp):
    w, app = _make_widget(qapp)
    from PyQt6.QtCore import QMimeData, QUrl
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile("/tmp/some_file.txt")])
    ev = QDropEvent(QPointF(1, 1), Qt.DropAction.CopyAction, md, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    w.dropEvent(ev)
    assert len(app.file_drop_calls) == 1
    assert app.text_drop_calls == []


def test_drop_event_with_plain_text_forwards_text_drop_not_native_insert(qapp):
    """
    Regression test: dragging plain text (not a file URL) must go through
    handle_text_drop, not Qt's native document insertion (which would bypass
    render() entirely on a non-read-only widget).
    """
    w, app = _make_widget(qapp)
    w.setPlainText("original")
    from PyQt6.QtCore import QMimeData
    md = QMimeData()
    md.setText("dragged text")
    ev = QDropEvent(QPointF(1, 1), Qt.DropAction.CopyAction, md, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    w.dropEvent(ev)
    assert app.text_drop_calls == ["dragged text"]
    assert app.file_drop_calls == []
    # Document content must be untouched -- render() owns all content changes.
    assert w.toPlainText() == "original"


def test_context_menu_forwarded(qapp):
    from PyQt6.QtGui import QContextMenuEvent
    w, app = _make_widget(qapp)
    ev = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(5, 5))
    w.contextMenuEvent(ev)
    assert len(app.menu_calls) == 1


def test_resize_event_notifies_app(qapp):
    w, app = _make_widget(qapp)
    w.show()
    qapp.processEvents()
    w.resize(300, 300)
    qapp.processEvents()
    w.resize(400, 400)
    qapp.processEvents()
    assert len(app.resize_calls) >= 1


def test_tab_key_does_not_shift_focus(qapp):
    w, app = _make_widget(qapp, key_handled=True)
    assert w.focusNextPrevChild(True) is False
