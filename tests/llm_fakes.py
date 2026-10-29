"""Shared fakes for the LLM tests (no network, no real model)."""

from __future__ import annotations

import json
from typing import Any

import requests


class FakeStreamResponse:
    """Stand-in for a streaming ``requests.Response``."""

    def __init__(self, lines: list[str | bytes] | None = None, status_code: int = 200, body: str = "", headers: dict | None = None):
        self._lines = lines or []
        self.status_code = status_code
        self.text = body
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def iter_lines(self, decode_unicode: bool = False):
        yield from self._lines

    def close(self) -> None:
        self.closed = True

    def json(self) -> Any:
        return json.loads(self.text)


def sse(*events: dict | str) -> list[str]:
    """Build Server-Sent-Event ``data:`` lines (blank separators included)."""
    lines: list[str] = []
    for event in events:
        lines.append("data: " + (event if isinstance(event, str) else json.dumps(event)))
        lines.append("")
    return lines


def openai_text(*pieces: str) -> list[str]:
    return sse(*[{"choices": [{"delta": {"content": piece}}]} for piece in pieces], "[DONE]")


def openai_tool_call(name: str, arguments: dict, call_id: str = "call_1") -> list[str]:
    args = json.dumps(arguments)
    half = len(args) // 2
    return sse(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": call_id, "function": {"name": name, "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[:half]}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[half:]}}]}}]},
        "[DONE]",
    )


def ndjson(*events: dict) -> list[str]:
    return [json.dumps(event) for event in events]
