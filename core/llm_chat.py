"""Streaming chat client for Ollama and OpenAI-compatible local LLM servers."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests


APP_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_terminals",
            "description": "List terminal tabs currently open inside Modern Multi Terminal. Use this to resolve names like the current tab before acting.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_terminal",
            "description": "Create and select a new terminal tab inside Modern Multi Terminal. This creates an application tab, not a macOS shell window.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Name for the new app terminal tab."}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_terminal",
            "description": "Rename an existing terminal tab inside Modern Multi Terminal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "terminal_name": {"type": "string", "description": "Existing tab name, or 'current' for the selected tab."},
                    "new_name": {"type": "string", "description": "New tab name."},
                },
                "required": ["terminal_name", "new_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "connect_terminal",
            "description": "Connect an existing app terminal tab to a remote host using SSH or Telnet. SSH requires username/password when password authentication is used.",
            "parameters": {
                "type": "object",
                "properties": {
                    "terminal_name": {"type": "string"},
                    "protocol": {"type": "string", "enum": ["ssh", "telnet"]},
                    "host": {"type": "string"},
                    "port": {"type": "integer"},
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                },
                "required": ["terminal_name", "protocol", "host", "port"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_terminal",
            "description": "Send literal text or a command to an already-connected app terminal, then return the terminal output. Never use shell commands to control app tabs; use the dedicated app tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "terminal_name": {"type": "string", "description": "Connected app tab name, or 'current' for the selected tab."},
                    "text": {"type": "string", "description": "Exact text or command to send to the remote/device terminal."},
                },
                "required": ["terminal_name", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_terminal",
            "description": "Read recent scrollback from an app terminal, usually to analyze logs.",
            "parameters": {
                "type": "object",
                "properties": {"terminal_name": {"type": "string", "description": "App tab name, or 'current'."}},
                "required": ["terminal_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sftp_upload",
            "description": "Upload a user-specified local file to a remote path over the selected connected SSH app session. Do not use for Telnet or serial.",
            "parameters": {
                "type": "object",
                "properties": {
                    "terminal_name": {"type": "string"},
                    "local_path": {"type": "string"},
                    "remote_path": {"type": "string"},
                },
                "required": ["terminal_name", "local_path", "remote_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sftp_download",
            "description": "Download a remote file over the selected connected SSH app session to a user-specified local path. Do not use for Telnet or serial.",
            "parameters": {
                "type": "object",
                "properties": {
                    "terminal_name": {"type": "string"},
                    "remote_path": {"type": "string"},
                    "local_path": {"type": "string"},
                },
                "required": ["terminal_name", "remote_path", "local_path"],
            },
        },
    },
]


def _extract_json_tool_call(text: str, tool_names: set[str]) -> dict[str, Any] | None:
    """Accept Qwen's observed fenced-JSON tool-call representation as a fallback."""
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.IGNORECASE | re.DOTALL)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict):
            name = value.get("name") or value.get("tool")
            if isinstance(name, str) and name in tool_names:
                candidates.append(json.dumps(value))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        name = value.get("name") or value.get("tool")
        arguments = value.get("arguments", value.get("parameters", {}))
        if isinstance(name, str) and name in tool_names and isinstance(arguments, dict):
            return {"id": "qwen-json-tool-call", "name": name, "arguments": arguments}
    return None


def _chat_endpoint(config: dict[str, Any]) -> tuple[str, bool]:
    endpoint = str(config.get("endpoint_url") or "http://localhost:11434/v1").strip().rstrip("/")
    if not endpoint:
        endpoint = "http://localhost:11434/v1"
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    if path.endswith("/api/chat"):
        return endpoint, True
    if path.endswith("/chat/completions"):
        return endpoint, False
    if not path:
        provider = str(config.get("provider") or "").strip().lower()
        try:
            ollama_default_port = parsed.port == 11434
        except ValueError:
            ollama_default_port = False
        if "ollama" in provider or ollama_default_port:
            return urlunsplit(parsed._replace(path="/api/chat")), True
        return urlunsplit(parsed._replace(path="/v1/chat/completions")), False
    return f"{endpoint}/chat/completions", False


