import json
import threading

from core import llm_chat


class FakeResponse:
    def __init__(self, lines):
        self.lines = lines
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_lines(self, decode_unicode=True):
        yield from self.lines

    def close(self):
        self.closed = True


def test_stream_chat_uses_openai_compatible_endpoint(monkeypatch):
    response = FakeResponse([
        'data: {"choices":[{"delta":{"content":"Hello"}}]}',
        'data: {"choices":[{"delta":{"content":" there"}}]}',
        "data: [DONE]",
    ])
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["payload"] = kwargs["json"]
        return response

    monkeypatch.setattr(llm_chat.requests, "post", fake_post)
    chunks = []
    result = llm_chat.stream_chat(
        {"endpoint_url": "http://localhost:1234/v1", "model_name": "local-model"},
        [{"role": "user", "content": "Hi"}],
        threading.Event(),
        chunks.append,
    )

    assert captured["url"] == "http://localhost:1234/v1/chat/completions"
    assert captured["payload"]["stream"] is True
    assert result == "Hello there"
    assert chunks == ["Hello", " there"]
    assert response.closed


def test_stream_chat_supports_native_ollama_endpoint(monkeypatch):
    response = FakeResponse([
        json.dumps({"message": {"content": "Local"}}),
        json.dumps({"message": {"content": " response"}, "done": True}),
    ])
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["payload"] = kwargs["json"]
        return response

    monkeypatch.setattr(llm_chat.requests, "post", fake_post)
    result = llm_chat.stream_chat(
        {"endpoint_url": "http://localhost:11434/api/chat", "model_name": "qwen2.5:7b"},
        [{"role": "user", "content": "Hi"}],
        threading.Event(),
        lambda _chunk: None,
    )

    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["payload"]["options"]["num_predict"] == 2048
    assert result == "Local response"


def test_ollama_root_url_routes_to_native_chat_api():
    endpoint, ollama_native = llm_chat._chat_endpoint(
        {"provider": "Ollama (Local)", "endpoint_url": "http://127.0.0.1:11434"}
    )

    assert endpoint == "http://127.0.0.1:11434/api/chat"
    assert ollama_native is True


def test_bare_openai_compatible_host_gets_v1_path():
    endpoint, ollama_native = llm_chat._chat_endpoint(
        {"provider": "Custom / Local HTTP", "endpoint_url": "http://localhost:1234"}
    )

    assert endpoint == "http://localhost:1234/v1/chat/completions"
    assert ollama_native is False


def test_chat_with_app_tools_handles_qwen_plain_json_tool_call(monkeypatch):
    first = FakeResponse([
        json.dumps({
            "message": {
                "content": '{"name":"create_terminal","arguments":{"name":"aby"}}',
                "done": True,
            }
        })
    ])
    second = FakeResponse([
        json.dumps({"message": {"content": "Created the application tab aby.", "done": True}})
    ])
    responses = iter([first, second])
    captured_payloads = []

    def fake_post(_url, **kwargs):
        captured_payloads.append(kwargs["json"])
        return next(responses)

    monkeypatch.setattr(llm_chat.requests, "post", fake_post)
    tool_results = []
    output_chunks = []
    result = llm_chat.chat_with_app_tools(
        {"provider": "Ollama (Local)", "endpoint_url": "http://127.0.0.1:11434", "model_name": "qwen"},
        [
            {"role": "system", "content": "Use app tools."},
            {"role": "user", "content": "Launch a terminal named aby."},
        ],
        threading.Event(),
        output_chunks.append,
        lambda name, arguments: tool_results.append((name, arguments)) or "Application created aby.",
    )

    assert tool_results == [("create_terminal", {"name": "aby"})]
    assert captured_payloads[0]["tools"] == llm_chat.APP_TOOL_DEFINITIONS
    assert captured_payloads[1]["tools"] == llm_chat.APP_TOOL_DEFINITIONS
    assert captured_payloads[1]["messages"][-1]["role"] == "user"
    assert "Application created aby." in captured_payloads[1]["messages"][-1]["content"]
    assert "Do not repeat an app operation" in captured_payloads[1]["messages"][-1]["content"]
    assert result == "Created the application tab aby."
    assert output_chunks == [result]
    assert first.closed and second.closed


def test_extract_json_tool_call_accepts_markdown_and_nested_arguments():
    tool_names = {"rename_terminal"}
    fenced = '```json\n{"name":"rename_terminal","arguments":{"terminal_name":"Terminal 1","new_name":"Terminal 2"}}\n```'
    plain = '{"name":"rename_terminal","arguments":{"terminal_name":"current","new_name":"abc"}}'

    assert llm_chat._extract_json_tool_call(fenced, tool_names)["arguments"]["new_name"] == "Terminal 2"
    assert llm_chat._extract_json_tool_call(plain, tool_names)["arguments"]["new_name"] == "abc"


def test_chat_with_app_tools_deduplicates_repeated_json_tool_calls(monkeypatch):
    repeated_call = json.dumps({
        "message": {
            "content": '{"name":"create_terminal","arguments":{"name":"aby"}}',
            "done": True,
        }
    })
    final_response = json.dumps({"message": {"content": "The terminal was created.", "done": True}})
    responses = iter([FakeResponse([repeated_call]), FakeResponse([repeated_call]), FakeResponse([final_response])])
    payloads = []

    def fake_post(_url, **kwargs):
        payloads.append(kwargs["json"])
        return next(responses)

    monkeypatch.setattr(llm_chat.requests, "post", fake_post)
    calls = []
    result = llm_chat.chat_with_app_tools(
        {"endpoint_url": "http://127.0.0.1:11434", "model_name": "qwen"},
        [{"role": "user", "content": "Create terminal aby"}],
        threading.Event(),
        lambda _chunk: None,
        lambda name, arguments: calls.append((name, arguments)) or "Created aby once.",
    )

    assert len(calls) == 1
    assert payloads[2]["tools"] == []
    assert result == "The terminal was created."