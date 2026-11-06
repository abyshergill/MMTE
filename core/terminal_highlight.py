"""Regex-based syntax highlighting for plain terminal output.

Only characters the remote side left in the *default* colour are touched, so real ANSI
colours (``ls --color``, coloured prompts, ``htop``) always win.  Highlighting is skipped
while a full-screen program (alternate screen) is active.

``highlight_spans(line)`` returns ``[(start, end, tag), ...]`` with no overlaps; ``FORMATS``
maps each tag to ``(colour, bold, underline)``.
"""

from __future__ import annotations

import re

FORMATS: dict[str, tuple[str, bool, bool]] = {
    "prompt_user": ("#7ee787", True, False),
    "prompt_path": ("#79c0ff", True, False),
    "error": ("#ff7b72", True, False),
    "warn": ("#e3b341", True, False),
    "ok": ("#56d364", False, False),
    "ip": ("#39c5cf", False, False),
    "mac": ("#d2a8ff", False, False),
    "url": ("#58a6ff", False, True),
    "path": ("#a5d6ff", False, False),
    "iface": ("#d2a8ff", False, False),
    "time": ("#8b949e", False, False),
    "number": ("#ffa657", False, False),
    "meta": ("#7c8cff", False, False),
    "string": ("#a5d6a7", False, False),
}

_I = re.IGNORECASE

# Order matters: earlier rules claim characters first.
_RULES: list[tuple[re.Pattern, str | tuple[str, ...]]] = [
    # [App messages] written by this application, e.g. "[Active: SSH 10.0.0.1]".
    (re.compile(r"^\s*\[(?:Active|Connection failed|Running|Disconnected|New terminal|Upload|Uploading|"
                r"Python|TTL|Playlist|Macro|Editor|Logging|No active connection|Send error)[^\]]*\]"), "meta"),
    # Shell prompts: user@host:path$ / user@host path # / [user@host path]$
    (re.compile(r"^(\[?[\w.\-]+@[\w.\-]+)((?::|\s)[^\s$#>\]]*)?(\]?\s?[$#>%])(?=\s|$)"),
     ("prompt_user", "prompt_path", "prompt_user")),
    (re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s'\"<>]+", _I), "url"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?::\d{1,5})?\b"), "ip"),
    (re.compile(r"\b(?:\d{4}-\d{2}-\d{2}[T ])?\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b"), "time"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "time"),
    (re.compile(r"\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b", _I), "mac"),
    (re.compile(r"(?<![\w:])(?=[0-9a-f:]*::|(?:[0-9a-f]{1,4}:){7})[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}(?:/\d{1,3})?(?![\w:])", _I), "ip"),
    (re.compile(r"\b(?:errors?|fail(?:ed|ure|s)?|fatal|denied|refused|unreachable|timed?\s?out|"
                r"critical|panic|invalid|not\s+found|no\s+such\s+file(?:\s+or\s+directory)?|"
                r"abort(?:ed)?|exception|traceback|segmentation\s+fault|cannot|unable|down|"
                r"disconnected|offline|err)\b", _I), "error"),
    (re.compile(r"\b(?:warn(?:ing|ings)?|deprecated|retry(?:ing)?|degraded|notice)\b", _I), "warn"),
    (re.compile(r"\b(?:ok|success(?:ful(?:ly)?)?|succeeded|passed|up|connected|active|enabled|done|"
                r"complete(?:d)?|established|running|online|ready|listen(?:ing)?)\b", _I), "ok"),
    (re.compile(r"\b(?:eth|ens|enp|eno|wlan|wlp|lo|br|bond|docker|veth|tun|tap|vlan|Vlan|"
                r"Gi|Fa|Te|Twe|Hu|GigabitEthernet|FastEthernet|TenGigabitEthernet|Ethernet|"
                r"Port-channel|Loopback|Tunnel|mgmt)\d(?:[\w/.:\-]*[\w/])?", 0), "iface"),
    (re.compile(r"(?:\"[^\"\n]{0,200}\"|'[^'\n]{0,200}')"), "string"),
    (re.compile(r"(?<![\w.\-])(?:~|\.{1,2})?/[\w.\-+@/]*[\w\-+@/]"), "path"),
    (re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:%|[KMGTP]i?B?|ms|us|ns|s|bps|[KMG]bps)?(?![\w.])"), "number"),
]


def highlight_spans(line: str) -> list[tuple[int, int, str]]:
    """Return non-overlapping ``(start, end, tag)`` spans for one rendered terminal row."""
    if not line or not line.strip():
        return []
    taken = bytearray(len(line))
    spans: list[tuple[int, int, str]] = []

    def claim(start: int, end: int, tag: str) -> None:
        if start >= end or any(taken[start:end]):
            return
        taken[start:end] = b"\x01" * (end - start)
        spans.append((start, end, tag))

    for pattern, tag in _RULES:
        for match in pattern.finditer(line):
            if isinstance(tag, tuple):
                for group_index, group_tag in enumerate(tag, start=1):
                    if match.group(group_index):
                        claim(match.start(group_index), match.end(group_index), group_tag)
            else:
                claim(match.start(), match.end(), tag)
    spans.sort()
    return spans
