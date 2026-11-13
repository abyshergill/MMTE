//! One AI request end to end, without any UI: retrieval, system prompt, the model call, and the
//! app tools the model may use. Progress is reported through [`Event`]s.

use super::agent::{run_chat, stream_plain};
use super::dispatch::*;
use super::intent::*;
use super::knowledge::APPLICATION_MEMORY_NAME;
use super::rag::RagManager;
use super::redact::redact_secrets;
use super::{is_cloud, Config};
use crate::conn::ConnSpec;
use crate::logclean::LogCleaner;
use crate::session::{Hub, Session};
use crossbeam_channel::Sender;
use fancy_regex::Regex;
use parking_lot::Mutex;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
#[cfg(test)]
use std::time::Duration;

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum NoteKind {
    Info,
    Warn,
    Error,
}

pub enum Event {
    Chunk(String),
    Note(String, NoteKind),
    /// Output of an app action; shown in the chat and added to the history.
    ActionResult { title: String, output: String },
    /// Start of a new assistant message (e.g. an interpretation of an action's output).
    AssistantHeader(String),
    Error(String),
    Finished { cancelled: bool, failed: bool, streamed: String, request_session: Option<u64>, auto_run: bool, approval_eligible: bool, explicit_exec: bool },
}

pub struct Shared {
    pub hub: Arc<Hub>,
    pub rag: Arc<Mutex<RagManager>>,
    pub proposals: Arc<ProposalQueue>,
    pub blocked: Arc<ProposalQueue>,
}

pub struct Request {
    pub query: String,
    pub auto_mode: bool,
    pub attach_terminal: bool,
    pub selected_memory: Option<String>,
    pub propose_only: bool,
}

const MAX_SEND_TARGETS: usize = 10;

struct ToolCtx<'a> {
    shared: &'a Shared,
    query: String,
    user_context: String,
    allow_send: bool,
    request_session: Option<Arc<Session>>,
    cancel: &'a Arc<AtomicBool>,
    auto_run_flag: &'a AtomicBool,
}

fn contains(re: &str, text: &str) -> bool {
    Regex::new(&format!("(?i){re}")).map(|r| r.is_match(text).unwrap_or(false)).unwrap_or(false)
}

pub fn plain_output(raw: &str) -> String {
    let mut c = LogCleaner::new();
    let mut out = c.feed(raw);
    out.push_str(&c.flush());
    out.trim().to_string()
}

/// Type `commands` into every session; returns the real output of each.
pub fn send_commands(sessions: &[Arc<Session>], commands: &[String], delay: f64, cancel: &Arc<AtomicBool>) -> String {
    let mut parts = Vec::new();
    for s in sessions.iter().take(MAX_SEND_TARGETS) {
        if s.script_running() {
            parts.push(format!("[{}]\n(not started: a script is already running on this terminal)", s.name()));
            continue;
        }
        let start = s.buf.lock().len();
        s.start_script("AI commands");
        for c in commands {
            if cancel.load(Ordering::SeqCst) {
                break;
            }
            s.send(&format!("{c}\n"));
            if s.sleep_checkpoint(delay.max(0.2)).is_err() {
                break;
            }
        }
        s.finish_script();
        let fresh = {
            let b = s.buf.lock();
            b.get(start.min(b.len())..).unwrap_or("").to_string()
        };
        let text = plain_output(&fresh);
        parts.push(format!("[{}]\n{}", s.name(), if text.is_empty() { "(no output)".to_string() } else { text }));
    }
    parts.join("\n\n")
}

