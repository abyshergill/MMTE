from __future__ import annotations
import re
import sys
import threading
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QDialog, QWidget, QLabel, QPushButton, QCheckBox,
    QLineEdit, QComboBox, QFrame, QScrollArea, QVBoxLayout, QHBoxLayout,
    QGridLayout, QFileDialog, QMessageBox, QSizePolicy, QStyle
)

from core.models import PlaylistItem, ScriptStopped
from core.script_engine import TeraTermMacroRunner, execute_python_script_sync
from gui.dialogs import OperatorGateDialog

TERMINAL_ACCENT = "#46f0a6"
TERMINAL_MUTED = "#7b8b99"
TERMINAL_ICON = "#d8f3dc"


def trigger_os_beep():
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        try:
            QApplication.beep()
        except Exception:
            print("\a", end="", file=sys.stderr)

def light_standard_icon(widget: QWidget, standard_pixmap: QStyle.StandardPixmap) -> QIcon:
    """Convert a platform icon to a light icon for the dark theme."""
    source_icon = widget.style().standardIcon(standard_pixmap)
    source_pixmap = source_icon.pixmap(QSize(18, 18))
    colored_pixmap = QPixmap(source_pixmap.size())
    colored_pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(colored_pixmap)
    painter.drawPixmap(0, 0, source_pixmap)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(colored_pixmap.rect(), QColor(TERMINAL_ICON))
    painter.end()
    return QIcon(colored_pixmap)


class PlaylistSupervisorThread(QThread):
    """
    QThread replacement for the original threading.Thread.
    Any cross-thread UI work is done via signals (Qt.QueuedConnection is used
    automatically when the receiving object lives on the main/GUI thread),
    mirroring the old `self.app.after(0, ...)` marshalling pattern.
    """
    select_session_requested = pyqtSignal(object)          # sess
    ui_update_requested = pyqtSignal()                      # -> ui_callback()
    gate_requested = pyqtSignal(object, str, str, object, dict)  # item, reason, tail, lock, holder
    suite_finished = pyqtSignal(str)                         # summary text

    def __init__(self, app: object, playlist: list[PlaylistItem], beep_on_alert: bool,
                 ui_callback: object, on_finish_callback: object, parent: QWidget | None = None):
        super().__init__(parent)
        self.app = app
        self.playlist = playlist
        self.beep = beep_on_alert
        self.ui_callback = ui_callback
        self.on_finish_callback = on_finish_callback
        self.stopped = False

        # Wire signals to their handlers (executed on the thread that owns
        # the connected slot -- i.e. the GUI thread -- via QueuedConnection).
        self.select_session_requested.connect(self._select_session_slot)
        self.ui_update_requested.connect(self.ui_callback)
        self.gate_requested.connect(self._spawn_gate_slot)
        self.suite_finished.connect(self.on_finish_callback)

    def _select_session_slot(self, sess: object):
        if sess in self.app.sessions:
            self.app.select_session(sess)

    def run(self):
        for idx, item in enumerate(self.playlist):
            if self.stopped:
                break
            sess = item.target_session
            if sess not in self.app.sessions:
                sess = self.app.sessions[0] if self.app.sessions else None
                if not sess:
                    break

            self.select_session_requested.emit(sess)
            sess.incoming.put(f"\r\n[═══ PLAYLIST STEP ({idx + 1}/{len(self.playlist)}) ═══]\r\n")
            item.status = "Running"
            self._push_ui()
            sess.incoming.put(f"[Playlist ➔] Executing: {Path(item.path).name}\n")

            sess.start_script(Path(item.path).name)
            start_marker, code_failed, error_reason = len(sess.recv_buffer), False, ""
            try:
                if item.path.lower().endswith(".ttl"):
                    TeraTermMacroRunner(self.app, sess).run_file(item.path)
                else:
                    execute_python_script_sync(sess, item.path)
            except ScriptStopped:
                item.status, self.stopped = "Stopped", True
                sess.incoming.put("[Playlist 🛑] Script stopped by user.\n")
                sess.finish_script()
                self._push_ui()
                break
            except Exception as e:
                code_failed, error_reason = True, f"Script Exception: {e}"
            finally:
                sess.finish_script()

            current_timeout, passed = item.timeout, False
            while not passed and not self.stopped:
                if code_failed:
                    choice = self._spawn_gate(sess, item, error_reason, start_marker)
                else:
                    deadline, detected = time.time() + current_timeout, False
                    while time.time() < deadline:
                        if self.stopped:
                            break
                        time.sleep(0.1)
                        item.status = f"Sniffing ({max(0.0, deadline - time.time()):.1f}s)"
                        self._push_ui()
                        if re.search(r'[\$#>\]]\s*$', sess.recv_buffer[start_marker:]):
                            detected = True
                            break
                    if detected:
                        passed, item.status = True, "Passed"
                        self._push_ui()
                        sess.incoming.put(f"[Playlist ✔] Hand-off verified for {Path(item.path).name}\n")
                        break
                    else:
                        choice = self._spawn_gate(sess, item, f"Deadline expired ({current_timeout}s) without prompt.", start_marker)

                if choice == "EXTEND":
                    code_failed, current_timeout = False, 15.0
                    sess.incoming.put("[Playlist ➔] Extension granted...\n")
                elif choice == "FORCE":
                    passed, item.status = True, "Passed (Forced)"
                    self._push_ui()
                    sess.incoming.put("[Playlist ⚠️] Forced progression.\n")
                else:
                    item.status, self.stopped = "Aborted", True
                    self._push_ui()
                    sess.incoming.put("[Playlist 🛑] Aborted.\n")
                    break

        if not self.stopped:
            names = "\n".join(f"✔   {Path(item.path).name}   [on {item.target_session.name}]" for item in self.playlist)
            self.suite_finished.emit(f"All {len(self.playlist)} scripts ran successfully:\n\n{names}")

    def _spawn_gate(self, sess: object, item: PlaylistItem, reason: str, buf_start: int) -> str:
        item.status = "GATE TRIGGERED"
        self._push_ui()
        if self.beep:
            trigger_os_beep()
        tail = sess.recv_buffer[buf_start:][-250:]
        lock, holder = threading.Event(), {"choice": "ABORT"}
        self.gate_requested.emit(item, reason, tail, lock, holder)
        lock.wait()
        return holder["choice"]

    def _spawn_gate_slot(self, item: object, reason: str, tail: str, lock: threading.Event, holder: dict):
        # Runs on the GUI thread. OperatorGateDialog is expected to set
        # holder["choice"] and call lock.set() when the user responds.
        OperatorGateDialog(self.app, item.path, reason, tail, lock, holder)

    def _push_ui(self):
        self.ui_update_requested.emit()


