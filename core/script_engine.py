from __future__ import annotations
import shlex
import sys
import threading
import time
import re
from pathlib import Path
from PyQt6.QtWidgets import QMessageBox
from core.connections import SSHConnection, SerialConnection, TelnetConnection
from PyQt6.QtWidgets import QMessageBox, QInputDialog, QLineEdit
from PyQt6.QtWidgets import QMessageBox, QInputDialog, QLineEdit, QDialog

from typing import TYPE_CHECKING
from core.models import ScriptStopped

if TYPE_CHECKING:
    from core.models import TerminalSession


class TeraTermMacroRunner:
    def __init__(self, app: object, session: "TerminalSession"):
        self.app = app
        self.session = session
        self.default_timeout = 60.0
        self.variables: dict[str, str] = {}

    def run_file(self, path: str) -> None:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line_no, raw in enumerate(lines, start=1):
            self.session.script_checkpoint()
            line = self._clean_line(raw)
            if not line: continue
            if "=" in line and not line.lower().startswith("connect"):
                parts = line.split("=", 1)
                var_name, var_value = parts[0].strip(), parts[1].strip()
                if var_name.isidentifier():
                    self.variables[var_name] = self._unquote(var_value)
                    continue
            cmd, arg = self._split_command(line)
            cmd = cmd.lower()
            if arg in self.variables: arg = self.variables[arg]
            if cmd == "end":
                self.session.incoming.put(f"\r\n[TTL line {line_no}: 'end' execution loop terminated successfully]\r\n")
                break
            self.session.incoming.put(f"\r\n[TTL line {line_no}: {cmd}]\r\n")
            if cmd == "connect": self._connect(arg)
            elif cmd == "wait": self._wait(arg)
            elif cmd == "sendln": self.session.send(self._unquote(arg) + "\n")
            elif cmd == "send": self.session.send(self._unquote(arg))
            elif cmd in {"pause", "mpause"}:
                delay = float(self._unquote(arg) or "1")
                if cmd == "mpause": delay /= 1000
                self._sleep_checkpoint(delay)
            else:
                self.session.incoming.put(f"\r\n[TTL warning: unsupported command on line {line_no}: {cmd}]\r\n")

    def _clean_line(self, raw: str) -> str:
        line = raw.strip()
        if not line or line.startswith(";"): return ""
        clean_chars, in_quote, quote_char = [], False, None
        for char in line:
            if char in ("'", '"'):
                if not in_quote: in_quote, quote_char = True, char
                elif char == quote_char: in_quote, quote_char = False, None
            elif char == ";" and not in_quote: break
            clean_chars.append(char)
        return "".join(clean_chars).strip()

    def _split_command(self, line: str) -> tuple[str, str]:
        parts = line.split(None, 1)
        return (parts[0], parts[1].strip()) if len(parts) == 2 else (parts[0], "")

    def _unquote(self, text: str) -> str:
        text = text.strip()
        return text[1:-1] if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'} else text

    def _sleep_checkpoint(self, seconds: float) -> None:
        """time.sleep() that stays responsive to Pause/Stop."""
        deadline = time.time() + seconds
        while True:
            self.session.script_checkpoint()
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            time.sleep(min(0.2, remaining))

    def _wait(self, arg: str) -> None:
        pattern = self._unquote(arg)
        if not pattern: return
        if not self.session.wait_for(pattern, self.default_timeout, case_sensitive=True):
            raise TimeoutError(f"wait timed out for: {pattern}")

    def _connect(self, arg: str) -> None:
        parts = shlex.split(self._unquote(arg))
        if not parts: raise ValueError("connect requires host")
        host, username, password, port, is_telnet = parts[0], "", "", None, False
        for item in parts[1:]:
            lower = item.lower()
            if lower in ("/telnet", "/t"): is_telnet = True
            elif lower.startswith("/user="): username = item.split("=", 1)[1]
            elif lower.startswith("/passwd=") or lower.startswith("/password="): password = item.split("=", 1)[1]
            elif lower.startswith("/port="): port = int(item.split("=", 1)[1])
        if port is None: port = 23 if is_telnet else 22
        done = threading.Event()
        result: dict[str, Exception | None] = {"error": None}
        def connect_on_ui_thread():
            try:
                if is_telnet: conn, lbl = TelnetConnection(host=host, port=port), f"Telnet {host}"
                else: conn, lbl = SSHConnection(host=host, username=username, password=password, port=port, look_for_keys=False, allow_agent=False), f"SSH {host}"
                self.app.connect_session(self.session, conn, lbl)
            except Exception as exc: result["error"] = exc
            finally: done.set()
        self.app.after(0, connect_on_ui_thread)
        done.wait(timeout=30)
        if result["error"]: raise result["error"]
        if not done.is_set(): raise TimeoutError("TTL connect timed out")
        time.sleep(0.5)


