# Bridge between the Rust application and user scripts / plugins.
# Protocol: one JSON object per line. We write requests to the real stdout and read replies from stdin.
import json
import os
import sys

_out = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
_in = os.fdopen(os.dup(0), "r", encoding="utf-8")


class _Stopped(BaseException):
    """The user stopped the script."""


_events = []


def _rpc(op, **args):
    args["op"] = op
    _out.write(json.dumps(args) + "\n")
    _out.flush()
    while True:
        line = _in.readline()
        if not line:
            raise _Stopped()
        reply = json.loads(line)
        if "event" in reply:  # output events can arrive while a call waits for its reply
            _events.append(reply)
            continue
        break
    if reply.get("ok"):
        return reply.get("value")
    err = reply.get("error", "error")
    if err == "__stopped__":
        raise _Stopped()
    raise RuntimeError(err)


class _TerminalPrint:
    def write(self, text):
        if text:
            _rpc("print", text=text, end="")
        return len(text)

    def flush(self):
        pass


sys.stdout = _TerminalPrint()
sys.stderr = open(os.devnull, "w") if os.environ.get("MMTE_QUIET_STDERR") else sys.__stderr__


def print(*args, sep=" ", end="\n"):
    _rpc("print", text=sep.join(str(a) for a in args), end=end)


def send(text, newline=True, delay=0.0):
    _rpc("send", text=str(text), newline=bool(newline), delay=float(delay))


def sleep(seconds):
    _rpc("sleep", seconds=float(seconds))


def wait(pattern_or_seconds, timeout=60.0, case_sensitive=True):
    if isinstance(pattern_or_seconds, (int, float)):
        _rpc("sleep", seconds=float(pattern_or_seconds))
        return True
    return bool(_rpc("wait", pattern=str(pattern_or_seconds), timeout=float(timeout), case_sensitive=bool(case_sensitive)))


def wait_connected(timeout=30.0):
    return bool(_rpc("wait_connected", timeout=float(timeout)))


def alert(message):
    return bool(_rpc("alert", message=str(message)))


def input(prompt=""):
    return _rpc("input", prompt=str(prompt))


def connect_ssh(host, username, password, port=22):
    _rpc("connect_ssh", host=host, username=username, password=password, port=int(port))


def connect_serial(port, baudrate, bytesize=8, parity="N", stopbits=1.0):
    _rpc("connect_serial", port=port, baudrate=int(baudrate), bytesize=int(bytesize), parity=str(parity), stopbits=float(stopbits))


def connect_telnet(host, port=23):
    _rpc("connect_telnet", host=host, port=int(port))


def log_on(log_path):
    _rpc("log_on", path=str(log_path))


def _log_off(log_path=None):
    _rpc("log_off", path=None if log_path is None else str(log_path))


class _LogOff:
    def __call__(self, log_path=None):
        _log_off(log_path)

    def __repr__(self):
        _log_off()
        return "<log_off executed>"


log_off = _LogOff()


def terminal_new(name=None):
    return _rpc("terminal_new", name=name)


def terminal_close(name=None):
    _rpc("terminal_close", name=name)


def terminal_rename(current_name, new_name):
    return bool(_rpc("terminal_rename", current=current_name, new=new_name))


def terminal_list():
    return _rpc("terminal_list")


def terminal_select(name):
    return bool(_rpc("terminal_select", name=name))


select_terminal = terminal_select


def run_script(sub_script_path):
    _rpc("run_script", path=str(sub_script_path), base=_script_dir)


class _TerminalName:
    def __str__(self):
        return _rpc("terminal_name")

    __repr__ = __str__


terminal_name = _TerminalName()
_script_dir = os.getcwd()


# --------------------------------------------------------------------------------------
# Plugins
# --------------------------------------------------------------------------------------
class _SessionProxy:
    def __init__(self, info):
        self.name = info.get("name", "")
        self.connected_label = info.get("label", "Disconnected")
        self.connected = bool(info.get("connected"))

    def send(self, text, newline=True):
        _rpc("send_to", target=self.name, text=str(text), newline=bool(newline))

    def recent_output(self, max_chars=6000):
        return _rpc("recent_output", target=self.name, max_chars=int(max_chars))


class PluginAPI:
    def __init__(self, registry, plugin):
        self._registry = registry
        self._plugin = plugin

    def add_menu_action(self, text, callback, shortcut=None):
        self._plugin["actions"].append({"text": text, "callback": callback, "shortcut": shortcut})

    def add_script_function(self, name, func):
        if not str(name).isidentifier() or not callable(func):
            raise ValueError("add_script_function needs an identifier name and a callable")
        self._registry["functions"][name] = func

    def on_output(self, callback):
        self._plugin["hooks"].append(callback)

    def current_session(self):
        info = _rpc("session_info", target=None)
        return _SessionProxy(info) if info else None

    def sessions(self):
        return [_SessionProxy(i) for i in _rpc("session_list")]

    def new_terminal(self, name=None):
        return _rpc("terminal_new", name=name)

    def send(self, text, newline=True, session=None):
        _rpc("send_to", target=getattr(session, "name", None), text=str(text), newline=bool(newline))

    def status(self, message):
        _rpc("status", text=str(message))


