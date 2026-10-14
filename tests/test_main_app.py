from __future__ import annotations
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import Qt, QEvent, QEventLoop
from PyQt6.QtGui import QKeyEvent

import main as main_module


from PyQt6.QtCore import QTimer


@pytest.fixture
def app_window(qapp):
    win = main_module.TerminalApp()
    yield win
    for t in win.findChildren(QTimer):
        t.stop()
    for s in list(win.sessions):
        try:
            s.close()
        except Exception:
            pass
    win.sessions.clear()
    win.close()
    win.deleteLater()
    qapp.processEvents()


def _key_event(key, text="", ctrl=False, shift=False):
    mods = Qt.KeyboardModifier.NoModifier
    if ctrl:
        mods |= Qt.KeyboardModifier.ControlModifier
    if shift:
        mods |= Qt.KeyboardModifier.ShiftModifier
    return QKeyEvent(QEvent.Type.KeyPress, key, mods, text)


# --- session lifecycle -----------------------------------------------------

def test_startup_creates_one_session(app_window):
    assert len(app_window.sessions) == 1
    assert app_window.active_session is app_window.sessions[0]


def test_new_terminal_adds_session_and_selects_it(app_window):
    s2 = app_window.new_terminal()
    assert s2 in app_window.sessions
    assert app_window.active_session is s2
    assert len(app_window.sessions) == 2


def test_new_terminal_default_naming_increments(app_window):
    s2 = app_window.new_terminal()
    assert s2.name != app_window.sessions[0].name


def test_new_terminal_with_explicit_name(app_window):
    s = app_window.new_terminal(name="MyServer")
    assert s.name == "MyServer"


def test_sessions_are_hashable_for_sidebar_dict(app_window):
    # Regression test: TerminalSession must support @dataclass(eq=False) so
    # it can be used as a dict key in session_list_rows.
    app_window.refresh_session_list()
    assert app_window.sessions[0] in app_window.session_list_rows


def test_get_session_by_name_case_insensitive_fallback(app_window):
    s = app_window.new_terminal(name="ProdBox")
    assert app_window.get_session_by_name("ProdBox") is s
    assert app_window.get_session_by_name("prodbox") is s
    assert app_window.get_session_by_name("nope") is None


def test_select_session_by_name(app_window):
    s2 = app_window.new_terminal(name="Second")
    s1 = app_window.sessions[0]
    app_window.select_session(s1)
    result = app_window.select_session_by_name("Second")
    assert result is s2
    assert app_window.active_session is s2


def test_close_session_by_name_removes_it(app_window):
    s2 = app_window.new_terminal(name="ToClose")
    app_window.close_session_by_name("ToClose")
    assert s2 not in app_window.sessions


def test_rename_terminal_by_name(app_window):
    s = app_window.sessions[0]
    old_name = s.name
    ok = app_window.rename_terminal_by_name(old_name, "Renamed")
    assert ok is True
    assert s.name == "Renamed"


def test_run_ai_automation_uses_app_script_engine(app_window):
    session = app_window.current_session()
    loop = QEventLoop()
    results = []
    started = app_window.run_ai_automation(
        session,
        "if terminal_rename('Terminal 1', 'Engine Renamed'):\n"
        "    print('AI engine ran')\n"
        "else:\n"
        "    print('AI engine failed')\n",
        "AI automation test",
        on_complete=lambda output: (results.append(output), loop.quit()),
    )
    QTimer.singleShot(3000, loop.quit)
    loop.exec()

    assert started is True
    assert session.name == "Engine Renamed"
    assert "AI engine ran" in session.recv_buffer
    assert results and "AI engine ran" in results[0]
    assert session.script_running is False


def test_ai_terminal_commands_use_script_engine_send(app_window):
    session = app_window.current_session()

    class CaptureConnection:
        def send(self, text):
            session.incoming.put(f"[CAPTURED APP SEND] {text}")

        def close(self):
            pass

    session.connection = CaptureConnection()
    session.connected_label = "Test Connection"
    loop = QEventLoop()
    app_window.ai_panel.run_ai_script_sequence(["echo APP_ENGINE_SEND_OK"], session=session)

    def check_finished():
        if "[CAPTURED APP SEND] echo APP_ENGINE_SEND_OK" in session.recv_buffer and not session.script_running:
            loop.quit()
        else:
            QTimer.singleShot(20, check_finished)

    QTimer.singleShot(0, check_finished)
    QTimer.singleShot(3000, loop.quit)
    loop.exec()

    assert "[CAPTURED APP SEND] echo APP_ENGINE_SEND_OK\n" in session.recv_buffer
    assert session.script_running is False


def test_close_session_direct_selects_previous_session(app_window):
    s1 = app_window.sessions[0]
    s2 = app_window.new_terminal()
    app_window.close_session_direct(s2)
    assert s2 not in app_window.sessions
    assert app_window.active_session is s1


def test_close_last_session_leaves_no_active_session(app_window):
    s1 = app_window.sessions[0]
    app_window.close_session_direct(s1)
    assert app_window.sessions == []
    assert app_window.active_session is None


# --- key handling: local echo (no connection) --------------------------------

def test_typed_char_local_echo_when_disconnected(app_window):
    s = app_window.sessions[0]
    assert s.connection is None
    before = s.recv_buffer
    handled = app_window.handle_terminal_key(s, _key_event(Qt.Key.Key_A, text="a"))
    assert handled is True
    assert s.recv_buffer == before + "a"


def test_backspace_local_echo_when_disconnected(app_window):
    s = app_window.sessions[0]
    app_window.handle_terminal_key(s, _key_event(Qt.Key.Key_Backspace))
    assert s.recv_buffer.endswith("\b \b")


