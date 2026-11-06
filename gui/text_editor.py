"""Built-in multi-tab text editor (Notepad++ style).

* Tabs with an unsaved-changes marker, line numbers, current-line highlight, auto-indent.
* Syntax colouring through Pygments (picked from the file name, a shebang or the Language menu);
  ``.log`` files use the terminal's own log highlighter.
* Find / Replace bar (case, whole word, regex), Go to Line, duplicate line, toggle comment, zoom.
* Keeps the file's encoding and line endings (LF / CRLF) when saving.
* Documents can be local files or remote files read and written over SFTP, so double-clicking a
  file in the SFTP browser, or typing ``vim file`` in an SSH terminal, edits it here.
"""

from __future__ import annotations

import codecs
import posixpath
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontDatabase,
    QKeySequence,
    QPainter,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextOption,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.terminal_highlight import FORMATS as LOG_FORMATS, highlight_spans
from gui.close_button import CloseButton
from gui.ui_style import TOKENS

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_HIGHLIGHT_CHARS = 3_000_000

# --------------------------------------------------------------------------------------------
# Languages
# --------------------------------------------------------------------------------------------

# Menu label -> pygments alias ("" = plain text, "log" = built-in log highlighter)
LANGUAGES: dict[str, str] = {
    "Plain Text": "",
    "Log File": "log",
    "Python": "python",
    "Bash / Shell": "bash",
    "Tera Term Macro": "teraterm",
    "YAML": "yaml",
    "JSON": "json",
    "INI / Config": "ini",
    "TOML": "toml",
    "XML": "xml",
    "HTML": "html",
    "CSS": "css",
    "JavaScript": "javascript",
    "TypeScript": "typescript",
    "C": "c",
    "C++": "cpp",
    "C#": "csharp",
    "Java": "java",
    "Go": "go",
    "Rust": "rust",
    "SQL": "sql",
    "Markdown": "markdown",
    "Dockerfile": "docker",
    "Makefile": "make",
    "Perl": "perl",
    "Lua": "lua",
    "PowerShell": "powershell",
    "Batch": "batch",
    "Diff / Patch": "diff",
    "Nginx": "nginx",
}

COMMENT_PREFIX = {
    "python": "#", "bash": "#", "yaml": "#", "toml": "#", "ini": ";", "perl": "#", "docker": "#",
    "make": "#", "powershell": "#", "nginx": "#", "teraterm": ";", "lua": "--", "sql": "--",
    "c": "//", "cpp": "//", "csharp": "//", "java": "//", "go": "//", "rust": "//",
    "javascript": "//", "typescript": "//", "batch": "REM ", "": "#", "log": "#",
}

_FILENAME_LANG = {
    ".log": "log", ".ttl": "teraterm", ".conf": "ini", ".cfg": "ini", ".service": "ini",
    ".env": "bash", ".bashrc": "bash", ".profile": "bash", ".zshrc": "bash",
}


def detect_language(name: str, text: str = "") -> str:
    """Return a pygments alias (or "" / "log") for a file name and its first line."""
    base = posixpath.basename(name or "").lower()
    suffix = posixpath.splitext(base)[1]
    if base in _FILENAME_LANG:
        return _FILENAME_LANG[base]
    if suffix in _FILENAME_LANG:
        return _FILENAME_LANG[suffix]
    if base in ("dockerfile", "containerfile"):
        return "docker"
    if base in ("makefile", "gnumakefile"):
        return "make"
    first = text.split("\n", 1)[0] if text else ""
    if first.startswith("#!"):
        if "python" in first:
            return "python"
        if any(sh in first for sh in ("bash", "/sh", "zsh", "ksh")):
            return "bash"
        if "perl" in first:
            return "perl"
    if suffix in (".txt", ""):
        return ""
    try:
        from pygments.lexers import get_lexer_for_filename
        from pygments.util import ClassNotFound

        try:
            lexer = get_lexer_for_filename(base)
            return lexer.aliases[0] if lexer.aliases else ""
        except ClassNotFound:
            return ""
    except Exception:
        return ""


def language_label(alias: str) -> str:
    for label, value in LANGUAGES.items():
        if value == alias:
            return label
    try:
        from pygments.lexers import get_lexer_by_name

        return get_lexer_by_name(alias).name
    except Exception:
        return alias or "Plain Text"


# --------------------------------------------------------------------------------------------
# Syntax highlighting
# --------------------------------------------------------------------------------------------

_TOKEN_COLORS: list[tuple[str, str, bool, bool]] = [
    # (pygments token path, colour, bold, italic) - most specific first
    ("Comment", "#7f8c98", False, True),
    ("Keyword.Constant", "#d19a66", False, False),
    ("Keyword.Type", "#e5c07b", False, False),
    ("Keyword", "#c678dd", True, False),
    ("Name.Builtin", "#e5c07b", False, False),
    ("Name.Function", "#61afef", False, False),
    ("Name.Class", "#e5c07b", True, False),
    ("Name.Decorator", "#d19a66", False, False),
    ("Name.Tag", "#e06c75", False, False),
    ("Name.Attribute", "#d19a66", False, False),
    ("Name.Variable", "#e06c75", False, False),
    ("Name.Exception", "#e5c07b", False, False),
    ("Name.Namespace", "#e5c07b", False, False),
    ("Name.Label", "#61afef", False, False),
    ("Name.Entity", "#d19a66", False, False),
    ("Literal.String.Doc", "#7f8c98", False, True),
    ("Literal.String.Escape", "#56b6c2", False, False),
    ("Literal.String.Interpol", "#56b6c2", False, False),
    ("Literal.String", "#98c379", False, False),
    ("Literal.Number", "#d19a66", False, False),
    ("Literal", "#98c379", False, False),
    ("Operator.Word", "#c678dd", True, False),
    ("Operator", "#56b6c2", False, False),
    ("Generic.Heading", "#61afef", True, False),
    ("Generic.Subheading", "#61afef", True, False),
    ("Generic.Inserted", "#98c379", False, False),
    ("Generic.Deleted", "#e06c75", False, False),
    ("Generic.Emph", "#d7dde5", False, True),
    ("Generic.Strong", "#d7dde5", True, False),
    ("Generic.Prompt", "#7ee787", True, False),
    ("Error", "#ff7b72", False, False),
]