def execute_python_script_sync(session: "TerminalSession", path: str):
    script_dir = Path(path).parent.resolve()
    cur_sess = [session]

    def _run_ui_action(action, timeout=10.0):
        done = threading.Event()
        res = {"val": None, "err": None}
        def runner():
            try:
                res["val"] = action()
            except Exception as e:
                res["err"] = e
            finally:
                done.set()
        session.app.after(0, runner)
        done.wait(timeout=timeout)
        if res["err"]:
            raise res["err"]
        return res["val"]

    def sleep_checkpoint(seconds: float):
        """Drop-in replacement for time.sleep() that stays responsive to
        the Pause/Stop controls in the main window."""
        deadline = time.time() + float(seconds)
        while True:
            cur_sess[0].script_checkpoint()
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            time.sleep(min(0.2, remaining))

    def send(text: str, newline: bool = True, delay: float = 0.0):
        cur_sess[0].script_checkpoint()
        if delay: sleep_checkpoint(delay)
        cur_sess[0].send(str(text) + ("\n" if newline else ""))

    def wait_connected(timeout: float = 30.0) -> bool:
        """Wait for the app's current terminal connection attempt to succeed."""
        deadline = time.monotonic() + float(timeout)
        while True:
            cur_sess[0].script_checkpoint()
            active_session = cur_sess[0]
            if active_session.connection is None:
                raise ConnectionError(f"{active_session.name} is not connected")
            if active_session.connected_label != "Disconnected":
                return True
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out connecting {active_session.name}")
            time.sleep(0.05)

    def print_to_terminal(*args, sep=" ", end="\n"):
        text = sep.join(str(a) for a in args) + end
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        cur_sess[0].incoming.put(text)

    def wait(pattern_or_seconds, timeout: float = 60.0, case_sensitive: bool = True) -> bool:
        if isinstance(pattern_or_seconds, (int, float)):
            sleep_checkpoint(float(pattern_or_seconds))
            return True
        elif isinstance(pattern_or_seconds, str):
            pattern = pattern_or_seconds
            if cur_sess[0].wait_for(pattern, timeout=timeout, case_sensitive=case_sensitive):
                return True
            cur_sess[0].incoming.put(f"\r\n[wait timed out for: '{pattern}']\r\n")
            return False
        else:
            time.sleep(1.0)
            return True


    def alert(message: str) -> bool:
        done = threading.Event()
        user_response = {"continue": False}

        def show_alert_dialog():
            try:
                parent_win = session.app
                box = QMessageBox(parent_win)
                box.setWindowTitle("Script Alert")
                box.setText(f"{message}\n\nDo you want to continue script execution?")
                box.setIcon(QMessageBox.Icon.Question)
                box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                ans = box.exec()
                user_response["continue"] = (ans == QMessageBox.StandardButton.Yes)
            except Exception as e1:
                print(f"Alert dialog failed: {e1}", file=sys.stderr)
                user_response["continue"] = False
            finally:
                done.set()

        try:
            if hasattr(session.app, "after"):
                session.app.after(0, show_alert_dialog)
            else:
                show_alert_dialog()
        except Exception:
            show_alert_dialog()

        done.wait()

        if not user_response["continue"]:
            cur_sess[0].incoming.put(f"\r\n[Script stopped by user alert: {message}]\r\n")
            raise RuntimeError(f"Script execution stopped by user at alert: '{message}'")

        cur_sess[0].incoming.put(f"\r\n[Alert acknowledged: '{message}'. Continuing script...]\r\n")
        return True

    def input(prompt: str = "") -> str:
        done = threading.Event()
        user_input = {"value": ""}

        def show_input_dialog():
            try:
                parent_win = session.app
                dialog = QInputDialog(parent_win)
                dialog.setWindowTitle("Script Input")
                dialog.setLabelText(prompt)
                dialog.setTextValue("")
                dialog.setTextEchoMode(QLineEdit.EchoMode.Normal)
                if dialog.exec() == QDialog.DialogCode.Accepted:
                    user_input["value"] = dialog.textValue()
                else:
                    user_input["value"] = ""
            except Exception as e1:
                print(f"Input dialog failed: {e1}", file=sys.stderr)
                user_input["value"] = ""
            finally:
                done.set()

        try:
            if hasattr(session.app, "after"):
                session.app.after(0, show_input_dialog)
            else:
                show_input_dialog()
        except Exception:
            show_input_dialog()

        done.wait()

        entered = user_input["value"]
        cur_sess[0].incoming.put(f"\r\n[Input '{prompt}': '{entered}']\r\n")
        return entered


    def connect_ssh(host, username, password, port=22):
        conn = SSHConnection(host=host, username=username, password=password, port=port, look_for_keys=False, allow_agent=False)
        _run_ui_action(lambda: session.app.connect_session(cur_sess[0], conn, f"SSH {host}"))
        wait_connected()

    def connect_serial(port, baudrate, bytesize=8, parity="N", stopbits=1.0):
        conn = SerialConnection(port=port, baudrate=baudrate, bytesize=bytesize, parity=parity, stopbits=stopbits)
        _run_ui_action(lambda: session.app.connect_session(cur_sess[0], conn, f"Serial {port} @ {baudrate}"))
        wait_connected()

    def connect_telnet(host, port=23):
        conn = TelnetConnection(host=host, port=port)
        _run_ui_action(lambda: session.app.connect_session(cur_sess[0], conn, f"Telnet {host}"))
        wait_connected()

    def log_on(log_path: str):
        p = Path(log_path)
        if not p.is_absolute():
            p = script_dir / p
        cur_sess[0].enable_custom_logging(p.resolve())

    def log_off(log_path: str | None = None):
        if log_path:
            p = Path(log_path)
            if not p.is_absolute():
                p = (script_dir / p).resolve()
            # Stop a specific log file
            cur_sess[0].disable_logging(p)
            cur_sess[0].incoming.put(f"\r\n[Logging STOPPED: {p.name}]\r\n")
        else:
            # Stop ALL active logs if no specific path was provided
            cur_sess[0].disable_logging()
            cur_sess[0].incoming.put(f"\r\n[ALL Logging STOPPED]\r\n")

    class _LogOffCallable:
        def __call__(self, log_path: str | None = None):
            log_off(log_path)
        def __repr__(self):
            log_off()
            return "<log_off executed>"

    def terminal_new(name: str | None = None):
        target_name = name if (name and isinstance(name, str)) else None
        new_s = _run_ui_action(lambda: session.app.new_terminal(name=target_name))
        if new_s:
            cur_sess[0] = new_s
            return new_s.name
        return None

    def terminal_close(name: str | None = None):
        target_name = name if name else cur_sess[0].name
        _run_ui_action(lambda: session.app.close_session_by_name(target_name))

    def terminal_rename(current_name: str, new_name: str):
        return _run_ui_action(lambda: session.app.rename_terminal_by_name(current_name, new_name))

    def terminal_list() -> list[str]:
        return _run_ui_action(lambda: [terminal.name for terminal in session.app.sessions])

    def terminal_select(name: str):
        target_s = _run_ui_action(lambda: session.app.select_session_by_name(name))
        if target_s:
            cur_sess[0] = target_s
            return True
        return False

    def run_script(sub_script_path: str):
        sp = Path(sub_script_path)
        if not sp.is_absolute():
            sp = script_dir / sp
        sp = sp.resolve()
        if not sp.exists():
            raise FileNotFoundError(f"Script file not found: {sp}")
        if sp.suffix.lower() == ".ttl":
            TeraTermMacroRunner(session.app, cur_sess[0]).run_file(str(sp))
        else:
            execute_python_script_sync(cur_sess[0], str(sp))

    code = Path(path).read_text(encoding="utf-8")

    transformed_lines = []
    for line in code.splitlines():
        if line.strip() == "log_off":
            indent = line[:len(line) - len(line.lstrip())]
            transformed_lines.append(f"{indent}log_off()")
        else:
            transformed_lines.append(line)
    code = "\n".join(transformed_lines)

    class DynamicTerminalName:
        def __str__(self):
            return cur_sess[0].name
        def __repr__(self):
            return cur_sess[0].name

    globs = {
        "__file__": path, "__name__": "__automation__",
        "send": send, "print": print_to_terminal,
        "connect_ssh": connect_ssh, "connect_serial": connect_serial, "connect_telnet": connect_telnet,
        "wait": wait, "sleep": sleep_checkpoint, "wait_connected": wait_connected,
        "alert": alert, "input": input,
        "log_on": log_on, "log_off": _LogOffCallable(),
        "terminal_new": terminal_new, "terminal_close": terminal_close,
        "terminal_rename": terminal_rename,
        "terminal_list": terminal_list,
        "terminal_select": terminal_select, "select_terminal": terminal_select,
        "run_script": run_script,
        "terminal_name": DynamicTerminalName()
    }
    session.script_checkpoint()
    exec(compile(code, path, "exec"), globs, {})
