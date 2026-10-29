from __future__ import annotations
import os
import re
import threading
import webbrowser
from pathlib import Path

import serial.tools.list_ports
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QWidget, QLabel, QPushButton, QLineEdit, QComboBox, QCheckBox,
    QRadioButton, QButtonGroup, QFrame, QScrollArea, QVBoxLayout, QHBoxLayout,
    QGridLayout, QFileDialog, QMessageBox, QPlainTextEdit
)

from core.presets import TMPL_SSH_LOGIN, TMPL_PY_AUTO, TMPL_SERIAL_AT, TMPL_TTL_BATCH

BAUD_RATES = ["300", "1200", "2400", "4800", "9600", "14400", "19200", "38400", "57600", "115200", "230400", "460800", "921600", "Custom"]
TERMINAL_ACCENT = "#46f0a6"
TERMINAL_MUTED = "#7b8b99"


def _bold(size=14):
    f = QFont()
    f.setPointSize(size)
    f.setBold(True)
    return f


class SamplesDialog(QDialog):
    def __init__(self, app: object):
        super().__init__(app)
        self.setWindowTitle("Help & Preset Repository")
        self.resize(660, 560)
        self.setModal(True)

        root = QVBoxLayout(self)
        title = QLabel("Preset Script Templates Repository")
        title.setFont(_bold(16))
        root.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)

        items = [
            ("SSH Auto-Login Macro (.ttl)", "Standard Tera Term TTL macro to auto-enter credentials.", TMPL_SSH_LOGIN, ".ttl"),
            ("Python Terminal Automation (.py)", "Send commands, pause, and log output sequentially.", TMPL_PY_AUTO, ".py"),
            ("Serial AT Command Tester (.py)", "Automated routine to query hardware modems.", TMPL_SERIAL_AT, ".py"),
            ("Batch Command Delay (.ttl)", "Pass multiple lines with timed execution pauses.", TMPL_TTL_BATCH, ".ttl"),
        ]
        for title_text, desc, code, ext in items:
            card = QFrame()
            card.setStyleSheet("background-color: palette(base); border-radius: 4px;")
            card_layout = QGridLayout(card)
            lbl_title = QLabel(title_text)
            lbl_title.setFont(_bold(13))
            lbl_title.setStyleSheet(f"color: {TERMINAL_ACCENT};")
            card_layout.addWidget(lbl_title, 0, 0)
            lbl_desc = QLabel(desc)
            lbl_desc.setStyleSheet(f"color: {TERMINAL_MUTED};")
            card_layout.addWidget(lbl_desc, 1, 0)
            dl_btn = QPushButton("Download")
            dl_btn.setStyleSheet("QPushButton { background-color: #2f6f95; color: white; }")
            dl_btn.clicked.connect(lambda _c, c=code, e=ext, t=title_text: self._save(t, c, e))
            card_layout.addWidget(dl_btn, 0, 1, 2, 1)
            content_layout.addWidget(card)

        more_frame = QFrame()
        more_frame.setStyleSheet("background-color: palette(alternate-base); border: 1px solid palette(mid); border-radius: 4px;")
        more_layout = QGridLayout(more_frame)
        lbl_more_title = QLabel("Need Advanced Scrapers?")
        lbl_more_title.setFont(_bold(12))
        more_layout.addWidget(lbl_more_title, 0, 0)
        lbl_more_desc = QLabel("Explore community user scripts and enterprise scrapers online.")
        lbl_more_desc.setStyleSheet(f"color: {TERMINAL_MUTED};")
        more_layout.addWidget(lbl_more_desc, 1, 0)
        more_btn = QPushButton("\U0001F310 More Scripts")
        more_btn.setStyleSheet("QPushButton { background-color: #7656b7; color: white; }")
        more_btn.clicked.connect(lambda: webbrowser.open("https://github.com/abyshergill/modern_multi_terminal/tree/main/Scripts"))
        more_layout.addWidget(more_btn, 0, 1, 2, 1)
        content_layout.addWidget(more_frame)

        creator_card = QFrame()
        creator_card.setStyleSheet("background-color: palette(base); border-radius: 4px;")
        creator_layout = QVBoxLayout(creator_card)
        lbl_creator = QLabel("\u2699\ufe0f Creator Information")
        lbl_creator.setFont(_bold(11))
        lbl_creator.setStyleSheet(f"color: {TERMINAL_ACCENT};")
        creator_layout.addWidget(lbl_creator)
        dev_btn = QPushButton("\U0001F464 Engineered by Aby Shergill (Click to Contact) \u2197")
        dev_btn.setFlat(True)
        dev_btn.setStyleSheet("color: #38b6ff; text-align: left; border: none;")
        dev_btn.clicked.connect(lambda: webbrowser.open("https://github.com/abyshergill"))
        creator_layout.addWidget(dev_btn)
        lbl_build = QLabel("Build v2.4 by Aby Shergill  \u00b7  BSD License")
        lbl_build.setStyleSheet(f"color: {TERMINAL_MUTED};")
        creator_layout.addWidget(lbl_build)
        content_layout.addWidget(creator_card)

        content_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)

    def _save(self, name: str, content: str, ext: str):
        safe = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
        path, _ = QFileDialog.getSaveFileName(self, f"Save {name}", f"{safe}{ext}", f"{ext.upper()} Script (*{ext});;All Files (*.*)")
        if path:
            Path(path).write_text(content.strip() + "\n", encoding="utf-8")
            QMessageBox.information(self, "Saved", f"Template saved successfully to:\n{path}")


