from __future__ import annotations

from PyQt6.QtWidgets import QWidget
from gui.daw_manager import MultiScriptDialog, trigger_os_beep
from core.models import PlaylistItem


class FakeSession:
    def __init__(self, name):
        self.name = name


class FakeApp(QWidget):
    def __init__(self, sessions):
        super().__init__()
        self.sessions = sessions
        self.active_session = sessions[0] if sessions else None


def _dialog(qapp, n_sessions=2):
    sessions = [FakeSession(f"Term {i}") for i in range(n_sessions)]
    app = FakeApp(sessions)
    d = MultiScriptDialog(app)
    return d, sessions


def test_empty_playlist_disables_launch(qapp):
    d, _ = _dialog(qapp)
    assert d.playlist == []
    assert d.launch_btn.isEnabled() is False


def test_route_session_reassigns_target(qapp):
    d, sessions = _dialog(qapp)
    item = PlaylistItem(path="/tmp/a.py", timeout=10.0, target_session=sessions[0])
    d.playlist.append(item)
    d._route_session(item, sessions[1].name)
    assert item.target_session is sessions[1]


def test_update_timeout_parses_float(qapp):
    d, sessions = _dialog(qapp)
    item = PlaylistItem(path="/tmp/a.py", timeout=10.0, target_session=sessions[0])
    from PyQt6.QtWidgets import QLineEdit
    entry = QLineEdit("42")
    d._update_t(item, entry)
    assert item.timeout == 42.0


def test_update_timeout_ignores_invalid_input(qapp):
    d, sessions = _dialog(qapp)
    item = PlaylistItem(path="/tmp/a.py", timeout=10.0, target_session=sessions[0])
    from PyQt6.QtWidgets import QLineEdit
    entry = QLineEdit("not-a-number")
    d._update_t(item, entry)
    assert item.timeout == 10.0  # unchanged


def test_swap_reorders_playlist(qapp):
    d, sessions = _dialog(qapp)
    a = PlaylistItem(path="/tmp/a.py", timeout=1.0, target_session=sessions[0])
    b = PlaylistItem(path="/tmp/b.py", timeout=1.0, target_session=sessions[0])
    d.playlist = [a, b]
    d._swap(0, 1)
    assert d.playlist == [b, a]


def test_swap_out_of_bounds_is_noop(qapp):
    d, sessions = _dialog(qapp)
    a = PlaylistItem(path="/tmp/a.py", timeout=1.0, target_session=sessions[0])
    d.playlist = [a]
    d._swap(0, 5)  # out of bounds; must not raise or corrupt list
    assert d.playlist == [a]


def test_remove_item(qapp):
    d, sessions = _dialog(qapp)
    a = PlaylistItem(path="/tmp/a.py", timeout=1.0, target_session=sessions[0])
    d.playlist = [a]
    d._rm(a)
    assert d.playlist == []


def test_add_files_appends_to_playlist_with_default_session(qapp, monkeypatch):
    d, sessions = _dialog(qapp)
    monkeypatch.setattr(
        "gui.daw_manager.QFileDialog.getOpenFileNames",
        lambda *a, **k: (["/tmp/one.ttl", "/tmp/two.py"], ""),
    )
    d._add_files()
    assert len(d.playlist) == 2
    assert all(item.target_session is d.app.active_session for item in d.playlist)


def test_playlist_items_are_hashable_for_status_label_dict(qapp):
    # PlaylistItem uses @dataclass(eq=False) specifically so it can be a
    # dict key in live_status_labels; this guards against a regression.
    a = PlaylistItem(path="/tmp/a.py", timeout=1.0, target_session=None)
    b = PlaylistItem(path="/tmp/a.py", timeout=1.0, target_session=None)
    d = {a: "one", b: "two"}
    assert d[a] == "one"
    assert d[b] == "two"
    assert a != b or a is b  # identity-based equality, not value-based


def test_trigger_os_beep_does_not_raise():
    trigger_os_beep()  # platform fallback path must not raise even off-Windows
