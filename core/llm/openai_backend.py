"""OpenAI-compatible ``/chat/completions`` backend (also used for GGUF streams)."""

from __future__ import annotations

import json
import re
import threading
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from core.llm.base import (
    ChatResult,
    ChunkCallback,
    LLMBackend,
    ResponseCallback,
    get_json,
    iter_stream_lines,
    post_stream,
)
from core.llm.errors import LLMError
from core.llm.settings import resolve_api_key

_REASONING_MODEL_RE = re.compile(r"^(?:openai/)?(?:o\d|gpt-5)", re.IGNORECASE)


def openai_chat_url(endpoint: str) -> str:
    endpoint = (endpoint or "").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return endpoint
    if not path:
        return urlunsplit(parsed._replace(path="/v1/chat/completions"))
    return f"{endpoint}/chat/completions"


def openai_models_url(endpoint: str) -> str:
    endpoint = (endpoint or "").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/chat"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    if not path:
        path = "/v1"
    return urlunsplit(parsed._replace(path=f"{path}/models"))


def to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            wire.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": [
                        {
                            "id": call.get("id") or f"call_{index}",
                            "type": "function",
                            "function": {
                                "name": call.get("name", ""),
                                "arguments": json.dumps(call.get("arguments") or {}),
                            },
                        }
                        for index, call in enumerate(message["tool_calls"])
                    ],
                }
            )
        elif role == "tool":
            wire.append(
                {
                    "role": "tool",
                    "tool_call_id": message.get("tool_call_id") or "call_0",
                    "content": str(message.get("content") or ""),
                }
            )
        else:
            wire.append({k: v for k, v in message.items() if k in ("role", "content", "name")})
    return wire


class OpenAIStreamAccumulator:
    """Folds OpenAI-style streaming chunks into text plus completed tool calls."""

    def __init__(self) -> None:
        self.content: list[str] = []
        self._calls: dict[int, dict[str, str]] = {}
        self._last_index = -1

    def feed(self, data: dict[str, Any]) -> str:
        if data.get("error"):
            err = data["error"]
            raise LLMError(str(err.get("message") if isinstance(err, dict) else err))
        choices = data.get("choices") or []
        if not choices:
            return ""
        delta = choices[0].get("delta") or {}
        text = str(delta.get("content") or "")
        if text:
            self.content.append(text)
        for call in delta.get("tool_calls") or []:
            self._merge_call(call)
        return text

    def _merge_call(self, call: dict[str, Any]) -> None:
        index = call.get("index")
        if index is None:
            # Some servers (e.g. Gemini's compat layer) omit the index: a fresh id
            # starts a new call, otherwise the delta continues the previous one.
            if call.get("id") and self._calls:
                index = self._last_index + 1
            else:
                index = max(self._last_index, 0)
        index = int(index)
        self._last_index = index
        target = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if call.get("id") and not target["id"]:
            target["id"] = str(call["id"])
        function = call.get("function") or {}
        name = function.get("name") or ""
        if name and name != target["name"]:
            target["name"] = name if not target["name"] else target["name"] + name
        arguments = function.get("arguments")
        if isinstance(arguments, dict):  # a few servers send an already-parsed object
            target["arguments"] = json.dumps(arguments)
        elif arguments:
            target["arguments"] += str(arguments)

    def result(self) -> ChatResult:
        calls = []
        for index, call in sorted(self._calls.items()):
            if not call["name"]:
                continue
            try:
                arguments = json.loads(call["arguments"] or "{}")
            except ValueError:
                arguments = {}
            calls.append(
                {
                    "id": call["id"] or f"call_{index}",
                    "name": call["name"],
                    "arguments": arguments if isinstance(arguments, dict) else {},
                }
            )
        return ChatResult("".join(self.content), calls)


class OpenAIBackend(LLMBackend):
    name = "openai"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        key = resolve_api_key(self.config)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _payload(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        model = self._model()
        payload: dict[str, Any] = {"model": model, "messages": to_openai_messages(messages), "stream": True}
        if tools:  # OpenAI rejects an empty tools array
            payload["tools"] = tools
        if _REASONING_MODEL_RE.match(model):
            # o-series / gpt-5 reject `max_tokens` and non-default temperature.
            payload["max_completion_tokens"] = self._max_tokens()
        else:
            payload["temperature"] = self._temperature()
            payload["max_tokens"] = self._max_tokens()
        return payload

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        cancel_event: threading.Event,
        on_chunk: ChunkCallback | None = None,
        set_response: ResponseCallback | None = None,
    ) -> ChatResult:
        from core.llm.factory import chat_endpoint  # local import: factory imports this module

        url, _native = chat_endpoint(self.config)
        response = post_stream(url, self._payload(messages, tools), self._headers(), self._timeout(), cancel_event, set_response)
        acc = OpenAIStreamAccumulator()
        try:
            for line in iter_stream_lines(response, cancel_event):
                if not line.startswith("data:"):
                    continue
                line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    data = json.loads(line)
                except (ValueError, TypeError):
                    continue
                text = acc.feed(data)
                if text and on_chunk:
                    on_chunk(text)
        finally:
            if set_response:
                set_response(None)
            response.close()
        return acc.result()

    def list_models(self) -> list[str]:
        from core.llm.factory import base_endpoint

        data = get_json(openai_models_url(base_endpoint(self.config)), self._headers())
        items = data.get("data") if isinstance(data, dict) else data
        return sorted({str(item.get("id")) for item in items or [] if isinstance(item, dict) and item.get("id")})