def test_enter_local_echo_when_disconnected(app_window):
    s = app_window.sessions[0]
    app_window.handle_terminal_key(s, _key_event(Qt.Key.Key_Return))
    assert s.recv_buffer.endswith("\r\n")


def test_all_keys_report_handled_true(app_window):
    # handle_terminal_key always returns True (fully intercepts terminal
    # input), matching the contract TerminalWidget.keyPressEvent relies on.
    s = app_window.sessions[0]
    assert app_window.handle_terminal_key(s, _key_event(Qt.Key.Key_F5)) is True


# --- key handling: connected session -----------------------------------------

class FakeConnection:
    label = "SSH"
    def __init__(self):
        self.sent = []
    def send(self, data):
        self.sent.append(data)


def test_typed_char_sent_to_connection(app_window):
    s = app_window.sessions[0]
    s.connection = FakeConnection()
    app_window.handle_terminal_key(s, _key_event(Qt.Key.Key_A, text="a"))
    assert s.connection.sent == ["a"]


def test_enter_sends_carriage_return_and_sets_awaiting_prompt(app_window):
    s = app_window.sessions[0]
    s.connection = FakeConnection()
    app_window.handle_terminal_key(s, _key_event(Qt.Key.Key_Return))
    assert s.connection.sent == ["\r"]
    assert s.awaiting_prompt is True


def test_arrow_keys_send_ansi_escapes(app_window):
    s = app_window.sessions[0]
    s.connection = FakeConnection()
    app_window.handle_terminal_key(s, _key_event(Qt.Key.Key_Up))
    app_window.handle_terminal_key(s, _key_event(Qt.Key.Key_Down))
    app_window.handle_terminal_key(s, _key_event(Qt.Key.Key_Left))
    app_window.handle_terminal_key(s, _key_event(Qt.Key.Key_Right))
    assert s.connection.sent == ["\x1b[A", "\x1b[B", "\x1b[D", "\x1b[C"]


def test_ctrl_letter_sends_control_code(app_window):
    s = app_window.sessions[0]
    s.connection = FakeConnection()
    app_window.handle_terminal_key(s, _key_event(Qt.Key.Key_C, ctrl=True))
    assert s.connection.sent == ["\x03"]  # Ctrl+C


def test_tab_sends_literal_tab_when_connected(app_window):
    s = app_window.sessions[0]
    s.connection = FakeConnection()
    app_window.handle_terminal_key(s, _key_event(Qt.Key.Key_Tab))
    assert s.connection.sent == ["\t"]


def test_backspace_blocked_before_prompt_column(app_window):
    s = app_window.sessions[0]
    s.connection = FakeConnection()
    s.prompt_row = s.screen.cursor.y
    s.prompt_col = s.screen.cursor.x
    app_window.handle_terminal_key(s, _key_event(Qt.Key.Key_Backspace))
    assert s.connection.sent == []  # blocked: cursor is at/before prompt column


def test_shift_pageup_scrolls_instead_of_sending(app_window):
    s = app_window.sessions[0]
    s.connection = FakeConnection()
    app_window.handle_terminal_key(s, _key_event(Qt.Key.Key_PageUp, shift=True))
    assert s.connection.sent == []
    # scroll_offset clamps to available history, which may be 0 on a fresh
    # session, but the key must not have been forwarded to the connection.


# --- sizing --------------------------------------------------------------------

def test_compute_terminal_size_within_bounds(app_window):
    s = app_window.sessions[0]
    cols, rows = app_window.compute_terminal_size(s)
    assert 20 <= cols <= 200
    assert 5 <= rows <= 80


def test_apply_resize_after_session_closed_does_not_raise(app_window):
    """
    Regression test: new_terminal() schedules a delayed _apply_resize() call.
    If the session is closed (and its widget deleted) before that timer
    fires, _apply_resize() must bail out instead of touching a dead C++
    widget.
    """
    s2 = app_window.new_terminal()
    app_window.close_session_direct(s2)
    app_window._apply_resize(s2)  # must not raise / crash


def test_new_terminal_resize_job_is_cancelled_on_close(app_window):
    s2 = app_window.new_terminal()
    assert s2._resize_job is not None
    app_window.close_session_direct(s2)
    assert s2._resize_job is None


# --- text drop (new behavior added alongside the widget fix) -----------------

def test_handle_text_drop_sends_when_connected(app_window):
    s = app_window.sessions[0]
    s.connection = FakeConnection()
    app_window.handle_text_drop(s, "dropped text")
    assert s.connection.sent == ["dropped text"]


def test_handle_text_drop_local_echoes_when_disconnected(app_window):
    s = app_window.sessions[0]
    before = s.recv_buffer
    app_window.handle_text_drop(s, "dropped text")
    assert s.recv_buffer == before + "dropped text"


def test_handle_text_drop_ignores_empty_string(app_window):
    s = app_window.sessions[0]
    before = s.recv_buffer
    app_window.handle_text_drop(s, "")
    assert s.recv_buffer == before


# --- after()/after_cancel() scheduling helper --------------------------------

def test_after_runs_on_main_thread_via_qtimer(app_window, qapp):
    called = []
    job = app_window.after(0, lambda: called.append(True))
    qapp.processEvents()
    import time
    deadline = time.time() + 1.0
    while not called and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert called == [True]


def test_after_cancel_prevents_callback(app_window, qapp):
    called = []
    job = app_window.after(200, lambda: called.append(True))
    app_window.after_cancel(job)
    import time
    time.sleep(0.3)
    qapp.processEvents()
    assert called == []