class RenameDialog(QDialog):
    def __init__(self, app: object, current_name: str):
        super().__init__(app)
        self.app = app
        self.result: str | None = None
        self.setWindowTitle("Rename Terminal")
        self.resize(380, 150)
        self.setModal(True)

        root = QVBoxLayout(self)
        lbl = QLabel("Enter New Terminal Name:")
        lbl.setFont(_bold(13))
        root.addWidget(lbl)

        self.entry = QLineEdit(current_name)
        root.addWidget(self.entry)
        self.entry.setFocus()
        self.entry.selectAll()

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._ok)
        save_btn.setDefault(True)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        root.addLayout(btn_row)

    def _ok(self):
        self.result = self.entry.text().strip()
        self.accept()


class SSHDialog(QDialog):
    def __init__(self, app: object):
        super().__init__(app)
        self.app = app
        self.result: dict | None = None
        self.setWindowTitle("Connect SSH")
        self.resize(460, 420)
        self.setModal(True)

        grid = QGridLayout(self)
        title = QLabel("SSH Connection")
        title.setFont(_bold(18))
        grid.addWidget(title, 0, 0, 1, 2)

        grid.addWidget(QLabel("Saved profile"), 1, 0)
        self.profile_combo = QComboBox()
        self.profile_combo.addItems([""] + sorted(self.app.profile_store.profiles.keys()))
        self.profile_combo.currentTextChanged.connect(self._load_profile)
        grid.addWidget(self.profile_combo, 1, 1)

        self.profile_name = QLineEdit()
        self.host = QLineEdit()
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.port = QLineEdit("22")

        rows = [("Profile name", self.profile_name), ("Host/IP", self.host), ("Username", self.username), ("Password", self.password), ("Port", self.port)]
        for idx, (label, widget) in enumerate(rows, start=2):
            grid.addWidget(QLabel(label), idx, 0)
            grid.addWidget(widget, idx, 1)

        self.save_profile_cb = QCheckBox("Save/update profile")
        self.save_profile_cb.setChecked(True)
        grid.addWidget(self.save_profile_cb, 7, 1)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        connect_btn = QPushButton("Connect")
        connect_btn.clicked.connect(self._ok)
        connect_btn.setDefault(True)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(connect_btn)
        grid.addLayout(btn_row, 8, 0, 1, 2)

    def _load_profile(self, name: str):
        if not name:
            return
        p = self.app.profile_store.profiles.get(name, {})
        self.profile_name.setText(name)
        self.host.setText(p.get("host", ""))
        self.username.setText(p.get("username", ""))
        self.port.setText(str(p.get("port", "22")))

    def _ok(self):
        if not self.host.text().strip():
            QMessageBox.critical(self, "Missing host", "Enter the SSH host/IP.")
            return
        try:
            port = int(self.port.text() or "22")
        except ValueError:
            QMessageBox.critical(self, "Invalid port", "Port must be numeric.")
            return
        self.result = {
            "profile_name": self.profile_name.text().strip(),
            "host": self.host.text().strip(),
            "username": self.username.text().strip(),
            "password": self.password.text(),
            "port": port,
            "save": self.save_profile_cb.isChecked(),
        }
        self.accept()


