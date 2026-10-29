from __future__ import annotations
import os
import stat
import threading
import posixpath
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QLabel, QPushButton, QLineEdit, QComboBox, QListWidget,
    QListWidgetItem, QFrame, QVBoxLayout, QHBoxLayout, QGridLayout,
    QFileDialog, QMessageBox
)

from core.sftp_hops import (
    get_hops, add_jump_hop, get_active_sftp, remove_last_hop,
    upload_folder, download_folder,
)


class AddHopDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self._parent_ref = parent
        self.result = None
        self.setWindowTitle("Add Jump Hop")
        self.resize(380, 260)
        self.setModal(True)

        grid = QGridLayout(self)
        title = QLabel("Tunnel to Next Server")
        f = QFont(); f.setPointSize(13); f.setBold(True)
        title.setFont(f)
        grid.addWidget(title, 0, 0, 1, 2)

        self.host_entry = QLineEdit()
        self.port_entry = QLineEdit("22")
        self.username_entry = QLineEdit()
        self.password_entry = QLineEdit()
        self.password_entry.setEchoMode(QLineEdit.EchoMode.Password)

        rows = [("Host/IP", self.host_entry), ("Port", self.port_entry), ("Username", self.username_entry), ("Password", self.password_entry)]
        for idx, (label, widget) in enumerate(rows, start=1):
            grid.addWidget(QLabel(label), idx, 0)
            grid.addWidget(widget, idx, 1)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        connect_btn = QPushButton("Connect")
        connect_btn.clicked.connect(self._ok)
        connect_btn.setDefault(True)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(connect_btn)
        grid.addLayout(btn_row, 6, 0, 1, 2)

    def _ok(self):
        if not self.host_entry.text().strip():
            QMessageBox.critical(self, "Missing host", "Enter the host/IP for this hop.")
            return
        try:
            port = int(self.port_entry.text() or "22")
        except ValueError:
            QMessageBox.critical(self, "Invalid port", "Port must be numeric.")
            return
        self.result = {
            "host": self.host_entry.text().strip(),
            "port": port,
            "username": self.username_entry.text().strip(),
            "password": self.password_entry.text(),
        }
        self.accept()


