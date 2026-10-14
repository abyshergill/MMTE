from __future__ import annotations
import queue
import threading
import time
from pathlib import Path

import pytest

from core.models import TerminalSession
from core.script_engine import TeraTermMacroRunner, execute_python_script_sync


class RecordingSession:
    """A lightweight stand-in for TerminalSession that records everything
    a script does, so we can assert on it without needing a real Qt widget."""

    def __init__(self, app):
        self.app = app
        self.name = "Test Terminal"
        self.incoming: "queue.Queue[str]" = queue.Queue()
        self.recv_buffer = ""
        self.sent: list[str] = []
        self.log_enabled = False
        self.log_calls: list = []
        self._wait_for_result = True
        self.connection = None

    def send(self, data):
        self.sent.append(data)

    def script_checkpoint(self):
        pass

    def wait_for(self, pattern, timeout=60.0, case_sensitive=True):
        self.log_calls.append(("wait_for", pattern))
        return self._wait_for_result

    def enable_custom_logging(self, path):
        self.log_calls.append(("enable", str(path)))
        self.log_enabled = True

    def disable_logging(self, path=None):
        self.log_calls.append(("disable", str(path) if path else None))
        self.log_enabled = False


# --- TeraTermMacroRunner: pure parsing helpers --------------------------------

@pytest.fixture
def runner(fake_app):
    session = RecordingSession(fake_app)
    return TeraTermMacroRunner(fake_app, session)


def test_clean_line_strips_comments(runner):
    assert runner._clean_line("; full comment line") == ""
    assert runner._clean_line("sendln 'hi' ; trailing comment") == "sendln 'hi'"


def test_clean_line_ignores_semicolon_inside_quotes(runner):
    assert runner._clean_line("sendln 'a;b'") == "sendln 'a;b'"


def test_split_command_with_and_without_args(runner):
    assert runner._split_command("wait '$'") == ("wait", "'$'")
    assert runner._split_command("end") == ("end", "")


def test_unquote_strips_matching_quotes_only(runner):
    assert runner._unquote("'hello'") == "hello"
    assert runner._unquote('"hello"') == "hello"
    assert runner._unquote("hello") == "hello"
    assert runner._unquote("'mismatched\"") == "'mismatched\""


# --- TeraTermMacroRunner: full file execution --------------------------------

def test_run_file_executes_sendln_wait_pause(tmp_path, fake_app):
    session = RecordingSession(fake_app)
    runner = TeraTermMacroRunner(fake_app, session)
    ttl = tmp_path / "script.ttl"
    ttl.write_text(
        "; comment\n"
        "sendln \"whoami\"\n"
        "wait '$'\n"
        "pause 0\n"
        "end\n"
        "sendln \"never reached\"\n",
        encoding="utf-8",
    )
    runner.run_file(str(ttl))
    assert session.sent == ["whoami\n"]
    assert ("wait_for", "$") in session.log_calls
    assert "never reached\n" not in session.sent


def test_run_file_variable_substitution(tmp_path, fake_app):
    session = RecordingSession(fake_app)
    runner = TeraTermMacroRunner(fake_app, session)
    ttl = tmp_path / "vars.ttl"
    ttl.write_text('greeting = "hello there"\nsendln greeting\n', encoding="utf-8")
    runner.run_file(str(ttl))
    assert session.sent == ["hello there\n"]


def test_run_file_wait_timeout_raises(tmp_path, fake_app):
    session = RecordingSession(fake_app)
    session._wait_for_result = False
    runner = TeraTermMacroRunner(fake_app, session)
    ttl = tmp_path / "timeout.ttl"
    ttl.write_text("wait 'never appears'\n", encoding="utf-8")
    with pytest.raises(TimeoutError):
        runner.run_file(str(ttl))


def test_connect_dispatches_ssh_via_app_after(fake_app):
    session = RecordingSession(fake_app)
    runner = TeraTermMacroRunner(fake_app, session)
    runner._connect("192.168.1.1 /user=admin /passwd=secret")
    assert fake_app.connect_calls, "expected connect_session to be invoked via app.after"
    called_session, conn, label = fake_app.connect_calls[0]
    assert called_session is session
    assert conn.host == "192.168.1.1"
    assert conn.username == "admin"
    assert "SSH" in label


def test_connect_dispatches_telnet_when_flagged(fake_app):
    session = RecordingSession(fake_app)
    runner = TeraTermMacroRunner(fake_app, session)
    runner._connect("10.0.0.1 /telnet /port=2323")
    _, conn, label = fake_app.connect_calls[0]
    assert conn.port == 2323
    assert "Telnet" in label


