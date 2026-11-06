from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from gui import icons


class ProfileEditDialog(QDialog):
    """Add or edit one saved SSH / Telnet profile.  ``result`` is ``(name, data)`` on accept."""

    def __init__(self, parent, kind: str, store, name: str | None = None):
        super().__init__(parent)
        self.kind = kind  # "ssh" | "telnet"
        self.store = store
        self.original_name = name
        self.result: tuple[str, dict] | None = None
        existing = dict(store.profiles.get(name, {})) if name else {}
        self._extra = {k: v for k, v in existing.items() if k not in ("host", "username", "port")}

        label = "SSH" if kind == "ssh" else "Telnet"
        self.setWindowTitle(f"{'Edit' if name else 'Add'} {label} Profile")
        self.setModal(True)
        self.resize(420, 0)

        root = QVBoxLayout(self)
        title = QLabel(f"{'Edit' if name else 'New'} {label} profile")
        f = QFont()
        f.setPointSize(14)
        f.setBold(True)
        title.setFont(f)
        root.addWidget(title)

        form = QFormLayout()
        form.setVerticalSpacing(10)
        self.name_edit = QLineEdit(name or "")
        self.name_edit.setPlaceholderText("e.g. core-router-01")
        form.addRow("Profile name", self.name_edit)
        self.host_edit = QLineEdit(str(existing.get("host", "")))
        self.host_edit.setPlaceholderText("192.168.1.1 or host.example.com")
        form.addRow("Host / IP", self.host_edit)
        self.user_edit = None
        if kind == "ssh":
            self.user_edit = QLineEdit(str(existing.get("username", "")))
            form.addRow("Username", self.user_edit)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(int(existing.get("port", 22 if kind == "ssh" else 23) or 0) or (22 if kind == "ssh" else 23))
        form.addRow("Port", self.port_spin)
        root.addLayout(form)

        hint = QLabel("Passwords are never stored. You are asked for them when you connect.")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("AccentBtn")
        save.setDefault(True)
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        root.addLayout(buttons)
        (self.host_edit if name else self.name_edit).setFocus()

    def _save(self):
        host = self.host_edit.text().strip()
        if not host:
            QMessageBox.warning(self, "Missing host", "Enter a host name or IP address.")
            return
        username = self.user_edit.text().strip() if self.user_edit is not None else ""
        port = int(self.port_spin.value())
        name = self.name_edit.text().strip()
        if not name:
            name = f"{username}@{host}:{port}" if (self.kind == "ssh" and username) else f"{host}:{port}"
        if name != self.original_name and name in self.store.profiles:
            answer = QMessageBox.question(
                self, "Replace profile?", f"A profile named '{name}' already exists. Replace it?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        data = dict(self._extra)
        data.update({"host": host, "port": port})
        if self.kind == "ssh":
            data["username"] = username
        self.result = (name, data)
        self.accept()


class ProfilesDialog(QDialog):
    def __init__(self, parent, ssh_store, telnet_store):
        super().__init__(parent)
        self.app = parent
        self.setWindowTitle("Saved Profiles")
        self.resize(640, 580)
        self.setModal(True)
        self.ssh_store = ssh_store
        self.telnet_store = telnet_store
        self._build_ui()

    # --- UI -------------------------------------------------------------
    def _section(self, root: QVBoxLayout, title: str, kind: str) -> QListWidget:
        header = QHBoxLayout()
        lbl = QLabel(title)
        f = QFont()
        f.setPointSize(14)
        f.setBold(True)
        lbl.setFont(f)
        header.addWidget(lbl, 1)
        add_btn = QPushButton(f"Add {'SSH' if kind == 'ssh' else 'Telnet'} Profile")
        add_btn.setObjectName("AccentBtn")
        add_btn.setIcon(icons.plus_icon(color="#0a0d1f", size=14))
        add_btn.clicked.connect(lambda: self.add_profile(kind))
        header.addWidget(add_btn)
        root.addLayout(header)

        box = QListWidget()
        box.itemDoubleClicked.connect(
            lambda item, k=kind: self.edit_profile(k, item.data(Qt.ItemDataRole.UserRole))
        )
        root.addWidget(box, 1)
        return box

    def _build_ui(self):
        root = QVBoxLayout(self)
        self.ssh_box = self._section(root, "Saved SSH Profiles", "ssh")
        root.addSpacing(6)
        self.telnet_box = self._section(root, "Saved Telnet Profiles", "telnet")

        hint = QLabel("Double-click a profile to edit it.")
        hint.setObjectName("Hint")
        root.addWidget(hint)

        self.ref_ssh()
        self.ref_telnet()

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)

    def _store(self, kind: str):
        return self.ssh_store if kind == "ssh" else self.telnet_store

    def _refresh(self, kind: str):
        self.ref_ssh() if kind == "ssh" else self.ref_telnet()

    def _fill(self, box: QListWidget, kind: str):
        box.clear()
        store = self._store(kind)
        if not store.profiles:
            item = QListWidgetItem("No saved profiles yet. Click 'Add' to create one.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            box.addItem(item)
            return
        for n, pf in sorted(store.profiles.items()):
            if kind == "ssh":
                detail = f"{pf.get('username', '')}@{pf.get('host', '')}:{pf.get('port', 22)}"
            else:
                detail = f"{pf.get('host', '')}:{pf.get('port', 23)}"
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, n)
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 6, 8, 6)
            row_layout.setSpacing(6)
            text = QLabel(f"<b>{_esc(n)}</b> &nbsp;<span style='color:#9aa8b6'>{_esc(detail)}</span>")
            row_layout.addWidget(text, 1)

            connect_btn = QPushButton("Connect")
            connect_btn.setToolTip(f"Open a terminal connected with '{n}'")
            connect_btn.setFixedHeight(32)
            connect_btn.clicked.connect(lambda _c=False, name=n, k=kind: self.connect_profile(k, name))
            row_layout.addWidget(connect_btn)

            edit_btn = QPushButton("Edit")
            edit_btn.setIcon(icons.pencil_icon(size=14))
            edit_btn.setFixedHeight(32)
            edit_btn.clicked.connect(lambda _c=False, name=n, k=kind: self.edit_profile(k, name))
            row_layout.addWidget(edit_btn)

            del_btn = QPushButton("Delete")
            del_btn.setObjectName("DangerBtn")
            del_btn.setFixedHeight(32)
            del_btn.clicked.connect(lambda _c=False, name=n, k=kind: self.delete_profile(k, name))
            row_layout.addWidget(del_btn)

            item.setSizeHint(QSize(row.sizeHint().width(), 60))
            box.addItem(item)
            box.setItemWidget(item, row)

    def ref_ssh(self):
        self._fill(self.ssh_box, "ssh")

    def ref_telnet(self):
        self._fill(self.telnet_box, "telnet")

    # --- actions --------------------------------------------------------
    def add_profile(self, kind: str):
        self._open_editor(kind, None)

    def edit_profile(self, kind: str, name: str | None):
        if name:
            self._open_editor(kind, name)

    def _open_editor(self, kind: str, name: str | None):
        dialog = ProfileEditDialog(self, kind, self._store(kind), name)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result:
            self.apply_edit(kind, name, *dialog.result)

    def apply_edit(self, kind: str, old_name: str | None, new_name: str, data: dict) -> None:
        store = self._store(kind)
        if old_name and old_name != new_name:
            store.profiles.pop(old_name, None)
        store.upsert(new_name, data)
        self._refresh(kind)

    def delete_profile(self, kind: str, name: str):
        answer = QMessageBox.question(self, "Delete profile", f"Delete the saved profile '{name}'?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._store(kind).delete(name)
        self._refresh(kind)

    def connect_profile(self, kind: str, name: str):
        app = self.app
        method = getattr(app, "connect_ssh_dialog" if kind == "ssh" else "connect_telnet_dialog", None)
        if method is None:
            return
        self.accept()
        # Never drop a live connection: use a fresh tab when the current one is busy.
        current = app.current_session() if hasattr(app, "current_session") else None
        if current is None or current.connection is not None or getattr(current, "connecting", False):
            app.new_terminal(name=name)
        method(profile=name)


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
