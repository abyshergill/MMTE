from core.log_clean import LogCleaner
from core.macro_recorder import MacroRecorder, extract_prompt
from core.plugins import PluginManager


def test_log_cleaner_removes_escape_garbage():
    raw = (
        "\x1b[1;32muart:~$ \x1b[m\x1b[8D\x1b[J[00:00:00.000] \x1b[0m<inf> i3c: init\x1b[0m\r\n"
        "\x1b7\x1b[999;999H\x1b[6n\x1b8root@a:~# \r\n"
        "Loaded: (\x1b]8;;file://a/x\x07/usr/x\x1b]8;;\x07 ); \x1b[0;32menabled\x1b[0m\r\n"
    )
    out = LogCleaner().feed(raw)
    assert "\x1b" not in out and "[m" not in out
    assert "[00:00:00.000] <inf> i3c: init" in out
    assert "Loaded: (/usr/x ); enabled" in out


def test_log_cleaner_handles_split_sequences():
    c = LogCleaner()
    out = c.feed("ab\x1b[3") + c.feed("1mcd\n")
    assert out == "abcd\n"


def test_macro_keeps_prompt_as_comment():
    r = MacroRecorder(session_name="t")
    r.record_line("ls -l", where=extract_prompt("root@a:~# ls -l"))
    script = r.to_python()
    assert "# [root@a:~#]" in script
    assert "send('ls -l')" in script
    assert "Replaying recorded macro" not in script


def test_plugin_loading_and_errors(tmp_path):
    (tmp_path / "good.py").write_text(
        "PLUGIN_NAME='Good'\n"
        "def register(api):\n"
        "    api.add_menu_action('Hi', lambda: None)\n"
        "    api.add_script_function('twice', lambda x: x * 2)\n"
    )
    (tmp_path / "bad.py").write_text("def register(api):\n    raise RuntimeError('boom')\n")
    (tmp_path / "noreg.py").write_text("X = 1\n")
    mgr = PluginManager(app=None, directories=[tmp_path])
    mgr.load_all()
    by_name = {p.path.stem: p for p in mgr.plugins}
    assert by_name["good"].loaded and by_name["good"].name == "Good"
    assert mgr.script_functions["twice"](3) == 6
    assert not by_name["bad"].loaded and "boom" in by_name["bad"].error
    assert not by_name["noreg"].loaded


def test_plugin_template_is_valid(tmp_path):
    path = PluginManager.create_from_template("My Plugin", tmp_path)
    mgr = PluginManager(app=None, directories=[tmp_path])
    mgr.load_all()
    assert mgr.plugins[0].loaded and path.exists()


def test_sidebar_show_button(qapp):
    import main as main_module
    win = main_module.TerminalApp()
    try:
        win.show()
        assert not win.sidebar_show_btn.isVisible()
        win.toggle_sidebar()
        assert win.sidebar_show_btn.isVisible()
        win.sidebar_show_btn.click()
        assert win.sidebar.isVisible() and not win.sidebar_show_btn.isVisible()
    finally:
        win.close()