class MultiScriptDialog(QDialog):
    def __init__(self, app: object):
        super().__init__(app)
        self.app, self.playlist, self.supervisor = app, [], None
        self.live_status_labels: dict[PlaylistItem, QLabel] = {}
        self.row_widgets: list[QWidget] = []

        self.setWindowTitle("DevOps Suite DAW Manager")
        self.resize(880, 520)
        self.setWindowModality(Qt.WindowModality.NonModal)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)

        # --- Header ---
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 8)
        title_lbl = QLabel("Multi-Script DAW Suite")
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title_lbl.setFont(title_font)
        header_layout.addWidget(title_lbl)
        header_layout.addStretch(1)
        self.beep_checkbox = QCheckBox("Audio chime on Human Gate")
        self.beep_checkbox.setChecked(True)
        header_layout.addWidget(self.beep_checkbox)
        root.addWidget(header)

        # --- Scrollable playlist area ---
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(4, 4, 4, 4)
        self.scroll_layout.addStretch(1)
        self.scroll_area.setWidget(self.scroll_content)
        root.addWidget(self.scroll_area, 1)

        # --- Toolbar ---
        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 16, 0, 0)
        add_btn = QPushButton("+ Add Script Files")
        add_btn.setStyleSheet("QPushButton { background-color: #2f6f95; color: white; }")
        add_btn.clicked.connect(self._add_files)
        toolbar_layout.addWidget(add_btn)
        toolbar_layout.addStretch(1)
        self.pause_btn = QPushButton("⏸ Pause")
        self.pause_btn.setFixedWidth(90)
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._toggle_pause)
        toolbar_layout.addWidget(self.pause_btn)
        self.stop_btn = QPushButton("⏹ Stop Suite")
        self.stop_btn.setFixedWidth(100)
        self.stop_btn.setStyleSheet("QPushButton { background-color: #8a2d3b; color: white; }")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_suite)
        toolbar_layout.addWidget(self.stop_btn)
        self.launch_btn = QPushButton("Launch Full Suite")
        self.launch_btn.setFixedWidth(150)
        self.launch_btn.setStyleSheet("QPushButton { background-color: #256d55; color: white; }")
        self.launch_btn.clicked.connect(self._launch_all)
        toolbar_layout.addWidget(self.launch_btn)
        root.addWidget(toolbar)

        self._rebuild_grid()
        self.show()

    def _add_files(self):
        default_sess = self.app.active_session if (self.app.active_session in self.app.sessions) else self.app.sessions[0]
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Scripts", "", "Scripts (*.ttl *.py);;All Files (*.*)"
        )
        for p in paths:
            self.playlist.append(PlaylistItem(path=p, timeout=15.0, target_session=default_sess))
        self._rebuild_grid()

    def _update_live_ui(self):
        colors = {"Pending": TERMINAL_MUTED, "Running": "#ffd54f", "Passed": TERMINAL_ACCENT, "GATE": "#ff5722"}
        for item, lbl in self.live_status_labels.items():
            try:
                color = colors.get(item.status.split("(")[0].strip(), "palette(text)")
                lbl.setText(item.status)
                lbl.setStyleSheet(f"color: {color};")
            except RuntimeError:
                # underlying C++ widget already deleted
                pass

        running_sess = self._current_running_session()
        active = bool(running_sess and running_sess.script_running)
        self.pause_btn.setEnabled(active)
        self.stop_btn.setEnabled(active)
        if active:
            paused = not running_sess.script_pause_event.is_set()
            self.pause_btn.setText("▶ Resume" if paused else "⏸ Pause")
        else:
            self.pause_btn.setText("⏸ Pause")

    def _current_running_session(self):
        for item in self.playlist:
            if item.target_session.script_running:
                return item.target_session
        return None

    def _toggle_pause(self):
        sess = self._current_running_session()
        if not sess or not sess.script_running:
            return
        paused = sess.toggle_pause_script()
        self.pause_btn.setText("▶ Resume" if paused else "⏸ Pause")

    def _stop_suite(self):
        if self.supervisor:
            self.supervisor.stopped = True
        sess = self._current_running_session()
        if sess and sess.script_running:
            sess.request_stop_script()

    def _clear_layout(self, layout):
        while layout.count():
            child = layout.takeAt(0)
            w = child.widget()
            if w is not None:
                w.deleteLater()

    def _rebuild_grid(self):
        # Remove the trailing stretch, clear everything, we'll re-add stretch at the end.
        self._clear_layout(self.scroll_layout)
        self.row_widgets.clear()
        self.live_status_labels.clear()

        if not self.playlist:
            empty_lbl = QLabel("[ Suite is empty. Click '+ Add Script Files' below. ]")
            empty_lbl.setStyleSheet(f"color: {TERMINAL_MUTED};")
            empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.scroll_layout.addWidget(empty_lbl)
            self.scroll_layout.addStretch(1)
            self.row_widgets.append(empty_lbl)
            self.launch_btn.setEnabled(False)
            return

        self.launch_btn.setEnabled(not self.supervisor or not self.supervisor.isRunning())
        session_names = [s.name for s in self.app.sessions]

        for idx, item in enumerate(self.playlist):
            row = QFrame()
            row.setStyleSheet("background-color: palette(base); border-radius: 4px;")
            row_layout = QGridLayout(row)
            row_layout.setContentsMargins(6, 10, 6, 10)
            self.row_widgets.append(row)

            idx_lbl = QLabel(f"{idx + 1}.")
            f = QFont(); f.setBold(True)
            idx_lbl.setFont(f)
            idx_lbl.setFixedWidth(24)
            row_layout.addWidget(idx_lbl, 0, 0)

            name_lbl = QLabel(Path(item.path).name)
            name_lbl.setFont(QFont("Consolas"))
            row_layout.addWidget(name_lbl, 0, 1)
            row_layout.setColumnStretch(1, 1)

            session_combo = QComboBox()
            session_combo.addItems(session_names)
            current_name = item.target_session.name if item.target_session in self.app.sessions else session_names[0]
            session_combo.setCurrentText(current_name)
            session_combo.setFixedWidth(115)
            session_combo.currentTextChanged.connect(lambda val, i=item: self._route_session(i, val))
            row_layout.addWidget(session_combo, 0, 2)

            wait_lbl = QLabel("Wait:")
            row_layout.addWidget(wait_lbl, 0, 3)

            t_entry = QLineEdit(str(int(item.timeout)))
            t_entry.setFixedWidth(45)
            t_entry.textChanged.connect(lambda _text, i=item, w=t_entry: self._update_t(i, w))
            row_layout.addWidget(t_entry, 0, 4)

            s_lbl = QLabel("s")
            row_layout.addWidget(s_lbl, 0, 5)

            status_lbl = QLabel(item.status)
            status_lbl.setFixedWidth(110)
            row_layout.addWidget(status_lbl, 0, 6)
            self.live_status_labels[item] = status_lbl

            ctrls = QWidget()
            ctrls_layout = QHBoxLayout(ctrls)
            ctrls_layout.setContentsMargins(4, 0, 0, 0)
            ctrls_layout.setSpacing(4)

           #up_btn = QPushButton("▲"); up_btn.setFixedWidth(24)
            up_btn = QPushButton()
            #up_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp))
            up_btn.setIcon(light_standard_icon(self, QStyle.StandardPixmap.SP_ArrowUp))
            up_btn.setIconSize(QSize(16, 16))
            up_btn.setFixedSize(32, 30)
            up_btn.setToolTip("Move script up")
            up_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            up_btn.clicked.connect(lambda _checked, i=idx: self._swap(i, i - 1))
            ctrls_layout.addWidget(up_btn)

            #down_btn = QPushButton("▼"); down_btn.setFixedWidth(24)
            down_btn = QPushButton()
            #down_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
            down_btn.setIcon(light_standard_icon(self, QStyle.StandardPixmap.SP_ArrowDown))
            down_btn.setIconSize(QSize(16, 16))
            down_btn.setFixedSize(32, 30)
            down_btn.setToolTip("Move script down")
            down_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            down_btn.clicked.connect(lambda _checked, i=idx: self._swap(i, i + 1))
            ctrls_layout.addWidget(down_btn)

            # single_btn = QPushButton("► Single")
            # single_btn.setFixedWidth(60)
            single_btn = QPushButton("Single")
           # single_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
            single_btn.setIcon(light_standard_icon(self, QStyle.StandardPixmap.SP_MediaPlay))
            single_btn.setIconSize(QSize(16, 16))
            single_btn.setFixedSize(82, 30)
            single_btn.setToolTip("Run this script only")
            single_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            single_btn.setStyleSheet("QPushButton { background-color: #2f6f95; color: white; }")
            single_btn.clicked.connect(lambda _checked, i=item: self._run_single(i))
            ctrls_layout.addWidget(single_btn)

            #rm_btn = QPushButton("×"); rm_btn.setFixedWidth(24)
            rm_btn = QPushButton()
            #rm_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCloseButton))
            rm_btn.setIcon(light_standard_icon(self, QStyle.StandardPixmap.SP_DialogCloseButton))
            rm_btn.setIconSize(QSize(16, 16))
            rm_btn.setFixedSize(32, 30)
            rm_btn.setToolTip("Remove script")
            rm_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            rm_btn.setStyleSheet("QPushButton { background-color: #8a2d3b; color: white; }")
            rm_btn.clicked.connect(lambda _checked, i=item: self._rm(i))
            ctrls_layout.addWidget(rm_btn)

            row_layout.addWidget(ctrls, 0, 7)

            self.scroll_layout.addWidget(row)

        self.scroll_layout.addStretch(1)
        self._update_live_ui()

    def _route_session(self, item: PlaylistItem, name: str):
        for s in self.app.sessions:
            if s.name == name:
                item.target_session = s
                break

    def _update_t(self, item: PlaylistItem, w: QLineEdit):
        try:
            item.timeout = float(w.text() or 15.0)
        except ValueError:
            pass

    def _swap(self, i1: int, i2: int):
        if 0 <= i1 < len(self.playlist) and 0 <= i2 < len(self.playlist):
            self.playlist[i1], self.playlist[i2] = self.playlist[i2], self.playlist[i1]
            self._rebuild_grid()

    def _rm(self, item: PlaylistItem):
        if item in self.playlist:
            self.playlist.remove(item)
        self._rebuild_grid()

    def _on_suite_success(self, summary: str):
        try:
            self.accept()
        except Exception:
            pass
        QMessageBox.information(self.app, "Suite Completed", summary)

    def _run_single(self, item: PlaylistItem):
        if self.supervisor and self.supervisor.isRunning():
            return
        self.supervisor = PlaylistSupervisorThread(
            self.app, [item], self.beep_checkbox.isChecked(),
            self._update_live_ui,
            lambda msg: QMessageBox.information(self.app, "Single Passed", msg),
            parent=self,
        )
        self.supervisor.start()

    def _launch_all(self):
        self.supervisor = PlaylistSupervisorThread(
            self.app, self.playlist, self.beep_checkbox.isChecked(),
            self._update_live_ui, self._on_suite_success,
            parent=self,
        )
        self.supervisor.start()
        self._update_live_ui()