class SerialDialog(QDialog):
    def __init__(self, app: object):
        super().__init__(app)
        self.app = app
        self.result: dict | None = None
        self.setWindowTitle("Connect Serial")
        self.resize(500, 360)
        self.setModal(True)

        grid = QGridLayout(self)
        title = QLabel("Serial Connection")
        title.setFont(_bold(18))
        grid.addWidget(title, 0, 0, 1, 2)

        raw_ports = serial.tools.list_ports.comports()
        self.port_map: dict[str, str] = {}
        combo_values = []
        if raw_ports:
            for p in raw_ports:
                self.port_map[p.description] = p.device
                combo_values.append(p.description)
        else:
            fallback = "COM1" if os.name == "nt" else "/dev/ttyUSB0"
            self.port_map[fallback] = fallback
            combo_values.append(fallback)
        preset_rates = [rate for rate in BAUD_RATES if rate != "Custom"]

        grid.addWidget(QLabel("Port"), 1, 0)
        self.port_combo = QComboBox()
        self.port_combo.addItems(combo_values)
        grid.addWidget(self.port_combo, 1, 1)

        grid.addWidget(QLabel("Baud rate mode"), 2, 0)
        mode_row = QHBoxLayout()
        self.preset_radio = QRadioButton("Preset")
        self.custom_radio = QRadioButton("Custom")
        self.preset_radio.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.preset_radio)
        group.addButton(self.custom_radio)
        self.preset_radio.toggled.connect(self._update_baud_mode)
        mode_row.addWidget(self.preset_radio)
        mode_row.addWidget(self.custom_radio)
        mode_row.addStretch(1)
        grid.addLayout(mode_row, 2, 1)

        grid.addWidget(QLabel("Preset baud"), 3, 0)
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(preset_rates)
        self.baud_combo.setCurrentText("115200")
        grid.addWidget(self.baud_combo, 3, 1)

        grid.addWidget(QLabel("Custom baud"), 4, 0)
        self.custom_baud_entry = QLineEdit()
        self.custom_baud_entry.setPlaceholderText("Example: 250000")
        grid.addWidget(self.custom_baud_entry, 4, 1)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        connect_btn = QPushButton("Connect")
        connect_btn.clicked.connect(self._ok)
        connect_btn.setDefault(True)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(connect_btn)
        grid.addLayout(btn_row, 5, 0, 1, 2)

        self._update_baud_mode()

    def _update_baud_mode(self):
        preset = self.preset_radio.isChecked()
        self.baud_combo.setEnabled(preset)
        self.custom_baud_entry.setEnabled(not preset)
        if not preset:
            self.custom_baud_entry.setFocus()

    def _ok(self):
        baud_text = self.custom_baud_entry.text().strip() if self.custom_radio.isChecked() else self.baud_combo.currentText().strip()
        try:
            baud = int(baud_text)
            if baud <= 0:
                raise ValueError
        except ValueError:
            QMessageBox.critical(self, "Invalid baud rate", "Baud rate must be numeric.")
            return
        chosen_display = self.port_combo.currentText().strip()
        self.result = {"port": self.port_map.get(chosen_display, chosen_display), "baud": baud}
        self.accept()


