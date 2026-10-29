"""HTTP backends: OpenAI-compatible, Ollama native, Anthropic."""

import json
import threading

import pytest
import requests

from core.llm import base
from core.llm.anthropic_backend import AnthropicBackend, anthropic_root, to_anthropic, to_anthropic_tools
from core.llm.errors import LLMError
from core.llm.factory import base_endpoint, chat_endpoint, create_backend, list_models, test_connection
from core.llm.gguf_backend import GGUFBackend
from core.llm.ollama_backend import OllamaBackend, ollama_root, to_ollama_messages
from core.llm.openai_backend import (
    OpenAIBackend,
    OpenAIStreamAccumulator,
    openai_chat_url,
    openai_models_url,
    to_openai_messages,
)
from core.llm.tools import APP_TOOL_DEFINITIONS
from llm_fakes import FakeStreamResponse, ndjson, openai_text, openai_tool_call, sse

OPENAI_CFG = {"provider": "OpenAI", "api_key": "sk-test", "endpoint_url": "https://api.openai.com/v1", "model_name": "gpt-4o-mini"}
OLLAMA_CFG = {"provider": "Ollama (Local)", "endpoint_url": "http://localhost:11434", "model_name": "llama3.1"}
ANTHROPIC_CFG = {"provider": "Anthropic", "api_key": "ak-test", "endpoint_url": "https://api.anthropic.com", "model_name": "claude-x"}
USER = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(base, "_sleep", lambda _s: None)


def capture_post(monkeypatch, responses):
    calls = []
    queue = list(responses)

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(requests, "post", fake_post)
    return calls


# ------------------------------------------------------------------ endpoint helpers

@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("https://api.openai.com/v1", "https://api.openai.com/v1/chat/completions"),
        ("https://api.openai.com/v1/", "https://api.openai.com/v1/chat/completions"),
        ("http://localhost:1234", "http://localhost:1234/v1/chat/completions"),
        ("http://x/v1/chat/completions", "http://x/v1/chat/completions"),
        ("https://generativelanguage.googleapis.com/v1beta/openai", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
    ],
)
def test_openai_chat_url(endpoint, expected):
    assert openai_chat_url(endpoint) == expected


def test_models_and_root_helpers():
    assert openai_models_url("https://api.openai.com/v1/chat/completions") == "https://api.openai.com/v1/models"
    assert openai_models_url("http://localhost:1234") == "http://localhost:1234/v1/models"
    assert ollama_root("http://h:11434/api/chat") == "http://h:11434"
    assert ollama_root("http://h:11434/v1") == "http://h:11434"
    assert anthropic_root("https://api.anthropic.com/v1/messages") == "https://api.anthropic.com"
    assert anthropic_root("") == "https://api.anthropic.com"


def test_chat_endpoint_matrix():
    assert chat_endpoint({"provider": "Ollama (Local)", "endpoint_url": "http://h:11434"}) == ("http://h:11434/api/chat", True)
    assert chat_endpoint({"endpoint_url": "http://h:11434/api/chat"}) == ("http://h:11434/api/chat", True)
    assert chat_endpoint({"endpoint_url": "http://h:11434/v1"}) == ("http://h:11434/v1/chat/completions", False)
    assert chat_endpoint({"provider": "Custom / Local HTTP", "endpoint_url": "http://h:1234"}) == ("http://h:1234/v1/chat/completions", False)
    assert base_endpoint({"provider": "OpenAI"}) == "https://api.openai.com/v1"


def test_create_backend_selects_by_provider():
    assert isinstance(create_backend(OPENAI_CFG), OpenAIBackend)
    assert isinstance(create_backend(OLLAMA_CFG), OllamaBackend)
    assert isinstance(create_backend(ANTHROPIC_CFG), AnthropicBackend)
    assert isinstance(create_backend({"provider": "Local GGUF File", "gguf_path": "x.gguf"}), GGUFBackend)
    assert isinstance(create_backend({"provider": "Google Gemini", "api_key": "k"}), OpenAIBackend)
    assert isinstance(create_backend({"provider": "LM Studio (Local)"}), OpenAIBackend)


