"""Ollama native ``/api/chat`` backend."""

from __future__ import annotations

import json
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


def ollama_root(endpoint: str) -> str:
    """Strip /api/chat, /v1 ... so only scheme://host:port[/prefix] remains."""
    endpoint = (endpoint or "http://localhost:11434").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    for suffix in ("/api/chat", "/api/embed", "/chat/completions", "/embeddings", "/v1", "/api"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit(parsed._replace(path=path, query="", fragment="")).rstrip("/")


def to_ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            wire.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": [
                        {"function": {"name": call.get("name", ""), "arguments": call.get("arguments") or {}}}
                        for call in message["tool_calls"]
                    ],
                }
            )
        elif role == "tool":
            item = {"role": "tool", "content": str(message.get("content") or "")}
            if message.get("name"):
                item["tool_name"] = message["name"]
            wire.append(item)
        else:
            wire.append({k: v for k, v in message.items() if k in ("role", "content")})
    return wire


class OllamaBackend(LLMBackend):
    name = "ollama"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        key = resolve_api_key(self.config)
        if key:  # Ollama behind an authenticating reverse proxy / Ollama Cloud
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _payload(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model(),
            "messages": to_ollama_messages(messages),
            "stream": True,
            "options": {
                "temperature": self._temperature(),
                "num_predict": self._max_tokens(),
                "num_ctx": int(self.config.get("context_length", 8192)),
            },
        }
        if tools is not None:
            payload["tools"] = tools
        return payload

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        cancel_event: threading.Event,
        on_chunk: ChunkCallback | None = None,
        set_response: ResponseCallback | None = None,
    ) -> ChatResult:
        from core.llm.factory import chat_endpoint

        url, _native = chat_endpoint(self.config)
        response = post_stream(url, self._payload(messages, tools), self._headers(), self._timeout(), cancel_event, set_response)
        content: list[str] = []
        calls: list[dict[str, Any]] = []
        try:
            for line in iter_stream_lines(response, cancel_event):
                try:
                    data = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("error"):
                    raise LLMError(str(data["error"]))
                message = data.get("message") or {}
                text = message.get("content") or ""
                if text:
                    content.append(str(text))
                    if on_chunk:
                        on_chunk(str(text))
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    arguments = function.get("arguments") or {}
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except ValueError:
                            arguments = {}
                    if function.get("name"):
                        calls.append(
                            {
                                "id": f"call_{len(calls)}",
                                "name": function["name"],
                                "arguments": arguments if isinstance(arguments, dict) else {},
                            }
                        )
        finally:
            if set_response:
                set_response(None)
            response.close()
        return ChatResult("".join(content), calls)

    def list_models(self) -> list[str]:
        from core.llm.factory import base_endpoint

        data = get_json(f"{ollama_root(base_endpoint(self.config))}/api/tags", self._headers())
        return sorted({str(m.get("name")) for m in (data.get("models") or []) if m.get("name")})
