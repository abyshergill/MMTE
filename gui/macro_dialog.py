"""Utility > Record Actions | Macro: choose a terminal and start / stop recording."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from gui import icons


class MacroRecordDialog(QDialog):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.setWindowTitle("Record Actions | Macro")
        self.setModal(True)
        self.resize(480, 0)

        root = QVBoxLayout(self)
        title = QLabel("Record Macro")
        f = QFont()
        f.setPointSize(15)
        f.setBold(True)
        title.setFont(f)
        root.addWidget(title)

        info = QLabel(
            "Everything you type in the chosen terminal is recorded: each command, and how long you "
            "waited before the next one. When you stop, the recording becomes a Python script you can "
            "save and replay with Scripting > Run Script or the Multi-Script Suite.\n\n"
            "Passwords typed at a password prompt are never stored; the script asks for them when it runs."
        )
        info.setWordWrap(True)
        info.setObjectName("Subtle")
        root.addWidget(info)
        root.addSpacing(6)

        row = QHBoxLayout()
        row.addWidget(QLabel("Terminal"))
        self.combo = QComboBox()
        row.addWidget(self.combo, 1)
        root.addLayout(row)

        self.state_label = QLabel("")
        self.state_label.setObjectName("Hint")
        root.addWidget(self.state_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        buttons.addWidget(close_btn)
        self.action_btn = QPushButton("Record Macro")
        self.action_btn.setIconSize(icons.ICON_SIZE)
        self.action_btn.setDefault(True)
        self.action_btn.clicked.connect(self._toggle)
        buttons.addWidget(self.action_btn)
        root.addLayout(buttons)

        for s in app.sessions:
            self.combo.addItem(s.name + ("   (recording)" if s.recorder else ""), s)
        if app.active_session in app.sessions:
            self.combo.setCurrentIndex(app.sessions.index(app.active_session))
        self.combo.currentIndexChanged.connect(self._refresh)
        self._refresh()

    def selected_session(self):
        return self.combo.currentData(Qt.ItemDataRole.UserRole)

    def _refresh(self, *_args):
        s = self.selected_session()
        if s is None:
            self.action_btn.setEnabled(False)
            self.state_label.setText("Open a terminal first.")
            return
        self.action_btn.setEnabled(True)
        if s.recorder:
            self.action_btn.setText("Stop && Generate Script")
            self.action_btn.setObjectName("DangerBtn")
            self.action_btn.setIcon(icons.stop_icon(size=14))
            self.state_label.setText(f"Recording - {s.recorder.command_count} command(s) so far.")
        else:
            self.action_btn.setText("Record Macro")
            self.action_btn.setObjectName("AccentBtn")
            self.action_btn.setIcon(icons.record_icon(size=14))
            self.state_label.setText(
                f"Connection: {s.connected_label}" + ("" if s.connection else "  (you can connect after starting)")
            )
        # re-polish so the objectName-based style applies
        self.action_btn.style().unpolish(self.action_btn)
        self.action_btn.style().polish(self.action_btn)

    def _toggle(self):
        s = self.selected_session()
        if s is None:
            return
        self.accept()
        if s.recorder:
            self.app.stop_macro_recording(s)
        else:
            self.app.start_macro_recording(s)
