from __future__ import annotations
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
import pyte
from PyQt6.QtGui import QTextCharFormat, QColor, QFont, QTextCursor
from core.theme import TERMINAL_BG, TERMINAL_FG, TERMINAL_ACCENT, PALETTE

if TYPE_CHECKING:
    from main import TerminalApp
    from core.connections import BaseConnection
    from gui.terminal_widget import TerminalWidget

DEFAULT_COLS = 80
DEFAULT_ROWS = 24


class ScriptStopped(Exception):
    """Raised inside automation code (TTL / Python scripts) when the user
    requests the running script be stopped."""


def _running_event() -> threading.Event:
    # Set == "not paused" (i.e. free to run). Cleared == paused.
    e = threading.Event()
    e.set()
    return e


@dataclass(eq=False)
class TerminalSession:
    app: "TerminalApp"
    name: str
    frame: object          # the QWidget wrapper placed in the QStackedWidget
    terminal: object       # the TerminalWidget (QPlainTextEdit subclass)
    incoming: queue.Queue[str] = field(default_factory=queue.Queue)
    connection: "BaseConnection" | None = None
    connected_label: str = "Disconnected"
    log_enabled: bool = False
    log_file: object | None = None
    recv_buffer: str = ""
    recv_condition: threading.Condition = field(default_factory=threading.Condition)
    cols: int = DEFAULT_COLS
    rows: int = DEFAULT_ROWS
    awaiting_prompt: bool = False
    prompt_row: int = -1
    prompt_col: int = 0
    scroll_offset: int = 0
    connection_lost_notified: bool = False
    sftp_hops: list = field(default_factory=list)
    active_hop_index: int = 0
    script_running: bool = False
    script_name: str = ""
    script_stop_event: threading.Event = field(default_factory=threading.Event)
    script_pause_event: threading.Event = field(default_factory=_running_event)

    def __post_init__(self):
        self.screen = pyte.HistoryScreen(self.cols, self.rows, history=5000, ratio=0.5)
        self.stream = pyte.Stream(self.screen)
        self._formats_ready = False
        self._format_cache: dict[str | None, QTextCharFormat] = {}
        self._resize_job = None

    @property
    def alt_screen_active(self) -> bool:
        try:
            mode = self.screen.mode
            return any(m in mode for m in (1049, 47, 1047))
        except Exception:
            return False

    def resize(self, cols: int, rows: int) -> None:
        cols = max(cols, 20)
        rows = max(rows, 5)
        if cols == self.cols and rows == self.rows:
            return
        self.cols, self.rows = cols, rows
        try:
            self.screen.resize(rows, cols)
        except Exception:
            pass
        if self.connection:
            try:
                self.connection.resize(cols, rows)
            except Exception:
                pass
        self.render()

    def scroll_history(self, delta_lines: int) -> None:
        try:
            max_offset = len(self.screen.history.top)
        except Exception:
            max_offset = 0
        self.scroll_offset = max(0, min(self.scroll_offset + delta_lines, max_offset))
        self.render()

    def snap_to_live(self) -> None:
        if self.scroll_offset:
            self.scroll_offset = 0
            self.render()

    def _widget_alive(self) -> bool:
        try:
            # Accessing a property on a deleted PyQt wrapper raises RuntimeError.
            self.terminal.isVisible()
            return True
        except RuntimeError:
            return False
        except Exception:
            return True

    def append(self, data: str) -> None:
        if not self._widget_alive():
            return
        try:
            self.stream.feed(data)
        except Exception:
            pass
        with self.recv_condition:
            self.recv_buffer += data
            if len(self.recv_buffer) > 250_000:
                self.recv_buffer = self.recv_buffer[-125_000:]
            self.recv_condition.notify_all()
        if self.log_enabled and self.log_file:
            try:
                self.log_file.write(data)
                self.log_file.flush()
            except Exception:
                pass

    def _setup_formats(self) -> None:
        default = QTextCharFormat()
        default.setForeground(QColor(TERMINAL_FG))
        self._format_cache[None] = default

        reverse = QTextCharFormat()
        reverse.setForeground(QColor(TERMINAL_BG))
        reverse.setBackground(QColor(TERMINAL_FG))
        self._format_cache["reverse"] = reverse

        bold = QTextCharFormat()
        bold.setForeground(QColor(TERMINAL_FG))
        bold.setFontWeight(QFont.Weight.Bold)
        self._format_cache["bold"] = bold

        for name, color in PALETTE.items():
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            self._format_cache[f"fg_{name}"] = fmt

    def _tag_for_char(self, char) -> str | None:
        if getattr(char, "reverse", False):
            return "reverse"
        fg = getattr(char, "fg", "default")
        if fg and fg != "default":
            candidate = f"fg_{fg}"
            if candidate in self._format_cache:
                return candidate
        if getattr(char, "bold", False):
            return "bold"
        return None

    def render(self) -> None:
        if not self._widget_alive():
            return
        text_widget: "TerminalWidget" = self.terminal
        if not self._formats_ready:
            self._setup_formats()
            self._formats_ready = True

        screen = self.screen
        rows = screen.lines
        cols = screen.columns
        if self.scroll_offset > 0:
            history = list(screen.history.top)
            total_history = len(history)
            k = min(self.scroll_offset, total_history)
            start = total_history - k
            end = min(total_history, start + rows)
            from_history = history[start:end]
            remaining = rows - len(from_history)
            from_buffer = [screen.buffer[y] for y in range(remaining)] if remaining > 0 else []
            display_rows = from_history + from_buffer
        else:
            display_rows = [screen.buffer[y] for y in range(rows)]

        text_widget.setUpdatesEnabled(False)
        cursor = text_widget.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        default_fmt = self._format_cache[None]

        for row_idx, row in enumerate(display_rows):
            run_text = ""
            run_tag: str | None = None
            first_run = True
            for x in range(cols):
                char = row[x]
                tag = self._tag_for_char(char)
                ch = char.data or " "
                if tag != run_tag:
                    if run_text or not first_run:
                        cursor.insertText(run_text, self._format_cache.get(run_tag, default_fmt))
                    run_text = ch
                    run_tag = tag
                    first_run = False
                else:
                    run_text += ch
            if run_text or first_run:
                cursor.insertText(run_text, self._format_cache.get(run_tag, default_fmt))
            if row_idx < len(display_rows) - 1:
                cursor.insertText("\n", default_fmt)

        if self.scroll_offset == 0:
            doc = text_widget.document()
            block_num = min(screen.cursor.y, max(doc.blockCount() - 1, 0))
            block = doc.findBlockByNumber(block_num)
            col = min(screen.cursor.x, max(cols - 1, 0))
            pos = block.position() + min(col, max(block.length() - 1, 0))
            cur2 = text_widget.textCursor()
            cur2.setPosition(pos)
            text_widget.setTextCursor(cur2)
            text_widget.ensureCursorVisible()
        text_widget.setUpdatesEnabled(True)

    def send(self, data: str) -> None:
        if not self.connection:
            if not self.connection_lost_notified:
                self.incoming.put("\r\n[No active connection]\r\n")
                self.connection_lost_notified = True
            return
        try:
            self.connection.send(data)
            if self.log_enabled and self.log_file:
                self.log_file.write(data)
                self.log_file.flush()
        except Exception as exc:
            self.incoming.put(f"\r\n[Send error: {exc}]\r\n")
            try:
                self.connection.close()
            except Exception:
                pass
            self.connection = None
            self.connected_label = "Disconnected"
            self.connection_lost_notified = True

    def wait_for(self, pattern: str, timeout: float = 60.0, case_sensitive: bool = True) -> bool:
        deadline = time.time() + timeout
        needle = pattern if case_sensitive else pattern.lower()
        with self.recv_condition:
            while True:
                self.script_checkpoint()
                haystack = self.recv_buffer if case_sensitive else self.recv_buffer.lower()
                if needle in haystack:
                    return True
                try:
                    for row in self.screen.buffer.values():
                        line_str = "".join(char.data or " " for char in row)
                        if not case_sensitive:
                            line_str = line_str.lower()
                        if needle in line_str:
                            return True
                except Exception:
                    pass
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self.recv_condition.wait(min(0.2, remaining))

    def clear_wait_buffer(self) -> None:
        with self.recv_condition:
            self.recv_buffer = ""

    def clear_screen(self) -> None:
        """Fully clear the terminal. Clearing only the QTextEdit widget is not
        enough: render() always repaints from self.screen's buffer, so the
        next render() call (e.g. triggered by the next byte of incoming data)
        would silently restore the 'cleared' content. Reset the underlying
        pyte screen buffer/history too so the clear actually sticks."""
        try:
            self.screen.reset()
        except Exception:
            pass
        self.scroll_offset = 0
        with self.recv_condition:
            self.recv_buffer = ""
        self.render()

    def script_checkpoint(self) -> None:
        """Call periodically from automation loops (TTL macro steps, python
        script send/wait calls). Blocks while the script is paused, and
        raises ScriptStopped if the user requested Stop."""
        if self.script_stop_event.is_set():
            raise ScriptStopped()
        self.script_pause_event.wait()
        if self.script_stop_event.is_set():
            raise ScriptStopped()

    def start_script(self, name: str) -> None:
        self.script_running = True
        self.script_name = name
        self.script_stop_event.clear()
        self.script_pause_event.set()

    def finish_script(self) -> None:
        self.script_running = False
        self.script_name = ""
        self.script_stop_event.clear()
        self.script_pause_event.set()

    def request_stop_script(self) -> None:
        self.script_stop_event.set()
        self.script_pause_event.set()  # unblock if currently paused

    def toggle_pause_script(self) -> bool:
        """Returns True if now paused, False if now resumed."""
        if self.script_pause_event.is_set():
            self.script_pause_event.clear()
            return True
        else:
            self.script_pause_event.set()
            return False

    def enable_custom_logging(self, file_path: Path) -> None:
        if self.log_enabled:
            return
        file_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = file_path.open("a", encoding="utf-8", errors="replace")
        self.log_enabled = True
        self.incoming.put(f"\r\n[Logging started -> {file_path}]\r\n")

    def disable_logging(self, file_path: "Path | str | None" = None) -> None:
        if file_path is not None and self.log_file is not None:
            try:
                current_path = Path(self.log_file.name).resolve()
                target_path = Path(file_path).resolve()
                if current_path != target_path:
                    # A different log than the one currently open; nothing to do.
                    return
            except Exception:
                pass
        was_enabled = self.log_enabled
        self.log_enabled = False
        try:
            if self.log_file:
                self.log_file.close()
        finally:
            self.log_file = None
        if was_enabled:
            self.incoming.put("\r\n[Logging stopped]\r\n")

    def close(self) -> None:
        self.close_sftp_hops()
        try:
            if self.connection:
                self.connection.close()
        finally:
            self.connection = None
            self.connected_label = "Disconnected"
            self.connection_lost_notified = False
            self.disable_logging()

    def close_sftp_hops(self) -> None:
        while self.sftp_hops:
            hop = self.sftp_hops.pop()
            try:
                if hop.get("sftp"):
                    hop["sftp"].close()
            except Exception:
                pass
            try:
                if hop.get("client"):
                    hop["client"].close()
            except Exception:
                pass
        self.active_hop_index = 0


@dataclass(eq=False)
class PlaylistItem:
    path: str
    timeout: float
    target_session: TerminalSession
    status: str = "Pending"
