import threading

from PyQt6.QtCore import QEventLoop, QTimer

from core.llm_chat import APP_TOOL_DEFINITIONS
from core.application_knowledge import APPLICATION_MEMORY_NAME
from gui.ai_assistant_panel import AIAssistantPanel


def _mock_app_tool_agent(monkeypatch, invoke_tools, captured=None):
    if captured is None:
        captured = {}

    def fake_chat(_config, messages, _cancel, on_chunk, tool_handler, _set_response, tools=None):
        captured["messages"] = messages
        captured["tools"] = tools
        result = invoke_tools(tool_handler)
        captured["tool_result"] = result
        on_chunk("The application tool completed. " + str(result))
        return "The application tool completed. " + str(result)

    monkeypatch.setattr("gui.ai_assistant_panel.chat_with_app_tools", fake_chat)
    return captured


def _wait_for_panel_request(panel, qapp):
    loop = QEventLoop()
    panel._bridge.request_finished.connect(lambda _cancelled: loop.quit())
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    qapp.processEvents()


def test_explicit_request_dispatches_response_block(monkeypatch, qapp):
    class FakeConfig:
        config = {"model_name": "test-model"}

        def get(self, key, default=None):
            return self.config.get(key, default)

    class FakeRag:
        def list_names(self):
            return []

    monkeypatch.setattr("gui.ai_assistant_panel.LLMConfigManager", FakeConfig)
    monkeypatch.setattr("gui.ai_assistant_panel.RAGMemoryManager", FakeRag)
    panel = AIAssistantPanel()
    panel._streamed_response = '```bash\necho "Hello world"\n```'
    panel._auto_run_requested = True
    panel._request_failed = False
    panel._discard_current_response = False
    panel._cancel_event = threading.Event()
    target_session = object()
    panel._request_session = target_session
    dispatched = {}

    def record_dispatch(commands, **kwargs):
        dispatched["commands"] = commands
        dispatched.update(kwargs)

    monkeypatch.setattr(panel, "run_ai_script_sequence", record_dispatch)
    panel._handle_request_finished(cancelled=False)

    assert dispatched == {
        "commands": ['echo "Hello world"'],
        "session": target_session,
        "use_current_session": False,
    }
    panel.close()


def test_explicit_terminal_write_request_is_detected():
    assert AIAssistantPanel._is_explicit_terminal_execution_request(
        "Write hello world inside this terminal 1"
    )


def test_explicit_terminal_run_request_is_detected():
    assert AIAssistantPanel._is_explicit_terminal_execution_request(
        "Please run this command in Terminal 1"
    )


def test_numbered_terminal_request_resolves_named_session(monkeypatch, qapp):
    class FakeConfig:
        config = {"model_name": "test-model"}

        def get(self, key, default=None):
            return self.config.get(key, default)

    class FakeRag:
        def list_names(self):
            return []

    class FakeWindow:
        active = object()
        terminal_one = object()

        def current_session(self):
            return self.active

        def get_session_by_name(self, name):
            assert name == "Terminal 1"
            return self.terminal_one

    monkeypatch.setattr("gui.ai_assistant_panel.LLMConfigManager", FakeConfig)
    monkeypatch.setattr("gui.ai_assistant_panel.RAGMemoryManager", FakeRag)
    panel = AIAssistantPanel()
    window = FakeWindow()
    panel.main_window = window

    assert panel._resolve_request_session("Write hello world in Terminal 1") is window.terminal_one
    assert panel._request_target_name == "Terminal 1"
    panel.close()


def test_log_analysis_is_not_mistaken_for_execution():
    assert not AIAssistantPanel._is_explicit_terminal_execution_request(
        "Analyze recent terminal logs for errors and suggest corrective steps"
    )


def test_advice_question_is_not_mistaken_for_execution():
    assert not AIAssistantPanel._is_explicit_terminal_execution_request(
        "What command should I write inside the terminal?"
    )


def test_advice_only_prompt_gets_read_only_app_tools():
    names = {
        tool["function"]["name"]
        for tool in AIAssistantPanel._tools_for_request(
            "In this application, how should you rename an app terminal tab? Do not rename anything; explain the mechanism."
        )
    }

    assert names == {"list_terminals", "read_terminal"}