impl ToolCtx<'_> {
    fn resolve_session(&self, name: &str) -> Option<Arc<Session>> {
        let key = name.trim().to_lowercase();
        if ["", "current", "active", "selected"].contains(&key.as_str()) {
            return self.request_session.clone().or_else(|| self.shared.hub.active_session());
        }
        match_name_pub(name, &self.shared.hub.list()).or_else(|| self.shared.hub.find(name))
    }

    fn execute(&self, tool: &str, args: &Value) -> String {
        let hub = &self.shared.hub;
        let sarg = |k: &str| args.get(k).and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
        match tool {
            "list_terminals" => {
                let list: Vec<Value> = hub.list().iter().map(|s| json!({"name": s.name(), "connected": s.is_connected(), "connection": s.label()})).collect();
                serde_json::to_string(&list).unwrap_or_default()
            }
            "create_terminal" => {
                if !contains(r"\b(?:launch|open|create|start|add)\b.{0,50}\bterminal\b", &self.query) {
                    return "Error: the current user request did not ask to create/open a terminal tab.".into();
                }
                let mut name = sarg("name");
                if name.is_empty() {
                    name = parse_create_terminal_request(&self.query).unwrap_or_default();
                }
                if name.is_empty() || name.chars().count() > 80 {
                    return "Error: provide a terminal name from 1 to 80 characters.".into();
                }
                if hub.find(&name).is_some() {
                    return format!("Error: a terminal named {name:?} already exists.");
                }
                let s = hub.new_session(Some(&name));
                hub.select(&s);
                format!("Created and selected terminal {name}.")
            }
            "rename_terminal" => {
                let Some(parsed) = parse_rename_request(&self.query) else { return "Error: the current user request did not explicitly ask to rename a terminal tab.".into() };
                let mut old = sarg("terminal_name");
                let mut new = sarg("new_name");
                if old.is_empty() || ["current", "active", "selected"].contains(&old.to_lowercase().as_str()) {
                    old = parsed.0.clone();
                }
                if new.is_empty() {
                    new = parsed.1.clone();
                }
                let Some(s) = self.resolve_session(&old).or_else(|| hub.find(&old)) else { return format!("Error: terminal {old:?} was not found.") };
                if new.is_empty() || new.chars().count() > 80 {
                    return "Error: provide a new terminal name from 1 to 80 characters.".into();
                }
                if let Some(d) = hub.find(&new) {
                    if d.id != s.id {
                        return format!("Error: a terminal named {new:?} already exists.");
                    }
                }
                let before = s.name();
                if hub.rename(&before, &new) { format!("Renamed {before} to {new}.") } else { format!("Could not rename {before}.") }
            }
            "connect_terminal" => {
                if !contains(r"\b(?:connect|launch|open|start)\b", &self.query) {
                    return "Error: the current user request did not ask to connect a terminal.".into();
                }
                let tname = sarg("terminal_name");
                let Some(s) = self.resolve_session(&tname) else { return format!("Error: terminal {tname:?} was not found. Create it first.") };
                let protocol = sarg("protocol").to_lowercase();
                let host = sarg("host");
                let port = args.get("port").and_then(|v| v.as_u64().or_else(|| v.as_str().and_then(|s| s.parse().ok()))).unwrap_or(0);
                if !["ssh", "telnet"].contains(&protocol.as_str()) || host.is_empty() || !(1..=65535).contains(&port) {
                    return "Error: provide protocol ssh/telnet, a host, and a valid port.".into();
                }
                if !self.user_context.contains(&host) || !self.user_context.contains(&port.to_string()) {
                    return "Error: host and port must come from the user's supplied connection details. Ask before connecting to an unprovided endpoint.".into();
                }
                if s.is_connected() {
                    return format!("Error: {} is already connected. Disconnect it before replacing the connection.", s.name());
                }
                let spec = if protocol == "ssh" {
                    let (u, p) = (sarg("username"), args.get("password").and_then(|v| v.as_str()).unwrap_or("").to_string());
                    if !u.is_empty() && !self.user_context.contains(&u) {
                        return "Error: SSH username was not present in the user's supplied details.".into();
                    }
                    if !p.is_empty() && !self.user_context.contains(&p) {
                        return "Error: SSH password was not present in the user's supplied details.".into();
                    }
                    ConnSpec::Ssh { host: host.clone(), username: u, password: p, port: port as u16, look_for_keys: false }
                } else {
                    ConnSpec::Telnet { host: host.clone(), port: port as u16 }
                };
                hub.select(&s);
                match s.connect(spec) {
                    Ok(()) => format!("Connected {} to {} {host}:{port}", s.name(), protocol.to_uppercase()),
                    Err(e) => format!("Error: could not connect {}: {e}", s.name()),
                }
            }
            "send_terminal" => self.tool_send(args),
            "read_terminal" => {
                let tname = sarg("terminal_name");
                let Some(s) = self.resolve_session(&tname) else { return format!("Error: terminal {tname:?} was not found.") };
                let out = s.recent_output(6000);
                format!("[{}]\n{}", s.name(), if out.is_empty() { "[No terminal output yet]".to_string() } else { out })
            }
            "sftp_upload" | "sftp_download" => self.tool_sftp(tool, args),
            other => format!("Error: unsupported application tool {other:?}."),
        }
    }

    fn tool_send(&self, args: &Value) -> String {
        let spec = args.get("terminal_name").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
        let spec = if spec.is_empty() { "current".to_string() } else { spec };
        let text = args.get("text").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let commands = if !text.trim().is_empty() && text.chars().count() <= 4000 { clean_commands([text.as_str()]) } else { vec![] };
        if commands.is_empty() {
            return "Error: command/text must contain 1 to 4000 characters.".into();
        }
        // the structured call is the execution path; don't also run the same commands from prose
        self.auto_run_flag.store(false, Ordering::SeqCst);
        if !self.allow_send {
            for c in &commands {
                self.shared.proposals.add(&spec, c);
            }
            return format!("Not sent yet: LLM \u{2192} Terminal is OFF, so the command(s) were queued and the user will be asked for permission: {}. Do not claim they ran; tell the user they are waiting for approval.", commands.join("; "));
        }
        let hub = &self.shared.hub;
        let mut sessions = hub.list();
        if sessions.is_empty() {
            sessions = vec![hub.new_session(None)];
        }
        let current = self.request_session.clone().or_else(|| hub.active_session());
        let r = resolve_targets(&spec, &sessions, current.as_ref());
        let mut notes: Vec<String> = Vec::new();
        if !r.missing.is_empty() {
            let known = sessions.iter().map(|s| s.name()).collect::<Vec<_>>().join(", ");
            notes.push(format!("No terminal named {}. Existing terminals: {known}.", r.missing.join(", ")));
        }
        if !r.not_connected.is_empty() {
            let waiting = r.not_connected.iter().map(|s| s.name()).collect::<Vec<_>>().join(", ");
            notes.push(format!("{waiting} is not connected, so nothing was sent there."));
            for c in &commands {
                self.shared.blocked.add(&waiting, c);
            }
        }
        if r.sessions.is_empty() {
            if r.not_connected.is_empty() && r.missing.is_empty() {
                for c in &commands {
                    self.shared.blocked.add("current", c);
                }
                notes.push("No terminal is connected.".into());
            } else if !r.missing.is_empty() && r.not_connected.is_empty() {
                return format!("Error: {}", notes.join(" "));
            }
            return format!("Error: {} The command(s) were kept and the user is being asked to connect a terminal; do not claim they ran and do not invent connection details.", notes.join(" "));
        }
        hub.select(&r.sessions[0]);
        let out = send_commands(&r.sessions, &commands, 0.7, self.cancel);
        std::iter::once(out).chain(notes).collect::<Vec<_>>().join("\n\n")
    }

    fn tool_sftp(&self, tool: &str, args: &Value) -> String {
        let sarg = |k: &str| args.get(k).and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
        let tname = sarg("terminal_name");
        let Some(s) = self.resolve_session(&tname) else { return format!("Error: terminal {tname:?} was not found.") };
        if !s.is_connected() || s.conn_kind() != "SSH" {
            return format!("Error: {} must have an active SSH connection for SFTP.", s.name());
        }
        let (local_v, remote_v) = (sarg("local_path"), sarg("remote_path"));
        if local_v.is_empty() || remote_v.is_empty() {
            return "Error: both local_path and remote_path are required.".into();
        }
        let upload = tool == "sftp_upload";
        let verb = if upload { r"\b(?:upload|put|transfer)\b" } else { r"\b(?:download|get|retrieve)\b" };
        if !contains(verb, &self.query) {
            return format!("Error: this turn did not explicitly request {}.", if upload { "an upload" } else { "a download" });
        }
        if !self.user_context.contains(&local_v) || !self.user_context.contains(&remote_v) {
            return format!("Error: {} paths must be supplied by the user.", if upload { "upload" } else { "download" });
        }
        let Some(ConnSpec::Ssh { host, username, password, port, .. }) = s.spec.lock().clone() else { return "Error: no SSH details for this terminal.".into() };
        let client = match crate::sftp::SftpClient::open(crate::sftp::HopSpec { host, port, username, password }) {
            Ok(c) => c,
            Err(e) => return format!("SFTP {} failed on {}: {e}", if upload { "upload" } else { "download" }, s.name()),
        };
        let local = std::path::PathBuf::from(shellexpand_home(&local_v));
        let result = if upload {
            if !local.is_file() {
                return format!("Error: local file does not exist or is not a regular file: {}", local.display());
            }
            client.upload(&local, &remote_v).map(|_| format!("Uploaded {} to {remote_v} over {}.", local.display(), s.name()))
        } else {
            client.download(&remote_v, &local).map(|_| format!("Downloaded {remote_v} to {} over {}.", local.display(), s.name()))
        };
        match result {
            Ok(m) => {
                s.local(&format!("\r\n[{m}]\r\n"));
                m
            }
            Err(e) => format!("SFTP {} failed on {}: {e}", if upload { "upload" } else { "download" }, s.name()),
        }
    }
}