def _make_format(color: str, bold: bool = False, italic: bool = False, underline: bool = False) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(color))
    if bold:
        fmt.setFontWeight(QFont.Weight.Bold)
    if italic:
        fmt.setFontItalic(True)
    if underline:
        fmt.setFontUnderline(True)
    return fmt


class CodeHighlighter(QSyntaxHighlighter):
    """Lexes the whole document with Pygments (debounced) and paints per block.

    Lexing the full text, rather than line by line, keeps multi-line strings and comments right.
    """

    def __init__(self, document: QTextDocument, language: str = ""):
        super().__init__(document)
        self._language = ""
        self._lexer = None
        self._block_formats: dict[int, list[tuple[int, int, QTextCharFormat]]] = {}
        self._format_cache: dict[object, QTextCharFormat | None] = {}
        self._log_formats = {
            tag: _make_format(color, bold, False, underline) for tag, (color, bold, underline) in LOG_FORMATS.items()
        }
        self._busy = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(180)
        self._timer.timeout.connect(self.relex)
        document.contentsChange.connect(self._on_change)
        self.set_language(language)

    @property
    def language(self) -> str:
        return self._language

    def set_language(self, alias: str) -> None:
        self._language = alias or ""
        self._lexer = None
        self._format_cache.clear()
        if self._language and self._language != "log":
            try:
                from pygments.lexers import get_lexer_by_name

                self._lexer = get_lexer_by_name(self._language, stripnl=False, stripall=False, ensurenl=False)
            except Exception:
                self._lexer = None
                self._language = ""
        self.relex()

    def _on_change(self, _pos: int, removed: int, added: int) -> None:
        if self._busy or (removed == 0 and added == 0):
            return
        if self._lexer is not None:
            self._timer.start()

    def _format_for(self, ttype) -> QTextCharFormat | None:
        if ttype in self._format_cache:
            return self._format_cache[ttype]
        name = str(ttype).replace("Token.", "", 1)
        result = None
        for path, color, bold, italic in _TOKEN_COLORS:
            if name == path or name.startswith(path + "."):
                result = _make_format(color, bold, italic)
                break
        self._format_cache[ttype] = result
        return result

    def relex(self) -> None:
        formats: dict[int, list[tuple[int, int, QTextCharFormat]]] = {}
        text = self.document().toPlainText()
        if self._lexer is not None and len(text) <= MAX_HIGHLIGHT_CHARS:
            try:
                line = col = 0
                for ttype, value in self._lexer.get_tokens(text):
                    fmt = self._format_for(ttype)
                    parts = value.split("\n")
                    for index, part in enumerate(parts):
                        if index:
                            line += 1
                            col = 0
                        if part and fmt is not None:
                            formats.setdefault(line, []).append((col, len(part), fmt))
                        col += len(part)
            except Exception:
                formats = {}
        self._block_formats = formats
        self._busy = True
        try:
            self.rehighlight()
        finally:
            self._busy = False

    def highlightBlock(self, text: str) -> None:
        if self._language == "log":
            if len(text) < 4000:
                for start, end, tag in highlight_spans(text):
                    self.setFormat(start, end - start, self._log_formats[tag])
            return
        for start, length, fmt in self._block_formats.get(self.currentBlock().blockNumber(), ()):
            self.setFormat(start, length, fmt)


# --------------------------------------------------------------------------------------------
# Editor widget
# --------------------------------------------------------------------------------------------

class _LineNumberArea(QWidget):
    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, event) -> None:
        self.editor.paint_line_numbers(event)


