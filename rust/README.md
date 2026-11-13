# Modern Multi Terminal Emulator — Rust edition

A native Rust rewrite of the application (egui / eframe). It keeps the features of the Python
version: multi-tab **SSH / Telnet / Serial** terminals, Tera Term (`.ttl`) and Python scripting, macro
recording, per-terminal logging, the SFTP browser with jump hops, the built-in text editor, the
Multi-Script Suite, Python plugins and the AI Test Engineer.

## Run

```bash
./run.sh            # macOS / Linux (installs Rust with rustup if needed)
run_windows.bat     # Windows (needs Rust from https://rustup.rs)
cargo run --release # from this folder
```

The first build downloads and compiles the dependencies (a few minutes; OpenSSL is built from source
so no system library is required). Python 3 is only needed to run `.py` scripts and plugins.

## What is where

| Area | Rust module |
|---|---|
| Terminal emulation, sessions, logging, waiting | `src/session.rs` (vt100 screen with 5000 lines of scrollback) |
| SSH / Telnet / Serial | `src/conn.rs` (libssh2, raw TCP with IAC handling, serialport) |
| Clean log files (no escape-code garbage) | `src/logclean.rs` |
| Macro recorder (keeps the prompt as a comment) | `src/macrorec.rs` |
| Syntax colours in the terminal | `src/highlight.rs` |
| TTL interpreter + Python bridge | `src/script.rs`, `src/prelude.py` |
| Plugins (menu items, script functions, output hooks) | `src/plugins.rs` |
| SFTP + jump hops | `src/sftp.rs`, `src/ui/windows.rs` |
| Text editor | `src/editlogic.rs`, `src/ui/editor.rs` |
| Multi-Script Suite | `src/suite.rs` |
| AI assistant | `src/ai/*`, `src/ui/ai_panel.rs` |

Settings, profiles, logs, macros, LLM config and RAG memories use the same folder
(`~/.modern_multi_terminal_emulator`) and file formats as the Python version, so existing data keeps working.

## Scripts and plugins

Python scripts run in a `python3` subprocess that talks to the app, so `send`, `wait`, `sleep`, `alert`,
`input`, `connect_*`, `log_on/off`, `terminal_*` and `run_script` behave exactly as before. Set `MMTE_PYTHON`
to choose the interpreter. Plugins are `.py` files in `plugins/` (next to the project) or
`~/.modern_multi_terminal_emulator/plugins`; see `../plugins/example_hello.py` and the **Plugins** menu
(`api.add_menu_action`, `api.add_script_function`, `api.on_output`, `api.send`, `api.status`, ...).

## Macro recording: Python or Tera Term

**Utility → Macro format** chooses what a recording is saved as. With Python 3 installed the default is
a Python script; without it the default is a Tera Term `.ttl` macro, which replays with no Python at all.
Both keep the prompt of every command as a comment, and neither stores passwords (the Python macro uses
`input(...)`, the `.ttl` macro uses `passwordbox`, so you are asked when it runs). The `.ttl` interpreter
understands `#13`-style character codes and concatenated strings (`'it'#39's'`), `inputbox` and `passwordbox`
(answer in `inputstr`).

## AI assistant

Providers: OpenAI, Anthropic, Gemini, Groq, Mistral, DeepSeek, xAI, OpenRouter, Together, Ollama, LM Studio,
llama.cpp server and any custom OpenAI-compatible endpoint, with native tool calling and a JSON-in-prompt
fallback. **A local `.gguf` file is not run in-process in this edition** — serve it with Ollama or
`llama-server -m model.gguf` and choose that provider. Embedding for RAG uses Ollama or OpenAI-compatible
servers, with a keyword (BM25) fallback when none is reachable.

## Tests

```bash
cargo test
```

The suite includes end-to-end tests against a real SSH server (`tests_support/ssh_server.py`, needs Python
with `paramiko`): shell sessions, SFTP, jump hops, remote editing and scripts. They skip themselves when
paramiko is not available.

## Developer switches

`MMTE_SCREENSHOT=out.png` saves a screenshot and quits; `MMTE_DEMO=1` pre-fills the terminal;
`MMTE_DEMO_WINDOW=ssh|telnet|serial|suite|profiles|plugins|samples|about|editor|ai|llm|rag` opens a window;
`MMTE_AUTOTELNET=host:port` connects at start-up.