pub fn match_name_pub(name: &str, sessions: &[Arc<Session>]) -> Option<Arc<Session>> {
    let key = name.trim().to_lowercase();
    if let Some(s) = sessions.iter().find(|s| s.name().to_lowercase() == key) {
        return Some(s.clone());
    }
    if !key.is_empty() && key.chars().all(|c| c.is_ascii_digit()) {
        return sessions.iter().find(|s| s.name().to_lowercase() == format!("terminal {key}")).cloned();
    }
    None
}

fn shellexpand_home(p: &str) -> String {
    if let Some(rest) = p.strip_prefix("~/") {
        if let Some(h) = dirs::home_dir() {
            return h.join(rest).to_string_lossy().to_string();
        }
    }
    p.to_string()
}

fn embedding_endpoint(cfg: &Config) -> String {
    let explicit = cfg.get_str("embedding_endpoint_url");
    if !explicit.is_empty() {
        return explicit;
    }
    let chat = cfg.get_str("endpoint_url");
    if chat.contains(":11434") { chat } else { "http://127.0.0.1:11434".into() }
}

fn request_session(shared: &Shared, query: &str) -> (Option<Arc<Session>>, String, bool) {
    // returns (session, target name, terminal_missing)
    if let Ok(Some(c)) = Regex::new(r"(?i)\bterminal\s+(\d+)\b").unwrap().captures(query) {
        let name = format!("Terminal {}", &c[1]);
        let s = shared.hub.find(&name);
        let missing = s.is_none();
        return (s, name, missing);
    }
    let current = shared.hub.active_session();
    let s = default_target(&shared.hub.list(), current.as_ref()).or(current);
    let name = s.as_ref().map(|s| s.name()).unwrap_or_else(|| "active terminal".into());
    (s, name, false)
}