def test_explicit_rename_prompt_gets_rename_tool():
    names = {
        tool["function"]["name"]
        for tool in AIAssistantPanel._tools_for_request("Rename Terminal 1 to Engineering")
    }

    assert "rename_terminal" in names
    assert "create_terminal" not in names


def test_multi_terminal_launch_gets_create_and_connect_tools():
    query = (
        "launch two terminals and print in them; SSH 127.0.0.1 2222 testuser testpass; "
        "Telnet 127.0.0.1 2323"
    )
    names = {
        tool["function"]["name"]
        for tool in AIAssistantPanel._tools_for_request(query)
    }

    assert {"create_terminal", "connect_terminal", "send_terminal"} <= names


def test_log_analysis_request_includes_active_terminal_scrollback(monkeypatch, qapp):
    class FakeConfig:
        config = {"model_name": "test-model"}

        def get(self, key, default=None):
            return self.config.get(key, default)

    class FakeRag:
        def list_names(self):
            return []

    class FakeSession:
        name = "Terminal 1"
        connection = None
        connected_label = "Disconnected"

        def __init__(self):
            self.connection = None
            self.connected_label = "Disconnected"
        terminal = object()

        def __init__(self):
            self.connection = None
            self.connected_label = "Disconnected"

        def recent_output(self, _limit):
            return "ERROR: simulated device failure"

    class FakeWindow:
        def __init__(self, session):
            self.session = session
            self.sessions = [session]

        def current_session(self):
            return self.session

    monkeypatch.setattr("gui.ai_assistant_panel.LLMConfigManager", FakeConfig)
    monkeypatch.setattr("gui.ai_assistant_panel.RAGMemoryManager", FakeRag)
    captured = {}

    def fake_chat(_config, messages, _cancel, on_chunk, _tool_handler, _set_response, tools=None):
        captured["messages"] = messages
        captured["tools"] = tools
        on_chunk("The attached log contains an error.")
        return "The attached log contains an error."

    monkeypatch.setattr("gui.ai_assistant_panel.chat_with_app_tools", fake_chat)
    panel = AIAssistantPanel()
    panel.main_window = FakeWindow(FakeSession())
    panel.prompt_input.setText("Analyze recent terminal logs for errors")
    loop = QEventLoop()
    panel._bridge.request_finished.connect(lambda _cancelled: loop.quit())
    QTimer.singleShot(3000, loop.quit)
    panel.send_query()
    loop.exec()

    system_prompt = captured["messages"][0]["content"]
    assert "ERROR: simulated device failure" in system_prompt
    assert "Modern Multi Terminal" in system_prompt
    assert any(tool["function"]["name"] == "read_terminal" for tool in captured["tools"])
    assert "[Attached 31 characters from Terminal 1]" in panel.chat_display.toPlainText()
    assert not panel._auto_run_requested
    panel.close()


def test_application_rag_passages_are_retrieved_into_ollama_system_prompt(monkeypatch, qapp):
    class FakeConfig:
        config = {"model_name": "qwen2.5-coder:3b", "endpoint_url": "http://127.0.0.1:11434"}

        def get(self, key, default=None):
            return self.config.get(key, default)

    class FakeRag:
        def ensure_application_memory(self):
            pass

        def list_names(self):
            return [APPLICATION_MEMORY_NAME]

        def get(self, name):
            return {
                "name": name,
                "assigned_job": "Application-only AI assistant engineer",
                "text_entries": ["App-owned instruction text."],
            }

        def retrieve_relevant(self, name, query, endpoint_url, top_k):
            assert endpoint_url == "http://127.0.0.1:11434"
            assert top_k == 5
            return "[Knowledge 1]\nAI terminal actions use terminal_new and connect through the app engine.", "Ollama embeddings"

    class FakeSession:
        name = "Terminal 1"
        connection = None
        connected_label = "Disconnected"
        terminal = object()

        def recent_output(self, _limit):
            return ""

    class FakeWindow:
        def __init__(self, session):
            self.session = session
            self.sessions = [session]

        def current_session(self):
            return self.session

    monkeypatch.setattr("gui.ai_assistant_panel.LLMConfigManager", FakeConfig)
    monkeypatch.setattr("gui.ai_assistant_panel.RAGMemoryManager", FakeRag)
    captured = {}

    def fake_chat(_config, messages, _cancel, on_chunk, _tool_handler, _set_response, tools=None):
        captured["messages"] = messages
        on_chunk("I will use the app engine.")

    monkeypatch.setattr("gui.ai_assistant_panel.chat_with_app_tools", fake_chat)
    panel = AIAssistantPanel()
    panel.main_window = FakeWindow(FakeSession())
    panel.prompt_input.setText("How does the application send a command to its terminal?")
    panel.send_query()
    _wait_for_panel_request(panel, qapp)

    system_prompt = captured["messages"][0]["content"]
    assert "AI terminal actions use terminal_new and connect through the app engine" in system_prompt
    assert "Ollama embeddings" in system_prompt
    panel.close()


