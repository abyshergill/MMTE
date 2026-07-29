from __future__ import annotations
import os
import sys
from pathlib import Path

# Must be set before any PyQt6 widget/application is created.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole test session (Qt only allows one per process)."""
    app = QApplication.instance() or QApplication(["pytest"])
    yield app


@pytest.fixture
def terminal_widget(qapp):
    """A real TerminalWidget, unattached to any TerminalApp, for widget-level tests."""
    from gui.terminal_widget import TerminalWidget
    w = TerminalWidget(app=None)
    yield w
    w.deleteLater()


@pytest.fixture
def terminal_session(qapp, terminal_widget):
    """A real TerminalSession backed by a real Qt widget, for models.py tests."""
    from core.models import TerminalSession
    session = TerminalSession(app=None, name="Test Terminal", frame=terminal_widget, terminal=terminal_widget)
    terminal_widget.session = session
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _no_blocking_message_boxes(monkeypatch, qapp):
    """
    Several code paths (dialog validation errors, connection failures, the
    'close terminal?' confirmation) pop a real modal QMessageBox. Under a
    real desktop these wait for a click; in a headless test run nothing will
    ever click them, so the process hangs forever. Stub the static methods
    so tests can exercise those code paths without a GUI event loop stall.
    """
    from PyQt6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)


class FakeApp:
    """Minimal stand-in for TerminalApp, used by script_engine / models tests
    that need `.after()` and session-management hooks without a real GUI."""

    def __init__(self):
        self.sessions = []
        self.active_session = None
        self.after_calls = []
        self.connect_calls = []

    def after(self, ms, callback):
        # Run synchronously; script_engine only relies on the call happening,
        # not on real timing, and its callers block on a threading.Event.
        self.after_calls.append((ms, callback))
        callback()
        return None

    def connect_session(self, session, conn, label):
        self.connect_calls.append((session, conn, label))
        session.connection = conn
        session.connected_label = label

    def select_session(self, session):
        self.active_session = session

    def new_terminal(self, name=None):
        return None

    def close_session_by_name(self, name):
        pass

    def rename_terminal_by_name(self, current, new):
        return False

    def select_session_by_name(self, name):
        return None


@pytest.fixture
def fake_app():
    return FakeApp()
