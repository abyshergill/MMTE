"""Qt-free helpers that decide *which* terminals receive AI-generated commands.

Everything here works on duck-typed "session" objects (anything with ``name``,
``connection`` and ``connected_label``), so it can be unit-tested without Qt.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

MAX_COMMANDS = 50
MAX_COMMAND_CHARS = 4000

_CURRENT_WORDS = {
    "", "current", "active", "selected", "this", "here",
    "current terminal", "active terminal", "selected terminal", "this terminal",
}
_ALL_WORDS = {
    "all", "all terminals", "every terminal", "each terminal", "all connected",
    "all active", "all active terminals", "all connected terminals", "*",
}
# Fence languages whose body is meant to be typed into a device/shell.
_SHELL_FENCE_LANGS = {
    "", "sh", "bash", "shell", "zsh", "cmd", "bat", "powershell", "pwsh", "ps1",
    "console", "terminal", "serial", "uart", "text-command",
}
_FENCE = re.compile(r"```[ \t]*([\w+.-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# --------------------------------------------------------------------------
# session helpers
# --------------------------------------------------------------------------

def is_connected(session: Any) -> bool:
    return bool(getattr(session, "connection", None))


def connection_summary(session: Any) -> str:
    """Short human label such as ``SSH 10.0.0.2`` or ``Not connected``."""
    if not is_connected(session):
        return "Not connected"
    label = str(getattr(session, "connected_label", "") or "").strip()
    return label if label and label.lower() != "disconnected" else "Connected"


def connected_sessions(sessions: Iterable[Any]) -> list[Any]:
    return [s for s in sessions if is_connected(s)]


def default_target(sessions: Iterable[Any], current: Any | None) -> Any | None:
    """Best terminal for "the terminal" when the user did not name one.

    * the selected tab, if it has a live connection;
    * otherwise the only connected tab (a very common real-life case: the user
      is looking at a blank tab while the device console is open next to it);
    * otherwise the selected tab (so the caller can explain it is not connected),
      or the first tab, or ``None`` when there are no tabs at all.
    """
    sessions = list(sessions)
    if current is not None and is_connected(current):
        return current
    live = connected_sessions(sessions)
    if len(live) == 1:
        return live[0]
    if current is not None:
        return current
    return sessions[0] if sessions else None


# --------------------------------------------------------------------------
# target resolution
# --------------------------------------------------------------------------

@dataclass
class TargetResolution:
    sessions: list[Any] = field(default_factory=list)       # connected, ready to receive input
    not_connected: list[Any] = field(default_factory=list)  # found, but no live connection
    missing: list[str] = field(default_factory=list)        # names that match no terminal
    note: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.sessions)


def _split_spec(spec: str) -> list[str]:
    return [p for p in re.split(r"\s*(?:,|;|&|\+|\band\b)\s*", spec or "", flags=re.IGNORECASE)]


def _match_name(name: str, sessions: list[Any]) -> Any | None:
    key = name.strip().lower()
    for s in sessions:
        if str(getattr(s, "name", "")).lower() == key:
            return s
    if key.isdigit():
        for s in sessions:
            if str(getattr(s, "name", "")).lower() == f"terminal {key}":
                return s
    return None


def resolve_targets(spec: str | None, sessions: Iterable[Any], current: Any | None = None) -> TargetResolution:
    """Turn a model/user supplied target (``"current"``, ``"all"``, ``"Terminal 1, Terminal 3"``)
    into concrete sessions."""
    sessions = list(sessions)
    spec = str(spec or "").strip()
    result = TargetResolution()
    wanted: list[Any] = []

    exact = _match_name(spec, sessions) if spec else None
    parts = [spec] if exact is not None else _split_spec(spec)
    for part in parts:
        key = part.strip().lower()
        if key in _ALL_WORDS:
            live = connected_sessions(sessions)
            wanted.extend(live)
            if not live:
                result.note = "No terminal is connected."
        elif key in _CURRENT_WORDS:
            target = default_target(sessions, current)
            if target is not None:
                wanted.append(target)
            else:
                result.note = "There are no terminal tabs."
        else:
            found = _match_name(part, sessions)
            if found is None:
                result.missing.append(part.strip())
            else:
                wanted.append(found)

    seen: set[int] = set()
    for s in wanted:
        if id(s) in seen:
            continue
        seen.add(id(s))
        (result.sessions if is_connected(s) else result.not_connected).append(s)
    return result


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def sanitize_command(command: str) -> str:
    """One line, no control characters (an embedded ESC/NUL could drive the device unexpectedly)."""
    text = _CONTROL_CHARS.sub("", str(command)).strip()
    return text[:MAX_COMMAND_CHARS] if len(text) > MAX_COMMAND_CHARS else text


def clean_commands(lines: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for line in lines:
        for part in str(line).splitlines():
            command = sanitize_command(part)
            if command:
                cleaned.append(command)
    return cleaned[:MAX_COMMANDS]


def parse_command_lines(text: str) -> list[str]:
    """Commands typed/edited by the user in the Run dialog (one per line; ``#`` lines are notes)."""
    return clean_commands(l for l in str(text).splitlines() if not l.strip().startswith("#"))


def extract_commands(text: str) -> list[str]:
    """Commands from fenced shell blocks in a model reply.

    Fences are paired left-to-right, so a ```python block followed by prose and
    a ```bash block can never be mis-paired into "commands".  Comment lines and a
    leading ``$ `` prompt marker are removed.
    """
    commands: list[str] = []
    for lang, body in _FENCE.findall(text or ""):
        if lang.lower() not in _SHELL_FENCE_LANGS:
            continue
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("$ "):
                stripped = stripped[2:].strip()
            commands.append(stripped)
    return clean_commands(commands)


def build_send_script(commands: Iterable[str], delay: float = 1.2, label: str | None = None,
                      select_name: str | None = None) -> str:
    """Python source for the app's script engine that types ``commands`` one by one."""
    wait_for = max(float(delay), 0.5)
    lines: list[str] = []
    if select_name:
        lines.append(f"terminal_select({select_name!r})")
    for command in clean_commands(commands):
        prefix = f"Sending to {label}: " if label else "Sending: "
        lines.append(f"print({(prefix + command)!r})")
        lines.append(f"send({command!r})")
        lines.append(f"wait({wait_for!r})")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# bookkeeping
# --------------------------------------------------------------------------

class ProposalQueue:
    """Commands the model wants to run while "LLM -> Terminal" is off (awaiting approval)."""

    def __init__(self) -> None:
        self._items: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def add(self, target: str, command: str) -> bool:
        command = sanitize_command(command)
        if not command:
            return False
        with self._lock:
            if len(self._items) >= MAX_COMMANDS:
                return False
            self._items.append((str(target or "current").strip() or "current", command))
        return True

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def groups(self) -> list[tuple[str, list[str]]]:
        """Commands grouped by target, in order of first appearance."""
        with self._lock:
            items = list(self._items)
        order: list[str] = []
        grouped: dict[str, list[str]] = {}
        names: dict[str, str] = {}
        for target, command in items:
            key = target.lower()
            if key not in grouped:
                order.append(key)
                grouped[key] = []
                names[key] = target
            grouped[key].append(command)
        return [(names[k], grouped[k]) for k in order]


class MultiRunTracker:
    """Calls ``on_done(results)`` exactly once, after ``total`` terminals reported back."""

    def __init__(self, total: int, on_done: Callable[[dict[str, str]], None]) -> None:
        self._remaining = max(int(total), 0)
        self._on_done = on_done
        self._results: dict[str, str] = {}
        self._lock = threading.Lock()
        self._fired = False
        if self._remaining == 0:
            self._fire()

    def complete(self, name: str, note: str = "") -> None:
        with self._lock:
            self._results[name] = note
            self._remaining -= 1
            ready = self._remaining <= 0
        if ready:
            self._fire()

    def _fire(self) -> None:
        with self._lock:
            if self._fired:
                return
            self._fired = True
            results = dict(self._results)
        self._on_done(results)
