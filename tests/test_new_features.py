"""Tests for: terminal syntax highlighting, macro recording, vi/nano -> built-in editor,
the text editor, profile add/edit, tab "+" / inline rename, hidden AI dock and the splash."""

from __future__ import annotations

import io
import time

import pytest
from PyQt6.QtCore import QEvent, QTimer, Qt
from PyQt6.QtGui import QKeyEvent

import main as main_module
from core.macro_recorder import MacroRecorder, extract_command, is_secret_prompt, parse_editor_command
from core.terminal_highlight import highlight_spans


# --- helpers ----------------------------------------------------------------------------------

@pytest.fixture
def app_window(qapp):
    win = main_module.TerminalApp()
    yield win
    for t in win.findChildren(QTimer):
        t.stop()
    if win._text_editor is not None:
        for doc in list(win._text_editor.documents):
            doc.editor.document().setModified(False)
        win._text_editor.close()
    for s in list(win.sessions):
        s.recorder = None
        try:
            s.close()
        except Exception:
            pass
    win.sessions.clear()
    win.close()
    win.deleteLater()
    qapp.processEvents()


def _key(key, text="", ctrl=False):
    mods = Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier
    return QKeyEvent(QEvent.Type.KeyPress, key, mods, text)


def _type(win, session, text):
    for ch in text:
        code = Qt.Key.Key_Space if ch == " " else (ord(ch.upper()) if ch.isalnum() else Qt.Key.Key_unknown)
        win.handle_terminal_key(session, _key(code, text=ch))


def _enter(win, session):
    win.handle_terminal_key(session, _key(Qt.Key.Key_Return))


