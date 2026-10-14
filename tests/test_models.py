from __future__ import annotations
import time
import pytest


# --- append / render -------------------------------------------------------

def test_render_paints_appended_text(terminal_session):
    terminal_session.append("hello world\r\n")
    terminal_session.render()
    assert "hello world" in terminal_session.terminal.toPlainText()


def test_append_accumulates_recv_buffer(terminal_session):
    terminal_session.append("abc")
    terminal_session.append("def")
    assert "abcdef" in terminal_session.recv_buffer


def test_recv_buffer_is_capped(terminal_session):
    terminal_session.append("x" * 300_000)
    assert len(terminal_session.recv_buffer) <= 250_000


def test_recent_output_includes_terminal_scrollback(terminal_session):
    terminal_session.append("\r\n".join(f"log-line-{index}" for index in range(40)))
    output = terminal_session.recent_output()
    assert "log-line-0" in output
    assert "log-line-39" in output


def test_recent_output_respects_character_limit(terminal_session):
    terminal_session.append("x" * 100)
    assert len(terminal_session.recent_output(12)) == 12


def test_render_is_noop_after_widget_deleted(terminal_session):
    terminal_session.terminal.deleteLater()
    # Force immediate C++ deletion isn't guaranteed synchronously in tests,
    # but append()/render() must not raise even if the widget were gone.
    terminal_session.append("data")
    terminal_session.render()  # should not raise


# --- scrollback --------------------------------------------------------------

def test_scroll_history_clamped_to_available_history(terminal_session):
    terminal_session.scroll_history(9999)
    assert terminal_session.scroll_offset >= 0
    max_offset = len(terminal_session.screen.history.top)
    assert terminal_session.scroll_offset <= max_offset


def test_scroll_history_never_goes_negative(terminal_session):
    terminal_session.scroll_history(-9999)
    assert terminal_session.scroll_offset == 0


def test_snap_to_live_resets_offset(terminal_session):
    terminal_session.scroll_offset = 5
    terminal_session.snap_to_live()
    assert terminal_session.scroll_offset == 0


# --- wait_for ------------------------------------------------------------------

def test_wait_for_finds_pattern_already_in_buffer(terminal_session):
    terminal_session.append("login: ready$ ")
    assert terminal_session.wait_for("ready$", timeout=1.0) is True


def test_wait_for_times_out_when_pattern_absent(terminal_session):
    start = time.time()
    found = terminal_session.wait_for("never-appears-xyz", timeout=0.3)
    elapsed = time.time() - start
    assert found is False
    assert elapsed < 2.0  # sanity: honored the short timeout


def test_wait_for_case_insensitive(terminal_session):
    terminal_session.append("PROMPT>")
    assert terminal_session.wait_for("prompt>", timeout=1.0, case_sensitive=False) is True


def test_clear_wait_buffer(terminal_session):
    terminal_session.append("leftover data")
    terminal_session.clear_wait_buffer()
    assert terminal_session.recv_buffer == ""


# --- send --------------------------------------------------------------------

def test_send_without_connection_notifies_once(terminal_session):
    terminal_session.send("ls\n")
    assert terminal_session.connection_lost_notified is True
    msgs = []
    while not terminal_session.incoming.empty():
        msgs.append(terminal_session.incoming.get_nowait())
    assert any("No active connection" in m for m in msgs)


def test_send_via_connection_forwards_data(terminal_session):
    class FakeConn:
        def __init__(self):
            self.sent = []
        def send(self, data):
            self.sent.append(data)
        def close(self):
            pass

    conn = FakeConn()
    terminal_session.connection = conn
    terminal_session.send("whoami\n")
    assert conn.sent == ["whoami\n"]


def test_send_error_tears_down_connection(terminal_session):
    class FailingConn:
        def send(self, data):
            raise RuntimeError("broken pipe")
        def close(self):
            pass

    terminal_session.connection = FailingConn()
    terminal_session.send("data")
    assert terminal_session.connection is None
    assert terminal_session.connected_label == "Disconnected"


# --- logging: covers the disable_logging(path) bug fix -----------------------

def test_logging_enable_writes_to_file(terminal_session, tmp_path):
    log_path = tmp_path / "session.log"
    terminal_session.enable_custom_logging(log_path)
    assert terminal_session.log_enabled is True
    terminal_session.disable_logging()
    assert terminal_session.log_enabled is False
    assert log_path.exists()


def test_disable_logging_with_matching_path_closes(terminal_session, tmp_path):
    log_path = tmp_path / "match.log"
    terminal_session.enable_custom_logging(log_path)
    terminal_session.disable_logging(log_path)  # regression test for the TypeError bug
    assert terminal_session.log_enabled is False
    assert terminal_session.log_file is None


def test_disable_logging_with_mismatched_path_leaves_log_open(terminal_session, tmp_path):
    log_path = tmp_path / "keep-open.log"
    other_path = tmp_path / "unrelated.log"
    terminal_session.enable_custom_logging(log_path)
    terminal_session.disable_logging(other_path)
    assert terminal_session.log_enabled is True
    terminal_session.disable_logging()  # cleanup


def test_disable_logging_with_no_active_log_is_a_noop(terminal_session, tmp_path):
    # Must not raise even though nothing is open.
    terminal_session.disable_logging(tmp_path / "nothing-open.log")
    assert terminal_session.log_enabled is False


def test_enable_logging_twice_is_ignored(terminal_session, tmp_path):
    p1 = tmp_path / "first.log"
    p2 = tmp_path / "second.log"
    terminal_session.enable_custom_logging(p1)
    terminal_session.enable_custom_logging(p2)  # should be a no-op while p1 is active
    assert terminal_session.log_file.name == str(p1)
    terminal_session.disable_logging()


# --- close / sftp hop cleanup --------------------------------------------------

def test_close_closes_connection_and_hops(terminal_session):
    calls = {"conn_closed": False, "hops_closed": []}

    class FakeConn:
        def close(self):
            calls["conn_closed"] = True

    class FakeHopHandle:
        def __init__(self, name):
            self.name = name
        def close(self):
            calls["hops_closed"].append(self.name)

    terminal_session.connection = FakeConn()
    terminal_session.sftp_hops = [
        {"label": "Hop 1", "client": None, "sftp": FakeHopHandle("sftp1")},
        {"label": "Hop 2", "client": FakeHopHandle("client2"), "sftp": FakeHopHandle("sftp2")},
    ]

    terminal_session.close()

    assert calls["conn_closed"] is True
    assert set(calls["hops_closed"]) == {"sftp1", "client2", "sftp2"}
    assert terminal_session.sftp_hops == []
    assert terminal_session.connection is None
    assert terminal_session.connected_label == "Disconnected"


def test_alt_screen_active_defaults_false(terminal_session):
    assert terminal_session.alt_screen_active is False


def test_resize_updates_dimensions_and_reflows(terminal_session):
    terminal_session.resize(100, 30)
    assert terminal_session.cols == 100
    assert terminal_session.rows == 30


def test_resize_enforces_minimums(terminal_session):
    terminal_session.resize(1, 1)
    assert terminal_session.cols >= 20
    assert terminal_session.rows >= 5
