"""Turn raw terminal output into readable log text.

Remote shells send colour codes, cursor moves, "erase line" commands, window-title and hyperlink
sequences.  Written to a file as-is they look like garbage (``[1;32muart:~$ [m[8D[J``).  ``LogCleaner``
replays the stream onto a single line buffer, so what lands in the log is the text a person saw.
It keeps its state between calls, so an escape sequence split across two reads is still handled.
"""

from __future__ import annotations


class LogCleaner:
    def __init__(self) -> None:
        self._line: list[str] = []
        self._col = 0
        self._state = "text"   # text | esc | csi | osc | osc_esc | skip1
        self._params = ""

    def feed(self, data: str) -> str:
        """Return the completed lines (each ending in ``\\n``) found in ``data``."""
        out: list[str] = []
        for ch in data:
            st = self._state
            if st == "text":
                if ch == "\x1b":
                    self._state = "esc"
                elif ch == "\n":
                    out.append("".join(self._line).rstrip() + "\n")
                    self._line, self._col = [], 0
                elif ch == "\r":
                    self._col = 0
                elif ch in "\x08\x7f":
                    self._col = max(0, self._col - 1)
                elif ch == "\t":
                    self._put(ch)
                elif ch >= " ":
                    self._put(ch)
                # every other control character (BEL, NUL, SO/SI ...) is dropped
            elif st == "esc":
                if ch == "[":
                    self._state, self._params = "csi", ""
                elif ch == "]":
                    self._state = "osc"
                elif ch in "()*+#%":      # charset selection etc: one more byte follows
                    self._state = "skip1"
                else:                       # ESC 7, ESC 8, ESC =, ESC > ... single-char sequences
                    self._state = "text"
            elif st == "skip1":
                self._state = "text"
            elif st == "csi":
                if "\x40" <= ch <= "\x7e":
                    self._csi(ch, self._params)
                    self._state = "text"
                else:
                    self._params += ch
            elif st == "osc":
                if ch == "\x07":
                    self._state = "text"
                elif ch == "\x1b":
                    self._state = "osc_esc"
            elif st == "osc_esc":
                self._state = "text"      # ESC \ ends the OSC (anything else abandons it)
        return "".join(out)

    def flush(self) -> str:
        """Return whatever is left on the unfinished line."""
        text = "".join(self._line).rstrip()
        self._line, self._col = [], 0
        return text + "\n" if text else ""

    # --- helpers -------------------------------------------------------------
    def _put(self, ch: str) -> None:
        if self._col < len(self._line):
            self._line[self._col] = ch
        else:
            self._line.extend(" " * (self._col - len(self._line)))
            self._line.append(ch)
        self._col += 1

    def _csi(self, final: str, params: str) -> None:
        nums = [p for p in params.lstrip("?>").split(";")]
        n = int(nums[0]) if nums and nums[0].isdigit() else None
        if final == "D":                       # cursor left
            self._col = max(0, self._col - (n or 1))
        elif final == "C":                     # cursor right
            self._col += n or 1
        elif final == "G":                     # cursor to column
            self._col = max(0, (n or 1) - 1)
        elif final == "K":                     # erase in line
            mode = n or 0
            if mode == 0:
                del self._line[self._col:]
            elif mode == 2:
                self._line, self._col = [], 0
            elif mode == 1:
                for i in range(min(self._col, len(self._line))):
                    self._line[i] = " "
        elif final == "J" and (n or 0) in (0, 2, 3):
            if (n or 0) == 0:
                del self._line[self._col:]
        # colours (m), cursor position reports (n), modes (h/l) and everything else are ignored
