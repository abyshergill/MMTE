"""Qt-free tests for core.terminal_dispatch (target selection + command handling)."""

from __future__ import annotations

from core.terminal_dispatch import (
    MultiRunTracker,
    ProposalQueue,
    build_send_script,
    clean_commands,
    connection_summary,
    default_target,
    extract_commands,
    parse_command_lines,
    resolve_targets,
)


class S:
    def __init__(self, name, connected=False, label="Disconnected"):
        self.name = name
        self.connection = object() if connected else None
        self.connected_label = label if connected else "Disconnected"


# ---- default target ---------------------------------------------------------

def test_default_target_prefers_connected_current_tab():
    a, b = S("Terminal 1", True, "SSH a"), S("Terminal 2", True, "Serial COM6")
    assert default_target([a, b], b) is b


def test_default_target_falls_back_to_only_connected_tab():
    blank, live = S("Terminal 1"), S("Terminal 2", True, "Serial COM6")
    assert default_target([blank, live], blank) is live


def test_default_target_keeps_current_when_nothing_or_many_connected():
    a, b, c = S("Terminal 1"), S("Terminal 2", True), S("Terminal 3", True)
    assert default_target([a], a) is a
    assert default_target([a, b, c], a) is a


def test_default_target_none_without_tabs():
    assert default_target([], None) is None


# ---- resolve_targets --------------------------------------------------------

def test_resolve_current_uses_default_target():
    blank, live = S("Terminal 1"), S("Terminal 2", True, "SSH x")
    res = resolve_targets("current", [blank, live], blank)
    assert res.sessions == [live] and not res.not_connected and not res.missing


def test_resolve_all_selects_only_connected():
    a, b, c = S("Terminal 1", True), S("Terminal 2"), S("Terminal 3", True)
    res = resolve_targets("all terminals", [a, b, c], a)
    assert res.sessions == [a, c]


def test_resolve_named_list_and_case_insensitive():
    a, b, c = S("Terminal 1", True), S("Terminal 2", True), S("Lab DUT", True)
    res = resolve_targets("terminal 1, LAB dut", [a, b, c], b)
    assert res.sessions == [a, c]


def test_resolve_bare_number_means_terminal_n():
    a, b = S("Terminal 1", True), S("Terminal 2", True)
    assert resolve_targets("2", [a, b], a).sessions == [b]


def test_resolve_reports_missing_and_unconnected():
    a, b = S("Terminal 1"), S("Terminal 2", True)
    res = resolve_targets("Terminal 1 and Terminal 9", [a, b], b)
    assert res.not_connected == [a]
    assert res.missing == ["Terminal 9"]
    assert not res.ok


def test_resolve_exact_name_containing_and_is_not_split():
    odd = S("Rack and Stack", True)
    assert resolve_targets("Rack and Stack", [odd], odd).sessions == [odd]


def test_resolve_deduplicates():
    a = S("Terminal 1", True)
    assert resolve_targets("current, Terminal 1, all", [a], a).sessions == [a]


def test_resolve_all_with_nothing_connected_has_note():
    res = resolve_targets("all", [S("Terminal 1")], None)
    assert not res.ok and "connected" in res.note.lower()


# ---- commands ---------------------------------------------------------------

def test_extract_commands_basic_and_prompt_marker():
    text = "Try:\n```bash\n# list files\n$ ls -la\nip addr\n```"
    assert extract_commands(text) == ["ls -la", "ip addr"]


def test_extract_commands_untagged_fence_and_multiple_blocks():
    text = "```\nuname -a\n```\nthen\n```sh\ndf -h\n```"
    assert extract_commands(text) == ["uname -a", "df -h"]


def test_extract_commands_ignores_non_shell_fences_and_does_not_mispair():
    text = (
        "```python\nprint('x')\n```\n"
        "Some prose that must never become a command.\n"
        "```bash\nreboot\n```"
    )
    assert extract_commands(text) == ["reboot"]


def test_extract_commands_none_found():
    assert extract_commands("no code here") == []
    assert extract_commands("") == []


def test_clean_commands_strips_control_chars_and_limits():
    out = clean_commands(["ls\x1b[31m", "  ", "a\nb"])
    assert out == ["ls[31m", "a", "b"]
    assert len(clean_commands(["x"] * 500)) == 50


def test_parse_command_lines_skips_comments_and_blanks():
    assert parse_command_lines("# note\n\nls\n  pwd  \n") == ["ls", "pwd"]


def test_build_send_script_is_valid_python_and_quotes_safely():
    code = build_send_script(['echo "it\'s"', "ls"], delay=0.1, label="T1", select_name="T1")
    compile(code, "<script>", "exec")  # must be syntactically valid
    assert code.startswith("terminal_select('T1')")
    assert code.count("send(") == 2
    assert "wait(0.5)" in code  # delay is floored so slow devices can answer


# ---- bookkeeping ------------------------------------------------------------

def test_connection_summary():
    assert connection_summary(S("a")) == "Not connected"
    assert connection_summary(S("a", True, "SSH 10.0.0.2")) == "SSH 10.0.0.2"


def test_proposal_queue_groups_by_target_in_order():
    q = ProposalQueue()
    assert q.add("Terminal 2", "ls")
    assert q.add("current", "pwd")
    assert q.add("terminal 2", "df")
    assert not q.add("current", "   ")
    assert len(q) == 3
    assert q.groups() == [("Terminal 2", ["ls", "df"]), ("current", ["pwd"])]
    q.clear()
    assert len(q) == 0 and q.groups() == []


def test_multi_run_tracker_fires_once_after_all_complete():
    fired = []
    t = MultiRunTracker(2, fired.append)
    t.complete("A", "ok")
    assert fired == []
    t.complete("B", "busy")
    assert fired == [{"A": "ok", "B": "busy"}]
    t.complete("C")  # late/extra completions never re-fire
    assert len(fired) == 1


def test_multi_run_tracker_zero_total_fires_immediately():
    fired = []
    MultiRunTracker(0, fired.append)
    assert fired == [{}]
