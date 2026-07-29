# Tests

Headless pytest suite for the Qt6 terminal app (139 tests). Runs entirely
offscreen (`QT_QPA_PLATFORM=offscreen`, set automatically in `tests/conftest.py`)
so no display is required.

## Setup

```bash
pip install -r requirements-test.txt
pip install PyQt6 pyte pyserial paramiko   # app runtime deps, needed to import the app
```

## Run

```bash
pytest                     # everything
pytest tests/test_models.py -v
pytest -k "resize"         # any test matching "resize"
```

## What's covered

| File                        | Covers |
|------------------------------|--------|
| `test_models.py`             | `TerminalSession`: pyte→Qt rendering, scrollback clamping, `wait_for`, send/connection teardown, logging enable/disable (incl. the `disable_logging(path)` fix), close/hop cleanup |
| `test_connections.py`        | `SSHConnection`/`SerialConnection`/`TelnetConnection` with mocked `paramiko`/`serial`/`socket` — no real network or hardware needed |
| `test_script_engine.py`      | TTL macro parsing (`_clean_line`, `_split_command`, `_unquote`), full `.ttl` file execution, the `execute_python_script_sync` exec() sandbox (`send`, `print`, `wait`, `log_on`/`log_off`, `run_script`, `terminal_name`) |
| `test_sftp_hops.py`          | Hop-chain initialization, jump-hop tunneling through mocked paramiko transports, hop teardown |
| `test_qt_theme.py`           | Dark/light detection via `QStyleHints`, palette fallback, Fusion style application |
| `test_terminal_widget.py`    | Event forwarding (key/mouse/wheel/context-menu/resize), plus regression tests for the click-drag-selection bug and the paste/drop-bypasses-render() bug |
| `test_main_app.py`           | `TerminalApp` session lifecycle, key routing (local-echo vs. connected), sizing, `after()`/`after_cancel()` scheduling, plus a regression test for the delayed-resize-after-close segfault |
| `test_dialogs.py`            | Input validation in `SSHDialog`/`SerialDialog`/`TelnetDialog`/`RenameDialog` |
| `test_daw_manager.py`        | Playlist management in `MultiScriptDialog` (reorder/remove/route/timeout editing) |

## Notes for writing more tests

- **Every test that touches `QMessageBox`** is covered by the autouse
  `_no_blocking_message_boxes` fixture in `conftest.py`, which stubs
  `critical`/`warning`/`information`/`question` so a validation-failure path
  doesn't pop a real modal dialog and hang the suite waiting for a click that
  will never come.
- **Never pass an anonymous `QWidget()`/custom `QWidget` subclass as a dialog
  parent without keeping a named reference to it** (e.g. `d = Dialog(FakeApp())`).
  PyQt6 can garbage-collect the Python-side parent object if nothing else
  references it, which deletes the C++ widget tree under the dialog too,
  surfacing as `RuntimeError: wrapped C/C++ object ... has been deleted`.
  Always do `app = FakeApp(); d = Dialog(app)` and keep `app` alive for the
  test's duration.
- **Any test that creates a real `TerminalApp()`** (see `test_main_app.py`'s
  `app_window` fixture) must stop all of its `QTimer`s
  (`win.findChildren(QTimer)`) before closing/deleting it. `deleteLater()`
  only *schedules* deletion — it does not synchronously cancel already-armed
  timers waiting on their real wall-clock deadline, so a `self.after(150, ...)`
  callback can fire later against an already-destroyed window and crash the
  interpreter (this is exactly how the delayed-resize segfault above was
  found).