class SFTPBrowserDialog(QDialog):
    def __init__(self, app, session):
        super().__init__(app)
        self.app = app
        self.session = session
        self.setWindowTitle(f"Remote Files \u2014 {session.name}")
        self.resize(760, 520)
        self.setModal(False)

        self.current_path = "."
        self.selected_entry = None
        self._nav_token = 0

        self._build_ui()
        self._init_hops_and_list()
        self.show()

    def _build_ui(self):
        root = QVBoxLayout(self)

        hop_bar = QHBoxLayout()
        self.hop_combo = QComboBox()
        self.hop_combo.addItem("Connecting...")
        self.hop_combo.currentTextChanged.connect(self._on_hop_selected)
        hop_bar.addWidget(self.hop_combo, 1)
        add_hop_btn = QPushButton("+ Add Jump Hop")
        add_hop_btn.setStyleSheet("QPushButton { background-color: #7656b7; color: white; }")
        add_hop_btn.clicked.connect(self._add_hop_dialog)
        hop_bar.addWidget(add_hop_btn)
        rm_hop_btn = QPushButton("Remove Last Hop")
        rm_hop_btn.setStyleSheet("QPushButton { background-color: #8a2d3b; color: white; }")
        rm_hop_btn.clicked.connect(self._remove_hop)
        hop_bar.addWidget(rm_hop_btn)
        root.addLayout(hop_bar)

        top = QHBoxLayout()
        self.path_entry = QLineEdit()
        self.path_entry.returnPressed.connect(lambda: self._navigate_to(self.path_entry.text()))
        top.addWidget(self.path_entry, 1)
        go_btn = QPushButton("Go")
        go_btn.clicked.connect(lambda: self._navigate_to(self.path_entry.text()))
        top.addWidget(go_btn)
        up_btn = QPushButton("Up")
        up_btn.clicked.connect(self._go_up)
        top.addWidget(up_btn)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh)
        top.addWidget(refresh_btn)
        root.addLayout(top)

        actions = QHBoxLayout()
        upload_btn = QPushButton("Upload File...")
        upload_btn.setStyleSheet("QPushButton { background-color: #2f6f95; color: white; }")
        upload_btn.clicked.connect(self._upload_dialog)
        actions.addWidget(upload_btn)
        download_btn = QPushButton("Download Selected")
        download_btn.setStyleSheet("QPushButton { background-color: #256d55; color: white; }")
        download_btn.clicked.connect(self._download_selected)
        actions.addWidget(download_btn)
        upload_folder_btn = QPushButton("Upload Folder...")
        upload_folder_btn.setStyleSheet("QPushButton { background-color: #2f6f95; color: white; }")
        upload_folder_btn.clicked.connect(self._upload_folder_dialog)
        actions.addWidget(upload_folder_btn)
        download_folder_btn = QPushButton("Download Folder")
        download_folder_btn.setStyleSheet("QPushButton { background-color: #256d55; color: white; }")
        download_folder_btn.clicked.connect(self._download_folder)
        actions.addWidget(download_folder_btn)
        delete_btn = QPushButton("Delete Selected")
        delete_btn.setStyleSheet("QPushButton { background-color: #8a2d3b; color: white; }")
        delete_btn.clicked.connect(self._delete_selected)
        actions.addWidget(delete_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        self.list_widget = QListWidget()
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)
        root.addWidget(self.list_widget, 1)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: palette(placeholder-text);")
        root.addWidget(self.status_label)

    def _set_status(self, text):
        try:
            self.status_label.setText(text)
        except Exception:
            pass

    def _refresh_hop_menu(self):
        hops = self.session.sftp_hops
        labels = [h["label"] for h in hops]
        self.hop_combo.blockSignals(True)
        self.hop_combo.clear()
        self.hop_combo.addItems(labels)
        if labels:
            self.hop_combo.setCurrentIndex(self.session.active_hop_index)
        self.hop_combo.blockSignals(False)

    def _init_hops_and_list(self):
        def worker():
            try:
                get_hops(self.session)
                self.app.after(0, self._refresh_hop_menu)
                sftp = get_active_sftp(self.session)
                home = sftp.normalize(".")
                self.current_path = home
                self.app.after(0, self._refresh)
            except Exception as exc:
                self.app.after(0, lambda e=exc: self._set_status(f"SFTP connect failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _on_hop_selected(self, label):
        if not label:
            return
        hops = self.session.sftp_hops
        for idx, h in enumerate(hops):
            if h["label"] == label:
                self.session.active_hop_index = idx
                break
        self._set_status(f"Switched to {label}")

        def worker():
            try:
                sftp = get_active_sftp(self.session)
                home = sftp.normalize(".")
                self.current_path = home
                self.app.after(0, self._refresh)
            except Exception as exc:
                self.app.after(0, lambda e=exc: self._set_status(f"Failed to switch hop: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _add_hop_dialog(self):
        d = AddHopDialog(self)
        if d.exec() != QDialog.DialogCode.Accepted or not d.result:
            return
        self._set_status(f"Tunneling to {d.result['host']}...")

        def worker():
            try:
                add_jump_hop(self.session, d.result["host"], d.result["port"], d.result["username"], d.result["password"])
                self.app.after(0, self._refresh_hop_menu)
                sftp = get_active_sftp(self.session)
                home = sftp.normalize(".")
                self.current_path = home
                self.app.after(0, self._refresh)
            except Exception as exc:
                error_text = str(exc)
                self.app.after(0, lambda: self._set_status(f"Jump hop failed: {error_text}"))
                self.app.after(0, lambda: QMessageBox.critical(
                    self, "Jump Hop Connection Failed",
                    f"Could not connect to {d.result['host']}:{d.result['port']}.\n\n{error_text}",
                ))
        threading.Thread(target=worker, daemon=True).start()

    def _remove_hop(self):
        if len(self.session.sftp_hops) <= 1:
            self._set_status("Only one hop remains; nothing to remove.")
            return
        remove_last_hop(self.session)
        self._refresh_hop_menu()

        def worker():
            try:
                sftp = get_active_sftp(self.session)
                home = sftp.normalize(".")
                self.current_path = home
                self.app.after(0, self._refresh)
            except Exception as exc:
                self.app.after(0, lambda e=exc: self._set_status(f"Failed to reload after removing hop: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _navigate_to(self, path):
        self.current_path = path.strip() or "."
        self._refresh()

    def _go_up(self):
        parent = posixpath.dirname(self.current_path.rstrip("/")) or "/"
        self.current_path = parent
        self._refresh()

    def _refresh(self):
        self._nav_token += 1
        token = self._nav_token
        target_path = self.current_path
        self._set_status("Loading...")

        def worker():
            try:
                sftp = get_active_sftp(self.session)
                raw_entries = sftp.listdir_attr(target_path)
                resolved = []
                for entry in raw_entries:
                    is_dir = stat.S_ISDIR(entry.st_mode)
                    is_link = stat.S_ISLNK(entry.st_mode)
                    if is_link:
                        full_path = posixpath.join(target_path.rstrip("/") or "/", entry.filename)
                        try:
                            target_stat = sftp.stat(full_path)
                            is_dir = stat.S_ISDIR(target_stat.st_mode)
                        except Exception:
                            is_dir = False
                    resolved.append((entry, is_dir))
                resolved.sort(key=lambda pair: (not pair[1], pair[0].filename.lower()))
                self.app.after(0, lambda: self._apply_refresh_result(token, target_path, resolved, None))
            except Exception as exc:
                error_text = str(exc)
                self.app.after(0, lambda: self._apply_refresh_result(token, target_path, [], error_text))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_refresh_result(self, token, path, resolved, error):
        if token != self._nav_token:
            return
        self.current_path = path
        self._populate(resolved, error=error)

    def _populate(self, resolved_entries, error=None):
        self.path_entry.setText(self.current_path)
        self.list_widget.clear()
        self.selected_entry = None

        if error:
            item = QListWidgetItem(f"\u26a0  Failed to open this folder: {error}")
            item.setForeground(Qt.GlobalColor.red)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_widget.addItem(item)
            self._set_status("Error loading directory")
            return

        if not resolved_entries:
            item = QListWidgetItem("(this folder is empty)")
            item.setForeground(Qt.GlobalColor.gray)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_widget.addItem(item)
            self._set_status("0 items")
            return

        for entry, is_dir in resolved_entries:
            icon = "\U0001F4C1" if is_dir else "\U0001F4C4"
            size = "" if is_dir else f"  ({entry.st_size:,} bytes)"
            item = QListWidgetItem(f"{icon}  {entry.filename}{size}")
            item.setData(Qt.ItemDataRole.UserRole, {"name": entry.filename, "is_dir": is_dir})
            self.list_widget.addItem(item)

        self._set_status(f"{len(resolved_entries)} items")

    def _on_item_clicked(self, item: QListWidgetItem):
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            self.selected_entry = data

    def _on_item_double_clicked(self, item: QListWidgetItem):
        data = item.data(Qt.ItemDataRole.UserRole)
        if data and data["is_dir"]:
            self.current_path = posixpath.join(self.current_path.rstrip("/") or "/", data["name"])
            self._refresh()

    def _upload_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Select file(s) to upload")
        if paths:
            self._upload_paths(list(paths))

    def _upload_paths(self, local_paths):
        target_dir = self.current_path

        def worker():
            try:
                sftp = get_active_sftp(self.session)
            except Exception as exc:
                self.app.after(0, lambda e=exc: self._set_status(f"SFTP unavailable: {e}"))
                return
            for lp in local_paths:
                name = Path(lp).name
                remote_path = posixpath.join(target_dir.rstrip("/") or "/", name)
                try:
                    self.app.after(0, lambda n=name: self._set_status(f"Uploading {n}..."))
                    sftp.put(lp, remote_path)
                except Exception as exc:
                    self.app.after(0, lambda n=name, e=exc: self._set_status(f"Failed to upload {n}: {e}"))
                    return
            self.app.after(0, lambda: self._set_status("Upload complete"))
            self.app.after(0, self._refresh)
        threading.Thread(target=worker, daemon=True).start()

    def _download_selected(self):
        if not self.selected_entry or self.selected_entry["is_dir"]:
            QMessageBox.warning(self, "No file selected", "Select a remote file (not a folder) to download.")
            return
        name = self.selected_entry["name"]
        local_path, _ = QFileDialog.getSaveFileName(self, "Save file as", name)
        if not local_path:
            return
        remote_path = posixpath.join(self.current_path.rstrip("/") or "/", name)

        def worker():
            try:
                self.app.after(0, lambda: self._set_status(f"Downloading {name}..."))
                sftp = get_active_sftp(self.session)
                sftp.get(remote_path, local_path)
                self.app.after(0, lambda: self._set_status(f"Downloaded {name} -> {local_path}"))
            except Exception as exc:
                self.app.after(0, lambda e=exc: self._set_status(f"Download failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _upload_folder_dialog(self):
        local_dir = QFileDialog.getExistingDirectory(self, "Select folder to upload")
        if not local_dir:
            return
        target_dir = self.current_path
        folder_name = Path(local_dir).name
        remote_target = posixpath.join(target_dir.rstrip("/") or "/", folder_name)

        def worker():
            try:
                sftp = get_active_sftp(self.session)
            except Exception as exc:
                self.app.after(0, lambda e=exc: self._set_status(f"SFTP unavailable: {e}"))
                return
            try:
                self.app.after(0, lambda: self._set_status(f"Uploading folder {folder_name}..."))

                def progress(local_path):
                    self.app.after(0, lambda: self._set_status(f"Uploading {Path(local_path).name}..."))

                upload_folder(sftp, local_dir, remote_target, progress_cb=progress)
                self.app.after(0, lambda: self._set_status(f"Folder '{folder_name}' uploaded"))
                self.app.after(0, self._refresh)
            except Exception as exc:
                self.app.after(0, lambda e=exc: self._set_status(f"Folder upload failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _download_folder(self):
        if not self.selected_entry or not self.selected_entry["is_dir"]:
            QMessageBox.warning(self, "No folder selected", "Select a remote folder (not a file) to download.")
            return
        name = self.selected_entry["name"]
        local_parent = QFileDialog.getExistingDirectory(self, "Choose where to save the folder")
        if not local_parent:
            return
        remote_path = posixpath.join(self.current_path.rstrip("/") or "/", name)
        local_path = os.path.join(local_parent, name)

        def worker():
            try:
                sftp = get_active_sftp(self.session)
            except Exception as exc:
                self.app.after(0, lambda e=exc: self._set_status(f"SFTP unavailable: {e}"))
                return
            try:
                self.app.after(0, lambda: self._set_status(f"Downloading folder {name}..."))

                def progress(remote_p):
                    self.app.after(0, lambda: self._set_status(f"Downloading {posixpath.basename(remote_p)}..."))

                download_folder(sftp, remote_path, local_path, progress_cb=progress)
                self.app.after(0, lambda: self._set_status(f"Folder downloaded to {local_path}"))
            except Exception as exc:
                self.app.after(0, lambda e=exc: self._set_status(f"Folder download failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _delete_selected(self):
        if not self.selected_entry:
            return
        if self.selected_entry["is_dir"]:
            QMessageBox.warning(self, "Not supported", "Folder deletion is not supported here.")
            return
        name = self.selected_entry["name"]
        if QMessageBox.question(self, "Delete file", f"Delete remote file '{name}'?") != QMessageBox.StandardButton.Yes:
            return
        remote_path = posixpath.join(self.current_path.rstrip("/") or "/", name)

        def worker():
            try:
                sftp = get_active_sftp(self.session)
                sftp.remove(remote_path)
                self.app.after(0, lambda: self._set_status(f"Deleted {name}"))
                self.app.after(0, self._refresh)
            except Exception as exc:
                self.app.after(0, lambda e=exc: self._set_status(f"Delete failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()
