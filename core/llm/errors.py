"""Exception types shared by every LLM backend."""

from __future__ import annotations


class LLMError(Exception):
    """A user-presentable failure talking to (or loading) a language model."""

    def __init__(self, message: str, *, status: int | None = None, hint: str = ""):
        super().__init__(message)
        self.status = status
        self.hint = hint

    def __str__(self) -> str:  # keep the hint visible in dialogs / chat panel
        base = super().__str__()
        return f"{base}\n{self.hint}" if self.hint else base


class LLMUnavailableError(LLMError):
    """An optional dependency (e.g. llama-cpp-python) or model file is missing."""