def _extract_chunk(data: dict[str, Any], ollama_native: bool) -> str:
    if ollama_native:
        return str(data.get("message", {}).get("content") or "")
    choices = data.get("choices") or []
    if not choices:
        return ""
    return str((choices[0].get("delta") or {}).get("content") or "")


def stream_chat(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    cancel_event: threading.Event,
    on_chunk: Callable[[str], None],
    set_response: Callable[[Any | None], None] | None = None,
) -> str:
    """Stream a response, forwarding text as it arrives; raises on HTTP errors."""
    endpoint, ollama_native = _chat_endpoint(config)
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    api_key = str(config.get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload: dict[str, Any] = {
        "model": config.get("model_name") or "llama3.1",
        "messages": messages,
        "stream": True,
    }
    if ollama_native:
        payload["options"] = {
            "temperature": float(config.get("temperature", 0.7)),
            "num_predict": int(config.get("max_tokens", 2048)),
        }
    else:
        payload["temperature"] = float(config.get("temperature", 0.7))
        payload["max_tokens"] = int(config.get("max_tokens", 2048))

    response = requests.post(endpoint, json=payload, headers=headers, stream=True, timeout=(10, 120))
    if set_response:
        set_response(response)
    try:
        response.raise_for_status()
        collected: list[str] = []
        for raw_line in response.iter_lines(decode_unicode=True):
            if cancel_event.is_set():
                break
            if not raw_line:
                continue
            if isinstance(raw_line, bytes):
                raw_line = raw_line.decode("utf-8", errors="replace")
            line = raw_line.strip()
            if not ollama_native:
                if not line.startswith("data:"):
                    continue
                line = line[5:].strip()
                if line == "[DONE]":
                    break
            try:
                data = json.loads(line)
            except (ValueError, TypeError):
                continue
            text = _extract_chunk(data, ollama_native)
            if text:
                collected.append(text)
                on_chunk(text)
        return "".join(collected)
    finally:
        if set_response:
            set_response(None)
        response.close()


def _receive_chat_message(
    config: dict[str, Any],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    cancel_event: threading.Event,
    set_response: Callable[[Any | None], None] | None,
) -> tuple[dict[str, Any], bool]:
    endpoint, ollama_native = _chat_endpoint(config)
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    api_key = str(config.get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload: dict[str, Any] = {
        "model": config.get("model_name") or "llama3.1",
        "messages": messages,
        "tools": tools,
        "stream": True,
    }
    if ollama_native:
        payload["options"] = {
            "temperature": float(config.get("temperature", 0.7)),
            "num_predict": int(config.get("max_tokens", 2048)),
        }
    else:
        payload["temperature"] = float(config.get("temperature", 0.7))
        payload["max_tokens"] = int(config.get("max_tokens", 2048))

    response = requests.post(endpoint, json=payload, headers=headers, stream=True, timeout=(10, 120))
    if set_response:
        set_response(response)
    try:
        response.raise_for_status()
        content: list[str] = []
        native_calls: list[dict[str, Any]] = []
        openai_calls: dict[int, dict[str, Any]] = {}
        for raw_line in response.iter_lines(decode_unicode=True):
            if cancel_event.is_set():
                return {"content": "", "tool_calls": []}, ollama_native
            if not raw_line:
                continue
            if isinstance(raw_line, bytes):
                raw_line = raw_line.decode("utf-8", errors="replace")
            line = raw_line.strip()
            if not ollama_native:
                if not line.startswith("data:"):
                    continue
                line = line[5:].strip()
                if line == "[DONE]":
                    break
            try:
                data = json.loads(line)
            except (ValueError, TypeError):
                continue

            if ollama_native:
                message = data.get("message") or {}
                text = message.get("content") or ""
                if text:
                    content.append(str(text))
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    name = function.get("name")
                    arguments = function.get("arguments") or {}
                    if name:
                        native_calls.append({"name": name, "arguments": arguments})
            else:
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content") or ""
                if text:
                    content.append(str(text))
                for call in delta.get("tool_calls") or []:
                    index = int(call.get("index", 0))
                    target = openai_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    target["id"] += call.get("id") or ""
                    function = call.get("function") or {}
                    target["name"] += function.get("name") or ""
                    target["arguments"] += function.get("arguments") or ""

        if ollama_native:
            return {"content": "".join(content), "tool_calls": native_calls}, True

        calls = []
        for index, call in sorted(openai_calls.items()):
            try:
                arguments = json.loads(call["arguments"] or "{}")
            except ValueError:
                arguments = {}
            calls.append({"id": call["id"] or f"call_{index}", "name": call["name"], "arguments": arguments})
        return {"content": "".join(content), "tool_calls": calls}, False
    finally:
        if set_response:
            set_response(None)
        response.close()


def chat_with_app_tools(
    config: dict[str, Any],
    messages: list[dict[str, Any]],
    cancel_event: threading.Event,
    on_chunk: Callable[[str], None],
    tool_handler: Callable[[str, dict[str, Any]], str],
    set_response: Callable[[Any | None], None] | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tool_rounds: int = 4,
) -> str:
    """Chat with the model and execute only the supplied app-native tools."""
    available_tools = APP_TOOL_DEFINITIONS if tools is None else tools
    tool_names = {
        item.get("function", {}).get("name", "")
        for item in available_tools
        if item.get("type") == "function"
    }
    conversation: list[dict[str, Any]] = [dict(message) for message in messages]
    completed_fallback_calls: dict[str, str] = {}

    for _ in range(max_tool_rounds + 1):
        if cancel_event.is_set():
            return ""
        message, ollama_native = _receive_chat_message(
            config,
            conversation,
            available_tools,
            cancel_event,
            set_response,
        )
        if cancel_event.is_set():
            return ""
        tool_calls = message.get("tool_calls") or []
        json_fallback_call = None
        if not tool_calls:
            json_fallback_call = _extract_json_tool_call(str(message.get("content") or ""), tool_names)
            if json_fallback_call:
                tool_calls = [json_fallback_call]

        if not tool_calls:
            text = str(message.get("content") or "No output generated.")
            on_chunk(text)
            return text

        if json_fallback_call:
            # Qwen2.5-Coder:3b (the configured local model) emits function
            # requests as JSON text rather than native tool_calls. Do not forge
            # a protocol-level assistant tool_call; give it the real app result
            # as a follow-up user message and ask for a final, tool-free answer.
            name = str(json_fallback_call.get("name") or "")
            arguments = json_fallback_call.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            call_key = json.dumps([name, arguments], sort_keys=True, default=str)
            repeated_call = call_key in completed_fallback_calls
            if repeated_call:
                result = "This exact app tool call already ran. Its actual result was:\n" + completed_fallback_calls[call_key]
            else:
                try:
                    result = str(tool_handler(name, arguments))
                except Exception as exc:
                    result = f"App tool {name} failed: {exc}"
                completed_fallback_calls[call_key] = result
            conversation.append({"role": "assistant", "content": str(message.get("content") or "")})
            conversation.append(
                {
                    "role": "user",
                    "content": (
                        f"The application {'already executed' if repeated_call else 'has now executed'} {name} with the requested values. "
                        f"Actual application result:\n{result}\n\n"
                        "If this completes the user's request, give a concise, truthful summary. "
                        "If another distinct app operation is needed to finish the request, call the appropriate next tool. Do not repeat an app operation that already succeeded."
                    ),
                }
            )
            if repeated_call:
                available_tools = []
                tool_names = set()
            continue

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": message.get("content") or "",
        }
        if ollama_native:
            assistant_message["tool_calls"] = [
                {
                    "function": {
                        "name": call.get("name", ""),
                        "arguments": call.get("arguments") or {},
                    }
                }
                for call in tool_calls
            ]
        else:
            assistant_message["tool_calls"] = [
                {
                    "id": call.get("id") or f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": call.get("name", ""),
                        "arguments": json.dumps(call.get("arguments") or {}),
                    },
                }
                for index, call in enumerate(tool_calls)
            ]
        conversation.append(assistant_message)

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
            if name not in tool_names or not isinstance(arguments, dict):
                result = f"Error: invalid app tool call {name!r}."
            else:
                try:
                    result = str(tool_handler(name, arguments))
                except Exception as exc:
                    result = f"App tool {name} failed: {exc}"

            if ollama_native:
                conversation.append({"role": "tool", "content": result})
            else:
                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or f"call_{index}",
                        "content": result,
                    }
                )

    final_text = "The model exceeded the application tool-call limit without completing a response."
    on_chunk(final_text)
    return final_text