/// Direct handling of simple, explicit terminal-management requests (no model needed).
/// Returns the action output when the request was handled.
fn handle_management_request(shared: &Shared, query: &str, tx: &Sender<Event>) -> Option<(String, String)> {
    let hub = &shared.hub;
    if let Some((tname, new_name)) = parse_rename_request(query) {
        let mut tname = tname;
        if ["current terminal", "active terminal"].contains(&tname.to_lowercase().as_str()) {
            if let Some(s) = hub.active_session() {
                tname = s.name();
            }
        }
        let session = hub.find(&tname);
        if let Some(d) = hub.find(&new_name) {
            if session.as_ref().map(|s| s.id) != Some(d.id) {
                let _ = tx.send(Event::Note(format!("A terminal named {new_name} already exists; no rename was made."), NoteKind::Error));
                return Some((String::new(), String::new()));
            }
        }
        if session.is_none() {
            let _ = tx.send(Event::Note(format!("Could not find {tname}; no terminal was renamed."), NoteKind::Error));
            return Some((String::new(), String::new()));
        }
        let out = if hub.rename(&tname, &new_name) { format!("Renamed {tname} to {new_name}.") } else { format!("Could not rename {tname}.") };
        return Some((format!("Rename {tname}"), out));
    }
    if let Some(name) = parse_create_terminal_request(query) {
        if let Some(s) = hub.find(&name) {
            hub.select(&s);
            return Some((format!("Select terminal {name}"), format!("Selected existing terminal {name}.")));
        }
        let s = hub.new_session(Some(&name));
        hub.select(&s);
        return Some((format!("Create terminal {name}"), format!("Created and selected terminal {name}.")));
    }
    let specs = parse_terminal_connection_specs(query);
    if !is_multi_terminal_setup_request(query, &specs) {
        return None;
    }
    if !contains(r"\b(?:print|type|write|send|enter)\b", query) {
        let _ = tx.send(Event::Note("I found the connection details, but no text was requested to send. Specify what to type after connecting.".into(), NoteKind::Warn));
        return Some((String::new(), String::new()));
    }
    let mut lines: Vec<String> = Vec::new();
    for (i, spec) in order_terminal_specs(query, specs).into_iter().take(2).enumerate() {
        let n = i + 1;
        let tname = format!("Terminal {n}");
        let session = hub.find(&tname).unwrap_or_else(|| hub.new_session(Some(&tname)));
        let cmd = format!("echo \"Terminal {n}\"");
        if session.is_connected() {
            let _ = tx.send(Event::Note(format!("{tname} is already connected; leaving that session unchanged."), NoteKind::Warn));
        } else {
            hub.select(&session);
            let cs = if spec.protocol == "ssh" {
                ConnSpec::Ssh { host: spec.host.clone(), username: spec.username.clone(), password: spec.password.clone(), port: spec.port, look_for_keys: false }
            } else {
                ConnSpec::Telnet { host: spec.host.clone(), port: spec.port }
            };
            let _ = tx.send(Event::Note(format!("Prepared {tname} using the app's {} connection helper for {}:{}.", spec.protocol.to_uppercase(), spec.host, spec.port), NoteKind::Info));
            if let Err(e) = session.connect(cs) {
                lines.push(format!("{tname} connection/action failed: {e}"));
                continue;
            }
        }
        let out = send_commands(&[session.clone()], &[cmd], 0.5, &Arc::new(AtomicBool::new(false)));
        lines.push(out);
    }
    Some(("Connect and send to requested terminals".into(), lines.join("\n\n")))
}