def test_extract_commands_uses_response_code_block():
    response = 'Run this command:\n```bash\necho "Hello world"\n```'
    assert AIAssistantPanel._extract_commands_from_text(response) == ['echo "Hello world"']


def test_application_output_is_returned_to_llm_for_summary(monkeypatch, qapp):
    class FakeConfig:
        config = {"model_name": "test-model"}

        def get(self, key, default=None):
            return self.config.get(key, default)

    class FakeRag:
        def list_names(self):
            return []

    monkeypatch.setattr("gui.ai_assistant_panel.LLMConfigManager", FakeConfig)
    monkeypatch.setattr("gui.ai_assistant_panel.RAGMemoryManager", FakeRag)
    captured = {}

    def fake_stream(config, messages, _cancel, on_chunk, _set_response):
        captured["messages"] = messages
        on_chunk("The test Telnet server echoed the command.")
        return "The test Telnet server echoed the command."

    monkeypatch.setattr("gui.ai_assistant_panel.stream_chat", fake_stream)
    panel = AIAssistantPanel()
    loop = QEventLoop()
    panel._bridge.request_finished.connect(lambda _cancelled: loop.quit())
    panel._handle_automation_result(
        "Send AI commands to Terminal 2",
        "[Terminal 2]\nEcho: echo hello\n",
        summarize_output=True,
    )
    QTimer.singleShot(3000, loop.quit)
    loop.exec()

    assert "Echo: echo hello" in str(captured["messages"])
    assert "Echo: echo hello" in panel.chat_display.toPlainText()
    assert "The test Telnet server echoed the command." in panel.chat_display.toPlainText()
    panel.close()


def test_connection_specs_parse_ssh_and_telnet_table():
    query = (
        "launch 2 terminals: Terminal 1 SSH 127.0.0.1 2222 testuser testpass; "
        "Terminal 2 Telnet 127.0.0.1 2323; print terminal 1 and terminal 2"
    )

    specs = AIAssistantPanel._parse_terminal_connection_specs(query)

    assert specs == [
        {"protocol": "ssh", "host": "127.0.0.1", "port": 2222, "username": "testuser", "password": "testpass"},
        {"protocol": "telnet", "host": "127.0.0.1", "port": 2323},
    ]


def test_connection_specs_can_assign_ssh_to_terminal_one():
    query = "launch 2 terminals: Terminal 1 with SSH and 1 with Telnet. Telnet 127.0.0.1 2323; SSH 127.0.0.1 2222 testuser testpass"
    specs = AIAssistantPanel._parse_terminal_connection_specs(query)

    ordered = AIAssistantPanel._order_terminal_specs(query, specs)
    assert ordered[0] == {
        "protocol": "ssh",
        "host": "127.0.0.1",
        "port": 2222,
        "username": "testuser",
        "password": "testpass",
    }
    assert ordered[1] == {"protocol": "telnet", "host": "127.0.0.1", "port": 2323}


def test_terminal_management_request_recognizes_two_services():
    query = (
        "Based on this data launch 2 terminal 1 with ssh and 1 with telnet and "
        "print inside those terminal terminal 1 and terminal 2. SSH 127.0.0.1 2222 testuser testpass; "
        "Telnet 127.0.0.1 2323"
    )
    specs = AIAssistantPanel._parse_terminal_connection_specs(query)

    assert AIAssistantPanel._is_multi_terminal_setup_request(query, specs)
    assert "print" in query.lower()


