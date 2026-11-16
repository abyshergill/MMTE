# Modern Multi Terminal Emulator — Rust edition

A native Rust application (egui / eframe). It keeps the features of the original Python
version: multi-tab **SSH / Telnet / Serial** terminals, Tera Term (`.ttl`) and Python scripting, macro
recording, per-terminal logging, the SFTP browser with jump hops, the built-in text editor, the
Multi-Script Suite, Python plugins and the AI Test Engineer.

## Run

```bash
./run.sh            # macOS / Linux (installs Rust with rustup if needed)
run_windows.bat     # Windows (needs Rust from https://rustup.rs)
cargo run --release
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
to choose the interpreter. Plugins are `.py` files in `plugins/` (next to the program) or
`~/.modern_multi_terminal_emulator/plugins`; see `plugins/example_hello.py` and the **Plugins** menu
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

## Security

- **SSH host keys are verified** before any password or key is offered. Keys are checked against
  `~/.ssh/known_hosts` and the app's own `known_hosts` file. A new host must be approved once (the
  fingerprint is shown); a host whose key **changed** is refused unless you explicitly accept the new key.
- **Passwords are never written to disk.** Profiles store host, user and port only. The SSH password is
  kept in memory only while the terminal is connected, password prompts in scripts and macros are masked,
  and recorded macros never contain a typed password.
- **Private files.** The settings folder is owner-only (0700) and everything in it - API keys, saved
  host keys, RAG memories - and new log files are created owner-only (0600).
- **API keys are not sent over plain HTTP** to anything but this machine or a private network address.
- **Cloud models see masked text.** Passwords and tokens in terminal output (attached scrollback, tool
  results, command output) are masked before they are sent to a non-local model.
- **The AI only acts on what you asked.** Tools that change something are offered only when the request
  calls for them, connection details and file paths must come from your own message, and with
  *LLM → Terminal* off every command waits for your approval.
- **SFTP never follows symbolic links** when deleting or copying folders, rejects file names that could
  escape the destination folder, and refuses oversized files.
- **Plugins and the Python bridge.** Plugins load only from the program's `plugins/` folder and your
  settings folder - never from the current working directory. The bridge is not written to a temp file
  and ignores Python files in the working directory.
- **Pasted text** cannot break out of bracketed-paste mode, and escape characters are stripped from it.
- Telnet and serial are unencrypted by nature; use SSH whenever the device supports it.

`cargo audit` reports no known vulnerabilities in the dependencies.

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
