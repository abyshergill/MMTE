from __future__ import annotations

import datetime as dt
import os
import posixpath
import queue
import re
import sys
import tempfile
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from core.config import APP_NAME, LOG_DIR, PROFILES_FILE, TELNET_PROFILES_FILE
from core.connections import BaseConnection, SSHConnection, SerialConnection, TelnetConnection
from core.models import ScriptStopped, TerminalSession
from core.profile_store import ProfileStore
from core.script_engine import TeraTermMacroRunner, execute_python_script_sync
from core.sftp_hops import get_active_sftp
from core.theme import TERMINAL_BG
from gui.daw_manager import MultiScriptDialog
from gui.dialogs import RenameDialog, SamplesDialog, SerialDialog, SSHDialog, TelnetDialog
from gui.helpers import _AfterBridge, set_hand_cursor
from gui.profiles_dialog import ProfilesDialog
from gui.rag_dialog import RAGManagementDialog
from gui.session_label import _SessionLabel
from gui.sftp_dialog import SFTPBrowserDialog
from gui.terminal_widget import TerminalWidget
from gui.ui_style import APP_STYLESHEET

from gui.ai_assistant_panel import AIAssistantPanel


class TerminalApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1340, 780)
        self.setMinimumSize(1020, 620)

        icon_path = Path("assets/icon.ico")
        if icon_path.exists():
            try:
                self.setWindowIcon(QIcon(str(icon_path)))
            except Exception:
                pass

        self._after_bridge = _AfterBridge()

        self.profile_store = ProfileStore(PROFILES_FILE)
        self.telnet_profile_store = ProfileStore(TELNET_PROFILES_FILE)
        self.sessions: list[TerminalSession] = []
        self.active_session: TerminalSession | None = None
        self.session_list_rows: dict[TerminalSession, QListWidgetItem] = {}

        self._build_ui()
        self.new_terminal()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self.poll_queues)
        self._poll_timer.start(40)

    def after(self, ms, callback, *args, **kwargs):
        if threading.current_thread() is threading.main_thread():
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: callback(*args, **kwargs))
            timer.start(max(int(ms), 0))
            return timer
        else:
            self._after_bridge.request.emit(callback, args, kwargs)
            return None

    def after_cancel(self, job):
        if job is not None:
            try:
                job.stop()
            except Exception:
                pass

    def toggle_sidebar(self):
        self.sidebar.setVisible(not self.sidebar.isVisible())
        self._sidebar_btn.setText("\u276f" if not self.sidebar.isVisible() else "\u276e")

    def _build_menu_bar(self):
        menubar = self.menuBar()
        menubar.setNativeMenuBar(False)

        conn_menu = menubar.addMenu("&Connection")
        conn_menu.addAction("SSH...", self.connect_ssh_dialog)
        conn_menu.addAction("Telnet...", self.connect_telnet_dialog)
        conn_menu.addAction("Serial...", self.connect_serial_dialog)
        conn_menu.addSeparator()
        conn_menu.addAction("Disconnect", self.disconnect_current)

        script_menu = menubar.addMenu("&Scripting")
        script_menu.addAction("Run Script...", self.run_script)
        self.pause_script_btn = script_menu.addAction("\u23f8 Pause Script", self.toggle_pause_script)
        self.stop_script_btn = script_menu.addAction("\u23f9 Stop Script", self.stop_script)
        self.pause_script_btn.setEnabled(False)
        self.stop_script_btn.setEnabled(False)
        script_menu.addSeparator()
        script_menu.addAction("Multi-Script Suite...", self.open_playlist_dialog)

        util_menu = menubar.addMenu("&Utility")
        util_menu.addAction("Files (SFTP Browser)...", self.open_sftp_dialog)
        util_menu.addAction("Saved Profiles...", self.manage_profiles)
        util_menu.addSeparator()
        util_menu.addAction("Clear Terminal", self.clear_terminal)
        util_menu.addSeparator()
        self.start_logging_action = util_menu.addAction("Start Logging...", self.start_logging)
        self.pause_logging_action = util_menu.addAction("\u23f8 Pause Logging", self.toggle_pause_logging)
        self.stop_logging_action = util_menu.addAction("\u23f9 Stop Logging", self.stop_logging)
        self.pause_logging_action.setEnabled(False)
        self.stop_logging_action.setEnabled(False)
        util_menu.addSeparator()
        toggle_sidebar_action = util_menu.addAction("Show/Hide Sidebar", self.toggle_sidebar)
        toggle_sidebar_action.setShortcut("Ctrl+B")

        # Single initialization for AI Assistant Dock Panel
        try:
            self.ai_dock = QDockWidget("AI Test Engineer", self)
            self.ai_panel = AIAssistantPanel(self)
            self.ai_dock.setWidget(self.ai_panel)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.ai_dock)

            ai_menu = menubar.addMenu("&AI")
            ai_window_action = self.ai_dock.toggleViewAction()
            ai_window_action.setText("AI Assistant Window")
            ai_menu.addAction(ai_window_action)

            ai_menu.addAction("Manage Project RAG Memories...", lambda: RAGManagementDialog(self).exec())
            ai_menu.addAction("LLM Settings...", self.ai_panel.open_settings)

        except Exception as e:
            print(f"Failed to attach AI Dock Panel: {e}", file=sys.stderr)

        help_menu = menubar.addMenu("&Help")
        help_menu.addAction("Sample Scripts && Templates...", lambda: SamplesDialog(self).exec())
        help_menu.addAction("Developer Info / About...", self._show_about)

    def _build_ui(self):
        self.setStyleSheet(APP_STYLESHEET)
        self._build_menu_bar()

        central = QWidget()
        self.setCentralWidget(central)
        grid = QGridLayout(central)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(0)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 1)

        self.sidebar = QFrame()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(245)
        self.sidebar.setAutoFillBackground(True)
        sb_layout = QVBoxLayout(self.sidebar)
        sb_layout.setContentsMargins(14, 16, 14, 12)

        title_row = QHBoxLayout()
        title_lbl = QLabel("TERMINALS")
        title_lbl.setObjectName("SidebarTitle")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title_lbl.setFont(title_font)
        title_row.addWidget(title_lbl, 1)

        self._sidebar_btn = QPushButton("\u276e")
        self._sidebar_btn.setObjectName("CollapseBtn")
        self._sidebar_btn.setFixedSize(24, 24)
        self._sidebar_btn.setFlat(True)
        set_hand_cursor(self._sidebar_btn)
        self._sidebar_btn.clicked.connect(self.toggle_sidebar)
        title_row.addWidget(self._sidebar_btn)
        sb_layout.addLayout(title_row)
        sb_layout.addSpacing(10)

        new_btn = QPushButton("+ New Terminal")
        new_btn.setObjectName("AccentBtn")
        new_btn.clicked.connect(lambda: self.new_terminal())
        sb_layout.addWidget(new_btn)

        rename_btn = QPushButton("Rename Selected")
        rename_btn.clicked.connect(self.rename_terminal)
        sb_layout.addWidget(rename_btn)

        close_btn = QPushButton("Close Selected")
        close_btn.setObjectName("DangerBtn")
        close_btn.clicked.connect(self.close_current_terminal)
        sb_layout.addWidget(close_btn)

        sb_layout.addSpacing(10)
        sessions_lbl = QLabel("OPEN SESSIONS")
        sessions_lbl.setObjectName("SidebarSubtitle")
        sb_layout.addWidget(sessions_lbl)
        self.session_list = QListWidget()
        self.session_list.setObjectName("SessionList")
        self.session_list.setFrameShape(QFrame.Shape.NoFrame)
        sb_layout.addWidget(self.session_list, 1)

        grid.addWidget(self.sidebar, 0, 0, 2, 1)

        self.content = QFrame()
        self.content.setStyleSheet(f"background-color: {TERMINAL_BG};")
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(12, 8, 12, 4)

        header_row = QHBoxLayout()
        self.header_label = QLabel("No terminal")
        header_font = QFont()
        header_font.setPointSize(12)
        header_font.setBold(True)
        self.header_label.setFont(header_font)
        header_row.addWidget(self.header_label)
        header_row.addStretch(1)
        self.script_status_label = QLabel("")
        self.script_status_label.setStyleSheet("color: #d99a2b; font-weight: bold; padding: 2px 8px;")
        header_row.addWidget(self.script_status_label)
        content_layout.addLayout(header_row)

        self.tab_bar = QTabBar()
        self.tab_bar.setExpanding(False)
        self.tab_bar.setTabsClosable(True)
        self.tab_bar.setMovable(True)
        self.tab_bar.currentChanged.connect(self._on_tab_changed)
        self.tab_bar.tabCloseRequested.connect(self._on_tab_closed)
        content_layout.addWidget(self.tab_bar)

        self.stack = QStackedWidget()
        content_layout.addWidget(self.stack, 1)

        grid.addWidget(self.content, 0, 1)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("StatusBar")
        self.status_label.setAutoFillBackground(True)
        grid.addWidget(self.status_label, 1, 1)

    def _on_tab_changed(self, index):
        session = self.tab_bar.tabData(index) if index >= 0 else None
        if session is not None:
            self.select_session(session)

    def _on_tab_closed(self, index):
        session = self.tab_bar.tabData(index)
        if session is not None:
            self.close_session(session)

    def _tab_index_for_session(self, session) -> int:
        for i in range(self.tab_bar.count()):
            if self.tab_bar.tabData(i) is session:
                return i
        return -1

    def _sync_tabs(self):
        self.tab_bar.blockSignals(True)
        while self.tab_bar.count() > 0:
            self.tab_bar.removeTab(0)
        for s in self.sessions:
            idx = self.tab_bar.addTab(s.name)
            self.tab_bar.setTabData(idx, s)
        if self.active_session is not None:
            idx = self._tab_index_for_session(self.active_session)
            if idx != -1:
                self.tab_bar.setCurrentIndex(idx)
        self.tab_bar.blockSignals(False)

    def _update_tab_text(self, session):
        idx = self._tab_index_for_session(session)
        if idx != -1:
            self.tab_bar.setTabText(idx, session.name)

    def new_terminal(self, name: str | None = None) -> TerminalSession:
        idx = len(self.sessions) + 1
        t_name = name if (name and isinstance(name, str)) else f"Terminal {idx}"

        frame = QWidget()
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(10, 10, 10, 10)
        terminal = TerminalWidget(self)
        frame_layout.addWidget(terminal)

        session = TerminalSession(app=self, name=t_name, frame=frame, terminal=terminal)
        terminal.session = session

        self.sessions.append(session)
        self.stack.addWidget(frame)
        self.select_session(session)
        session.append("\r\n[New terminal. Use SSH, Telnet or Serial, then type directly here.]\r\n")
        session.render()
        self.refresh_session_list()
        session._resize_job = self.after(150, lambda: self._apply_resize(session))
        return session

    def get_session_by_name(self, name: str) -> TerminalSession | None:
        for s in self.sessions:
            if s.name == name:
                return s
        for s in self.sessions:
            if s.name.lower() == name.lower():
                return s
        return None

    def select_session_by_name(self, name: str) -> TerminalSession | None:
        s = self.get_session_by_name(name)
        if s:
            self.select_session(s)
            return s
        return None

    def close_session_by_name(self, name: str) -> None:
        s = self.get_session_by_name(name)
        if s:
            self.close_session_direct(s)

    def rename_terminal_by_name(self, current_name: str, new_name: str) -> bool:
        s = self.get_session_by_name(current_name)
        if s and new_name:
            s.name = new_name
            self._update_tab_text(s)
            self.select_session(s)
            self.refresh_session_list()
            return True
        return False

    def close_session_direct(self, s: TerminalSession):
        job = getattr(s, "_resize_job", None)
        if job:
            self.after_cancel(job)
            s._resize_job = None
        s.close()
        if s in self.sessions:
            self.sessions.remove(s)
        if self.active_session is s:
            self.active_session = None
        try:
            self.stack.removeWidget(s.frame)
            s.frame.deleteLater()
        except Exception:
            pass
        if self.sessions:
            self.select_session(self.sessions[-1])
        else:
            self.header_label.setText("No terminal")
        self.refresh_session_list()

    def has_selection(self, session: TerminalSession) -> bool:
        try:
            return session.terminal.textCursor().hasSelection()
        except Exception:
            return False

    def compute_terminal_size(self, session: TerminalSession) -> tuple[int, int]:
        text_widget = session.terminal
        fm = text_widget.fontMetrics()
        char_width = max(fm.horizontalAdvance("0"), 1)
        line_height = max(fm.lineSpacing(), 1)

        padding_px = 14
        widget_width = max(text_widget.viewport().width() - padding_px, char_width)
        widget_height = max(text_widget.viewport().height() - padding_px, line_height)

        cols = max(min(widget_width // char_width, 200), 20)
        rows = max(min(widget_height // line_height, 80), 5)
        return cols, rows

    def on_terminal_resize(self, session: TerminalSession):
        job = getattr(session, "_resize_job", None)
        if job:
            self.after_cancel(job)
        session._resize_job = self.after(250, lambda: self._apply_resize(session))

    def _apply_resize(self, session: TerminalSession):
        session._resize_job = None
        if session not in self.sessions or not session._widget_alive():
            return
        try:
            cols, rows = self.compute_terminal_size(session)
            session.resize(cols, rows)
        except Exception:
            pass

    def handle_terminal_click(self, session: TerminalSession, event):
        self.select_session(session)

    def handle_mousewheel(self, session: TerminalSession, event) -> None:
        self.select_session(session)
        delta = event.angleDelta().y()
        steps = int(delta / 120) if abs(delta) >= 120 else (1 if delta > 0 else -1)
        session.scroll_history(steps * 3)

    def handle_terminal_key(self, session: TerminalSession, event) -> bool:
        key = event.key()
        modifiers = event.modifiers()
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)

        telnet_escape = (
            event.text() == "\x1d"
            or (ctrl and event.key() in (Qt.Key.Key_BracketRight, Qt.Key.Key_BracketLeft, Qt.Key.Key_5))
            or (ctrl and event.nativeVirtualKey() in (0xDB, 0xDD))
        )

        connection_label = str(getattr(session.connection, "label", "")).lower()
        if telnet_escape and connection_label == "telnet":
            session.close()
            session.append("\r\n[Telnet session disconnected via Ctrl + ] escape sequence]\r\n")
            session.render()
            self.refresh_session_list()
            return True

        text = event.text()
        letter = chr(key).lower() if Qt.Key.Key_A <= key <= Qt.Key.Key_Z else None

        non_scroll_keys = {Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Meta}
        if session.scroll_offset and key not in non_scroll_keys:
            session.scroll_offset = 0
            session.render()

        if ctrl and shift and letter == "c":
            self.copy_selection(session)
            return True
        if ctrl and shift and letter == "x":
            self.cut_selection(session)
            return True
        if ctrl and shift and letter == "v":
            self.paste_to_terminal(session)
            return True

        if shift and key == Qt.Key.Key_PageUp:
            session.scroll_history(session.rows - 2)
            return True
        if shift and key == Qt.Key.Key_PageDown:
            session.scroll_history(-(session.rows - 2))
            return True

        if session.connection:
            if ctrl and not shift:
                if letter == "c" and self.has_selection(session):
                    self.copy_selection(session)
                    return True
                ctrl_map = {chr(c + 96): chr(c) for c in range(1, 27)}
                if letter and letter in ctrl_map:
                    session.send(ctrl_map[letter])
                    return True

            if key == Qt.Key.Key_Left:
                session.send("\x1b[D")
                return True
            if key == Qt.Key.Key_Right:
                session.send("\x1b[C")
                return True
            if key == Qt.Key.Key_Up:
                session.send("\x1b[A")
                return True
            if key == Qt.Key.Key_Down:
                session.send("\x1b[B")
                return True

            if key == Qt.Key.Key_Backspace:
                cursor = session.screen.cursor
                if session.prompt_row == cursor.y and cursor.x <= session.prompt_col:
                    return True
                session.send("\x7f")
                return True

            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                session.send("\r")
                session.awaiting_prompt = True
                return True

            special = {
                Qt.Key.Key_Tab: "\t",
                Qt.Key.Key_Escape: "\x1b",
                Qt.Key.Key_Delete: "\x1b[3~",
                Qt.Key.Key_Home: "\x1b[H",
                Qt.Key.Key_End: "\x1b[F",
                Qt.Key.Key_PageUp: "\x1b[5~",
                Qt.Key.Key_PageDown: "\x1b[6~",
            }
            if key in special:
                session.send(special[key])
                return True

            if text and len(text) == 1 and ord(text) >= 32:
                session.send(text)
                return True

            return True

        if ctrl and not shift and letter == "c":
            if self.has_selection(session):
                self.copy_selection(session)
            return True
        if ctrl and not shift and letter == "x":
            if self.has_selection(session):
                self.cut_selection(session)
            return True
        if ctrl and not shift and letter == "v":
            self.paste_to_terminal(session)
            return True
        if key == Qt.Key.Key_Backspace:
            session.append("\b \b")
            session.render()
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            session.append("\r\n")
            session.render()
            return True
        if text and len(text) == 1 and ord(text) >= 32:
            session.append(text)
            session.render()
            return True
        return True

    def copy_selection(self, session: TerminalSession):
        try:
            text = session.terminal.textCursor().selectedText().replace("\u2029", "\n")
            QApplication.clipboard().setText(text)
        except Exception:
            pass

    def cut_selection(self, session: TerminalSession):
        self.copy_selection(session)

    def paste_to_terminal(self, session: TerminalSession):
        try:
            text = QApplication.clipboard().text()
            if session.connection:
                session.send(text)
            else:
                session.append(text)
                session.render()
        except Exception:
            pass

    def show_context_menu(self, session: TerminalSession, event):
        self.select_session(session)
        has_sel = self.has_selection(session)

        menu = QMenu(self)
        copy_act = menu.addAction("Copy")
        copy_act.setEnabled(has_sel)
        cut_act = menu.addAction("Cut")
        cut_act.setEnabled(has_sel)
        paste_act = menu.addAction("Paste")
        menu.addSeparator()
        select_all_act = menu.addAction("Select All")

        try:
            global_pos = event.globalPos()
        except AttributeError:
            global_pos = session.terminal.mapToGlobal(event.pos())

        chosen = menu.exec(global_pos)
        if chosen is copy_act:
            self.copy_selection(session)
        elif chosen is cut_act:
            self.cut_selection(session)
        elif chosen is paste_act:
            self.paste_to_terminal(session)
        elif chosen is select_all_act:
            self.select_all(session)

    def select_all(self, session: TerminalSession):
        try:
            session.terminal.selectAll()
        except Exception:
            pass

    def select_session(self, session: TerminalSession):
        if self.active_session is not session:
            self.active_session = session
            self.stack.setCurrentWidget(session.frame)
            session.terminal.setFocus()
            self._highlight_session_list()
            idx = self._tab_index_for_session(session)
            if idx != -1 and self.tab_bar.currentIndex() != idx:
                self.tab_bar.blockSignals(True)
                self.tab_bar.setCurrentIndex(idx)
                self.tab_bar.blockSignals(False)

        self.header_label.setText(self._status_text(session))
        self._update_script_status_ui(session)
        self._update_logging_menu_ui(session)

    def _highlight_session_list(self):
        for s, item in self.session_list_rows.items():
            row_widget = self.session_list.itemWidget(item)
            if row_widget is not None:
                if s is self.active_session:
                    row_widget.setAutoFillBackground(True)
                    row_widget.setStyleSheet(
                        "background-color: palette(highlight); color: palette(highlighted-text);"
                    )
                else:
                    row_widget.setAutoFillBackground(False)
                    row_widget.setStyleSheet("background-color: transparent;")

    def refresh_session_list(self):
        self.session_list.clear()
        self.session_list_rows.clear()
        self._sync_tabs()
        for s in self.sessions:
            item = QListWidgetItem()
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(2, 2, 2, 2)

            label = _SessionLabel(self, s, s.name)
            row_layout.addWidget(label, 1)

            close_btn = QPushButton("\u00d7")
            close_btn.setFixedWidth(28)
            close_btn.setStyleSheet("QPushButton { background-color: #70303b; color: white; }")
            close_btn.clicked.connect(lambda _c, sess=s: self.close_session(sess))
            row_layout.addWidget(close_btn)

            item.setSizeHint(row.sizeHint())
            self.session_list.addItem(item)
            self.session_list.setItemWidget(item, row)
            self.session_list_rows[s] = item

        self._highlight_session_list()

    def current_session(self) -> TerminalSession | None:
        return self.active_session

    def _status_text(self, s: TerminalSession) -> str:
        if s.log_enabled:
            log_state = "PAUSED" if s.log_paused else "ON"
        else:
            log_state = "OFF"
        base = f"{s.name}  \u00b7  {s.connected_label}  \u00b7  Logging: {log_state}"
        if s.scroll_offset:
            base += f"  \u00b7  Scrollback ({s.scroll_offset} lines back)"
        return base

    def connect_ssh_dialog(self):
        s = self.current_session() or self.new_terminal()
        d = SSHDialog(self)
        if d.exec() != QDialog.DialogCode.Accepted or not d.result:
            return
        if d.result["save"]:
            pn = d.result["profile_name"] or f"{d.result['username']}@{d.result['host']}:{d.result['port']}"
            self.profile_store.upsert(
                pn, {"host": d.result["host"], "username": d.result["username"], "port": d.result["port"]}
            )
        conn = SSHConnection(
            host=d.result["host"],
            username=d.result["username"],
            password=d.result["password"],
            port=d.result["port"],
            look_for_keys=not bool(d.result["password"]),
            allow_agent=not bool(d.result["password"]),
        )
        self.connect_session(s, conn, f"SSH {d.result['host']}")

    def connect_serial_dialog(self):
        s = self.current_session() or self.new_terminal()
        d = SerialDialog(self)
        if d.exec() == QDialog.DialogCode.Accepted and d.result:
            self.connect_session(
                s,
                SerialConnection(d.result["port"], d.result["baud"]),
                f"Serial {d.result['port']} @ {d.result['baud']}",
            )

    def connect_telnet_dialog(self):
        s = self.current_session() or self.new_terminal()
        d = TelnetDialog(self)
        if d.exec() != QDialog.DialogCode.Accepted or not d.result:
            return
        if d.result["save"]:
            pn = d.result["profile_name"] or f"{d.result['host']}:{d.result['port']}"
            self.telnet_profile_store.upsert(pn, {"host": d.result["host"], "port": d.result["port"]})
        self.connect_session(s, TelnetConnection(d.result["host"], d.result["port"]), f"Telnet {d.result['host']}")

    def connect_session(self, s: TerminalSession, conn: BaseConnection, label: str):
        if s.connection:
            s.close()
        s.connecting = True

        def worker():
            try:
                conn.start(s.incoming)
                if s not in self.sessions:  # tab was closed while we were connecting
                    conn.close()
                    return
                # Publish the connection only once it is usable. Assigning it earlier
                # let keystrokes typed during the handshake hit a half-open channel,
                # raise, and tear the brand-new connection down.
                s.connection = conn
                s.connected_label = label
                s.connection_lost_notified = False
                s.incoming.put(f"\r\n[Active: {label}]\r\n")
                s.awaiting_prompt = True
                self.after(300, lambda: self._apply_resize(s))
            except Exception as exc:
                error_text = str(exc)
                try:
                    conn.close()  # release sockets / paramiko transport threads
                except Exception:
                    pass
                s.connection, s.connected_label = None, "Disconnected"
                s.incoming.put(f"\r\n[Connection failed: {error_text}]\r\n")
                self.after(0, lambda: self.select_session(s) if s in self.sessions else None)
                self.after(
                    0,
                    lambda: QMessageBox.critical(
                        self, "Connection Failed", f"Could not establish {label}.\n\n{error_text}"
                    ),
                )
            finally:
                s.connecting = False

        threading.Thread(target=worker, daemon=True).start()
        self.status_label.setText(f"Connecting: {label}")
        self.after(200, lambda: self.select_session(s) if s in self.sessions else None)

    def _show_about(self):
        QMessageBox.about(
            self,
            "About Modern Multi Terminal",
            "<b>Modern Multi Terminal</b><br>SSH / Serial / Telnet terminal with scripting "
            "(Python + Tera Term macros), SFTP and an AI assistant that can use Ollama, "
            "hosted LLM APIs or a local GGUF model.<br><br>"
            "Engineered by Aby Shergill &middot; BSD License<br>"
            "<a href='https://github.com/abyshergill/modern_multi_terminal'>github.com/abyshergill/modern_multi_terminal</a>",
        )

    def disconnect_current(self):
        s = self.current_session()
        if s:
            s.close()
            s.append("\r\n[Disconnected]\r\n")
            s.render()
            self.select_session(s)

    def rename_terminal(self):
        s = self.current_session()
        if not s:
            return
        d = RenameDialog(self, s.name)
        if d.exec() == QDialog.DialogCode.Accepted and d.result:
            s.name = d.result
            self._update_tab_text(s)
            self.select_session(s)
            self.refresh_session_list()

    def close_current_terminal(self):
        if self.current_session():
            self.close_session(self.current_session())

    def close_session(self, s: TerminalSession):
        if QMessageBox.question(self, "Close terminal", f"Close {s.name}?") != QMessageBox.StandardButton.Yes:
            return
        self.close_session_direct(s)

    def start_logging(self):
        s = self.current_session()
        if not s:
            QMessageBox.warning(self, "No Terminal", "Open terminal first.")
            return
        if s.log_enabled:
            QMessageBox.information(
                self,
                "Already logging",
                f"'{s.name}' is already logging to a file. Stop it first to start a new one.",
            )
            return
        sn = re.sub(r"[^A-Za-z0-9_.-]+", "_", s.name).strip("_") or "terminal"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        default_name = f"{sn}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        p, _ = QFileDialog.getSaveFileName(
            self, "Save Log", str(LOG_DIR / default_name), "Logs (*.log);;All Files (*.*)"
        )
        if p:
            s.enable_custom_logging(Path(p))
            self.select_session(s)

    def toggle_pause_logging(self):
        s = self.current_session()
        if not s or not s.log_enabled:
            return
        if s.log_paused:
            s.resume_logging()
        else:
            s.pause_logging()
        self.select_session(s)

    def stop_logging(self):
        s = self.current_session()
        if not s or not s.log_enabled:
            return
        s.disable_logging()
        self.select_session(s)

    def _update_logging_menu_ui(self, s: TerminalSession) -> None:
        if s.log_enabled:
            self.start_logging_action.setEnabled(False)
            self.pause_logging_action.setEnabled(True)
            self.pause_logging_action.setText("\u25b6 Resume Logging" if s.log_paused else "\u23f8 Pause Logging")
            self.stop_logging_action.setEnabled(True)
        else:
            self.start_logging_action.setEnabled(True)
            self.pause_logging_action.setEnabled(False)
            self.pause_logging_action.setText("\u23f8 Pause Logging")
            self.stop_logging_action.setEnabled(False)

    def open_playlist_dialog(self):
        if not self.sessions:
            self.new_terminal()
        MultiScriptDialog(self)

    def open_sftp_dialog(self):
        s = self.current_session()
        if not s or not s.connection or s.connection.label != "SSH":
            QMessageBox.warning(self, "SSH required", "The Files browser requires an active SSH connection.")
            return
        SFTPBrowserDialog(self, s)

    def handle_text_drop(self, session: TerminalSession, text: str):
        if not text:
            return
        self.select_session(session)
        if session.connection:
            session.send(text)
        else:
            session.append(text)
            session.render()

    def handle_file_drop(self, session: TerminalSession, event):
        if not (session.connection and session.connection.label == "SSH"):
            QMessageBox.warning(self, "SSH required", "Drag-and-drop upload requires an active SSH connection.")
            return
        try:
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
        except Exception:
            paths = []
        if not paths:
            return

        def worker():
            try:
                sftp = get_active_sftp(session)
                remote_home = sftp.normalize(".")
            except Exception as exc:
                session.incoming.put(f"\r\n[Upload failed: could not open SFTP session: {exc}]\r\n")
                return
            for local_path in paths:
                name = Path(local_path).name
                remote_path = posixpath.join(remote_home.rstrip("/") or "/", name)
                try:
                    session.incoming.put(f"\r\n[Uploading {name} -> {remote_path} ...]\r\n")
                    sftp.put(local_path, remote_path)
                    session.incoming.put(f"\r\n[Upload complete: {name}]\r\n")
                except Exception as exc:
                    session.incoming.put(f"\r\n[Upload failed for {name}: {exc}]\r\n")

        threading.Thread(target=worker, daemon=True).start()

    def clear_terminal(self):
        s = self.current_session()
        if s:
            s.clear_screen()

    def run_script(self):
        s = self.current_session()
        if not s:
            return
        if s.script_running:
            QMessageBox.warning(
                self,
                "Script already running",
                f"'{s.script_name}' is already running on this terminal. Stop it first, or switch to another terminal.",
            )
            return
        p, _ = QFileDialog.getOpenFileName(self, "Select Script", "", "Scripts (*.ttl *.py);;All Files (*.*)")
        if p:
            threading.Thread(
                target=self._run_ttl if p.lower().endswith(".ttl") else self._run_python,
                args=(s, p),
                daemon=True,
            ).start()

    def _run_ttl(self, s: TerminalSession, p: str):
        s.start_script(Path(p).name)
        try:
            s.incoming.put(f"\r\n[Running TTL: {p}]\r\n")
            TeraTermMacroRunner(self, s).run_file(p)
            s.incoming.put("\r\n[TTL completed]\r\n")
        except ScriptStopped:
            s.incoming.put("\r\n[TTL stopped by user]\r\n")
        except Exception as exc:
            s.incoming.put(f"\r\n[TTL error: {exc}]\r\n")
        finally:
            s.finish_script()

    def _run_python(
        self,
        s: TerminalSession,
        p: str,
        script_name: str | None = None,
        already_started: bool = False,
    ):
        if not already_started:
            s.start_script(script_name or Path(p).name)
        elif script_name:
            s.script_name = script_name
        try:
            s.incoming.put(f"\r\n[Running Python: {p}]\r\n")
            execute_python_script_sync(s, p)
            s.incoming.put("\r\n[Python completed]\r\n")
        except ScriptStopped:
            s.incoming.put("\r\n[Python script stopped by user]\r\n")
        except Exception as exc:
            s.incoming.put(f"\r\n[Python error: {exc}]\r\n")
        finally:
            s.finish_script()

    def run_ai_automation(
        self,
        s: TerminalSession,
        code: str,
        script_name: str = "AI Terminal Action",
        on_complete=None,
    ) -> bool:
        """Run validated AI-generated app actions through the normal Python script engine."""
        if s not in self.sessions:
            return False
        if s.script_running:
            s.incoming.put(f"\r\n[AI action not started: {s.script_name} is already running]\r\n")
            return False

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", prefix="ai_terminal_action_", delete=False, encoding="utf-8"
        ) as script_file:
            script_file.write(code)
            script_path = script_file.name

        output_offsets = {session: len(session.recv_buffer) for session in self.sessions}
        s.start_script(script_name)

        def worker():
            try:
                s.incoming.put(f"\r\n[Running app-native AI automation: {script_name}]\r\n")
                self._run_python(s, script_path, script_name=script_name, already_started=True)
            finally:
                try:
                    Path(script_path).unlink(missing_ok=True)
                except OSError:
                    pass
                if on_complete:
                    self.after(0, lambda: self._finish_ai_automation(output_offsets, on_complete))

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _finish_ai_automation(self, output_offsets: dict[TerminalSession, int], on_complete) -> None:
        """Flush terminal queues, then return all output generated by an AI script."""
        self.poll_queues()
        outputs = []
        for session in list(self.sessions):
            start = output_offsets.get(session, 0)
            content = session.recv_buffer
            new_content = content[start:] if len(content) >= start else content
            if new_content.strip():
                outputs.append(f"[{session.name}]\n{new_content.strip()}")
        on_complete("\n\n".join(outputs))

    def manage_profiles(self):
        ProfilesDialog(self, self.profile_store, self.telnet_profile_store).exec()

    def poll_queues(self):
        for s in list(self.sessions):
            got_data = False
            while True:
                try:
                    data = s.incoming.get_nowait()
                except queue.Empty:
                    break
                got_data = True
                try:
                    s.append(data)
                except Exception as exc:
                    print(f"[append() error on '{s.name}']: {exc}", file=sys.stderr)

            if got_data:
                try:
                    s.render()
                except Exception as exc:
                    print(f"[render() error on '{s.name}']: {exc}", file=sys.stderr)

            if s.awaiting_prompt:
                s.prompt_row = s.screen.cursor.y
                s.prompt_col = s.screen.cursor.x
                s.awaiting_prompt = False

            if s is self.active_session:
                self.header_label.setText(self._status_text(s))
                self._update_script_status_ui(s)
                self._update_logging_menu_ui(s)

    def _update_script_status_ui(self, s: TerminalSession) -> None:
        if s.script_running:
            paused = not s.script_pause_event.is_set()
            state = "PAUSED" if paused else "RUNNING"
            self.script_status_label.setText(f"\u23f3 Script {state}: {s.script_name}")
            self.pause_script_btn.setEnabled(True)
            self.pause_script_btn.setText("\u25b6 Resume" if paused else "\u23f8 Pause Script")
            self.stop_script_btn.setEnabled(True)
        else:
            self.script_status_label.setText("")
            self.pause_script_btn.setEnabled(False)
            self.pause_script_btn.setText("\u23f8 Pause Script")
            self.stop_script_btn.setEnabled(False)

    def toggle_pause_script(self):
        s = self.current_session()
        if s and s.script_running:
            s.toggle_pause_script()
            self._update_script_status_ui(s)

    def stop_script(self):
        s = self.current_session()
        if s and s.script_running:
            s.request_stop_script()

    def closeEvent(self, event):
        for s in list(self.sessions):
            s.close()
        event.accept()