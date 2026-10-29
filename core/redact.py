"""Mask credentials in text before it leaves the machine (e.g. terminal scrollback
attached to a prompt for a cloud model)."""

from __future__ import annotations

import re

MASK = "[REDACTED]"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # PEM private keys
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), MASK),
    # Authorization headers / bearer tokens
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*(?:bearer|basic|token)\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1" + MASK),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"), r"\1" + MASK),
    # Well known token shapes
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), MASK),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"), MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), MASK),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), MASK),
    # key=value / key: value secrets (password, passwd, secret, token, api_key ...)
    (
        re.compile(
            r"(?i)\b((?:pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)"
            r"\s*[:=]\s*)(?!\[REDACTED\])(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\"']+)"
        ),
        r"\1" + MASK,
    ),
    # Tera Term style /passwd=xxx and ssh -o Password=xxx
    (re.compile(r"(?i)(/passwd=)\S+"), r"\1" + MASK),
    # Prompt lines such as "Password: hunter2" are handled above; "[sudo] password for bob: x" below
    (re.compile(r"(?i)(\[sudo\] password for [^\r\n:]+:\s*)\S+"), r"\1" + MASK),
    # user:password@host in URLs
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(@)", re.IGNORECASE), r"\1" + MASK + r"\2"),
]


def redact_secrets(text: str) -> str:
    """Return ``text`` with likely credentials replaced by ``[REDACTED]``."""
    if not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
