from __future__ import annotations
from unittest.mock import MagicMock

from PyQt6.QtWidgets import QWidget
from gui.dialogs import SSHDialog, SerialDialog, TelnetDialog, RenameDialog


class FakeProfileStore:
    def __init__(self):
        self.profiles = {}
    def upsert(self, name, data):
        self.profiles[name] = data


class FakeApp(QWidget):
    def __init__(self):
        super().__init__()
        self.profile_store = FakeProfileStore()


def test_ssh_dialog_rejects_empty_host(qapp):
    d = SSHDialog(FakeApp())
    d.host.setText("")
    d._ok()
    assert d.result is None


def test_ssh_dialog_rejects_non_numeric_port(qapp):
    d = SSHDialog(FakeApp())
    d.host.setText("10.0.0.1")
    d.port.setText("not-a-number")
    d._ok()
    assert d.result is None


def test_ssh_dialog_accepts_valid_input(qapp):
    d = SSHDialog(FakeApp())
    d.host.setText("10.0.0.1")
    d.username.setText("admin")
    d.password.setText("secret")
    d.port.setText("2222")
    d._ok()
    assert d.result is not None
    assert d.result["host"] == "10.0.0.1"
    assert d.result["port"] == 2222


def test_ssh_dialog_loads_saved_profile(qapp):
    app = FakeApp()
    app.profile_store.profiles["prod"] = {"host": "prod.example.com", "username": "root", "port": 22}
    d = SSHDialog(app)
    d._load_profile("prod")
    assert d.host.text() == "prod.example.com"
    assert d.username.text() == "root"


def test_serial_dialog_rejects_non_numeric_custom_baud(qapp):
    app = FakeApp()
    d = SerialDialog(app)
    d.custom_radio.setChecked(True)
    d.custom_baud_entry.setText("fast")
    d._ok()
    assert d.result is None


def test_serial_dialog_accepts_preset_baud(qapp):
    app = FakeApp()
    d = SerialDialog(app)
    d.preset_radio.setChecked(True)
    d.baud_combo.setCurrentText("115200")
    d._ok()
    assert d.result is not None
    assert d.result["baud"] == 115200


def test_serial_dialog_accepts_custom_baud(qapp):
    app = FakeApp()
    d = SerialDialog(app)
    d.custom_radio.setChecked(True)
    d.custom_baud_entry.setText("250000")
    d._ok()
    assert d.result is not None
    assert d.result["baud"] == 250000


def test_serial_dialog_rejects_zero_or_negative_baud(qapp):
    app = FakeApp()
    d = SerialDialog(app)
    d.custom_radio.setChecked(True)
    d.custom_baud_entry.setText("0")
    d._ok()
    assert d.result is None


def test_telnet_dialog_rejects_empty_host(qapp):
    app = FakeApp()
    d = TelnetDialog(app)
    d.host_entry.setText("")
    d._ok()
    assert d.result is None


def test_telnet_dialog_accepts_valid_input(qapp):
    app = FakeApp()
    d = TelnetDialog(app)
    d.host_entry.setText("10.0.0.5")
    d.port_entry.setText("2323")
    d._ok()
    assert d.result == {"host": "10.0.0.5", "port": 2323}


def test_telnet_dialog_rejects_non_numeric_port(qapp):
    app = FakeApp()
    d = TelnetDialog(app)
    d.host_entry.setText("10.0.0.5")
    d.port_entry.setText("abc")
    d._ok()
    assert d.result is None


def test_rename_dialog_trims_whitespace(qapp):
    app = FakeApp()
    d = RenameDialog(app, "Old Name")
    d.entry.setText("  New Name  ")
    d._ok()
    assert d.result == "New Name"
