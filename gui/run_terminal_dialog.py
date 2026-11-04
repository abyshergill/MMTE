"""Pick the terminals that should receive a set of commands, then run them.

Used in two situations:

* the AI panel's **Run in Terminal** button (manual: the user chooses the terminals);
* **approval** of commands the model prepared while *LLM -> Terminal* is off.

Only terminals with a live connection can be ticked.  A not-yet-connected tab can be
connected from here (SSH / Telnet / Serial) or a new tab created, so the user never
has to abandon the dialog just to open a terminal first.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from core.terminal_dispatch import connection_summary, is_connected, parse_command_lines
from gui.ui_style import TOKENS


class RunInTerminalDialog(QDialog):
    def __init__(
        self,
        main_window,
        commands: list[str],
        preselect: list | None = None,
        approval: bool = False,
        default_delay: float = 1.2,
        parent=None,
    ):
        super().__init__(parent or main_window)
        self.main_window = main_window
        self._approval = approval
        self._checked: set[int] = set()
        self._wanted: set[int] = set()  # tabs the user is connecting right now -> tick once live
        self._rows: list = []
        self._signature: tuple = ()
        self._updating = False

        self.setWindowTitle("Approve AI commands" if approval else "Run in Terminal")
        self.setModal(True)
        self.resize(560, 560)

        sessions = self._sessions()
        for s in preselect or []:
            (self._checked if is_connected(s) else self._wanted).add(id(s))
        if not self._checked and not self._wanted:
            live = [s for s in sessions if is_connected(s)]
            current = getattr(main_window, "current_session", lambda: None)()
            if current is not None and is_connected(current):
                self._checked.add(id(current))
            elif len(live) == 1:
                self._checked.add(id(live[0]))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        title = QLabel("The AI wants to run these commands" if approval else "Run commands in terminals")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)
        sub = QLabel(
            "Review or edit the commands, tick the terminals that should receive them, then run."
            if approval
            else "Edit the commands if needed, then tick the active terminals that should receive them."
        )
        sub.setObjectName("Subtle")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        cmd_lbl = QLabel("COMMANDS  (one per line)")
        cmd_lbl.setObjectName("Hint")
        layout.addWidget(cmd_lbl)
        self.editor = QPlainTextEdit()
        self.editor.setObjectName("CommandEditor")
        self.editor.setPlainText("\n".join(commands))
        self.editor.setMinimumHeight(110)
        self.editor.textChanged.connect(self._update_run_state)
        layout.addWidget(self.editor, 1)

        head = QHBoxLayout()
        term_lbl = QLabel("TERMINALS")
        term_lbl.setObjectName("Hint")
        head.addWidget(term_lbl)
        head.addStretch(1)
        self.select_all_cb = QCheckBox("Select all connected")
        self.select_all_cb.toggled.connect(self._on_select_all)
        head.addWidget(self.select_all_cb)
        layout.addLayout(head)

        self.picker = QListWidget()
        self.picker.setObjectName("TerminalPicker")
        self.picker.itemChanged.connect(self._on_item_changed)
        self.picker.setMinimumHeight(120)
        layout.addWidget(self.picker, 2)

        self.empty_hint = QLabel(
            "No terminal is connected yet. Select a tab above and choose Connect…, "
            "or press New terminal."
        )
        self.empty_hint.setObjectName("Hint")
        self.empty_hint.setWordWrap(True)
        layout.addWidget(self.empty_hint)

        tools = QHBoxLayout()
        self.connect_btn = QPushButton("Connect selected…")
        menu = QMenu(self.connect_btn)
        menu.addAction("SSH…", lambda: self._connect_with("ssh"))
        menu.addAction("Telnet…", lambda: self._connect_with("telnet"))
        menu.addAction("Serial…", lambda: self._connect_with("serial"))
        self.connect_btn.setMenu(menu)
        tools.addWidget(self.connect_btn)
        self.new_btn = QPushButton("New terminal")
        self.new_btn.clicked.connect(self._new_terminal)
        tools.addWidget(self.new_btn)
        tools.addStretch(1)
        delay_lbl = QLabel("Wait between commands")
        delay_lbl.setObjectName("Subtle")
        tools.addWidget(delay_lbl)
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0.5, 60.0)
        self.delay_spin.setSingleStep(0.5)
        self.delay_spin.setDecimals(1)
        self.delay_spin.setSuffix(" s")
        self.delay_spin.setValue(max(0.5, float(default_delay)))
        tools.addWidget(self.delay_spin)
        layout.addLayout(tools)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_btn = QPushButton("Don't run" if approval else "Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_btn)
        self.run_btn = QPushButton("Run")
        self.run_btn.setObjectName("PrimaryBtn")
        self.run_btn.setDefault(True)
        self.run_btn.clicked.connect(self._accept_if_valid)
        buttons.addWidget(self.run_btn)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.setInterval(600)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh(force=True)

    # ------------------------------------------------------------------ data
    def _sessions(self) -> list:
        return list(getattr(self.main_window, "sessions", []) or [])

    def commands(self) -> list[str]:
        return parse_command_lines(self.editor.toPlainText())

    def selected_sessions(self) -> list:
        return [s for s in self._sessions() if id(s) in self._checked and is_connected(s)]

    def delay(self) -> float:
        return float(self.delay_spin.value())

    # ------------------------------------------------------------------- ui
    def _refresh(self, force: bool = False) -> None:
        sessions = self._sessions()
        signature = tuple((id(s), s.name, is_connected(s), connection_summary(s)) for s in sessions)
        if signature == self._signature and not force:
            return
        self._signature = signature
        self._updating = True
        try:
            self.picker.clear()
            self._rows = sessions
            alive = {id(s) for s in sessions}
            self._checked &= alive
            for s in sessions:
                live = is_connected(s)
                if live and id(s) in self._wanted:  # a tab we just connected -> tick it
                    self._checked.add(id(s))
                    self._wanted.discard(id(s))
                item = QListWidgetItem(f"{s.name}      ·      {connection_summary(s)}")
                if live:
                    item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    item.setCheckState(Qt.CheckState.Checked if id(s) in self._checked else Qt.CheckState.Unchecked)
                else:
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    item.setForeground(QBrush(QColor(TOKENS["mute"])))
                    item.setToolTip("Not connected. Select this row and use “Connect selected…”.")
                self.picker.addItem(item)
        finally:
            self._updating = False
        self._update_run_state()

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        if self._updating:
            return
        row = self.picker.row(item)
        if not 0 <= row < len(self._rows):
            return
        key = id(self._rows[row])
        if item.checkState() == Qt.CheckState.Checked:
            self._checked.add(key)
        else:
            self._checked.discard(key)
        self._update_run_state()

    def _on_select_all(self, on: bool) -> None:
        if self._updating:
            return
        live = [s for s in self._rows if is_connected(s)]
        if on:
            self._checked.update(id(s) for s in live)
        else:
            self._checked.difference_update(id(s) for s in live)
        self._refresh(force=True)

    def _update_run_state(self) -> None:
        live = [s for s in self._rows if is_connected(s)]
        picked = self.selected_sessions()
        cmds = self.commands()
        self.empty_hint.setVisible(not live)
        self._updating = True
        try:
            self.select_all_cb.setEnabled(bool(live))
            self.select_all_cb.setChecked(bool(live) and len(picked) == len(live))
        finally:
            self._updating = False
        count = len(picked)
        self.run_btn.setText(f"Run on {count} terminal{'s' if count != 1 else ''}" if count else "Run")
        self.run_btn.setEnabled(bool(cmds) and count > 0)

    # -------------------------------------------------------------- actions
    def _highlighted_session(self):
        row = self.picker.currentRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _connect_with(self, kind: str) -> None:
        mw = self.main_window
        if mw is None:
            return
        session = self._highlighted_session()
        if session is None:
            session = next((s for s in self._rows if not is_connected(s)), None) or mw.new_terminal()
        mw.select_session(session)
        self._wanted.add(id(session))
        getattr(mw, f"connect_{kind}_dialog")()
        self._refresh(force=True)

    def _new_terminal(self) -> None:
        if self.main_window is not None:
            self._wanted.add(id(self.main_window.new_terminal()))
        self._refresh(force=True)

    def _accept_if_valid(self) -> None:
        if self.commands() and self.selected_sessions():
            self.accept()

    def done(self, result: int) -> None:
        self._timer.stop()
        super().done(result)