def test_rename_request_parses_named_tab():
    assert AIAssistantPanel._parse_rename_request("rename the terminal 1 to abc") == ("terminal 1", "abc")
    assert AIAssistantPanel._parse_rename_request("rename terminal 1 abc") == ("terminal 1", "abc")
    assert AIAssistantPanel._parse_rename_request(
        "change the current Terminal 1 name to Terminal 2"
    ) == ("Terminal 1", "Terminal 2")


def test_create_terminal_request_parses_application_wording():
    assert AIAssistantPanel._parse_create_terminal_request(
        "launch the new terminal for application with name aby"
    ) == "aby"
    assert AIAssistantPanel._parse_create_terminal_request(
        "launch a new terminal and give name aby"
    ) == "aby"


def test_send_query_renames_real_app_terminal_immediately(monkeypatch, qapp):
    class FakeConfig:
        config = {"model_name": "test-model"}

        def get(self, key, default=None):
            return self.config.get(key, default)

    class FakeRag:
        def list_names(self):
            return []

    class FakeSession:
        name = "Terminal 1"
        connection = None
        connected_label = "Disconnected"

    class FakeWindow:
        def __init__(self):
            self.session = FakeSession()
            self.automation = []
            self.sessions = [self.session]

        def get_session_by_name(self, name):
            return self.session if self.session.name.lower() == name.lower() else None

        def rename_terminal_by_name(self, current, new):
            session = self.get_session_by_name(current)
            if not session:
                return False
            session.name = new
            return True

        def run_ai_automation(self, session, code, script_name, on_complete=None):
            self.automation.append((session, code, script_name, on_complete))
            if on_complete:
                on_complete(f"[{session.name}]\n{script_name} completed\n")
            return True

        def after(self, _ms, callback):
            callback()

    monkeypatch.setattr("gui.ai_assistant_panel.LLMConfigManager", FakeConfig)
    monkeypatch.setattr("gui.ai_assistant_panel.RAGMemoryManager", FakeRag)
    panel = AIAssistantPanel()
    window = FakeWindow()
    panel.main_window = window
    captured = _mock_app_tool_agent(
        monkeypatch,
        lambda call: call("rename_terminal", {"terminal_name": "current", "new_name": "Terminal 2"}),
    )
    panel.prompt_input.setText("change the current Terminal 1 name to Terminal 2")

    panel.send_query()
    _wait_for_panel_request(panel, qapp)

    assert window.session.name == "Terminal 1"
    assert "terminal_rename('Terminal 1', 'Terminal 2')" in window.automation[0][1]
    assert "Renamed Terminal 1 to Terminal 2" in window.automation[0][1]
    assert "application's Python automation engine" in panel.chat_display.toPlainText()
    assert window.automation[0][3] is not None
    assert {tool["function"]["name"] for tool in captured["tools"]} == {
        "list_terminals", "read_terminal", "rename_terminal"
    }
    panel.close()


def test_send_query_creates_named_application_terminal(monkeypatch, qapp):
    class FakeConfig:
        config = {"model_name": "test-model"}

        def get(self, key, default=None):
            return self.config.get(key, default)

    class FakeRag:
        def list_names(self):
            return []

    class FakeSession:
        def __init__(self, name):
            self.name = name
            self.connection = None
            self.connected_label = "Disconnected"

    class FakeWindow:
        def __init__(self):
            self.sessions = [FakeSession("Terminal 1")]
            self.active = self.sessions[0]
            self.automation = []

        def get_session_by_name(self, name):
            return next((session for session in self.sessions if session.name.lower() == name.lower()), None)

        def current_session(self):
            return self.active

        def new_terminal(self, name):
            session = FakeSession(name)
            self.sessions.append(session)
            self.active = session
            return session

        def select_session(self, session):
            self.active = session

        def run_ai_automation(self, session, code, script_name, on_complete=None):
            self.automation.append((session, code, script_name, on_complete))
            if on_complete:
                on_complete(f"[{session.name}]\n{script_name} completed\n")
            return True

        def after(self, _ms, callback):
            callback()

    monkeypatch.setattr("gui.ai_assistant_panel.LLMConfigManager", FakeConfig)
    monkeypatch.setattr("gui.ai_assistant_panel.RAGMemoryManager", FakeRag)
    panel = AIAssistantPanel()
    window = FakeWindow()
    panel.main_window = window
    _mock_app_tool_agent(
        monkeypatch,
        lambda call: call("create_terminal", {"name": "aby"}),
    )
    panel.prompt_input.setText("launch the new terminal for application with name aby")

    panel.send_query()
    _wait_for_panel_request(panel, qapp)

    assert len(window.sessions) == 1
    assert "terminal_new('aby')" in window.automation[0][1]
    assert "Created and selected terminal aby." in window.automation[0][1]
    assert "application's Python automation engine" in panel.chat_display.toPlainText()
    assert window.automation[0][3] is not None
    assert "Application response" in panel.chat_display.toPlainText()
    assert panel._cancel_event is None
    panel.close()