# ------------------------------------------------------------------ OpenAI-compatible

def test_openai_streams_text_and_sends_bearer_key(monkeypatch):
    calls = capture_post(monkeypatch, [FakeStreamResponse(openai_text("Hel", "lo"))])
    chunks = []
    result = OpenAIBackend(OPENAI_CFG).stream(USER, None, threading.Event(), chunks.append)
    assert result.content == "Hello" and chunks == ["Hel", "lo"] and result.tool_calls == []
    call = calls[0]
    assert call["url"] == "https://api.openai.com/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["json"]["stream"] is True and "tools" not in call["json"]
    assert call["json"]["max_tokens"] == 2048


def test_openai_assembles_streamed_tool_call_arguments(monkeypatch):
    capture_post(monkeypatch, [FakeStreamResponse(openai_tool_call("create_terminal", {"name": "lab", "x": 1}))])
    result = OpenAIBackend(OPENAI_CFG).stream(USER, APP_TOOL_DEFINITIONS, threading.Event())
    assert result.tool_calls == [{"id": "call_1", "name": "create_terminal", "arguments": {"name": "lab", "x": 1}}]


def test_openai_empty_tools_list_is_omitted_but_real_tools_are_sent(monkeypatch):
    calls = capture_post(monkeypatch, [FakeStreamResponse(openai_text("a"))])
    OpenAIBackend(OPENAI_CFG).stream(USER, [], threading.Event())
    OpenAIBackend(OPENAI_CFG).stream(USER, APP_TOOL_DEFINITIONS, threading.Event())
    assert "tools" not in calls[0]["json"]  # OpenAI answers 400 to "tools": []
    assert calls[1]["json"]["tools"] == APP_TOOL_DEFINITIONS


@pytest.mark.parametrize("model", ["o3-mini", "gpt-5", "openai/o1"])
def test_openai_reasoning_models_use_max_completion_tokens(monkeypatch, model):
    calls = capture_post(monkeypatch, [FakeStreamResponse(openai_text("a"))])
    OpenAIBackend({**OPENAI_CFG, "model_name": model}).stream(USER, None, threading.Event())
    payload = calls[0]["json"]
    assert payload["max_completion_tokens"] == 2048
    assert "max_tokens" not in payload and "temperature" not in payload


def test_openai_tool_call_deltas_without_index_are_merged():
    acc = OpenAIStreamAccumulator()
    acc.feed({"choices": [{"delta": {"tool_calls": [{"id": "a", "function": {"name": "f", "arguments": '{"x":'}}]}}]})
    acc.feed({"choices": [{"delta": {"tool_calls": [{"function": {"arguments": "1}"}}]}}]})
    acc.feed({"choices": [{"delta": {"tool_calls": [{"id": "b", "function": {"name": "g", "arguments": "{}"}}]}}]})
    assert acc.result().tool_calls == [
        {"id": "a", "name": "f", "arguments": {"x": 1}},
        {"id": "b", "name": "g", "arguments": {}},
    ]


def test_openai_stream_error_event_raises():
    with pytest.raises(LLMError, match="boom"):
        OpenAIStreamAccumulator().feed({"error": {"message": "boom"}})


def test_openai_http_error_includes_server_message_and_hint(monkeypatch):
    body = json.dumps({"error": {"message": "Incorrect API key provided"}})
    capture_post(monkeypatch, [FakeStreamResponse(status_code=401, body=body)])
    with pytest.raises(LLMError) as err:
        OpenAIBackend(OPENAI_CFG).stream(USER, None, threading.Event())
    assert err.value.status == 401
    assert "Incorrect API key provided" in str(err.value) and "API key" in err.value.hint


def test_transient_status_is_retried_then_succeeds(monkeypatch):
    calls = capture_post(monkeypatch, [FakeStreamResponse(status_code=503), FakeStreamResponse(status_code=429), FakeStreamResponse(openai_text("ok"))])
    result = OpenAIBackend(OPENAI_CFG).stream(USER, None, threading.Event())
    assert result.content == "ok" and len(calls) == 3


