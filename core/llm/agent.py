"""Backend-independent chat loop with app-tool execution.

Two tool protocols are supported and chosen per request:

* **native** - the provider returns structured tool calls (OpenAI, Anthropic,
  Ollama, ...).
* **prompt** - the model is taught a JSON convention in the system prompt and
  its JSON reply is parsed (local GGUF files, small Qwen/Llama models that
  print a call as text instead of using the tool API).  The native path also
  falls back to this parser when a model "talks JSON" anyway.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from core.llm.base import ChatResult, ChunkCallback, ResponseCallback, ThinkFilter, strip_think_blocks
from core.llm.factory import create_backend
from core.llm.providers import resolve_provider
from core.llm.tools import APP_TOOL_DEFINITIONS, extract_json_tool_call, tool_names, tool_prompt

ToolHandler = Callable[[str, dict[str, Any]], str]


# --------------------------------------------------------------------------
# context window management
# --------------------------------------------------------------------------

def context_budget_chars(config: dict[str, Any]) -> int:
    """Rough character budget for the prompt (≈3 chars per token) for the active backend."""
    spec = resolve_provider(config)
    if spec.backend == "gguf":
        window = int(config.get("n_ctx", 4096))
    elif spec.backend == "ollama":
        window = int(config.get("context_length", 8192))
    else:
        return 600_000  # hosted models: 128k+ tokens
    reserve = int(config.get("max_tokens", 2048))
    return max(3000, (window - reserve) * 3)


def trim_history(messages: list[dict[str, Any]], budget_chars: int) -> list[dict[str, Any]]:
    """Drop the oldest non-system turns until the prompt fits ``budget_chars``.

    System messages and the most recent message are always kept, so a long chat
    degrades gracefully instead of overflowing a small local model's window.
    """
    def size(msg: dict[str, Any]) -> int:
        return len(str(msg.get("content") or "")) + 16

    total = sum(size(m) for m in messages)
    if total <= budget_chars:
        return list(messages)
    kept = list(messages)
    index = 0
    while total > budget_chars and index < len(kept) - 1:
        if kept[index].get("role") == "system":
            index += 1
            continue
        total -= size(kept[index])
        del kept[index]
    return kept


# --------------------------------------------------------------------------
# live output
# --------------------------------------------------------------------------

class _LiveEmitter:
    """Streams visible text to the UI while holding back anything that looks like a
    JSON tool call, and hides ``<think>`` reasoning blocks."""

    def __init__(self, on_chunk: ChunkCallback, always_stream: bool = False):
        self._on_chunk = on_chunk
        self._filter = ThinkFilter()
        self._pending = ""
        self._streaming: bool | None = True if always_stream else None

    def feed(self, chunk: str) -> None:
        visible = self._filter.feed(chunk)
        if not visible:
            return
        if self._streaming is None:
            self._pending += visible
            stripped = self._pending.lstrip()
            if not stripped:
                return
            self._streaming = not stripped.startswith(("{", "```", "["))
            if self._streaming:
                text, self._pending = self._pending, ""
                self._on_chunk(text)
            return
        if self._streaming:
            self._on_chunk(visible)

    def finish(self, final_text: str) -> None:
        """Called when the turn ended without a tool call."""
        tail = self._filter.flush()
        if self._streaming:
            if tail:
                self._on_chunk(tail)
        elif final_text:
            self._on_chunk(final_text)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def stream_plain(
    config: dict[str, Any],
    messages: list[dict[str, Any]],
    cancel_event: threading.Event,
    on_chunk: ChunkCallback,
    set_response: ResponseCallback | None = None,
) -> str:
    """Plain streamed chat (no tools). Returns the full visible reply."""
    backend = create_backend(config)
    prompt = trim_history(messages, context_budget_chars(config))
    emitter = _LiveEmitter(on_chunk, always_stream=True)
    result = backend.stream(prompt, None, cancel_event, emitter.feed, set_response)
    emitter.finish(strip_think_blocks(result.content))
    return strip_think_blocks(result.content)


def _with_tool_prompt(conversation: list[dict[str, Any]], tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    addendum = tool_prompt(tools)
    for message in conversation:
        if message.get("role") == "system":
            message["content"] = f"{message.get('content', '')}\n\n{addendum}".strip()
            return conversation
    return [{"role": "system", "content": addendum}, *conversation]


def run_chat(
    config: dict[str, Any],
    messages: list[dict[str, Any]],
    cancel_event: threading.Event,
    on_chunk: ChunkCallback,
    tool_handler: ToolHandler,
    set_response: ResponseCallback | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tool_rounds: int = 4,
) -> str:
    """Chat with the configured model, executing only the supplied app-native tools."""
    backend = create_backend(config)
    mode = str(config.get("tool_mode") or "auto")
    available = [] if mode == "off" else list(APP_TOOL_DEFINITIONS if tools is None else tools)
    use_native = mode == "native" or (mode == "auto" and backend.native_tools)
    names = tool_names(available)

    conversation: list[dict[str, Any]] = trim_history(
        [dict(message) for message in messages], context_budget_chars(config)
    )
    if available and not use_native:
        conversation = _with_tool_prompt(conversation, available)
    completed_fallback_calls: dict[str, str] = {}

    for _ in range(max_tool_rounds + 1):
        if cancel_event.is_set():
            return ""
        emitter = _LiveEmitter(on_chunk)
        wire_tools = available if (use_native and mode != "off") else None
        result: ChatResult = backend.stream(conversation, wire_tools, cancel_event, emitter.feed, set_response)
        if cancel_event.is_set():
            return ""

        content = strip_think_blocks(result.content)
        tool_calls = list(result.tool_calls) if use_native else []
        json_fallback_call = None
        if not tool_calls:
            json_fallback_call = extract_json_tool_call(content, names)
            if json_fallback_call:
                tool_calls = [json_fallback_call]

        if not tool_calls:
            text = content or "No output generated."
            emitter.finish(text)
            if not content:
                on_chunk(text)
            return text

        if json_fallback_call:
            # Models that print the call as JSON text: don't forge a protocol-level
            # assistant tool_call; give them the real result as a follow-up user
            # message and ask for a final answer.
            name = str(json_fallback_call.get("name") or "")
            arguments = json_fallback_call.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            call_key = json.dumps([name, arguments], sort_keys=True, default=str)
            repeated_call = call_key in completed_fallback_calls
            if repeated_call:
                tool_result = "This exact app tool call already ran. Its actual result was:\n" + completed_fallback_calls[call_key]
            else:
                try:
                    tool_result = str(tool_handler(name, arguments))
                except Exception as exc:
                    tool_result = f"App tool {name} failed: {exc}"
                completed_fallback_calls[call_key] = tool_result
            conversation.append({"role": "assistant", "content": content})
            conversation.append(
                {
                    "role": "user",
                    "content": (
                        f"The application {'already executed' if repeated_call else 'has now executed'} {name} with the requested values. "
                        f"Actual application result:\n{tool_result}\n\n"
                        "If this completes the user's request, give a concise, truthful summary. "
                        "If another distinct app operation is needed to finish the request, call the appropriate next tool. "
                        "Do not repeat an app operation that already succeeded."
                    ),
                }
            )
            if repeated_call:
                available = []
                names = set()
            continue

        conversation.append(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {"id": call.get("id") or f"call_{i}", "name": call.get("name", ""), "arguments": call.get("arguments") or {}}
                    for i, call in enumerate(tool_calls)
                ],
            }
        )
        for index, call in enumerate(tool_calls):
            if cancel_event.is_set():
                return ""
            name = str(call.get("name") or "")
            arguments = call.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            if name not in names or not isinstance(arguments, dict):
                tool_result = f"Error: invalid app tool call {name!r}."
            else:
                try:
                    tool_result = str(tool_handler(name, arguments))
                except Exception as exc:
                    tool_result = f"App tool {name} failed: {exc}"
            conversation.append(
                {"role": "tool", "tool_call_id": call.get("id") or f"call_{index}", "name": name, "content": tool_result}
            )

    final_text = "The model exceeded the application tool-call limit without completing a response."
    on_chunk(final_text)
    return final_text