/// Runs one request. Blocking: call from a worker thread.
pub fn run_request(shared: &Shared, cfg: &Config, req: &Request, history: &[Value], cancel: &Arc<AtomicBool>, tx: &Sender<Event>) {
    let query = req.query.clone();
    let advice_only = is_advice_only_request(&query);
    let auto_run = req.auto_mode && !advice_only && !req.propose_only;
    let approval_eligible = !req.auto_mode && !advice_only && !req.propose_only;
    shared.proposals.clear();
    shared.blocked.clear();
    let (req_session, target_name, _missing) = request_session(shared, &query);
    let finish = |streamed: String, failed: bool| {
        let _ = tx.send(Event::Finished { cancelled: cancel.load(Ordering::SeqCst), failed, streamed, request_session: req_session.as_ref().map(|s| s.id), auto_run, approval_eligible, explicit_exec: is_explicit_terminal_execution_request(&query) });
    };

    // direct handling of simple explicit management requests
    if let Some((title, output)) = handle_management_request(shared, &query, tx) {
        if !title.is_empty() {
            let _ = tx.send(Event::ActionResult { title: title.clone(), output: output.clone() });
            summarize_action(cfg, history, &title, &output, cancel, tx, &finish);
            return;
        }
        finish(String::new(), false);
        return;
    }

    let mut terminal_context = String::new();
    if req.attach_terminal {
        if let Some(s) = &req_session {
            terminal_context = s.recent_output(6000);
        }
        if !terminal_context.is_empty() && cfg.get_bool("redact_secrets", true) && is_cloud(cfg) {
            terminal_context = redact_secrets(&terminal_context);
        }
        if !terminal_context.is_empty() {
            let _ = tx.send(Event::Note(format!("[Attached {} characters from {target_name}]", terminal_context.chars().count()), NoteKind::Info));
        } else {
            let _ = tx.send(Event::Note(format!("[No terminal scrollback was captured from {target_name}]"), NoteKind::Warn));
        }
    }
    let open_terminals: Vec<Value> = shared.hub.list().iter().map(|s| json!({"name": s.name(), "connected": s.is_connected(), "connection": s.label()})).collect();
    let tools = tools_for_request(&query, auto_run);
    let mut sys = vec!["You are the agent embedded inside Modern Multi Terminal, a desktop terminal application. The supplied app tools execute real operations in its GUI and embedded terminal sessions. They are the only way to manipulate this app; never suggest xterm, macOS Terminal, or a general shell command for tab operations. Call create_terminal, rename_terminal, connect_terminal, send_terminal, read_terminal, or list_terminals as appropriate. The arguments must contain actual user values, never copy parameter descriptions/schema objects into argument values. Examples: 'launch a new terminal and give name aby' => create_terminal(arguments={name: 'aby'}); 'rename the current Terminal 1 name to Terminal 2' => rename_terminal(arguments={terminal_name: 'current', new_name: 'Terminal 2'}). For explicit requests to type/run/send, call send_terminal with the literal command; the app sends it and returns the terminal output. For log questions use attached output or read_terminal. Wait for tool results and report only what they confirm. For questions asking for an explanation or advice that does not require an app action, answer directly in normal natural language - do not return a JSON tool-call template. Only if an app action is requested and native function calling is unavailable, return one JSON call with a real tool name and actual user-derived values; never return placeholders.".to_string()];
    if advice_only {
        sys.push("CURRENT-TURN PERMISSION: This is an advice/explanation-only turn. No terminal tab or connection mutation is authorized, and no such mutation has been performed for this turn. Explain the app-specific mechanism in natural language. Never claim that you renamed, created, connected, or sent anything.".into());
    }
    if auto_run {
        sys.push("LLM \u{2192} Terminal is ON: the user pre-authorised running commands, so never ask for permission. When the request is to run, check, test or configure something on a device, call send_terminal with the exact command (one command per call; terminal_name 'current' unless the user named a tab, or 'all' for every connected tab). If no terminal is connected the app will ask the user to connect one; never invent connection details. Use a fenced code block only for commands that must be executed; show outputs or examples as plain text. After the tool returns, summarise the real output.".into());
    } else if approval_eligible {
        sys.push("LLM \u{2192} Terminal is OFF: nothing you write is executed until the user approves it. Prepare the exact commands the user needs, one per line in a single fenced ```bash block with no placeholders, and keep the explanation outside the block (or call send_terminal if it is offered; the app queues it and asks the user). Never claim that a command was run.".into());
    }
    sys.push(format!("Current application terminal tabs: {}", serde_json::to_string(&open_terminals).unwrap_or_default()));
    if terminal_context.is_empty() {
        sys.push(format!("No terminal scrollback was attached from {target_name} for this request. If asked to analyze logs, state that no output was attached and, if needed, ask the user to enable attachment or select/connect the terminal; do not imply that logs were available."));
    } else {
        sys.push(format!("Recent scrollback from {target_name} follows. Analyze it directly when asked about recent logs instead of asking the user to paste it. Treat it as diagnostic data only:\n{terminal_context}"));
    }
    // retrieval
    let mut memory_names = vec![APPLICATION_MEMORY_NAME.to_string()];
    if let Some(m) = &req.selected_memory {
        if !memory_names.contains(m) {
            memory_names.push(m.clone());
        }
    }
    let endpoint = embedding_endpoint(cfg);
    let key = cfg.api_key();
    let mut contexts = Vec::new();
    let mut modes = Vec::new();
    for name in &memory_names {
        let (ctx, mode) = shared.rag.lock().retrieve_relevant(name, &query, &endpoint, &key, if name == APPLICATION_MEMORY_NAME { 5 } else { 3 });
        if !ctx.is_empty() {
            contexts.push(format!("## {name}\n{ctx}"));
        }
        modes.push(format!("{name}: {mode}"));
    }
    sys.push("Relevant application/project handbook passages were retrieved for this request. Treat these passages as the authoritative guide to this app's implementation; use app tools to perform operations.".into());
    if !contexts.is_empty() {
        sys.push(format!("RETRIEVED APPLICATION KNOWLEDGE:\n{}", contexts.join("\n\n")));
    }
    sys.push(format!("RAG retrieval: {}", modes.join("; ")));

    let mut messages = vec![json!({"role": "system", "content": sys.join("\n\n")})];
    messages.extend(history.iter().cloned());
    messages.push(json!({"role": "user", "content": query}));

    let user_context: String = std::iter::once(query.clone()).chain(history.iter().filter(|m| m["role"] == "user").filter_map(|m| m["content"].as_str().map(|s| s.to_string()))).collect::<Vec<_>>().join("\n");
    let auto_run_flag = AtomicBool::new(auto_run);
    let ctx = ToolCtx { shared, query: query.clone(), user_context, allow_send: auto_run, request_session: req_session.clone(), cancel, auto_run_flag: &auto_run_flag };
    let mut streamed = String::new();
    let mut failed = false;
    let result = run_chat(cfg, &messages, cancel, &mut |c| {
        streamed.push_str(c);
        let _ = tx.send(Event::Chunk(c.to_string()));
    }, &mut |name, args| ctx.execute(name, args), Some(tools), 4);
    if let Err(e) = result {
        if !cancel.load(Ordering::SeqCst) {
            failed = true;
            let _ = tx.send(Event::Error(e.to_string()));
        }
    }
    // the tool path may have cleared the auto-run flag so fenced commands are not run twice
    let auto = auto_run_flag.load(Ordering::SeqCst);
    let _ = tx.send(Event::Finished { cancelled: cancel.load(Ordering::SeqCst), failed, streamed, request_session: req_session.as_ref().map(|s| s.id), auto_run: auto, approval_eligible, explicit_exec: is_explicit_terminal_execution_request(&query) });
}

