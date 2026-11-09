"""Python plugin support.

A plugin is a ``.py`` file (or a folder with ``__init__.py``) placed in one of:

* ``<app folder>/plugins``
* ``~/.modern_multi_terminal_emulator/plugins``

It must define ``register(api)``.  ``api`` is a :class:`PluginAPI` and lets the plugin

* add a menu entry under **Plugins**           ``api.add_menu_action("Say hi", callback)``
* add a function usable inside Python scripts  ``api.add_script_function("my_func", fn)``
* react to terminal output                     ``api.on_output(callback)``  (callback(session, text))
* talk to the app                              ``api.send("ls")``, ``api.current_session()``,
  ``api.status("text")``, ``api.new_terminal()``, ``api.app``

Optional module attributes: ``PLUGIN_NAME``, ``PLUGIN_VERSION``, ``PLUGIN_DESCRIPTION``.
A plugin that raises an error is skipped and the error is shown in Plugins > Plugin Manager;
it never stops the application from starting.
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from core.config import CONFIG_DIR

APP_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"
USER_PLUGIN_DIR = CONFIG_DIR / "plugins"

PLUGIN_TEMPLATE = '''"""My plugin - describe what it does here."""

PLUGIN_NAME = "My Plugin"
PLUGIN_VERSION = "1.0"
PLUGIN_DESCRIPTION = "Example plugin created from the template."


def register(api):
    # 1) A menu entry under Plugins > My Plugin
    def say_hello():
        api.status("Hello from my plugin!")
        api.send("echo hello")          # types the command into the current terminal

    api.add_menu_action("Say hello", say_hello)

    # 2) A function you can call from Python scripts: hello("world")
    api.add_script_function("hello", lambda name="world": f"hello {name}")

    # 3) React to everything a terminal prints
    def on_text(session, text):
        if "ERROR" in text:
            api.status(f"ERROR seen in {session.name}")

    api.on_output(on_text)
'''


@dataclass
class PluginInfo:
    name: str
    path: Path
    version: str = ""
    description: str = ""
    loaded: bool = False
    error: str = ""
    menu_actions: list = field(default_factory=list)


class PluginAPI:
    """What a plugin is allowed to see.  One instance per plugin."""

    def __init__(self, manager: "PluginManager", info: PluginInfo):
        self._manager = manager
        self._info = info
        self.app = manager.app

    # --- extending the app -----------------------------------------------------
    def add_menu_action(self, text: str, callback, shortcut: str | None = None) -> None:
        self._info.menu_actions.append((text, callback, shortcut))

    def add_script_function(self, name: str, func) -> None:
        if not name.isidentifier() or not callable(func):
            raise ValueError("add_script_function needs an identifier name and a callable")
        self._manager.script_functions[name] = func

    def on_output(self, callback) -> None:
        self._manager.output_hooks.append((self._info.name, callback))

    # --- talking to the app ------------------------------------------------------
    def current_session(self):
        return self.app.current_session()

    def sessions(self):
        return list(self.app.sessions)

    def new_terminal(self, name: str | None = None):
        return self.app.new_terminal(name)

    def send(self, text: str, newline: bool = True, session=None) -> None:
        target = session or self.current_session()
        if target is not None:
            target.send(str(text) + ("\n" if newline else ""))

    def status(self, message: str) -> None:
        self.app.after(0, lambda: self.app.status_label.setText(str(message)))

    @property
    def settings(self):
        return self.app.settings


class PluginManager:
    def __init__(self, app, directories: list[Path] | None = None):
        self.app = app
        self.directories = directories if directories is not None else [APP_PLUGIN_DIR, USER_PLUGIN_DIR]
        self.plugins: list[PluginInfo] = []
        self.script_functions: dict = {}
        self.output_hooks: list = []

    # --- discovery ---------------------------------------------------------------
    def _candidates(self) -> list[Path]:
        found: list[Path] = []
        for directory in self.directories:
            if not directory.is_dir():
                continue
            for item in sorted(directory.iterdir()):
                if item.name.startswith(("_", ".")):
                    continue
                if item.is_file() and item.suffix == ".py":
                    found.append(item)
                elif item.is_dir() and (item / "__init__.py").exists():
                    found.append(item)
        return found

    def load_all(self) -> None:
        self.plugins, self.script_functions, self.output_hooks = [], {}, []
        for path in self._candidates():
            self.plugins.append(self._load(path))

    def _load(self, path: Path) -> PluginInfo:
        stem = path.stem if path.is_file() else path.name
        info = PluginInfo(name=stem, path=path)
        module_name = f"mmte_plugin_{stem}"
        try:
            entry = path if path.is_file() else path / "__init__.py"
            spec = importlib.util.spec_from_file_location(
                module_name, entry, submodule_search_locations=None if path.is_file() else [str(path)]
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            info.name = str(getattr(module, "PLUGIN_NAME", stem))
            info.version = str(getattr(module, "PLUGIN_VERSION", ""))
            info.description = str(getattr(module, "PLUGIN_DESCRIPTION", ""))
            register = getattr(module, "register", None)
            if not callable(register):
                raise AttributeError("the plugin has no register(api) function")
            register(PluginAPI(self, info))
            info.loaded = True
        except Exception as exc:
            info.error = f"{exc.__class__.__name__}: {exc}"
            info.traceback = traceback.format_exc()
            sys.modules.pop(module_name, None)
        return info

    # --- runtime hooks -------------------------------------------------------------
    def dispatch_output(self, session, text: str) -> None:
        for name, callback in list(self.output_hooks):
            try:
                callback(session, text)
            except Exception as exc:
                print(f"[plugin {name}] output hook failed: {exc}", file=sys.stderr)

    @staticmethod
    def create_from_template(name: str, directory: Path = USER_PLUGIN_DIR) -> Path:
        safe = "".join(c if c.isalnum() or c == "_" else "_" for c in name.strip()).strip("_").lower() or "my_plugin"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{safe}.py"
        if target.exists():
            raise FileExistsError(f"{target} already exists")
        target.write_text(PLUGIN_TEMPLATE, encoding="utf-8")
        return target