def load_plugins(dirs):
    import importlib.util
    import traceback

    registry = {"functions": {}, "plugins": []}
    paths = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for item in sorted(os.listdir(d)):
            if item.startswith(("_", ".")):
                continue
            full = os.path.join(d, item)
            if os.path.isfile(full) and item.endswith(".py"):
                paths.append(full)
            elif os.path.isdir(full) and os.path.exists(os.path.join(full, "__init__.py")):
                paths.append(full)
    for path in paths:
        stem = os.path.basename(path)[:-3] if path.endswith(".py") else os.path.basename(path)
        plugin = {"name": stem, "path": path, "version": "", "description": "", "loaded": False, "error": "", "actions": [], "hooks": []}
        try:
            entry = path if os.path.isfile(path) else os.path.join(path, "__init__.py")
            name = "mmte_plugin_" + stem
            spec = importlib.util.spec_from_file_location(
                name, entry, submodule_search_locations=None if os.path.isfile(path) else [path]
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            plugin["name"] = str(getattr(module, "PLUGIN_NAME", stem))
            plugin["version"] = str(getattr(module, "PLUGIN_VERSION", ""))
            plugin["description"] = str(getattr(module, "PLUGIN_DESCRIPTION", ""))
            register = getattr(module, "register", None)
            if not callable(register):
                raise AttributeError("the plugin has no register(api) function")
            register(PluginAPI(registry, plugin))
            plugin["loaded"] = True
        except Exception as exc:
            plugin["error"] = "%s: %s" % (exc.__class__.__name__, exc)
            sys.stderr.write(traceback.format_exc())
        registry["plugins"].append(plugin)
    return registry


def _plugin_dirs():
    return [d for d in os.environ.get("MMTE_PLUGIN_DIRS", "").split(os.pathsep) if d]


def _main():
    import traceback

    mode = sys.argv[1]
    if mode == "plugins":
        reg = load_plugins(_plugin_dirs())
        listing = []
        for p in reg["plugins"]:
            listing.append({
                "name": p["name"], "path": p["path"], "version": p["version"], "description": p["description"],
                "loaded": p["loaded"], "error": p["error"], "hooks": len(p["hooks"]),
                "actions": [{"text": a["text"], "shortcut": a["shortcut"]} for a in p["actions"]],
                "functions": sorted(reg["functions"].keys()) if p["loaded"] else [],
            })
        _out.write(json.dumps({"op": "result", "value": listing}) + "\n")
        _out.flush()
        return 0
    if mode == "hooks":
        reg = load_plugins(_plugin_dirs())
        hooks = [(p["name"], h) for p in reg["plugins"] if p["loaded"] for h in p["hooks"]]
        _out.write(json.dumps({"op": "hooks_ready", "count": len(hooks)}) + "\n")
        _out.flush()
        while True:
            if _events:
                ev = _events.pop(0)
            else:
                line = _in.readline()
                if not line:
                    return 0
                ev = json.loads(line)
                if "event" not in ev:
                    continue
            if ev["event"] == "quit":
                return 0
            if ev["event"] == "output":
                proxy = _SessionProxy({"name": ev.get("session", ""), "label": ev.get("label", ""), "connected": ev.get("connected", False)})
                for name, cb in hooks:
                    try:
                        cb(proxy, ev.get("text", ""))
                    except _Stopped:
                        return 0
                    except Exception as exc:
                        _rpc("status", text="Plugin %s output hook failed: %s" % (name, exc))
    if mode == "action":
        reg = load_plugins(_plugin_dirs())
        path, index = sys.argv[2], int(sys.argv[3])
        for p in reg["plugins"]:
            if p["path"] == path and p["loaded"]:
                p["actions"][index]["callback"]()
                return 0
        raise RuntimeError("plugin not found: " + path)
    if mode == "script":
        global _script_dir
        path = os.path.abspath(sys.argv[2])
        _script_dir = os.path.dirname(path)
        os.chdir(_script_dir)
        sys.path.insert(0, _script_dir)
        reg = load_plugins(_plugin_dirs())
        with open(path, "r", encoding="utf-8") as fh:
            code = fh.read()
        lines = []
        for line in code.splitlines():
            if line.strip() == "log_off":
                line = line[: len(line) - len(line.lstrip())] + "log_off()"
            lines.append(line)
        code = "\n".join(lines)
        g = {
            "__file__": path, "__name__": "__automation__",
            "send": send, "print": print, "connect_ssh": connect_ssh, "connect_serial": connect_serial,
            "connect_telnet": connect_telnet, "wait": wait, "sleep": sleep, "wait_connected": wait_connected,
            "alert": alert, "input": input, "log_on": log_on, "log_off": log_off,
            "terminal_new": terminal_new, "terminal_close": terminal_close, "terminal_rename": terminal_rename,
            "terminal_list": terminal_list, "terminal_select": terminal_select, "select_terminal": terminal_select,
            "run_script": run_script, "terminal_name": terminal_name,
        }
        for k, v in reg["functions"].items():
            g.setdefault(k, v)
        exec(compile(code, path, "exec"), g)
        return 0
    raise RuntimeError("unknown mode " + mode)


if __name__ == "__main__":
    try:
        rc = _main()
    except _Stopped:
        rc = 3
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 0
    except BaseException:
        import traceback
        _out.write(json.dumps({"op": "error", "text": traceback.format_exc()}) + "\n")
        _out.flush()
        rc = 1
    _out.flush()
    os._exit(rc or 0)