def _pump(qapp, condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class FakeSFTP:
    def __init__(self, files=None, home="/home/aby"):
        self.files = dict(files or {})
        self.home = home

    def normalize(self, path):
        return self.home

    def stat(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)

        class Attrs:
            st_mode = 0o100644
            st_size = len(self.files[path])

        return Attrs()

    def open(self, path, mode="rb"):
        sftp = self
        if "r" in mode:
            return io.BytesIO(self.files[path])

        class Writer(io.BytesIO):
            def __exit__(self, *exc):
                sftp.files[path] = self.getvalue()
                return super().__exit__(*exc)

        return Writer()


class FakeSSH:
    label = "SSH"
    host, username, port = "10.0.0.1", "aby", 22

    def __init__(self, session=None, sftp=None, cwd="/home/aby/project"):
        self.sent = []
        self.session = session
        self.sftp = sftp or FakeSFTP()
        self.cwd = cwd

    def send(self, data):
        self.sent.append(data)
        if "__MMTE_CWD_" in data and self.session is not None:
            token = data.split("__MMTE_CWD_", 1)[1].split("__", 1)[0]
            self.session.incoming.put(f"\r\n__MMTE_CWD_{token}__{self.cwd}__END\r\n")

    def get_sftp(self):
        return self.sftp

    def close(self):
        pass

    def resize(self, cols, rows):
        pass


# --- syntax highlighting ----------------------------------------------------------------------

def test_highlight_spans_cover_prompt_ip_error_and_numbers():
    line = "aby@router01:~/logs$ ping 10.0.0.1 failed after 3 tries"
    found = {line[a:b]: tag for a, b, tag in highlight_spans(line)}
    assert found["aby@router01"] == "prompt_user"
    assert found[":~/logs"] == "prompt_path"
    assert found["10.0.0.1"] == "ip"
    assert found["failed"] == "error"
    assert found["3"] == "number"


def test_highlight_spans_never_overlap():
    line = "2026-11-06 12:01:22 WARNING eth0 fe80::1/64 00:1a:2b:3c:4d:5e https://x.io/a 91%"
    spans = highlight_spans(line)
    for (a1, b1, _), (a2, b2, _) in zip(spans, spans[1:]):
        assert b1 <= a2


def test_render_colours_plain_output_but_keeps_ansi_colours(terminal_session):
    from core.terminal_highlight import FORMATS

    terminal_session.append("ERROR here \x1b[32mERROR\x1b[0m")
    terminal_session.render()
    doc = terminal_session.terminal.document()

    def colour_at(pos):
        from PyQt6.QtGui import QTextCursor

        c = QTextCursor(doc)
        c.setPosition(pos + 1)
        return c.charFormat().foreground().color().name()

    assert colour_at(0) == FORMATS["error"][0]
    # The second ERROR is coloured green by the remote side and must stay that way.
    from core.theme import PALETTE

    assert colour_at(11) == PALETTE["green"]


def test_highlight_can_be_disabled(terminal_session):
    from core.theme import TERMINAL_FG
    from PyQt6.QtGui import QTextCursor

    terminal_session.highlight = False
    terminal_session.append("ERROR")
    terminal_session.render()
    c = QTextCursor(terminal_session.terminal.document())
    c.setPosition(1)
    assert c.charFormat().foreground().color().name() == TERMINAL_FG


def test_toggle_highlighting_updates_sessions_and_persists(app_window):
    app_window.toggle_syntax_highlighting(False)
    assert all(not s.highlight for s in app_window.sessions)
    from core.app_settings import AppSettings

    assert AppSettings().get("syntax_highlighting") is False


# --- macro recorder (pure) --------------------------------------------------------------------

def test_extract_command_strips_prompt():
    assert extract_command("aby@h:~/x$ vim a.txt") == "vim a.txt"
    assert extract_command("Router# show ip int brief") == "show ip int brief"
    assert extract_command("ls") == "ls"


def test_secret_prompt_detection():
    assert is_secret_prompt("[sudo] password for aby: ")
    assert is_secret_prompt("Enter passphrase for key '/x/id': ")
    assert not is_secret_prompt("aby@h:~$ echo password")


@pytest.mark.parametrize(
    "command, expected",
    [
        ("vim a.txt", (False, ["a.txt"])),
        ("sudo -u root vim /etc/hosts", (True, ["/etc/hosts"])),
        ('nano -T 4 "my file.txt" b', (False, ["my file.txt", "b"])),
        ("vi +12 x.py", (False, ["x.py"])),
        ("/usr/bin/vim f", (False, ["f"])),
        ("vim", None),
        ("cat a | vim -", None),
        ("vim a > b", None),
        ("ls -la", None),
    ],
)
def test_parse_editor_command(command, expected):
    assert parse_editor_command(command) == expected


def test_recorder_generates_runnable_script_with_timing():
    ticks = iter([0.0, 1.5, 4.0, 4.0, 9.0, 10.0])
    rec = MacroRecorder("T1", "SSH h", clock=lambda: next(ticks))
    rec.record_line("show version")
    rec.record_line("", secret=True, prompt="Password:")
    rec.record_keys("\x03")
    rec.record_line("exit")
    script = rec.to_python()
    assert "send('show version')" in script

    calls = []
    namespace = {
        "send": lambda text, newline=True: calls.append(("send", text, newline)),
        "sleep": lambda s: calls.append(("sleep", round(s, 2))),
        "input": lambda prompt="": "s3cret",
        "print": lambda *a, **k: None,
    }
    exec(compile(script, "macro.py", "exec"), namespace)
    assert calls == [
        ("sleep", 1.5), ("send", "show version", True),
        ("sleep", 2.5), ("send", "s3cret", True),
        ("send", "\x03", False),
        ("sleep", 5.0), ("send", "exit", True),
    ]


# --- macro recording in the main window --------------------------------------------------------

def test_recording_captures_typed_commands_and_hides_passwords(app_window):
    s = app_window.sessions[0]
    s.connection = FakeSSH()
    app_window.start_macro_recording(s)
    assert s.recorder is not None
    assert app_window.tab_bar.tabText(0).startswith("● ")
    assert "Macro: REC" in app_window._status_text(s)

    _type(app_window, s, "show version")
    _enter(app_window, s)
    s.append("\r\nPassword: ")
    _type(app_window, s, "hunter2")
    _enter(app_window, s)
    app_window.handle_terminal_key(s, _key(Qt.Key.Key_C, ctrl=True))

    script = app_window.stop_macro_recording(s, ask_to_save=False, open_in_editor=False)
    assert s.recorder is None
    assert "send('show version')" in script
    assert "hunter2" not in script
    assert "send(input('Password:'))" in script
    assert "send('\\x03', newline=False)" in script
    assert "connect_ssh('10.0.0.1', 'aby'" in script
    assert not app_window.tab_bar.tabText(0).startswith("●")


def test_recording_reads_command_from_screen_after_history_recall(app_window):
    s = app_window.sessions[0]
    s.connection = FakeSSH()
    app_window.start_macro_recording(s)
    app_window.handle_terminal_key(s, _key(Qt.Key.Key_Up))  # shell recalls a command
    s.append("\r\naby@h:~$ uptime")  # ... which the remote echoes
    _enter(app_window, s)
    script = app_window.stop_macro_recording(s, ask_to_save=False, open_in_editor=False)
    assert "send('uptime')" in script


def test_stop_recording_opens_script_in_editor(app_window):
    s = app_window.sessions[0]
    s.connection = FakeSSH()
    app_window.start_macro_recording(s)
    _type(app_window, s, "ls")
    _enter(app_window, s)
    app_window.stop_macro_recording(s, ask_to_save=False)
    editor = app_window._text_editor
    assert editor is not None and len(editor.documents) == 1
    doc = editor.documents[0]
    assert "send('ls')" in doc.editor.toPlainText()
    assert doc.highlighter.language == "python"


def test_closing_a_recording_tab_keeps_the_recording(app_window):
    s = app_window.sessions[0]
    s.connection = FakeSSH()
    app_window.start_macro_recording(s)
    _type(app_window, s, "pwd")
    _enter(app_window, s)
    app_window.close_session_direct(s)
    assert "send('pwd')" in app_window._text_editor.documents[0].editor.toPlainText()


# --- vi / vim / nano -> built-in editor ----------------------------------------------------------

def test_vim_command_is_intercepted_on_ssh(app_window, monkeypatch):
    s = app_window.sessions[0]
    s.connection = FakeSSH()
    opened = []
    monkeypatch.setattr(app_window, "_open_files_from_terminal", lambda *a: opened.append(a))
    _type(app_window, s, "vim notes.txt")
    _enter(app_window, s)
    assert opened and opened[0][1:] == ("vim notes.txt", False, ["notes.txt"])
    assert "\r" not in s.connection.sent  # Enter was not sent: vim never starts remotely


def test_vim_runs_normally_when_interception_is_off(app_window, monkeypatch):
    app_window.toggle_editor_interception(False)
    s = app_window.sessions[0]
    s.connection = FakeSSH()
    monkeypatch.setattr(app_window, "_open_files_from_terminal", lambda *a: pytest.fail("intercepted"))
    _type(app_window, s, "vim notes.txt")
    _enter(app_window, s)
    assert s.connection.sent[-1] == "\r"


def test_vim_not_intercepted_on_serial(app_window, monkeypatch):
    s = app_window.sessions[0]
    conn = FakeSSH()
    conn.label = "Serial"
    s.connection = conn
    monkeypatch.setattr(app_window, "_open_files_from_terminal", lambda *a: pytest.fail("intercepted"))
    _type(app_window, s, "vi x")
    _enter(app_window, s)
    assert conn.sent[-1] == "\r"


def test_resolve_remote_path():
    resolve = main_module.TerminalApp._resolve_remote_path
    assert resolve("a.txt", "/srv/app", "/home/u") == "/srv/app/a.txt"
    assert resolve("../b", "/srv/app", "/home/u") == "/srv/b"
    assert resolve("~/c", "/srv", "/home/u") == "/home/u/c"
    assert resolve("/etc/hosts", "/srv", "/home/u") == "/etc/hosts"


def test_vim_opens_remote_file_and_saves_back(app_window, qapp):
    s = app_window.sessions[0]
    sftp = FakeSFTP({"/home/aby/project/notes.txt": b"one\r\ntwo\r\n"})
    s.connection = FakeSSH(session=s, sftp=sftp)
    _type(app_window, s, "vim notes.txt")
    _enter(app_window, s)
    assert s.connection.sent[len("vim notes.txt")] == "\x15"  # typed command was wiped

    assert _pump(qapp, lambda: app_window._text_editor is not None and app_window._text_editor.documents)
    editor = app_window._text_editor
    doc = editor.documents[0]
    assert doc.remote_path == "/home/aby/project/notes.txt"
    assert doc.editor.toPlainText() == "one\ntwo\n"
    assert doc.eol == "\r\n"

    doc.editor.appendPlainText("three")
    assert editor.save_document(doc, wait=True)
    assert _pump(qapp, lambda: not doc.editor.document().isModified())
    assert sftp.files["/home/aby/project/notes.txt"] == b"one\r\ntwo\r\n\r\nthree"


def test_vim_on_missing_file_opens_new_document(app_window, qapp):
    s = app_window.sessions[0]
    sftp = FakeSFTP({})
    s.connection = FakeSSH(session=s, sftp=sftp)
    _type(app_window, s, "nano /tmp/new.conf")
    _enter(app_window, s)
    assert _pump(qapp, lambda: app_window._text_editor is not None and app_window._text_editor.documents)
    doc = app_window._text_editor.documents[0]
    assert doc.remote_path == "/tmp/new.conf" and doc.editor.toPlainText() == ""
    # Absolute path: no need to ask the shell for its working directory.
    assert not any("__MMTE_CWD_" in d for d in s.connection.sent)


# --- text editor -----------------------------------------------------------------------------------

def test_editor_local_roundtrip_keeps_crlf_and_bom(qapp, tmp_path):
    from gui.text_editor import TextEditorWindow

    path = tmp_path / "cfg.ini"
    path.write_bytes(b"\xef\xbb\xbf[a]\r\nx=1\r\n")
    win = TextEditorWindow()
    doc = win.open_local(str(path))
    assert doc.bom and doc.eol == "\r\n" and doc.highlighter.language == "ini"
    win.find_edit.setText("x=1")
    win.replace_edit.setText("x=2")
    assert win.replace_all() == 1
    assert win.save_current()
    assert path.read_bytes() == b"\xef\xbb\xbf[a]\r\nx=2\r\n"
    win.close()


def test_editor_refuses_binary(qapp, tmp_path):
    from gui.text_editor import decode_bytes

    with pytest.raises(ValueError):
        decode_bytes(b"\x7fELF\x00\x00\x01")


def test_editor_language_detection():
    from gui.text_editor import detect_language

    assert detect_language("x.py") == "python"
    assert detect_language("run", "#!/bin/bash\necho") == "bash"
    assert detect_language("syslog.log") == "log"
    assert detect_language("login.ttl") == "teraterm"
    assert detect_language("notes.txt") == ""


def test_editor_toggle_comment_and_duplicate(qapp):
    from gui.text_editor import TextEditorWindow

    win = TextEditorWindow()
    doc = win.open_text("a.py", "x = 1\n", language="python")
    doc.editor.selectAll()
    doc.editor.toggle_comment()
    assert doc.editor.toPlainText().startswith("# x = 1")
    doc.editor.selectAll()
    doc.editor.toggle_comment()
    assert doc.editor.toPlainText().startswith("x = 1")
    doc.editor.document().setModified(False)
    win.close()


def test_sftp_double_click_opens_file_in_editor(app_window, qapp):
    from PyQt6.QtWidgets import QListWidgetItem

    from gui.sftp_dialog import SFTPBrowserDialog

    s = app_window.sessions[0]
    sftp = FakeSFTP({"/srv/app.yaml": b"key: value\n"})
    s.connection = FakeSSH(session=s, sftp=sftp)
    s.sftp_hops = [{"label": "Hop 1: 10.0.0.1", "client": None, "sftp": sftp}]
    dialog = SFTPBrowserDialog.__new__(SFTPBrowserDialog)
    from PyQt6.QtWidgets import QDialog, QLabel

    QDialog.__init__(dialog, app_window)
    dialog.app, dialog.session, dialog.current_path = app_window, s, "/srv"
    dialog.status_label = QLabel()
    dialog._refresh = lambda: None
    item = QListWidgetItem("app.yaml")
    item.setData(Qt.ItemDataRole.UserRole, {"name": "app.yaml", "is_dir": False})
    dialog._on_item_double_clicked(item)
    assert _pump(qapp, lambda: app_window._text_editor is not None and app_window._text_editor.documents)
    doc = app_window._text_editor.documents[0]
    assert doc.remote_path == "/srv/app.yaml" and doc.highlighter.language == "yaml"


# --- profiles ------------------------------------------------------------------------------------

def test_profiles_dialog_add_edit_rename(qapp, tmp_path):
    from core.profile_store import ProfileStore
    from gui.profiles_dialog import ProfileEditDialog, ProfilesDialog

    ssh = ProfileStore(tmp_path / "ssh.json")
    telnet = ProfileStore(tmp_path / "telnet.json")
    ssh.upsert("old", {"host": "1.1.1.1", "username": "a", "port": 22})
    dialog = ProfilesDialog(None, ssh, telnet)

    edit = ProfileEditDialog(dialog, "ssh", ssh, "old")
    assert edit.host_edit.text() == "1.1.1.1"
    edit.name_edit.setText("new")
    edit.host_edit.setText("2.2.2.2")
    edit._save()
    dialog.apply_edit("ssh", "old", *edit.result)
    assert "old" not in ssh.profiles
    assert ssh.profiles["new"] == {"host": "2.2.2.2", "username": "a", "port": 22}
    assert ProfileStore(tmp_path / "ssh.json").profiles == ssh.profiles  # persisted

    add = ProfileEditDialog(dialog, "telnet", telnet, None)
    add.host_edit.setText("10.0.0.9")
    add._save()
    dialog.apply_edit("telnet", None, *add.result)
    assert telnet.profiles == {"10.0.0.9:23": {"host": "10.0.0.9", "port": 23}}


def test_profile_edit_requires_host(qapp, tmp_path):
    from core.profile_store import ProfileStore
    from gui.profiles_dialog import ProfileEditDialog

    edit = ProfileEditDialog(None, "ssh", ProfileStore(tmp_path / "s.json"), None)
    edit._save()
    assert edit.result is None


# --- tabs, AI dock, splash -------------------------------------------------------------------------

def test_plus_button_opens_new_terminal(app_window):
    before = len(app_window.sessions)
    app_window.new_tab_btn.click()
    assert len(app_window.sessions) == before + 1


def test_ctrl_shift_t_opens_new_terminal(app_window):
    before = len(app_window.sessions)
    mods = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    app_window.handle_terminal_key(
        app_window.sessions[0], QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_T, mods, "T")
    )
    assert len(app_window.sessions) == before + 1


def test_double_click_tab_renames_inline(app_window):
    from gui.session_label import InlineRenameEdit

    s = app_window.sessions[0]
    app_window._begin_tab_rename(0)
    edit = app_window.tab_bar.findChild(InlineRenameEdit)
    assert edit is not None and edit.text() == s.name
    edit.setText("Core Router")
    edit._commit()
    assert s.name == "Core Router"
    assert app_window.tab_bar.tabText(0) == "Core Router"


def test_inline_rename_escape_cancels(app_window):
    from gui.session_label import InlineRenameEdit

    s = app_window.sessions[0]
    old = s.name
    app_window._begin_tab_rename(0)
    edit = app_window.tab_bar.findChild(InlineRenameEdit)
    edit.setText("nope")
    edit.keyPressEvent(_key(Qt.Key.Key_Escape))
    assert s.name == old


def test_ai_dock_hidden_at_launch(app_window):
    app_window.show()
    assert app_window.ai_dock.isHidden()


def test_splash_pixmap_renders(qapp):
    pixmap = main_module.build_splash_pixmap(None)
    assert not pixmap.isNull() and pixmap.width() > 0