def test_connection_failure_is_retried_then_reported_clearly(monkeypatch):
    calls = capture_post(monkeypatch, [requests.ConnectionError("refused")])
    with pytest.raises(LLMError, match="Could not reach"):
        OpenAIBackend(OPENAI_CFG).stream(USER, None, threading.Event())
    assert len(calls) == base.MAX_RETRIES + 1


def test_cancel_stops_reading_the_stream(monkeypatch):
    capture_post(monkeypatch, [FakeStreamResponse(openai_text("a", "b", "c"))])
    cancel = threading.Event()
    seen = []

    def on_chunk(text):
        seen.append(text)
        cancel.set()

    OpenAIBackend(OPENAI_CFG).stream(USER, None, cancel, on_chunk)
    assert seen == ["a"]


def test_openai_message_conversion_covers_tool_round_trip():
    wire = to_openai_messages(
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "f", "arguments": {"a": 1}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "f", "content": "done"},
        ]
    )
    assert wire[1]["tool_calls"][0]["function"] == {"name": "f", "arguments": '{"a": 1}'}
    assert wire[2] == {"role": "tool", "tool_call_id": "c1", "content": "done"}


def test_openai_list_models(monkeypatch):
    seen = {}

    class R:
        def raise_for_status(self): pass
        def json(self): return {"data": [{"id": "b"}, {"id": "a"}]}

    def fake_get(url, headers=None, timeout=None):
        seen["url"], seen["headers"] = url, headers
        return R()

    monkeypatch.setattr(requests, "get", fake_get)
    assert list_models(OPENAI_CFG) == ["a", "b"]
    assert seen["url"] == "https://api.openai.com/v1/models" and seen["headers"]["Authorization"] == "Bearer sk-test"


# ------------------------------------------------------------------ Ollama

def test_ollama_payload_has_context_window_and_native_tools(monkeypatch):
    calls = capture_post(monkeypatch, [FakeStreamResponse(ndjson({"message": {"content": "hi"}, "done": True}))])
    OllamaBackend({**OLLAMA_CFG, "context_length": 16384}).stream(USER, APP_TOOL_DEFINITIONS, threading.Event())
    payload = calls[0]["json"]
    assert calls[0]["url"] == "http://localhost:11434/api/chat"
    assert payload["options"]["num_ctx"] == 16384 and payload["tools"] == APP_TOOL_DEFINITIONS


def test_ollama_parses_tool_calls_with_string_or_dict_arguments(monkeypatch):
    lines = ndjson(
        {"message": {"tool_calls": [{"function": {"name": "a", "arguments": {"x": 1}}}]}},
        {"message": {"tool_calls": [{"function": {"name": "b", "arguments": '{"y": 2}'}}]}, "done": True},
    )
    capture_post(monkeypatch, [FakeStreamResponse(lines)])
    result = OllamaBackend(OLLAMA_CFG).stream(USER, APP_TOOL_DEFINITIONS, threading.Event())
    assert [(c["name"], c["arguments"]) for c in result.tool_calls] == [("a", {"x": 1}), ("b", {"y": 2})]


def test_ollama_error_line_raises(monkeypatch):
    capture_post(monkeypatch, [FakeStreamResponse(ndjson({"error": "model 'x' not found"}))])
    with pytest.raises(LLMError, match="not found"):
        OllamaBackend(OLLAMA_CFG).stream(USER, None, threading.Event())


def test_ollama_tool_message_carries_tool_name():
    wire = to_ollama_messages([{"role": "tool", "name": "f", "content": "ok", "tool_call_id": "x"}])
    assert wire == [{"role": "tool", "content": "ok", "tool_name": "f"}]


