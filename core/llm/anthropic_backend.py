"""Anthropic Messages API backend (``/v1/messages``) with streaming + tool use."""

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

ANTHROPIC_VERSION = "2023-06-01"


def anthropic_root(endpoint: str) -> str:
    endpoint = (endpoint or "https://api.anthropic.com").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    for suffix in ("/v1/messages", "/v1"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit(parsed._replace(path=path, query="", fragment="")).rstrip("/")


def to_anthropic(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Canonical messages -> (system prompt, alternating user/assistant block list)."""
    system_parts: list[str] = []
    turns: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)  # the API requires strict alternation
        else:
            turns.append({"role": role, "content": list(blocks)})

    for message in messages:
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "system":
            if content:
                system_parts.append(content)
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            if content.strip():
                blocks.append({"type": "text", "text": content})
            for index, call in enumerate(message.get("tool_calls") or []):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id") or f"toolu_{index}",
                        "name": call.get("name", ""),
                        "input": call.get("arguments") or {},
                    }
                )
            push("assistant", blocks)
        elif role == "tool":
            push(
                "user",
                [{"type": "tool_result", "tool_use_id": message.get("tool_call_id") or "toolu_0", "content": content}],
            )
        else:
            if content.strip():
                push("user", [{"type": "text", "text": content}])
    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": [{"type": "text", "text": "(conversation start)"}]})
    return "\n\n".join(system_parts), turns


def to_anthropic_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    converted = []
    for item in tools or []:
        fn = item.get("function", {})
        converted.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


class AnthropicBackend(LLMBackend):
    name = "anthropic"

    def _headers(self) -> dict[str, str]:
        key = resolve_api_key(self.config)
        if not key:
            raise LLMError(
                "No Anthropic API key configured.",
                hint="Enter it in AI > LLM Settings or set the ANTHROPIC_API_KEY environment variable.",
            )
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
        }

    def _url(self) -> str:
        return f"{anthropic_root(str(self.config.get('endpoint_url') or ''))}/v1/messages"

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        cancel_event: threading.Event,
        on_chunk: ChunkCallback | None = None,
        set_response: ResponseCallback | None = None,
    ) -> ChatResult:
        system, turns = to_anthropic(messages)
        payload: dict[str, Any] = {
            "model": self._model("claude-3-5-haiku-latest"),
            "max_tokens": self._max_tokens(),
            "messages": turns,
            "stream": True,
            "temperature": max(0.0, min(1.0, self._temperature())),
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = to_anthropic_tools(tools)

        response = post_stream(self._url(), payload, self._headers(), self._timeout(), cancel_event, set_response)
        text_parts: list[str] = []
        blocks: dict[int, dict[str, Any]] = {}
        try:
            for line in iter_stream_lines(response, cancel_event):
                if not line.startswith("data:"):
                    continue
                try:
                    data = json.loads(line[5:].strip())
                except (ValueError, TypeError):
                    continue
                kind = data.get("type")
                if kind == "error":
                    err = data.get("error") or {}
                    raise LLMError(str(err.get("message") or err or "Anthropic stream error"))
                if kind == "content_block_start":
                    block = data.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        blocks[int(data.get("index", 0))] = {
                            "id": block.get("id") or "",
                            "name": block.get("name") or "",
                            "json": "",
                        }
                elif kind == "content_block_delta":
                    delta = data.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text = str(delta.get("text") or "")
                        if text:
                            text_parts.append(text)
                            if on_chunk:
                                on_chunk(text)
                    elif delta.get("type") == "input_json_delta":
                        target = blocks.get(int(data.get("index", 0)))
                        if target is not None:
                            target["json"] += str(delta.get("partial_json") or "")
                elif kind == "message_stop":
                    break
        finally:
            if set_response:
                set_response(None)
            response.close()

        calls = []
        for index, block in sorted(blocks.items()):
            try:
                arguments = json.loads(block["json"] or "{}")
            except ValueError:
                arguments = {}
            calls.append(
                {
                    "id": block["id"] or f"toolu_{index}",
                    "name": block["name"],
                    "arguments": arguments if isinstance(arguments, dict) else {},
                }
            )
        return ChatResult("".join(text_parts), calls)

    def list_models(self) -> list[str]:
        data = get_json(f"{anthropic_root(str(self.config.get('endpoint_url') or ''))}/v1/models?limit=100", self._headers())
        return sorted({str(m.get("id")) for m in (data.get("data") or []) if m.get("id")})