class CodeEditor(QPlainTextEdit):
    cursor_moved = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CodeEditor")
        self.comment_prefix = "#"
        self._gutter = _LineNumberArea(self)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        for family in ("JetBrains Mono", "Cascadia Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono"):
            if family in QFontDatabase.families():
                font = QFont(family)
                break
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        font.setPointSize(12)
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self.cursorPositionChanged.connect(self.cursor_moved)
        self._update_gutter_width()
        self._highlight_current_line()

    # --- gutter ---------------------------------------------------------
    def line_number_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 18 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter_width(self, *_args) -> None:
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _update_gutter(self, rect: QRect, dy: int) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(), self.line_number_width(), cr.height()))

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self._gutter)
        painter.fillRect(event.rect(), QColor("#0b0f15"))
        painter.setPen(QColor(TOKENS["line"]))
        painter.drawLine(self._gutter.width() - 1, event.rect().top(), self._gutter.width() - 1, event.rect().bottom())
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        current = self.textCursor().blockNumber()
        height = self.fontMetrics().height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor("#d7dde5" if number == current else "#4b5563"))
                painter.drawText(
                    0, top, self._gutter.width() - 10, height, Qt.AlignmentFlag.AlignRight, str(number + 1)
                )
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1
        painter.end()

    def _highlight_current_line(self) -> None:
        if self.isReadOnly():
            self.setExtraSelections([])
            return
        selection = QTextEdit.ExtraSelection()
        selection.format.setBackground(QColor("#161c26"))
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.cursor = self.textCursor()
        selection.cursor.clearSelection()
        self.setExtraSelections([selection])
        self._gutter.update()

    # --- editing helpers -------------------------------------------------
    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers():
            cursor = self.textCursor()
            line = cursor.block().text()
            indent = line[: len(line) - len(line.lstrip(" \t"))]
            before = line[: cursor.positionInBlock()].rstrip()
            if before.endswith((":", "{", "(", "[")) and self.comment_prefix in ("#", "//"):
                indent += "\t" if indent.startswith("\t") else "    "
            super().keyPressEvent(event)
            self.insertPlainText(indent)
            return
        if key == Qt.Key.Key_Tab and not event.modifiers():
            cursor = self.textCursor()
            if cursor.hasSelection():
                self._indent_selection(+1)
                return
        if key == Qt.Key.Key_Backtab:
            self._indent_selection(-1)
            return
        super().keyPressEvent(event)

    def _selected_blocks(self):
        cursor = self.textCursor()
        doc = self.document()
        start = doc.findBlock(cursor.selectionStart())
        end = doc.findBlock(cursor.selectionEnd())
        if cursor.hasSelection() and cursor.selectionEnd() == end.position() and end != start:
            end = end.previous()
        blocks = []
        block = start
        while block.isValid():
            blocks.append(block)
            if block == end:
                break
            block = block.next()
        return blocks

    def _indent_selection(self, direction: int) -> None:
        cursor = self.textCursor()
        cursor.beginEditBlock()
        for block in self._selected_blocks():
            c = QTextCursor(block)
            if direction > 0:
                c.insertText("    ")
            else:
                text = block.text()
                remove = 1 if text.startswith("\t") else min(4, len(text) - len(text.lstrip(" ")))
                for _ in range(remove):
                    c.deleteChar()
        cursor.endEditBlock()

    def toggle_comment(self) -> None:
        prefix = self.comment_prefix or "#"
        blocks = [b for b in self._selected_blocks()]
        non_empty = [b for b in blocks if b.text().strip()]
        if not non_empty:
            return
        uncomment = all(b.text().lstrip().startswith(prefix) for b in non_empty)
        cursor = self.textCursor()
        cursor.beginEditBlock()
        for block in non_empty:
            text = block.text()
            indent = len(text) - len(text.lstrip())
            c = QTextCursor(block)
            c.setPosition(block.position() + indent)
            if uncomment:
                strip = len(prefix) + (1 if text[indent + len(prefix): indent + len(prefix) + 1] == " " else 0)
                c.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, strip)
                c.removeSelectedText()
            else:
                c.insertText(prefix + ("" if prefix.endswith(" ") else " "))
        cursor.endEditBlock()

    def duplicate_line(self) -> None:
        cursor = self.textCursor()
        if cursor.hasSelection():
            text = cursor.selectedText().replace(" ", "\n")
            pos = cursor.selectionEnd()
            cursor.setPosition(pos)
            cursor.insertText(text)
            return
        block = cursor.block()
        c = QTextCursor(block)
        c.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        c.insertText("\n" + block.text())

    def zoom(self, delta: int) -> None:
        font = self.font()
        font.setPointSize(max(6, min(40, font.pointSize() + delta)))
        self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self._update_gutter_width()

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)


# --------------------------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------------------------

@dataclass(eq=False)
class EditorDocument:
    title: str
    local_path: str | None = None          # local file
    remote_path: str | None = None         # remote file (SFTP)
    remote_label: str = ""                 # e.g. "SSH 10.0.0.1"
    get_sftp: Callable[[], object] | None = None
    encoding: str = "utf-8"
    bom: bool = False
    eol: str = "\n"
    on_saved: Callable[["EditorDocument"], None] | None = None
    editor: CodeEditor | None = field(default=None, repr=False)
    highlighter: CodeHighlighter | None = field(default=None, repr=False)

    @property
    def is_remote(self) -> bool:
        return self.remote_path is not None

    @property
    def display_path(self) -> str:
        if self.is_remote:
            return f"{self.remote_label}:{self.remote_path}" if self.remote_label else self.remote_path
        return self.local_path or self.title


def decode_bytes(raw: bytes) -> tuple[str, str, bool, str]:
    """Return ``(text_with_lf, encoding, has_bom, eol)``; raises ValueError for binary data."""
    if b"\x00" in raw[:8192] and not raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        raise ValueError("This looks like a binary file and cannot be edited as text.")
    bom = False
    if raw.startswith(codecs.BOM_UTF8):
        text, encoding, bom = raw[len(codecs.BOM_UTF8):].decode("utf-8", errors="replace"), "utf-8", True
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text, encoding, bom = raw.decode("utf-16"), "utf-16", True
    else:
        try:
            text, encoding = raw.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            text, encoding = raw.decode("latin-1"), "latin-1"
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    eol = "\r\n" if crlf > lf else "\n"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, encoding, bom, eol


def encode_text(text: str, encoding: str, bom: bool, eol: str) -> bytes:
    if eol != "\n":
        text = text.replace("\n", eol)
    if encoding == "utf-16":
        return text.encode("utf-16")  # always written with a BOM
    data = text.encode(encoding, errors="replace")
    if bom and encoding == "utf-8":
        data = codecs.BOM_UTF8 + data
    return data