# --- execute_python_script_sync: the exec() sandbox ----------------------------

def test_script_send_and_print(tmp_path, fake_app):
    session = RecordingSession(fake_app)
    script = tmp_path / "s.py"
    script.write_text('send("ls")\nprint("hello")\n', encoding="utf-8")
    execute_python_script_sync(session, str(script))
    assert session.sent == ["ls\n"]
    out = _drain_all(session.incoming)
    assert any("hello" in o for o in out)


def test_script_wait_with_seconds_sleeps(tmp_path, fake_app):
    session = RecordingSession(fake_app)
    script = tmp_path / "s.py"
    script.write_text("wait(0.05)\n", encoding="utf-8")
    start = time.time()
    execute_python_script_sync(session, str(script))
    assert time.time() - start >= 0.04


def test_script_wait_with_pattern_delegates_to_session(tmp_path, fake_app):
    session = RecordingSession(fake_app)
    script = tmp_path / "s.py"
    script.write_text('wait("ready")\n', encoding="utf-8")
    execute_python_script_sync(session, str(script))
    assert ("wait_for", "ready") in session.log_calls


def test_script_connect_telnet_waits_until_app_session_is_connected(tmp_path, fake_app):
    session = RecordingSession(fake_app)
    script = tmp_path / "connect.py"
    script.write_text('connect_telnet("127.0.0.1", port=2323)\n', encoding="utf-8")

    execute_python_script_sync(session, str(script))

    assert fake_app.connect_calls
    assert session.connection is fake_app.connect_calls[0][1]
    assert session.connected_label == "Telnet 127.0.0.1"


def test_script_log_on_and_bare_log_off(tmp_path, fake_app):
    """log_off with no args must stop whatever is currently logging (no path)."""
    session = RecordingSession(fake_app)
    script = tmp_path / "s.py"
    script.write_text('log_on("out.log")\nlog_off\n', encoding="utf-8")
    execute_python_script_sync(session, str(script))
    assert ("enable", str((tmp_path / "out.log").resolve())) in session.log_calls
    assert ("disable", None) in session.log_calls


def test_script_log_off_with_explicit_path(tmp_path, fake_app):
    """Regression test: log_off("path") must not raise TypeError against
    TerminalSession.disable_logging(path)."""
    session = RecordingSession(fake_app)
    script = tmp_path / "s.py"
    script.write_text('log_on("named.log")\nlog_off("named.log")\n', encoding="utf-8")
    execute_python_script_sync(session, str(script))
    expected_path = str((tmp_path / "named.log").resolve())
    assert ("enable", expected_path) in session.log_calls
    assert ("disable", expected_path) in session.log_calls


def test_script_run_script_recurses_into_subscript(tmp_path, fake_app):
    session = RecordingSession(fake_app)
    sub = tmp_path / "sub.py"
    sub.write_text('send("from-sub")\n', encoding="utf-8")
    main_script = tmp_path / "main.py"
    main_script.write_text('run_script("sub.py")\n', encoding="utf-8")
    execute_python_script_sync(session, str(main_script))
    assert session.sent == ["from-sub\n"]


def test_script_run_script_missing_file_raises(tmp_path, fake_app):
    session = RecordingSession(fake_app)
    main_script = tmp_path / "main.py"
    main_script.write_text('run_script("does_not_exist.py")\n', encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        execute_python_script_sync(session, str(main_script))


def test_script_terminal_name_reflects_current_session(tmp_path, fake_app):
    session = RecordingSession(fake_app)
    script = tmp_path / "s.py"
    script.write_text('print(terminal_name)\n', encoding="utf-8")
    execute_python_script_sync(session, str(script))
    out = _drain_all(session.incoming)
    assert any(session.name in o for o in out)


def test_script_raising_exception_propagates(tmp_path, fake_app):
    session = RecordingSession(fake_app)
    script = tmp_path / "s.py"
    script.write_text('raise ValueError("boom")\n', encoding="utf-8")
    with pytest.raises(ValueError):
        execute_python_script_sync(session, str(script))


# --- integration-ish: real TerminalSession with logging on disk --------------

def test_real_session_log_off_with_path_does_not_raise(terminal_session, tmp_path):
    """End-to-end regression test using the actual TerminalSession (not the
    RecordingSession stand-in) to confirm the fix works against the real
    dataclass, not just the test double's compatible signature."""
    log_path = tmp_path / "real.log"
    terminal_session.enable_custom_logging(log_path)
    terminal_session.disable_logging(log_path)  # must not raise TypeError
    assert terminal_session.log_enabled is False


def _drain_all(q: "queue.Queue[str]") -> list[str]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out
