"""Record what a user types into a terminal and turn it into a replayable Python script.

The generated script uses the app's own automation API (``send``, ``sleep``, ``input`` ...), so it
runs from *Scripting > Run Script...* or as a row in the Multi-Script Suite.

The module also holds the small, GUI-free helpers used to work out which command was typed when
Enter is pressed (``extract_command``), whether the line was a password prompt
(``is_secret_prompt``) and whether the command opens a terminal editor (``parse_editor_command``).
"""

from __future__ import annotations

import datetime as dt
import re
import shlex
import time
from dataclasses import dataclass, field

# --------------------------------------------------------------------------------------------
# Command-line helpers
# --------------------------------------------------------------------------------------------

# A shell / CLI prompt ends with one of these characters followed by a space.
_PROMPT_RE = re.compile(r"^.*?[$#>%]\s+")
_SECRET_RE = re.compile(r"(pass(word|phrase|code)?|passwd|\bpin\b|secret|token)[^:]{0,40}:\s*$", re.IGNORECASE)

# Editor options that consume the next argument (so it is not mistaken for a file name).
_OPTIONS_WITH_VALUE = {"-c", "--cmd", "-u", "-U", "-i", "-T", "-s", "-w", "-W", "-S", "-q", "-t",
                       "--rcfile", "-Y", "--syntax", "--tabsize"}

EDITOR_COMMANDS = {"vi", "vim", "nvim", "nano", "view", "vim.tiny", "vim.basic", "pico", "edit", "sensible-editor"}


def extract_command(screen_line: str) -> str:
    """Strip the prompt from a rendered terminal line and return the typed command."""
    line = (screen_line or "").rstrip()
    match = _PROMPT_RE.match(line)
    if match:
        return line[match.end():].strip()
    return line.strip()


def is_secret_prompt(screen_line: str) -> bool:
    """True when the line looks like a password / passphrase prompt (input is not echoed)."""
    return bool(_SECRET_RE.search((screen_line or "").rstrip()))


def parse_editor_command(command: str) -> tuple[bool, list[str]] | None:
    """Return ``(uses_sudo, [files...])`` when ``command`` opens vi / vim / nano, else ``None``.

    Options (``-R``, ``+12``, ``-c cmd``) are skipped.  Commands with pipes, redirects or
    chained commands are left alone so they still run in the real terminal.
    """
    text = (command or "").strip()
    if not text or any(token in text for token in ("|", ";", "&&", "||", ">", "<", "`", "$(")):
        return None
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    if not parts:
        return None
    sudo = False
    if parts[0] == "sudo":
        sudo = True
        parts = parts[1:]
        while parts and parts[0].startswith("-"):
            flag = parts.pop(0)
            if flag in ("-u", "-g", "-C", "-p") and parts:
                parts.pop(0)
    if parts and parts[0] == "sudoedit":
        sudo, parts = True, ["vim"] + parts[1:]
    if not parts or parts[0].rsplit("/", 1)[-1] not in EDITOR_COMMANDS:
        return None
    files: list[str] = []
    skip_next = False
    for arg in parts[1:]:
        if skip_next:
            skip_next = False
        elif arg in _OPTIONS_WITH_VALUE:
            skip_next = True
        elif not arg.startswith(("-", "+")):
            files.append(arg)
    if not files:
        return None
    return sudo, files


# --------------------------------------------------------------------------------------------
# Recorder
# --------------------------------------------------------------------------------------------

CONTROL_KEY_NAMES = {
    "\x03": "Ctrl+C", "\x04": "Ctrl+D", "\x1a": "Ctrl+Z", "\x1c": "Ctrl+\\", "\x0c": "Ctrl+L",
}


@dataclass
class MacroStep:
    delay: float           # seconds since the previous step
    kind: str              # "line" | "secret" | "keys" | "note"
    text: str
    comment: str = ""


@dataclass
class MacroRecorder:
    session_name: str
    connection_label: str = "Disconnected"
    connect_hint: str = ""        # a commented connect_ssh(...) line, when known
    clock: object = time.monotonic
    steps: list[MacroStep] = field(default_factory=list)

    def __post_init__(self):
        self.started_at = self.clock()
        self.started_wall = dt.datetime.now()
        self._last = self.started_at

    # --- recording -----------------------------------------------------------
    def _delay(self) -> float:
        now = self.clock()
        delay = max(0.0, now - self._last)
        self._last = now
        return delay

    def record_line(self, text: str, secret: bool = False, prompt: str = "") -> None:
        if secret:
            self.steps.append(MacroStep(self._delay(), "secret", "", comment=prompt.strip()))
        else:
            self.steps.append(MacroStep(self._delay(), "line", text))

    def record_keys(self, keys: str) -> None:
        self.steps.append(MacroStep(self._delay(), "keys", keys, CONTROL_KEY_NAMES.get(keys, "")))

    def record_note(self, note: str) -> None:
        self.steps.append(MacroStep(self._delay(), "note", note))

    @property
    def command_count(self) -> int:
        return sum(1 for s in self.steps if s.kind in ("line", "secret", "keys"))

    def elapsed(self) -> float:
        return max(0.0, self.clock() - self.started_at)

    # --- output ----------------------------------------------------------------
    def to_python(self) -> str:
        total = self.elapsed()
        lines = [
            "# Macro recorded with Modern Multi Terminal Emulator",
            f"# Terminal : {self.session_name} ({self.connection_label})",
            f"# Recorded : {self.started_wall:%Y-%m-%d %H:%M:%S}  -  {self.command_count} command(s)  -  {total:.1f} s",
            "#",
            "# Replay: Scripting > Run Script... (or add it to the Multi-Script Suite).",
            "#   send(text)     types the text and presses Enter",
            "#   pause(seconds) waits as long as you did while recording",
            "# Tip: swap a pause(...) for wait(\"$ \") to continue as soon as the prompt appears.",
            "",
            "TIMING_SCALE = 1.0  # 0.5 = replay twice as fast, 0 = no pauses",
            "",
            "",
            "def pause(seconds):",
            "    if TIMING_SCALE > 0 and seconds > 0:",
            "        sleep(seconds * TIMING_SCALE)",
            "",
            "",
        ]
        if self.connect_hint:
            lines += [
                "# The terminal was already connected when recording started. To connect from the",
                "# script instead, uncomment the next line:",
                f"# {self.connect_hint}",
                "",
            ]
        lines.append('print("Replaying recorded macro ...")')
        for step in self.steps:
            delay = round(step.delay, 1)
            if delay >= 0.1:
                lines.append(f"pause({delay})")
            if step.kind == "line":
                lines.append(f"send({step.text!r})")
            elif step.kind == "secret":
                label = step.comment or "Password"
                lines.append(f"send(input({label!r}))  # hidden input - not stored in the macro")
            elif step.kind == "keys":
                note = f"  # {step.comment}" if step.comment else ""
                lines.append(f"send({step.text!r}, newline=False){note}")
            elif step.kind == "note":
                lines.append(f"# {step.text}")
        lines.append('print("Macro finished.")')
        return "\n".join(lines) + "\n"
