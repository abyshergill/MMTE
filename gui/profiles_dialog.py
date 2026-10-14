from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

class ProfilesDialog(QDialog):
    def __init__(self, parent, ssh_store, telnet_store):
        super().__init__(parent)
        self.setWindowTitle("Saved Profiles")
        self.resize(560, 520)
        self.setModal(True)
        self.ssh_store = ssh_store
        self.telnet_store = telnet_store
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)

        f = QFont()
        f.setPointSize(14)
        f.setBold(True)

        ssh_title = QLabel("Saved SSH Profiles")
        ssh_title.setFont(f)
        root.addWidget(ssh_title)

        self.ssh_box = QListWidget()
        root.addWidget(self.ssh_box, 1)

        telnet_title = QLabel("Saved Telnet Profiles")
        telnet_title.setFont(f)
        root.addWidget(telnet_title)

        self.telnet_box = QListWidget()
        root.addWidget(self.telnet_box, 1)

        self.ref_ssh()
        self.ref_telnet()

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)

    def ref_ssh(self):
        self.ssh_box.clear()
        for n, pf in sorted(self.ssh_store.profiles.items()):
            item = QListWidgetItem()
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(6, 4, 6, 4)
            row_layout.addWidget(QLabel(f"{n} ({pf.get('username','')}@{pf.get('host','')})"), 1)
            del_btn = QPushButton("Delete")
            del_btn.setFixedWidth(70)
            del_btn.setStyleSheet("QPushButton { background-color: #8a2d3b; color: white; }")
            del_btn.clicked.connect(lambda _c, name=n: (self.ssh_store.delete(name), self.ref_ssh()))
            row_layout.addWidget(del_btn)
            item.setSizeHint(row.sizeHint())
            self.ssh_box.addItem(item)
            self.ssh_box.setItemWidget(item, row)

    def ref_telnet(self):
        self.telnet_box.clear()
        for n, pf in sorted(self.telnet_store.profiles.items()):
            item = QListWidgetItem()
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(6, 4, 6, 4)
            row_layout.addWidget(QLabel(f"{n} ({pf.get('host','')}:{pf.get('port','')})"), 1)
            del_btn = QPushButton("Delete")
            del_btn.setFixedWidth(70)
            del_btn.setStyleSheet("QPushButton { background-color: #8a2d3b; color: white; }")
            del_btn.clicked.connect(lambda _c, name=n: (self.telnet_store.delete(name), self.ref_telnet()))
            row_layout.addWidget(del_btn)
            item.setSizeHint(row.sizeHint())
            self.telnet_box.addItem(item)
            self.telnet_box.setItemWidget(item, row)