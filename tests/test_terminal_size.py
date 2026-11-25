"""Terminal size handling: the remote side must always know the size that is shown.

When it did not, long lines wrapped at the wrong column (text overwrote itself) and vi / nano
drew rows that were not on screen (the file looked empty).
"""

from __future__ import annotations

import queue
import socket
import threading
import time

import paramiko
import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFontMetricsF

import main as main_module
from core.connections import IAC, OPT_NAWS, SB, SE, WILL, DO, SSHConnection, TelnetConnection, TelnetProtocol


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


def _wait(qapp, condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


class RecordingConnection:
    """Stand-in connection that records what size the app announces."""

    label = "Telnet"

    def __init__(self):
        self.resizes: list[tuple[int, int]] = []
        self.initial_size = None

    def start(self, incoming):
        incoming.put("[fake connected]\r\n")

    def send(self, data):
        pass

    def resize(self, cols, rows):
        self.resizes.append((cols, rows))

    def close(self):
        pass


# --- the size reaches the remote side -------------------------------------------------------

def test_new_connection_is_told_the_size_even_if_the_window_did_not_change(app_window, qapp):
    app_window.resize(1100, 700)
    app_window.show()
    s = app_window.sessions[0]
    # let the layout settle so the terminal size is stable before connecting
    _wait(qapp, lambda: False, timeout=0.5)
    app_window._apply_resize(s)
    shown = (s.cols, s.rows)
    conn = RecordingConnection()
    app_window.connect_session(s, conn, "Fake")
    assert _wait(qapp, lambda: s.connection is conn)
    # opened at the size actually shown
    assert conn.initial_size == shown
    # The follow-up resize announces the size although the local size did not change.
    # Before the fix it returned early, so the remote side never heard the real size.
    app_window._apply_resize(s)
    assert conn.resizes == [shown]
    # nothing is sent again while the size stays the same ...
    app_window._apply_resize(s)
    assert conn.resizes == [shown]
    # ... and a real change is announced
    s.resize(s.cols - 3, s.rows - 2)
    assert conn.resizes[-1] == (s.cols, s.rows)


def test_session_announces_size_to_a_fresh_connection(app_window):
    s = app_window.sessions[0]
    conn = RecordingConnection()
    s.connection = conn
    s.remote_size = None
    s.resize(s.cols, s.rows)  # same local size
    assert conn.resizes == [(s.cols, s.rows)]


def test_terminal_size_fits_the_visible_area(app_window, qapp):
    app_window.resize(1100, 700)
    app_window.show()
    qapp.processEvents()
    s = app_window.sessions[0]
    cols, rows = app_window.compute_terminal_size(s)
    fm = QFontMetricsF(s.terminal.font())
    row_width = fm.horizontalAdvance("0" * cols)
    # a full row must fit inside the view, so it can never spill onto the next line
    assert row_width <= s.terminal.viewport().width()
    assert rows * fm.lineSpacing() <= s.terminal.viewport().height()


def test_terminal_widget_never_wraps_a_row(app_window):
    from PyQt6.QtWidgets import QPlainTextEdit

    s = app_window.sessions[0]
    assert s.terminal.lineWrapMode() == QPlainTextEdit.LineWrapMode.NoWrap


# --- rendering ----------------------------------------------------------------------------

def test_wide_characters_do_not_shift_the_row(app_window):
    s = app_window.sessions[0]
    s.clear_screen()
    s.append("中文ab|")
    s.render()
    first = s.terminal.toPlainText().split("\n")[0]
    assert first.startswith("中文ab|"), repr(first)


def test_full_screen_programs_are_detected(app_window):
    s = app_window.sessions[0]
    assert s.alt_screen_active is False
    s.append("\x1b[?1049h")  # what vim / nano send when they start
    assert s.alt_screen_active is True
    s.append("\x1b[?1049l")
    assert s.alt_screen_active is False


# --- Telnet window size (NAWS, RFC 1073) ---------------------------------------------------

def test_telnet_reports_window_size_when_asked():
    p = TelnetProtocol(size_getter=lambda: (100, 30))
    payload, replies = p.feed(bytes([IAC, DO, OPT_NAWS]) + b"login: ")
    assert payload == b"login: "
    assert replies == bytes([IAC, WILL, OPT_NAWS, IAC, SB, OPT_NAWS, 0, 100, 0, 30, IAC, SE])
    assert p.naws_enabled


def test_telnet_naws_escapes_255():
    assert TelnetProtocol.naws(255, 24) == bytes([IAC, SB, OPT_NAWS, 0, IAC, IAC, 0, 24, IAC, SE])


def test_telnet_without_size_still_refuses_naws():
    p = TelnetProtocol()
    _, replies = p.feed(bytes([IAC, DO, OPT_NAWS]))
    assert replies == bytes([IAC, 252, OPT_NAWS])  # WONT
    assert not p.naws_enabled


def test_telnet_resize_is_sent_once_negotiated():
    a, b = socket.socketpair()
    try:
        conn = TelnetConnection("x")
        conn.sock = a
        conn.resize(120, 40)  # not negotiated yet: nothing on the wire
        b.setblocking(False)
        with pytest.raises(BlockingIOError):
            b.recv(64)
        conn.protocol.naws_enabled = True
        conn.resize(132, 50)
        b.setblocking(True)
        b.settimeout(2)
        assert b.recv(64) == TelnetProtocol.naws(132, 50)
    finally:
        a.close()
        b.close()


# --- SSH: the PTY is opened at the shown size and resized on the wire ----------------------

class _PtyServer(paramiko.ServerInterface):
    def __init__(self, record):
        self.record = record

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        self.record.append(("pty", width, height))
        return True

    def check_channel_shell_request(self, channel):
        return True

    def check_channel_window_change_request(self, channel, width, height, pixelwidth, pixelheight):
        self.record.append(("resize", width, height))
        return True


def test_ssh_pty_uses_the_shown_size_and_follows_resizes():
    record: list = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    host_key = paramiko.RSAKey.generate(2048)

    def serve():
        sock, _ = listener.accept()
        t = paramiko.Transport(sock)
        t.add_server_key(host_key)
        t.start_server(server=_PtyServer(record))
        channel = t.accept(10)  # keep a reference: a dropped Channel object closes itself
        time.sleep(3)
        if channel is not None:
            channel.close()
        t.close()

    threading.Thread(target=serve, daemon=True).start()
    conn = SSHConnection("127.0.0.1", "u", "p", port=port, look_for_keys=False, allow_agent=False)
    conn.initial_size = (117, 41)
    try:
        conn.start(queue.Queue())
        conn.resize(90, 25)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and ("resize", 90, 25) not in record:
            time.sleep(0.05)
    finally:
        conn.close()
        listener.close()
    # before the fix the PTY was always 100x30, whatever was on screen
    assert record[0] == ("pty", 117, 41)
    assert ("resize", 90, 25) in record