/// Ask the model to interpret the real output of an action.
pub fn summarize_action(cfg: &Config, history: &[Value], title: &str, output: &str, cancel: &Arc<AtomicBool>, tx: &Sender<Event>, finish: &dyn Fn(String, bool)) {
    let _ = tx.send(Event::AssistantHeader("AI interpretation:".into()));
    let tail: String = { let c: Vec<char> = output.chars().collect(); c[c.len().saturating_sub(8000)..].iter().collect() };
    let mut messages = vec![json!({"role": "system", "content": "You are the AI Test Engineer in Modern Multi Terminal. A requested action was run through the app's own terminal automation engine. The following is the actual output returned by the application terminal. Explain what it shows, be concise, and do not claim success unless the output proves it. Do not return another command or claim to have performed additional actions."})];
    messages.extend(history.iter().cloned());
    messages.push(json!({"role": "user", "content": format!("Summarize the application response for {title}:\n{tail}")}));
    let mut streamed = String::new();
    match stream_plain(cfg, &messages, cancel, &mut |c| {
        streamed.push_str(c);
        let _ = tx.send(Event::Chunk(c.to_string()));
    }) {
        Ok(_) => finish(streamed, false),
        Err(e) => {
            if !cancel.load(Ordering::SeqCst) {
                let _ = tx.send(Event::Error(e.to_string()));
            }
            finish(streamed, true);
        }
    }
}