def test_send_query_creates_and_connects_two_named_tabs(monkeypatch, qapp):
    class FakeConfig:
        config = {"model_name": "test-model"}

        def get(self, key, default=None):
            return self.config.get(key, default)

    class FakeRag:
        def list_names(self):
            return []

    class FakeSession:
        def __init__(self, name):
            self.name = name
            self.connection = None
            self.connected_label = "Disconnected"

    class FakeWindow:
        def __init__(self):
            self.sessions = [FakeSession("Terminal 1"), FakeSession("Terminal 2")]
            self.active = self.sessions[0]
            self.automation = []

        def get_session_by_name(self, name):
            return next((session for session in self.sessions if session.name == name), None)

        def current_session(self):
            return self.active

        def select_session(self, session):
            self.active = session

        def new_terminal(self, name):
            session = FakeSession(name)
            self.sessions.append(session)
            return session

        def run_ai_automation(self, session, code, script_name, on_complete=None):
            self.automation.append((session, code, script_name, on_complete))
            if "connect_ssh(" in code or "connect_telnet(" in code:
                session.connection = object()
                session.connected_label = "Connected for test"
            if on_complete:
                on_complete(f"[{session.name}]\n{script_name} completed\n")
            return True

        def after(self, _ms, callback):
            callback()

    monkeypatch.setattr("gui.ai_assistant_panel.LLMConfigManager", FakeConfig)
    monkeypatch.setattr("gui.ai_assistant_panel.RAGMemoryManager", FakeRag)
    panel = AIAssistantPanel()
    window = FakeWindow()
    panel.main_window = window
    tool_results = []

    def ask_app_tools(call):
        planned_calls = [
            ("connect_terminal", {"terminal_name": "Terminal 1", "protocol": "ssh", "host": "127.0.0.1", "port": 2222, "username": "testuser", "password": "testpass"}),
            ("send_terminal", {"terminal_name": "Terminal 1", "text": 'echo "Terminal 1"'}),
            ("connect_terminal", {"terminal_name": "Terminal 2", "protocol": "telnet", "host": "127.0.0.1", "port": 2323}),
            ("send_terminal", {"terminal_name": "Terminal 2", "text": 'echo "Terminal 2"'}),
        ]
        for tool_name, arguments in planned_calls:
            tool_results.append(call(tool_name, arguments))
        return tool_results[-1]

    captured = _mock_app_tool_agent(monkeypatch, ask_app_tools)
    panel.prompt_input.setText(
        "Based on this data launch 2 terminal 1 with ssh and 1 with telnet and print inside those terminals terminal 1 and terminal 2. "
        "Connection Type Host Port Username Password Telnet 127.0.0.1 2323 (None) (None) SSH 127.0.0.1 2222 testuser testpass"
    )

    panel.send_query()
    _wait_for_panel_request(panel, qapp)

    assert len(window.automation) == 4
    scripts = [entry[1] for entry in window.automation]
    assert any("connect_ssh('127.0.0.1', 'testuser', 'testpass', port=2222)" in script for script in scripts)
    assert any("connect_telnet('127.0.0.1', port=2323)" in script for script in scripts)
    assert any("send('echo \"Terminal 1\"')" in script for script in scripts)
    assert any("send('echo \"Terminal 2\"')" in script for script in scripts)
    assert len(tool_results) == 4
    assert {tool["function"]["name"] for tool in captured["tools"]} == {
        "list_terminals", "read_terminal", "create_terminal", "connect_terminal", "send_terminal"
    }
    panel.close()
