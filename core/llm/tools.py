"""Tool schemas exposed to the model and helpers for prompt-based tool calling."""

from __future__ import annotations

import json
import re
from typing import Any

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


def tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    return {
        item.get("function", {}).get("name", "")
        for item in (tools or [])
        if item.get("type") == "function"
    }


def extract_json_tool_call(text: str, names: set[str]) -> dict[str, Any] | None:
    """Recover a tool call the model wrote as JSON text instead of a native tool call.

    Accepts fenced ```json blocks and bare JSON objects, with ``name``/``tool``/
    ``function`` as the tool key and ``arguments``/``parameters``/``args`` as the
    argument key.  Only names present in ``names`` are accepted.
    """
    if not text or not names:
        return None
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.IGNORECASE | re.DOTALL)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict) and _tool_key(value) in names:
            candidates.append(json.dumps(value))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        name = _tool_key(value)
        arguments = _arguments_key(value)
        if name in names and isinstance(arguments, dict):
            return {"id": "qwen-json-tool-call", "name": name, "arguments": arguments}
    return None


def _tool_key(value: dict[str, Any]) -> str:
    name = value.get("name") or value.get("tool")
    if not name and isinstance(value.get("function"), dict):
        name = value["function"].get("name")
    return name if isinstance(name, str) else ""


def _arguments_key(value: dict[str, Any]) -> Any:
    for key in ("arguments", "parameters", "args"):
        if key in value:
            return value[key]
    if isinstance(value.get("function"), dict):
        for key in ("arguments", "parameters"):
            if key in value["function"]:
                return value["function"][key]
    return {}


def tool_prompt(tools: list[dict[str, Any]]) -> str:
    """System-prompt addendum that teaches a tool-unaware model the JSON protocol."""
    lines = []
    for item in tools:
        fn = item.get("function", {})
        params = fn.get("parameters", {}).get("properties", {})
        required = set(fn.get("parameters", {}).get("required", []))
        args = ", ".join(f"{k}{'*' if k in required else ''}: {v.get('type', 'string')}" for k, v in params.items())
        lines.append(f"- {fn.get('name')}({args}): {fn.get('description', '')}")
    return (
        "You can operate the application with these tools (* = required argument):\n"
        + "\n".join(lines)
        + "\n\nTo use a tool, reply with ONLY one JSON object and nothing else, for example "
        '{"name": "list_terminals", "arguments": {}}. '
        "Use real values from the user's request, never placeholders. "
        "After the application returns the tool result, answer the user in plain language. "
        "If no tool is needed, answer normally without JSON."
    )