/// Used by the "Run in Terminal" flow: send, then have the model interpret the output.
pub fn run_commands_and_summarize(shared: &Shared, cfg: &Config, history: &[Value], sessions: &[Arc<Session>], commands: &[String], delay: f64, cancel: &Arc<AtomicBool>, tx: &Sender<Event>) {
    let names = sessions.iter().map(|s| s.name()).collect::<Vec<_>>().join(", ");
    let _ = tx.send(Event::Note(format!("Running {} command(s) on {names} through the application's automation engine.", commands.len()), NoteKind::Info));
    if let Some(s) = sessions.first() {
        shared.hub.select(s);
    }
    let output = send_commands(sessions, commands, delay, cancel);
    let title = format!("Send AI commands to {names}");
    let _ = tx.send(Event::ActionResult { title: title.clone(), output: output.clone() });
    let finish = |streamed: String, failed: bool| {
        let _ = tx.send(Event::Finished { cancelled: cancel.load(Ordering::SeqCst), failed, streamed, request_session: None, auto_run: false, approval_eligible: false, explicit_exec: false });
    };
    if cancel.load(Ordering::SeqCst) {
        finish(String::new(), false);
        return;
    }
    summarize_action(cfg, history, &title, &output, cancel, tx, &finish);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ai::rag::RagManager;
    use std::io::{Read, Write};

    /// Fake OpenAI-compatible server: answers each connection with the next canned SSE body.
    fn serve_many(bodies: Vec<String>) -> u16 {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            for body in bodies {
                let Ok((mut s, _)) = listener.accept() else { return };
                let mut buf = [0u8; 65536];
                let mut req = Vec::new();
                loop {
                    let n = s.read(&mut buf).unwrap_or(0);
                    req.extend_from_slice(&buf[..n]);
                    let text = String::from_utf8_lossy(&req).to_string();
                    if let Some(h) = text.find("\r\n\r\n") {
                        let len = text[..h].lines().find_map(|l| l.to_lowercase().strip_prefix("content-length:").map(|v| v.trim().parse::<usize>().unwrap_or(0))).unwrap_or(0);
                        if req.len() >= h + 4 + len {
                            break;
                        }
                    }
                    if n == 0 {
                        break;
                    }
                }
                let resp = format!("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
                let _ = s.write_all(resp.as_bytes());
            }
        });
        port
    }

    fn make(port: u16) -> (Shared, Config, crossbeam_channel::Receiver<crate::session::UiRequest>) {
        let (hub, rx) = Hub::new();
        let dir = std::env::temp_dir().join(format!("mmte_chat_{}_{}", std::process::id(), port));
        let rag = Arc::new(Mutex::new(RagManager::load_from(dir.join("rag.json"))));
        let mut cfg = Config::load_from(std::path::Path::new("/nonexistent"));
        cfg.set("provider", json!("Custom / Local HTTP"));
        cfg.set("endpoint_url", json!(format!("http://127.0.0.1:{port}/v1")));
        cfg.set("embedding_endpoint_url", json!("http://127.0.0.1:9"));
        (Shared { hub, rag, proposals: Arc::new(ProposalQueue::default()), blocked: Arc::new(ProposalQueue::default()) }, cfg, rx)
    }

    fn collect(rx: &crossbeam_channel::Receiver<Event>) -> (String, Vec<String>, bool) {
        let mut text = String::new();
        let mut notes = Vec::new();
        let mut finished = false;
        while let Ok(ev) = rx.recv_timeout(Duration::from_secs(5)) {
            match ev {
                Event::Chunk(c) => text.push_str(&c),
                Event::Note(n, _) => notes.push(n),
                Event::Error(e) => notes.push(format!("ERR {e}")),
                Event::ActionResult { output, .. } => notes.push(format!("ACTION {output}")),
                Event::Finished { .. } => {
                    finished = true;
                    break;
                }
                _ => {}
            }
        }
        (text, notes, finished)
    }

    #[test]
    fn model_uses_tool_then_answers() {
        let tool = "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"c1\",\"function\":{\"name\":\"list_terminals\",\"arguments\":\"{}\"}}]}}]}\n\ndata: [DONE]\n\n".to_string();
        let answer = "data: {\"choices\":[{\"delta\":{\"content\":\"You have 1 terminal.\"}}]}\n\ndata: [DONE]\n\n".to_string();
        let port = serve_many(vec![tool, answer]);
        let (shared, cfg, _ui) = make(port);
        shared.hub.new_session(None);
        let (tx, rx) = crossbeam_channel::unbounded();
        let cancel = Arc::new(AtomicBool::new(false));
        let req = Request { query: "which terminals are open".into(), auto_mode: false, attach_terminal: false, selected_memory: None, propose_only: false };
        run_request(&shared, &cfg, &req, &[], &cancel, &tx);
        let (text, notes, finished) = collect(&rx);
        assert!(finished, "{notes:?}");
        assert_eq!(text, "You have 1 terminal.");
    }

    #[test]
    fn send_is_queued_when_auto_mode_is_off() {
        let tool = "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"c1\",\"function\":{\"name\":\"send_terminal\",\"arguments\":\"{\\\"terminal_name\\\":\\\"current\\\",\\\"text\\\":\\\"ls -l\\\"}\"}}]}}]}\n\ndata: [DONE]\n\n".to_string();
        let answer = "data: {\"choices\":[{\"delta\":{\"content\":\"Waiting for your approval.\"}}]}\n\ndata: [DONE]\n\n".to_string();
        let port = serve_many(vec![tool, answer]);
        let (shared, cfg, _ui) = make(port);
        shared.hub.new_session(None);
        let (tx, rx) = crossbeam_channel::unbounded();
        let cancel = Arc::new(AtomicBool::new(false));
        let req = Request { query: "run ls -l in terminal 1".into(), auto_mode: false, attach_terminal: false, selected_memory: None, propose_only: false };
        run_request(&shared, &cfg, &req, &[], &cancel, &tx);
        let (_text, _notes, finished) = collect(&rx);
        assert!(finished);
        let groups = shared.proposals.groups();
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0].1, vec!["ls -l"]);
    }

    #[test]
    fn direct_rename_and_create_need_no_model() {
        let (shared, cfg, _ui) = make(9);
        let a = shared.hub.new_session(None);
        let (tx, rx) = crossbeam_channel::unbounded();
        let cancel = Arc::new(AtomicBool::new(false));
        // the summary call fails (no server) but the action itself must already be done
        let req = Request { query: "rename the current terminal to Lab".into(), auto_mode: false, attach_terminal: false, selected_memory: None, propose_only: false };
        run_request(&shared, &cfg, &req, &[], &cancel, &tx);
        let (_t, notes, finished) = collect(&rx);
        assert!(finished);
        assert!(notes.iter().any(|n| n.contains("Renamed Terminal 1 to Lab")), "{notes:?}");
        assert_eq!(a.name(), "Lab");
        let req = Request { query: "create a terminal named Router".into(), auto_mode: false, attach_terminal: false, selected_memory: None, propose_only: false };
        run_request(&shared, &cfg, &req, &[], &cancel, &tx);
        let _ = collect(&rx);
        assert!(shared.hub.find("Router").is_some());
    }

    #[test]
    fn tools_refuse_unrequested_mutations() {
        let (shared, _cfg, _ui) = make(9);
        shared.hub.new_session(None);
        let cancel = Arc::new(AtomicBool::new(false));
        let flag = AtomicBool::new(false);
        let ctx = ToolCtx { shared: &shared, query: "just tell me about tabs".into(), user_context: String::new(), allow_send: false, request_session: None, cancel: &cancel, auto_run_flag: &flag };
        assert!(ctx.execute("create_terminal", &json!({"name": "x"})).starts_with("Error"));
        assert!(ctx.execute("connect_terminal", &json!({"terminal_name": "current", "protocol": "ssh", "host": "1.2.3.4", "port": 22})).starts_with("Error"));
        assert!(ctx.execute("nope", &json!({})).starts_with("Error"));
        let listed = ctx.execute("list_terminals", &json!({}));
        assert!(listed.contains("Terminal 1"));
    }

    #[test]
    fn send_commands_reports_output_from_a_real_session() {
        // a Telnet echo-ish server
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            if let Ok((mut s, _)) = listener.accept() {
                let _ = s.write_all(b"login: ");
                let mut buf = [0u8; 256];
                while let Ok(n) = s.read(&mut buf) {
                    if n == 0 {
                        break;
                    }
                    let _ = s.write_all(b"got:");
                    let _ = s.write_all(&buf[..n]);
                }
            }
        });
        let (hub, _rx) = Hub::new();
        let s = hub.new_session(None);
        s.connect(ConnSpec::Telnet { host: "127.0.0.1".into(), port }).unwrap();
        let out = send_commands(&[s.clone()], &["hello".to_string()], 0.4, &Arc::new(AtomicBool::new(false)));
        assert!(out.contains("got:hello"), "{out}");
        s.close();
    }
}