def test_ollama_list_models_uses_api_tags(monkeypatch):
    seen = {}

    class R:
        def raise_for_status(self): pass
        def json(self): return {"models": [{"name": "qwen3:8b"}, {"name": "llama3.1:latest"}]}

    monkeypatch.setattr(requests, "get", lambda url, **kw: seen.setdefault("url", url) and R())
    assert list_models(OLLAMA_CFG) == ["llama3.1:latest", "qwen3:8b"]
    assert seen["url"] == "http://localhost:11434/api/tags"


def test_test_connection_reports_missing_model_and_success(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return {"models": [{"name": "llama3.1:latest"}]}

    monkeypatch.setattr(requests, "get", lambda url, **kw: R())
    ok, msg = test_connection(OLLAMA_CFG)
    assert ok and "Connected" in msg  # "llama3.1" matches "llama3.1:latest"
    ok, msg = test_connection({**OLLAMA_CFG, "model_name": "nope"})
    assert not ok and "nope" in msg and "llama3.1:latest" in msg


def test_test_connection_unreachable(monkeypatch):
    def boom(url, **kw):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "get", boom)
    ok, msg = test_connection(OLLAMA_CFG)
    assert not ok and "Could not reach" in msg


# ------------------------------------------------------------------ Anthropic

def test_anthropic_conversion_merges_roles_and_wraps_tool_results():
    system, turns = to_anthropic(
        [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok", "tool_calls": [{"id": "t1", "name": "f", "arguments": {"a": 1}}]},
            {"role": "tool", "tool_call_id": "t1", "name": "f", "content": "result"},
            {"role": "user", "content": "thanks"},
        ]
    )
    assert system == "be brief"
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    assert turns[1]["content"][1] == {"type": "tool_use", "id": "t1", "name": "f", "input": {"a": 1}}
    assert turns[2]["content"][0]["type"] == "tool_result" and turns[2]["content"][1]["type"] == "text"


def test_anthropic_conversation_always_starts_with_user():
    _system, turns = to_anthropic([{"role": "assistant", "content": "hello"}])
    assert turns[0]["role"] == "user" and turns[1]["role"] == "assistant"


def test_anthropic_tools_use_input_schema():
    tools = to_anthropic_tools(APP_TOOL_DEFINITIONS)
    assert tools[0]["input_schema"]["type"] == "object" and {"name", "description", "input_schema"} == set(tools[0])


def test_anthropic_streams_text_and_tool_use(monkeypatch):
    lines = sse(
        {"type": "message_start"},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Working"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu1", "name": "create_terminal"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"name":'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"lab"}'}},
        {"type": "message_stop"},
    )
    lines.insert(0, "event: message_start")
    calls = capture_post(monkeypatch, [FakeStreamResponse(lines)])
    chunks = []
    result = AnthropicBackend(ANTHROPIC_CFG).stream(
        [{"role": "system", "content": "sys"}, *USER], APP_TOOL_DEFINITIONS, threading.Event(), chunks.append
    )
    assert result.content == "Working" and chunks == ["Working"]
    assert result.tool_calls == [{"id": "tu1", "name": "create_terminal", "arguments": {"name": "lab"}}]
    call = calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == "ak-test" and "anthropic-version" in call["headers"]
    assert call["json"]["system"] == "sys" and call["json"]["max_tokens"] == 2048


def test_anthropic_requires_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="API key"):
        AnthropicBackend({**ANTHROPIC_CFG, "api_key": ""}).stream(USER, None, threading.Event())


def test_anthropic_error_event_raises(monkeypatch):
    capture_post(monkeypatch, [FakeStreamResponse(sse({"type": "error", "error": {"message": "overloaded"}}))])
    with pytest.raises(LLMError, match="overloaded"):
        AnthropicBackend(ANTHROPIC_CFG).stream(USER, None, threading.Event())


def test_anthropic_temperature_is_clamped_to_api_range(monkeypatch):
    calls = capture_post(monkeypatch, [FakeStreamResponse(sse({"type": "message_stop"}))])
    AnthropicBackend({**ANTHROPIC_CFG, "temperature": 1.8}).stream(USER, None, threading.Event())
    assert calls[0]["json"]["temperature"] == 1.0
