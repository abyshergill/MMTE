"""Shared types and helpers for all LLM backends.

Conversation messages use one *canonical* shape across the application::

    {"role": "system" | "user" | "assistant" | "tool", "content": str,
     "tool_calls": [{"id", "name", "arguments": dict}],   # assistant only
     "tool_call_id": str, "name": str}                    # tool only

Each backend converts that shape to its own wire format.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

from core.llm.errors import LLMError

ChunkCallback = Callable[[str], None]
ResponseCallback = Callable[[Any | None], None]

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 2

# Indirection so tests can run without real sleeping.
_sleep = time.sleep


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class LLMBackend:
    """Interface implemented by every provider backend."""

    name = "base"
    #: True when the wire protocol carries structured tool calls natively.
    native_tools = True

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        cancel_event: threading.Event,
        on_chunk: ChunkCallback | None = None,
        set_response: ResponseCallback | None = None,
    ) -> ChatResult:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        raise NotImplementedError

    # Convenience used by both ``stream`` implementations.
    def _temperature(self) -> float:
        return float(self.config.get("temperature", 0.7))

    def _max_tokens(self) -> int:
        return int(self.config.get("max_tokens", 2048))

    def _model(self, default: str = "llama3.1") -> str:
        return str(self.config.get("model_name") or default)

    def _timeout(self) -> tuple[float, float]:
        return (10.0, float(self.config.get("request_timeout", 120)))


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def describe_http_error(response: Any, exc: Exception) -> LLMError:
    """Turn an HTTP failure into an actionable message that includes the body."""
    status = getattr(response, "status_code", None)
    detail = ""
    try:
        body = response.text if hasattr(response, "text") else ""
        data = json.loads(body) if body else {}
        err = data.get("error", data) if isinstance(data, dict) else {}
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("detail") or "")
        elif err:
            detail = str(err)
        if not detail and body:
            detail = body.strip()[:300]
    except (ValueError, TypeError, AttributeError):
        pass
    hint = ""
    if status in (401, 403):
        hint = "Check the API key (or the matching environment variable) in AI > LLM Settings."
    elif status == 404:
        hint = "Check the endpoint URL and that the model name is installed/available for this provider."
    elif status == 429:
        hint = "Rate limit or quota reached; wait a moment or choose another model/provider."
    message = f"HTTP {status}: {detail}" if status and detail else (f"HTTP {status}" if status else str(exc))
    return LLMError(message, status=status, hint=hint)


def post_stream(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: tuple[float, float],
    cancel_event: threading.Event,
    set_response: ResponseCallback | None,
) -> Any:
    """POST ``payload`` and return an open streaming response.

    Connection problems and transient HTTP statuses (429/5xx) are retried a couple
    of times with backoff; permanent failures raise :class:`LLMError` with the
    server's message instead of a bare ``HTTPError``.
    """
    attempt = 0
    while True:
        try:
            response = requests.post(url, json=payload, headers=headers, stream=True, timeout=timeout)
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES and not cancel_event.is_set():
                attempt += 1
                _sleep(min(2.0 ** attempt, 8.0))
                continue
            raise LLMError(
                f"Could not reach the model server at {url}: {exc}",
                hint="Is the server running, and is the endpoint URL correct?",
            ) from exc
        status = getattr(response, "status_code", 200)
        if isinstance(status, int) and status in RETRY_STATUS and attempt < MAX_RETRIES and not cancel_event.is_set():
            retry_after = 0.0
            try:
                retry_after = float(getattr(response, "headers", {}).get("Retry-After", 0))
            except (TypeError, ValueError, AttributeError):
                pass
            response.close()
            attempt += 1
            _sleep(min(max(retry_after, 2.0 ** attempt), 10.0))
            continue
        if set_response:
            set_response(response)
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            err = describe_http_error(response, exc)
            if set_response:
                set_response(None)
            response.close()
            raise err from exc
        return response


def get_json(url: str, headers: dict[str, str], timeout: float = 10.0) -> Any:
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        resp = getattr(exc, "response", None)
        if resp is not None:
            raise describe_http_error(resp, exc) from exc
        raise LLMError(f"Could not reach {url}: {exc}", hint="Is the server running?") from exc
    except ValueError as exc:
        raise LLMError(f"Unexpected (non-JSON) reply from {url}") from exc


def iter_stream_lines(response: Any, cancel_event: threading.Event):
    """Yield stripped, non-empty text lines; stops early when cancelled."""
    for raw in response.iter_lines(decode_unicode=True):
        if cancel_event.is_set():
            return
        if not raw:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        line = raw.strip()
        if line:
            yield line


# --------------------------------------------------------------------------
# <think> block filtering (Qwen3, DeepSeek-R1, ... emit their reasoning inline)
# --------------------------------------------------------------------------

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def strip_think_blocks(text: str) -> str:
    text = _THINK_RE.sub("", text)
    # An unterminated block (cut off by max_tokens/cancel): drop the dangling part.
    idx = text.lower().rfind("<think>")
    return text[:idx] if idx != -1 else text


class ThinkFilter:
    """Incrementally hides ``<think>...</think>`` spans in a chunk stream."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self._buf = ""
        self._inside = False

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out: list[str] = []
        while True:
            lowered = self._buf.lower()
            if self._inside:
                end = lowered.find(self.CLOSE)
                if end == -1:
                    # keep a tail that might be the start of "</think>"
                    self._buf = self._buf[-(len(self.CLOSE) - 1):]
                    break
                self._buf = self._buf[end + len(self.CLOSE):].lstrip()
                self._inside = False
            else:
                start = lowered.find(self.OPEN)
                if start == -1:
                    keep = _partial_suffix(lowered, self.OPEN)
                    emit_to = len(self._buf) - keep
                    out.append(self._buf[:emit_to])
                    self._buf = self._buf[emit_to:]
                    break
                out.append(self._buf[:start])
                self._buf = self._buf[start + len(self.OPEN):]
                self._inside = True
        return "".join(out)

    def flush(self) -> str:
        tail = "" if self._inside else self._buf
        self._buf, self._inside = "", False
        return tail


def _partial_suffix(text: str, token: str) -> int:
    for size in range(min(len(token) - 1, len(text)), 0, -1):
        if token.startswith(text[-size:]):
            return size
    return 0