class _Bridge(QObject):
    call = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(lambda fn: fn())


# --------------------------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------------------------

class TextEditorWindow(QMainWindow):
    def __init__(self, app=None, settings=None):
        super().__init__(None)
        self.app = app
        self.settings = settings
        self.setWindowTitle("MMTE Editor")
        self.resize(1080, 720)
        self.setAcceptDrops(True)
        self._bridge = _Bridge()
        self.documents: list[EditorDocument] = []

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("EditorTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setMovable(True)
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs, 1)
        self._build_find_bar(layout)
        self.setCentralWidget(central)

        self._build_menus()
        self._build_status_bar()
        self._update_actions()

    # --- thread helper ---------------------------------------------------
    def _ui(self, fn: Callable[[], None]) -> None:
        self._bridge.call.emit(fn)

    # --- construction ------------------------------------------------------
    def _build_menus(self) -> None:
        bar = self.menuBar()
        bar.setNativeMenuBar(False)

        def act(menu, text, slot, shortcut=None, checkable=False):
            action = QAction(text, self)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            action.setCheckable(checkable)
            action.triggered.connect(slot)
            menu.addAction(action)
            return action

        file_menu = bar.addMenu("&File")
        act(file_menu, "New", self.new_document, QKeySequence.StandardKey.New)
        act(file_menu, "Open...", self.open_dialog, QKeySequence.StandardKey.Open)
        file_menu.addSeparator()
        self.save_action = act(file_menu, "Save", self.save_current, QKeySequence.StandardKey.Save)
        self.save_as_action = act(file_menu, "Save As (local copy)...", self.save_current_as, "Ctrl+Shift+S")
        self.save_all_action = act(file_menu, "Save All", self.save_all, "Ctrl+Alt+S")
        self.reload_action = act(file_menu, "Reload from Disk / Server", self.reload_current)
        file_menu.addSeparator()
        self.close_action = act(file_menu, "Close Tab", lambda: self.close_tab(self.tabs.currentIndex()), "Ctrl+W")
        act(file_menu, "Exit Editor", self.close)

        edit_menu = bar.addMenu("&Edit")
        act(edit_menu, "Undo", lambda: self._editor_call("undo"), QKeySequence.StandardKey.Undo)
        act(edit_menu, "Redo", lambda: self._editor_call("redo"), "Ctrl+Y")
        edit_menu.addSeparator()
        act(edit_menu, "Cut", lambda: self._editor_call("cut"), QKeySequence.StandardKey.Cut)
        act(edit_menu, "Copy", lambda: self._editor_call("copy"), QKeySequence.StandardKey.Copy)
        act(edit_menu, "Paste", lambda: self._editor_call("paste"), QKeySequence.StandardKey.Paste)
        act(edit_menu, "Select All", lambda: self._editor_call("selectAll"), QKeySequence.StandardKey.SelectAll)
        edit_menu.addSeparator()
        act(edit_menu, "Duplicate Line", lambda: self._editor_call("duplicate_line"), "Ctrl+D")
        act(edit_menu, "Toggle Comment", lambda: self._editor_call("toggle_comment"), "Ctrl+/")
        edit_menu.addSeparator()
        eol_menu = edit_menu.addMenu("Line Endings")
        act(eol_menu, "Unix (LF)", lambda: self._set_eol("\n"))
        act(eol_menu, "Windows (CR LF)", lambda: self._set_eol("\r\n"))

        search_menu = bar.addMenu("&Search")
        act(search_menu, "Find...", lambda: self.show_find(False), QKeySequence.StandardKey.Find)
        act(search_menu, "Replace...", lambda: self.show_find(True), "Ctrl+H")
        act(search_menu, "Find Next", lambda: self.find(forward=True), "F3")
        act(search_menu, "Find Previous", lambda: self.find(forward=False), "Shift+F3")
        act(search_menu, "Go to Line...", self.goto_line, "Ctrl+G")

        view_menu = bar.addMenu("&View")
        self.wrap_action = act(view_menu, "Word Wrap", self._toggle_wrap, checkable=True)
        self.wrap_action.setChecked(bool(self.settings.get("editor_word_wrap", False)) if self.settings else False)
        self.whitespace_action = act(view_menu, "Show Whitespace", self._toggle_whitespace, checkable=True)
        view_menu.addSeparator()
        act(view_menu, "Zoom In", lambda: self._zoom(1), QKeySequence.StandardKey.ZoomIn)
        act(view_menu, "Zoom Out", lambda: self._zoom(-1), QKeySequence.StandardKey.ZoomOut)
        act(view_menu, "Reset Zoom", lambda: self._zoom(0), "Ctrl+0")

        lang_menu = bar.addMenu("&Language")
        for label, alias in LANGUAGES.items():
            act(lang_menu, label, lambda _c=False, a=alias: self.set_language(a))

    def _build_find_bar(self, layout: QVBoxLayout) -> None:
        self.find_bar = QFrame()
        self.find_bar.setObjectName("FindBar")
        grid = QVBoxLayout(self.find_bar)
        grid.setContentsMargins(10, 6, 10, 6)
        grid.setSpacing(6)

        row1 = QHBoxLayout()
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Find")
        self.find_edit.returnPressed.connect(lambda: self.find(forward=True))
        self.find_edit.textChanged.connect(self._update_match_count)
        row1.addWidget(self.find_edit, 1)
        prev_btn = QPushButton("Previous")
        prev_btn.clicked.connect(lambda: self.find(forward=False))
        row1.addWidget(prev_btn)
        next_btn = QPushButton("Next")
        next_btn.setObjectName("AccentBtn")
        next_btn.clicked.connect(lambda: self.find(forward=True))
        row1.addWidget(next_btn)
        self.case_cb = QCheckBox("Match case")
        self.word_cb = QCheckBox("Whole word")
        self.regex_cb = QCheckBox("Regex")
        for cb in (self.case_cb, self.word_cb, self.regex_cb):
            cb.toggled.connect(self._update_match_count)
            row1.addWidget(cb)
        self.match_label = QLabel("")
        self.match_label.setObjectName("Hint")
        self.match_label.setMinimumWidth(90)
        row1.addWidget(self.match_label)
        close_btn = CloseButton(tooltip="Close find bar")
        close_btn.clicked.connect(self.hide_find)
        row1.addWidget(close_btn)
        grid.addLayout(row1)

        self.replace_row = QWidget()
        row2 = QHBoxLayout(self.replace_row)
        row2.setContentsMargins(0, 0, 0, 0)
        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText("Replace with")
        self.replace_edit.returnPressed.connect(self.replace_one)
        row2.addWidget(self.replace_edit, 1)
        rep_btn = QPushButton("Replace")
        rep_btn.clicked.connect(self.replace_one)
        row2.addWidget(rep_btn)
        rep_all_btn = QPushButton("Replace All")
        rep_all_btn.clicked.connect(self.replace_all)
        row2.addWidget(rep_all_btn)
        grid.addWidget(self.replace_row)

        self.find_bar.hide()
        layout.addWidget(self.find_bar)

    def _build_status_bar(self) -> None:
        status = self.statusBar()
        self.path_label = QLabel("")
        self.pos_label = QLabel("")
        self.lang_label = QLabel("")
        self.eol_label = QLabel("")
        self.enc_label = QLabel("")
        status.addWidget(self.path_label, 1)
        for lbl in (self.pos_label, self.lang_label, self.eol_label, self.enc_label):
            status.addPermanentWidget(lbl)

    # --- document management ----------------------------------------------
    def current_document(self) -> EditorDocument | None:
        widget = self.tabs.currentWidget()
        for doc in self.documents:
            if doc.editor is widget:
                return doc
        return None

    def _editor_call(self, name: str) -> None:
        doc = self.current_document()
        if doc and doc.editor:
            getattr(doc.editor, name)()

    def _add_document(self, doc: EditorDocument, text: str, language: str | None = None) -> EditorDocument:
        editor = CodeEditor()
        editor.setPlainText(text)
        editor.document().setModified(False)
        editor.moveCursor(QTextCursor.MoveOperation.Start)
        if self.settings and self.settings.get("editor_word_wrap", False):
            editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        if self.settings:
            size = int(self.settings.get("editor_font_size", 12) or 12)
            font = editor.font()
            font.setPointSize(size)
            editor.setFont(font)
        doc.editor = editor
        lang = detect_language(doc.remote_path or doc.local_path or doc.title, text) if language is None else language
        doc.highlighter = CodeHighlighter(editor.document(), lang)
        editor.comment_prefix = COMMENT_PREFIX.get(doc.highlighter.language, "#")
        editor.document().modificationChanged.connect(lambda _m, d=doc: self._refresh_tab(d))
        editor.cursor_moved.connect(self._update_status)
        self.documents.append(doc)
        index = self.tabs.addTab(editor, doc.title)
        button = CloseButton(tooltip=f"Close {doc.title}")
        button.clicked.connect(lambda _c=False, d=doc: self.close_document(d))
        self.tabs.tabBar().setTabButton(index, QTabBar.ButtonPosition.RightSide, button)
        self.tabs.setCurrentIndex(index)
        self._refresh_tab(doc)
        self._apply_whitespace(editor)
        editor.setFocus()
        self._update_actions()
        return doc

    def _find_existing(self, local_path: str | None = None, remote_path: str | None = None, label: str = ""):
        for doc in self.documents:
            if local_path and doc.local_path and Path(doc.local_path).resolve() == Path(local_path).resolve():
                return doc
            if remote_path and doc.remote_path == remote_path and doc.remote_label == label:
                return doc
        return None

    def new_document(self) -> EditorDocument:
        count = sum(1 for d in self.documents if d.title.startswith("new ")) + 1
        return self._add_document(EditorDocument(title=f"new {count}"), "", language="")

    def open_text(self, title: str, text: str, language: str | None = None, suggested_path: str | None = None):
        """Open in-memory text (e.g. a generated macro). Saving asks for a location."""
        doc = EditorDocument(title=title)
        doc._suggested_path = suggested_path  # type: ignore[attr-defined]
        doc = self._add_document(doc, text, language=language)
        doc.editor.document().setModified(True)
        self._refresh_tab(doc)
        self.show_and_raise()
        return doc

    def open_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Open file(s)")
        for path in paths:
            self.open_local(path)

    def open_local(self, path: str) -> EditorDocument | None:
        existing = self._find_existing(local_path=path)
        if existing:
            self.tabs.setCurrentWidget(existing.editor)
            self.show_and_raise()
            return existing
        try:
            p = Path(path)
            if p.exists() and p.stat().st_size > MAX_FILE_BYTES:
                raise ValueError(f"File is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
            raw = p.read_bytes() if p.exists() else b""
            text, encoding, bom, eol = decode_bytes(raw)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot open file", f"{path}\n\n{exc}")
            return None
        doc = EditorDocument(title=Path(path).name, local_path=str(path), encoding=encoding, bom=bom, eol=eol)
        self.show_and_raise()
        return self._add_document(doc, text)

    def open_remote(
        self,
        get_sftp: Callable[[], object],
        remote_path: str,
        label: str = "",
        on_saved: Callable[[EditorDocument], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        create_if_missing: bool = False,
    ) -> None:
        """Download ``remote_path`` over SFTP in the background, then open it in a tab."""
        existing = self._find_existing(remote_path=remote_path, label=label)
        if existing:
            self.tabs.setCurrentWidget(existing.editor)
            self.show_and_raise()
            return
        self.statusBar().showMessage(f"Opening {remote_path} ...", 5000)

        def worker():
            try:
                sftp = get_sftp()
                try:
                    attrs = sftp.stat(remote_path)
                    exists = True
                except FileNotFoundError:
                    exists = False
                except IOError as exc:
                    if getattr(exc, "errno", None) == 2:
                        exists = False
                    else:
                        raise
                if not exists:
                    if not create_if_missing:
                        raise FileNotFoundError(f"No such file: {remote_path}")
                    raw = b""
                else:
                    import stat as _stat

                    if _stat.S_ISDIR(attrs.st_mode or 0):
                        raise IsADirectoryError(f"{remote_path} is a directory")
                    if (attrs.st_size or 0) > MAX_FILE_BYTES:
                        raise ValueError(f"File is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
                    with sftp.open(remote_path, "rb") as fh:
                        raw = fh.read()
                text, encoding, bom, eol = decode_bytes(raw)
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__

                def fail():
                    self.statusBar().showMessage(f"Open failed: {message}", 8000)
                    if on_error:
                        on_error(message)
                    else:
                        QMessageBox.warning(self, "Cannot open remote file", f"{remote_path}\n\n{message}")

                self._ui(fail)
                return

            def done():
                doc = EditorDocument(
                    title=posixpath.basename(remote_path) or remote_path,
                    remote_path=remote_path,
                    remote_label=label,
                    get_sftp=get_sftp,
                    encoding=encoding,
                    bom=bom,
                    eol=eol,
                    on_saved=on_saved,
                )
                self._add_document(doc, text)
                if not exists:
                    self.statusBar().showMessage(f"New file - it will be created on {label or 'the server'} when you save.", 8000)
                self.show_and_raise()

            self._ui(done)

        threading.Thread(target=worker, daemon=True).start()

    def show_and_raise(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    # --- saving -----------------------------------------------------------
    def save_current(self) -> bool:
        doc = self.current_document()
        return self.save_document(doc) if doc else False

    def save_all(self) -> None:
        for doc in list(self.documents):
            if doc.editor and doc.editor.document().isModified():
                self.save_document(doc)

    def save_document(self, doc: EditorDocument, wait: bool = False) -> bool:
        if doc.editor is None:
            return False
        if doc.is_remote:
            return self._save_remote(doc, wait=wait)
        if not doc.local_path:
            return self.save_document_as(doc)
        try:
            Path(doc.local_path).write_bytes(encode_text(doc.editor.toPlainText(), doc.encoding, doc.bom, doc.eol))
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", f"{doc.local_path}\n\n{exc}")
            return False
        doc.editor.document().setModified(False)
        self.statusBar().showMessage(f"Saved {doc.local_path}", 4000)
        if doc.on_saved:
            doc.on_saved(doc)
        return True

    def _save_remote(self, doc: EditorDocument, wait: bool = False) -> bool:
        data = encode_text(doc.editor.toPlainText(), doc.encoding, doc.bom, doc.eol)
        revision = doc.editor.document().revision()
        self.statusBar().showMessage(f"Saving {doc.display_path} ...")
        outcome = {"ok": False}
        finished = threading.Event()

        def worker():
            try:
                sftp = doc.get_sftp()
                with sftp.open(doc.remote_path, "wb") as fh:
                    fh.write(data)
                outcome["ok"] = True
                error = None
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
            finished.set()

            def done():
                if doc.editor is None:
                    return
                if error:
                    self.statusBar().showMessage(f"Save failed: {error}", 8000)
                    QMessageBox.critical(self, "Save failed", f"Could not write {doc.display_path}\n\n{error}")
                    return
                if doc.editor.document().revision() == revision:
                    doc.editor.document().setModified(False)
                self.statusBar().showMessage(f"Saved {doc.display_path}", 4000)
                if doc.on_saved:
                    doc.on_saved(doc)

            self._ui(done)

        threading.Thread(target=worker, daemon=True).start()
        if wait:
            loop_deadline = 30.0
            import time as _time

            start = _time.monotonic()
            while not finished.is_set() and _time.monotonic() - start < loop_deadline:
                QApplication.processEvents()
                finished.wait(0.02)
            QApplication.processEvents()
            return outcome["ok"]
        return True

    def save_current_as(self) -> bool:
        doc = self.current_document()
        return self.save_document_as(doc) if doc else False

    def save_document_as(self, doc: EditorDocument) -> bool:
        suggested = getattr(doc, "_suggested_path", None) or doc.local_path or doc.title
        path, _ = QFileDialog.getSaveFileName(self, "Save As", str(suggested))
        if not path:
            return False
        if doc.is_remote:
            # Keep the remote document untouched; write a local copy.
            try:
                Path(path).write_bytes(encode_text(doc.editor.toPlainText(), doc.encoding, doc.bom, doc.eol))
                self.statusBar().showMessage(f"Saved a local copy to {path}", 5000)
                return True
            except Exception as exc:
                QMessageBox.critical(self, "Save failed", f"{path}\n\n{exc}")
                return False
        doc.local_path = path
        doc.title = Path(path).name
        if doc.highlighter and not doc.highlighter.language:
            doc.highlighter.set_language(detect_language(path, doc.editor.toPlainText()))
            doc.editor.comment_prefix = COMMENT_PREFIX.get(doc.highlighter.language, "#")
        ok = self.save_document(doc)
        self._refresh_tab(doc)
        return ok

    def reload_current(self) -> None:
        doc = self.current_document()
        if not doc:
            return
        if doc.editor.document().isModified():
            if QMessageBox.question(self, "Reload", "Discard your unsaved changes and reload?") != QMessageBox.StandardButton.Yes:
                return
        if doc.is_remote:
            remote, label, get_sftp, on_saved = doc.remote_path, doc.remote_label, doc.get_sftp, doc.on_saved
            self._remove_document(doc)
            self.open_remote(get_sftp, remote, label, on_saved=on_saved, create_if_missing=True)
        elif doc.local_path:
            path = doc.local_path
            self._remove_document(doc)
            self.open_local(path)

    # --- closing ------------------------------------------------------------
    def _confirm_discard(self, doc: EditorDocument) -> bool:
        if not doc.editor or not doc.editor.document().isModified():
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"Save changes to '{doc.title}'?")
        box.setStandardButtons(
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel
        )
        choice = box.exec()
        if choice == QMessageBox.StandardButton.Save:
            return self.save_document(doc, wait=True)
        return choice == QMessageBox.StandardButton.Discard

    def close_document(self, doc: EditorDocument) -> bool:
        if not self._confirm_discard(doc):
            return False
        self._remove_document(doc)
        return True

    def close_tab(self, index: int) -> None:
        widget = self.tabs.widget(index)
        for doc in self.documents:
            if doc.editor is widget:
                self.close_document(doc)
                return

    def _remove_document(self, doc: EditorDocument) -> None:
        if doc in self.documents:
            self.documents.remove(doc)
        if doc.editor is not None:
            index = self.tabs.indexOf(doc.editor)
            if index >= 0:
                self.tabs.removeTab(index)
            doc.editor.deleteLater()
        doc.editor = None
        self._update_actions()
        self._update_status()

    def closeEvent(self, event) -> None:
        for doc in list(self.documents):
            if not self._confirm_discard(doc):
                event.ignore()
                return
        for doc in list(self.documents):
            self._remove_document(doc)
        event.accept()

    # --- find / replace --------------------------------------------------------
    def show_find(self, replace: bool) -> None:
        self.find_bar.show()
        self.replace_row.setVisible(replace)
        doc = self.current_document()
        if doc and doc.editor and doc.editor.textCursor().hasSelection():
            selected = doc.editor.textCursor().selectedText()
            if " " not in selected:
                self.find_edit.setText(selected)
        self.find_edit.setFocus()
        self.find_edit.selectAll()
        self._update_match_count()

    def hide_find(self) -> None:
        self.find_bar.hide()
        doc = self.current_document()
        if doc and doc.editor:
            doc.editor.setFocus()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self.find_bar.isVisible():
            self.hide_find()
            return
        super().keyPressEvent(event)

    def _pattern(self) -> re.Pattern | None:
        needle = self.find_edit.text()
        if not needle:
            return None
        source = needle if self.regex_cb.isChecked() else re.escape(needle)
        if self.word_cb.isChecked():
            source = rf"\b(?:{source})\b"
        try:
            return re.compile(source, 0 if self.case_cb.isChecked() else re.IGNORECASE)
        except re.error:
            return None

    def _update_match_count(self, *_args) -> None:
        doc = self.current_document()
        pattern = self._pattern()
        if not doc or not doc.editor or pattern is None:
            self.match_label.setText("" if self.find_edit.text() == "" or pattern else "bad regex")
            return
        count = sum(1 for m in pattern.finditer(doc.editor.toPlainText()) if m.end() > m.start())
        self.match_label.setText(f"{count} match{'es' if count != 1 else ''}")

    def find(self, forward: bool = True) -> bool:
        doc = self.current_document()
        pattern = self._pattern()
        if not doc or not doc.editor or pattern is None:
            if not self.find_bar.isVisible():
                self.show_find(False)
            return False
        editor = doc.editor
        text = editor.toPlainText()
        cursor = editor.textCursor()
        matches = [m for m in pattern.finditer(text) if m.end() > m.start()]
        if not matches:
            self.statusBar().showMessage("No matches", 3000)
            return False
        if forward:
            pos = cursor.selectionEnd() if cursor.hasSelection() else cursor.position()
            match = next((m for m in matches if m.start() >= pos), matches[0])
        else:
            pos = cursor.selectionStart() if cursor.hasSelection() else cursor.position()
            match = next((m for m in reversed(matches) if m.end() <= pos), matches[-1])
        c = editor.textCursor()
        c.setPosition(match.start())
        c.setPosition(match.end(), QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(c)
        editor.centerCursor()
        return True

    def replace_one(self) -> None:
        doc = self.current_document()
        pattern = self._pattern()
        if not doc or not doc.editor or pattern is None:
            return
        cursor = doc.editor.textCursor()
        selected = cursor.selectedText()
        match = pattern.fullmatch(selected) if selected else None
        if match:
            replacement = match.expand(self.replace_edit.text()) if self.regex_cb.isChecked() else self.replace_edit.text()
            cursor.insertText(replacement)
        self.find(forward=True)
        self._update_match_count()

    def replace_all(self) -> int:
        doc = self.current_document()
        pattern = self._pattern()
        if not doc or not doc.editor or pattern is None:
            return 0
        text = doc.editor.toPlainText()
        repl = self.replace_edit.text()
        if self.regex_cb.isChecked():
            new_text, count = pattern.subn(repl, text)
        else:
            new_text, count = pattern.subn(lambda _m: repl, text)
        if count:
            cursor = doc.editor.textCursor()
            cursor.beginEditBlock()
            cursor.select(QTextCursor.SelectionType.Document)
            cursor.insertText(new_text)
            cursor.endEditBlock()
        self.statusBar().showMessage(f"Replaced {count} occurrence{'s' if count != 1 else ''}", 4000)
        self._update_match_count()
        return count

    def goto_line(self) -> None:
        doc = self.current_document()
        if not doc or not doc.editor:
            return
        editor = doc.editor
        line, ok = QInputDialog.getInt(
            self, "Go to Line", f"Line (1 - {editor.blockCount()}):", editor.textCursor().blockNumber() + 1, 1,
            editor.blockCount(),
        )
        if ok:
            block = editor.document().findBlockByNumber(line - 1)
            cursor = editor.textCursor()
            cursor.setPosition(block.position())
            editor.setTextCursor(cursor)
            editor.centerCursor()
            editor.setFocus()

    # --- view ----------------------------------------------------------------
    def _toggle_wrap(self, checked: bool) -> None:
        mode = QPlainTextEdit.LineWrapMode.WidgetWidth if checked else QPlainTextEdit.LineWrapMode.NoWrap
        for doc in self.documents:
            if doc.editor:
                doc.editor.setLineWrapMode(mode)
        if self.settings:
            self.settings.set("editor_word_wrap", bool(checked))

    def _apply_whitespace(self, editor: CodeEditor) -> None:
        option = editor.document().defaultTextOption()
        flags = option.flags()
        if self.whitespace_action.isChecked():
            flags |= QTextOption.Flag.ShowTabsAndSpaces
        else:
            flags &= ~QTextOption.Flag.ShowTabsAndSpaces
        option.setFlags(flags)
        editor.document().setDefaultTextOption(option)

    def _toggle_whitespace(self, _checked: bool) -> None:
        for doc in self.documents:
            if doc.editor:
                self._apply_whitespace(doc.editor)

    def _zoom(self, delta: int) -> None:
        doc = self.current_document()
        if not doc or not doc.editor:
            return
        if delta == 0:
            font = doc.editor.font()
            font.setPointSize(12)
            doc.editor.setFont(font)
            doc.editor.zoom(0)
        else:
            doc.editor.zoom(delta)
        if self.settings:
            self.settings.set("editor_font_size", doc.editor.font().pointSize())

    def set_language(self, alias: str) -> None:
        doc = self.current_document()
        if doc and doc.highlighter:
            doc.highlighter.set_language(alias)
            doc.editor.comment_prefix = COMMENT_PREFIX.get(doc.highlighter.language, "#")
            self._update_status()

    def _set_eol(self, eol: str) -> None:
        doc = self.current_document()
        if doc and doc.eol != eol:
            doc.eol = eol
            doc.editor.document().setModified(True)
            self._update_status()

    # --- status -------------------------------------------------------------------
    def _refresh_tab(self, doc: EditorDocument) -> None:
        if doc.editor is None:
            return
        index = self.tabs.indexOf(doc.editor)
        if index < 0:
            return
        modified = doc.editor.document().isModified()
        self.tabs.setTabText(index, f"{doc.title} ●" if modified else doc.title)
        self.tabs.setTabToolTip(index, doc.display_path)
        if doc is self.current_document():
            self._update_title(doc)

    def _update_title(self, doc: EditorDocument | None) -> None:
        if doc is None:
            self.setWindowTitle("MMTE Editor")
            return
        star = "*" if doc.editor and doc.editor.document().isModified() else ""
        self.setWindowTitle(f"{star}{doc.display_path} - MMTE Editor")

    def _on_tab_changed(self, _index: int) -> None:
        self._update_status()
        self._update_actions()
        if self.find_bar.isVisible():
            self._update_match_count()

    def _update_actions(self) -> None:
        has_doc = self.current_document() is not None
        for action in (self.save_action, self.save_as_action, self.save_all_action, self.reload_action, self.close_action):
            action.setEnabled(has_doc)

    def _update_status(self) -> None:
        doc = self.current_document()
        self._update_title(doc)
        if not doc or not doc.editor:
            for lbl in (self.path_label, self.pos_label, self.lang_label, self.eol_label, self.enc_label):
                lbl.setText("")
            return
        cursor = doc.editor.textCursor()
        sel = len(cursor.selectedText())
        self.path_label.setText(("\U0001F310 " if doc.is_remote else "") + doc.display_path)
        self.pos_label.setText(
            f"Ln {cursor.blockNumber() + 1}, Col {cursor.positionInBlock() + 1}"
            + (f"  ({sel} selected)" if sel else "")
            + f"   {doc.editor.blockCount()} lines"
        )
        self.lang_label.setText(language_label(doc.highlighter.language if doc.highlighter else ""))
        self.eol_label.setText("Windows (CR LF)" if doc.eol == "\r\n" else "Unix (LF)")
        self.enc_label.setText(doc.encoding.upper() + (" BOM" if doc.bom else ""))

    # --- drag & drop ---------------------------------------------------------------
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            if url.toLocalFile():
                self.open_local(url.toLocalFile())
        event.acceptProposedAction()
