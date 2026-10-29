"""Chat entry points used by the GUI.

The implementation lives in :mod:`core.llm` (providers, backends, agent loop).
This module keeps the original import path and names stable:

* ``stream_chat``          plain streamed chat
* ``chat_with_app_tools``  chat + execution of the app-native tools
* ``APP_TOOL_DEFINITIONS`` tool schemas offered to the model
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import requests  # noqa: F401  (re-exported so tests can patch ``llm_chat.requests``)

from core.llm.agent import run_chat, stream_plain
from core.llm.errors import LLMError  # noqa: F401
from core.llm.factory import chat_endpoint as _chat_endpoint  # noqa: F401
from core.llm.tools import APP_TOOL_DEFINITIONS  # noqa: F401
from core.llm.tools import extract_json_tool_call as _extract_json_tool_call  # noqa: F401


def stream_chat(
    config: dict[str, Any],
    messages: list[dict[str, Any]],
    cancel_event: threading.Event,
    on_chunk: Callable[[str], None],
    set_response: Callable[[Any | None], None] | None = None,
) -> str:
    """Stream a plain (tool-free) reply from whichever provider ``config`` selects."""
    return stream_plain(config, messages, cancel_event, on_chunk, set_response)


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
    return run_chat(config, messages, cancel_event, on_chunk, tool_handler, set_response, tools, max_tool_rounds)