class TelnetDialog(QDialog):
    def __init__(self, app: object):
        super().__init__(app)
        self.app = app
        self.result: dict | None = None
        self.setWindowTitle("Connect Telnet")
        self.resize(420, 280)
        self.setModal(True)

        grid = QGridLayout(self)
        title = QLabel("Telnet Connection")
        title.setFont(_bold(16))
        grid.addWidget(title, 0, 0, 1, 2)

        grid.addWidget(QLabel("Saved profile"), 1, 0)
        self.profile_combo = QComboBox()
        telnet_profiles = getattr(getattr(self.app, "telnet_profile_store", None), "profiles", {})
        self.profile_combo.addItems([""] + sorted(telnet_profiles.keys()))
        self.profile_combo.currentTextChanged.connect(self._load_profile)
        grid.addWidget(self.profile_combo, 1, 1)

        grid.addWidget(QLabel("Profile name"), 2, 0)
        self.profile_name = QLineEdit()
        grid.addWidget(self.profile_name, 2, 1)

        grid.addWidget(QLabel("Host / IP"), 3, 0)
        self.host_entry = QLineEdit()
        self.host_entry.setPlaceholderText("192.168.1.1")
        grid.addWidget(self.host_entry, 3, 1)

        grid.addWidget(QLabel("Port"), 4, 0)
        self.port_entry = QLineEdit("23")
        self.port_entry.setFixedWidth(80)
        grid.addWidget(self.port_entry, 4, 1)

        self.save_profile_cb = QCheckBox("Save/update profile")
        self.save_profile_cb.setChecked(True)
        grid.addWidget(self.save_profile_cb, 5, 1)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        connect_btn = QPushButton("Connect")
        connect_btn.clicked.connect(self._ok)
        connect_btn.setDefault(True)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(connect_btn)
        grid.addLayout(btn_row, 6, 0, 1, 2)

    def _load_profile(self, name: str):
        if not name:
            return
        telnet_store = getattr(self.app, "telnet_profile_store", None)
        p = getattr(telnet_store, "profiles", {}).get(name, {})
        self.profile_name.setText(name)
        self.host_entry.setText(p.get("host", ""))
        self.port_entry.setText(str(p.get("port", "23")))

    def _ok(self):
        if not self.host_entry.text().strip():
            QMessageBox.critical(self, "Missing Host", "Enter IP.")
            return
        try:
            port = int(self.port_entry.text().strip() or 23)
        except ValueError:
            QMessageBox.critical(self, "Invalid Port", "Numeric only.")
            return
        self.result = {
            "profile_name": self.profile_name.text().strip(),
            "host": self.host_entry.text().strip(),
            "port": port,
            "save": self.save_profile_cb.isChecked(),
        }
        self.accept()


class OperatorGateDialog(QDialog):
    def __init__(self, app: object, script_name: str, reason: str, tail_log: str, event_lock: threading.Event, decision_holder: dict):
        super().__init__(app)
        self.app = app
        self.decision_holder, self.event_lock = decision_holder, event_lock
        self.setWindowTitle("\u26a0\ufe0f Playlist Gate Triggered")
        self.resize(560, 340)
        self.setModal(True)

        root = QVBoxLayout(self)
        title = QLabel("\u26a0\ufe0f Execution Handoff Failed")
        title.setFont(_bold(16))
        title.setStyleSheet("color: #ff5722;")
        root.addWidget(title)

        info = QLabel(f"Script: '{Path(script_name).name}'\nReason: {reason}")
        info.setStyleSheet(f"color: {TERMINAL_MUTED};")
        root.addWidget(info)

        ctx_lbl = QLabel("Terminal Output Context (Last 250 chars):")
        ctx_lbl.setFont(_bold(11))
        root.addWidget(ctx_lbl)

        box = QPlainTextEdit()
        box.setFixedHeight(100)
        box.setReadOnly(True)
        box.setStyleSheet("background-color: #0b131c; color: #d8f3dc;")  # intentionally terminal-styled, matches the live terminal viewport
        box.setFont(QFont("Consolas", 11))
        box.setPlainText(tail_log if tail_log.strip() else "[No recent terminal output]")
        root.addWidget(box)

        btn_row = QHBoxLayout()
        extend_btn = QPushButton("+15s More Time")
        extend_btn.setStyleSheet("QPushButton { background-color: #2f6f95; color: white; }")
        extend_btn.clicked.connect(lambda: self._choose("EXTEND"))
        force_btn = QPushButton("Force Next Script")
        force_btn.setStyleSheet("QPushButton { background-color: #d16232; color: white; }")
        force_btn.clicked.connect(lambda: self._choose("FORCE"))
        abort_btn = QPushButton("Abort Playlist")
        abort_btn.setStyleSheet("QPushButton { background-color: #8a2d3b; color: white; }")
        abort_btn.clicked.connect(lambda: self._choose("ABORT"))
        btn_row.addWidget(extend_btn)
        btn_row.addWidget(force_btn)
        btn_row.addWidget(abort_btn)
        root.addLayout(btn_row)

        self.show()

    def _release_with_abort(self):
        if not self.event_lock.is_set():
            self.decision_holder["choice"] = "ABORT"
            self.event_lock.set()

    def closeEvent(self, event):
        # Mirrors the original WM_DELETE_WINDOW -> ABORT behavior.
        self._release_with_abort()
        event.accept()

    def reject(self):
        # Escape key: QDialog.reject() hides the dialog WITHOUT a closeEvent, which would
        # leave the playlist supervisor thread waiting forever on event_lock.
        self._release_with_abort()
        super().reject()

    def _choose(self, action: str):
        self.decision_holder["choice"] = action
        self.event_lock.set()
        self.close()
