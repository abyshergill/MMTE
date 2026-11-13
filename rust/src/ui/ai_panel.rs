//! AI assistant dock: chat, modes (AUTO-RUN / ASK FIRST), Run-in-Terminal picker, LLM settings, RAG manager.

use super::theme;
use crate::ai::chat::{self, Event, NoteKind, Request, Shared};
use crate::ai::dispatch::{extract_commands, parse_command_lines, resolve_targets, ProposalQueue};
use crate::ai::knowledge::APPLICATION_MEMORY_NAME;
use crate::ai::rag::{RagManager, AVAILABLE_EMBEDDING_MODELS};
use crate::ai::{self, Config, PROVIDERS};
use crate::session::{Hub, Session};
use crossbeam_channel::{unbounded, Receiver, Sender};
use eframe::egui::{self, Align, Color32, Layout, RichText};
use parking_lot::Mutex;
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

#[derive(Clone, Copy, PartialEq)]
enum Kind {
    User,
    AiHeader,
    Ai,
    Note,
    Warn,
    Err,
    Action,
}

struct Line {
    kind: Kind,
    text: String,
}

struct SettingsForm {
    provider: String,
    endpoint: String,
    model: String,
    api_key: String,
    temperature: f64,
    max_tokens: i64,
    context_length: i64,
    timeout: i64,
    tool_mode: String,
    gguf_path: String,
    embedding_endpoint: String,
    redact: bool,
    models: Vec<String>,
    status: String,
    rx: Option<Receiver<Result<Vec<String>, String>>>,
}

struct RagForm {
    selected: String,
    name: String,
    model: String,
    job: String,
    new_text: String,
    files: Vec<std::path::PathBuf>,
    status: String,
}

struct RunDlg {
    commands: String,
    checked: Vec<u64>,
    wanted: Vec<u64>,
    delay: f64,
    approval: bool,
}

pub struct AiPanel {
    pub open: bool,
    hub: Arc<Hub>,
    shared: Arc<Shared>,
    cfg: Config,
    chat: Vec<Line>,
    history: Vec<Value>,
    input: String,
    auto_mode: bool,
    attach: bool,
    selected_rag: Option<String>,
    tx: Sender<Event>,
    rx: Receiver<Event>,
    cancel: Option<Arc<AtomicBool>>,
    streamed: String,
    failed: bool,
    discard: bool,
    status: String,
    settings: Option<SettingsForm>,
    rag_dlg: Option<RagForm>,
    run_dlg: Option<RunDlg>,
    run_queue: VecDeque<(Vec<String>, Vec<u64>, bool)>,
    propose_only_next: bool,
    focus_input: bool,
    connect_request: Option<(u64, &'static str)>,
    last_commands: Vec<String>,
}

const MAX_HISTORY: usize = 40;

impl AiPanel {
    pub fn new(hub: Arc<Hub>) -> Self {
        let cfg = Config::load();
        let (tx, rx) = unbounded();
        let shared = Arc::new(Shared { hub: hub.clone(), rag: Arc::new(Mutex::new(RagManager::load())), proposals: Arc::new(ProposalQueue::default()), blocked: Arc::new(ProposalQueue::default()) });
        let auto_mode = cfg.get_bool("llm_to_terminal", false);
        Self { open: false, hub, shared, cfg, chat: Vec::new(), history: Vec::new(), input: String::new(), auto_mode, attach: true, selected_rag: None, tx, rx, cancel: None, streamed: String::new(), failed: false, discard: false, status: String::new(), settings: None, rag_dlg: None, run_dlg: None, run_queue: VecDeque::new(), propose_only_next: false, focus_input: false, connect_request: None, last_commands: Vec::new() }
    }

    /// Sample conversation for screenshots (MMTE_DEMO_WINDOW=ai).
    pub fn demo(&mut self) {
        self.open = true;
        self.note("check the uptime of the device", Kind::User);
        self.chat.push(Line { kind: Kind::AiHeader, text: "AI Assistant:".into() });
        self.chat.push(Line { kind: Kind::Note, text: "[Attached 412 characters from Terminal 1]".into() });
        self.chat.push(Line { kind: Kind::Action, text: "Application response \u{2014} Send AI commands to Terminal 1:\n[Terminal 1]\nroot@lab:~# uptime\n 10:01:22 up 3 days, 2:11, 1 user".into() });
        self.chat.push(Line { kind: Kind::AiHeader, text: "AI interpretation:".into() });
        self.chat.push(Line { kind: Kind::Ai, text: "The device has been up for 3 days and has one logged-in user.".into() });
        self.chat.push(Line { kind: Kind::Warn, text: "The AI prepared 1 command(s). Waiting for your permission.".into() });
    }

    pub fn wants_keyboard(&self) -> bool {
        self.open || self.settings.is_some() || self.rag_dlg.is_some() || self.run_dlg.is_some()
    }

    pub fn take_connect_request(&mut self) -> Option<(u64, &'static str)> {
        self.connect_request.take()
    }

    pub fn menu(&mut self, ui: &mut egui::Ui) {
        ui.menu_button("AI", |ui| {
            if ui.checkbox(&mut self.open, "AI Assistant Window").changed() {
                self.focus_input = self.open;
            }
            if ui.button("Manage Project RAG Memories...").clicked() {
                self.open_rag();
                ui.close();
            }
            if ui.button("LLM Settings...").clicked() {
                self.open_settings();
                ui.close();
            }
        });
    }

    fn note(&mut self, text: &str, kind: Kind) {
        self.chat.push(Line { kind, text: text.to_string() });
    }

    // --- events from the worker ---------------------------------------------------------------
    fn poll(&mut self) {
        while let Ok(ev) = self.rx.try_recv() {
            match ev {
                Event::Chunk(c) => {
                    if self.discard {
                        continue;
                    }
                    self.streamed.push_str(&c);
                    match self.chat.last_mut() {
                        Some(l) if l.kind == Kind::Ai => l.text.push_str(&c),
                        _ => self.chat.push(Line { kind: Kind::Ai, text: c }),
                    }
                    self.status = "Receiving model response...".into();
                }
                Event::Note(t, k) => self.note(&t, match k { NoteKind::Info => Kind::Note, NoteKind::Warn => Kind::Warn, NoteKind::Error => Kind::Err }),
                Event::ActionResult { title, output } => {
                    let shown: String = { let c: Vec<char> = output.trim().chars().collect(); c[c.len().saturating_sub(5000)..].iter().collect() };
                    let shown = if shown.is_empty() { "The application completed the automation but returned no terminal output.".to_string() } else { shown };
                    self.chat.push(Line { kind: Kind::Action, text: format!("Application response \u{2014} {title}:\n{shown}") });
                    self.history.push(json!({"role": "assistant", "content": format!("Application response for {title}:\n{shown}")}));
                }
                Event::AssistantHeader(h) => {
                    self.streamed.clear();
                    self.chat.push(Line { kind: Kind::AiHeader, text: h });
                }
                Event::Error(e) => {
                    self.failed = true;
                    self.note(&format!("Error: {e}"), Kind::Err);
                }
                Event::Finished { cancelled, failed, streamed, request_session, auto_run, approval_eligible, explicit_exec } => {
                    if failed {
                        self.failed = true;
                    }
                    self.finish_request(cancelled, streamed, request_session, auto_run, approval_eligible, explicit_exec);
                }
            }
        }
    }

    fn finish_request(&mut self, cancelled: bool, streamed: String, request_session: Option<u64>, auto_run: bool, approval_eligible: bool, explicit_exec: bool) {
        let text = if streamed.trim().is_empty() { self.streamed.clone() } else { streamed };
        if !text.trim().is_empty() && !self.discard {
            self.history.push(json!({"role": "assistant", "content": text}));
        }
        if cancelled && !self.discard {
            self.note("[Response stopped]", Kind::Warn);
        } else if text.is_empty() && !self.failed && !self.discard {
            // actions with no model text (e.g. fast paths) are fine; only flag real empties
            if self.history.last().map(|m| m["role"] == "user").unwrap_or(false) {
                self.note("[The model returned an empty response]", Kind::Err);
            }
        }
        self.cancel = None;
        self.status.clear();
        let usable = !cancelled && !self.failed && !self.discard;
        if !usable {
            self.shared.proposals.clear();
            self.shared.blocked.clear();
        }
        let proposals = &self.shared.proposals;
        if auto_run && usable {
            let commands = extract_commands(&self.streamed);
            if !commands.is_empty() {
                self.run_on_request_session(commands, request_session);
            } else if proposals.len() == 0 && self.shared.blocked.len() == 0 && explicit_exec {
                self.failed = true;
                self.note("Error: The request asked to send a command, but the model returned no fenced shell command. Nothing was sent.", Kind::Err);
            }
        } else if approval_eligible && usable && proposals.len() == 0 {
            for c in extract_commands(&self.streamed) {
                proposals.add("current", &c);
            }
        }
        if usable {
            self.prompt_pending(request_session);
        }
        self.streamed.clear();
        self.failed = false;
        self.discard = false;
    }

    /// Send commands to one terminal; if it cannot receive them, open the picker so one can be connected.
    fn run_on_request_session(&mut self, commands: Vec<String>, session_id: Option<u64>) {
        let sessions = self.hub.list();
        let target = session_id.and_then(|id| sessions.iter().find(|s| s.id == id).cloned()).or_else(|| crate::ai::dispatch::default_target(&sessions, self.hub.active_session().as_ref()));
        match target {
            Some(s) if s.is_connected() => self.run_commands(commands, vec![s], 1.2),
            other => {
                self.note("The target terminal is not connected. Choose or connect a terminal to send the commands.", Kind::Warn);
                self.run_queue.push_back((commands, other.map(|s| vec![s.id]).unwrap_or_default(), false));
            }
        }
    }

    fn prompt_pending(&mut self, request_session: Option<u64>) {
        let mut groups: Vec<(String, Vec<String>, bool)> = self.shared.proposals.groups().into_iter().map(|(s, c)| (s, c, true)).collect();
        groups.extend(self.shared.blocked.groups().into_iter().map(|(s, c)| (s, c, false)));
        self.shared.proposals.clear();
        self.shared.blocked.clear();
        let sessions = self.hub.list();
        let current = request_session.and_then(|id| sessions.iter().find(|s| s.id == id).cloned()).or_else(|| self.hub.active_session());
        for (spec, commands, approval) in groups {
            let r = resolve_targets(&spec, &sessions, current.as_ref());
            if approval {
                self.note(&format!("The AI prepared {} command(s). Waiting for your permission.", commands.len()), Kind::Warn);
            } else {
                self.note(&format!("{} command(s) are ready, but no connected terminal could receive them. Connect one to continue.", commands.len()), Kind::Warn);
            }
            let pre: Vec<u64> = r.sessions.iter().chain(r.not_connected.iter()).map(|s| s.id).collect();
            self.run_queue.push_back((commands, pre, approval));
        }
    }

    fn open_next_run_dialog(&mut self) {
        if self.run_dlg.is_some() {
            return;
        }
        if let Some((commands, pre, approval)) = self.run_queue.pop_front() {
            self.last_commands = commands.clone();
            let sessions = self.hub.list();
            let mut checked = Vec::new();
            let mut wanted = Vec::new();
            for id in pre {
                if let Some(s) = sessions.iter().find(|s| s.id == id) {
                    if s.is_connected() { checked.push(id) } else { wanted.push(id) }
                }
            }
            self.run_dlg = Some(RunDlg { commands: commands.join("\n"), checked, wanted, delay: 1.2, approval });
        }
    }

    fn run_commands(&mut self, commands: Vec<String>, sessions: Vec<Arc<Session>>, delay: f64) {
        let commands = crate::ai::dispatch::clean_commands(commands.iter().map(|s| s.as_str()));
        let sessions: Vec<Arc<Session>> = sessions.into_iter().filter(|s| s.is_connected()).collect();
        if commands.is_empty() || sessions.is_empty() {
            self.note("Error: Nothing to send: no commands or no connected terminal selected.", Kind::Err);
            return;
        }
        let cancel = Arc::new(AtomicBool::new(false));
        self.cancel = Some(cancel.clone());
        self.streamed.clear();
        self.status = "Running commands...".into();
        let (shared, cfg, history, tx) = (self.shared.clone(), self.cfg.clone(), self.history.clone(), self.tx.clone());
        std::thread::spawn(move || chat::run_commands_and_summarize(&shared, &cfg, &history, &sessions, &commands, delay, &cancel, &tx));
    }

    // --- sending ---------------------------------------------------------------------------------
    fn send(&mut self) {
        if let Some(c) = &self.cancel {
            c.store(true, Ordering::SeqCst);
            self.status = "Stopping response...".into();
            if let Some(s) = self.hub.active_session() {
                if s.script_running() {
                    s.request_stop_script();
                }
            }
            return;
        }
        let query = self.input.trim().to_string();
        if query.is_empty() {
            return;
        }
        self.input.clear();
        self.note(&query, Kind::User);
        self.chat.push(Line { kind: Kind::AiHeader, text: "AI Assistant:".into() });
        self.history.push(json!({"role": "user", "content": query}));
        if self.history.len() > MAX_HISTORY {
            let cut = self.history.len() - MAX_HISTORY;
            self.history.drain(..cut);
        }
        let cancel = Arc::new(AtomicBool::new(false));
        self.cancel = Some(cancel.clone());
        self.streamed.clear();
        self.failed = false;
        self.status = "Connecting to the model...".into();
        let req = Request { query, auto_mode: self.auto_mode, attach_terminal: self.attach, selected_memory: self.selected_rag.clone(), propose_only: std::mem::take(&mut self.propose_only_next) };
        let (shared, cfg, tx) = (self.shared.clone(), self.cfg.clone(), self.tx.clone());
        let history: Vec<Value> = self.history[..self.history.len() - 1].to_vec();
        std::thread::spawn(move || chat::run_request(&shared, &cfg, &req, &history, &cancel, &tx));
    }

    fn clear(&mut self) {
        if let Some(c) = &self.cancel {
            self.discard = true;
            c.store(true, Ordering::SeqCst);
        }
        self.history.clear();
        self.chat.clear();
        self.shared.proposals.clear();
        self.shared.blocked.clear();
    }

    // --- UI ------------------------------------------------------------------------------------------
    pub fn right_panel(&mut self, ui: &mut egui::Ui, _hub: &Arc<Hub>) {
        self.poll();
        if !self.open {
            return;
        }
        egui::Panel::right("ai_panel").default_size(420.0).min_size(320.0).resizable(true).frame(egui::Frame::default().fill(theme::BG1).inner_margin(egui::Margin::same(12))).show(ui, |ui| {
            ui.horizontal(|ui| {
                ui.label(RichText::new("AI TEST ENGINEER").strong().size(14.0));
                let (txt, col) = if self.auto_mode { ("AUTO-RUN", theme::ERR) } else { ("ASK FIRST", theme::WARN) };
                ui.label(RichText::new(txt).color(col).strong().size(11.0));
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if ui.small_button("LLM Settings").clicked() {
                        self.open_settings();
                    }
                    if ui.small_button("RAG").clicked() {
                        self.open_rag();
                    }
                    if ui.small_button("\u{00D7}").on_hover_text("Close the AI window").clicked() {
                        self.open = false;
                    }
                });
            });
            ui.horizontal(|ui| {
                ui.label(RichText::new("Memory").color(theme::MUTE).size(12.0));
                let names = self.shared.rag.lock().list_names();
                let cur = self.selected_rag.clone().unwrap_or_else(|| "(application guide only)".into());
                egui::ComboBox::from_id_salt("ai_rag").width(240.0).selected_text(cur).show_ui(ui, |ui| {
                    if ui.selectable_label(self.selected_rag.is_none(), "(application guide only)").clicked() {
                        self.selected_rag = None;
                    }
                    for n in names.iter().filter(|n| n.as_str() != APPLICATION_MEMORY_NAME) {
                        if ui.selectable_label(self.selected_rag.as_deref() == Some(n.as_str()), n).clicked() {
                            self.selected_rag = Some(n.clone());
                        }
                    }
                });
            });
            ui.add_space(4.0);
            let input_h = 90.0;
            let avail = ui.available_height() - input_h - 118.0;
            egui::Frame::new().fill(theme::BG0).stroke(egui::Stroke::new(1.0, theme::LINE)).corner_radius(8.0).inner_margin(8.0).show(ui, |ui| {
                egui::ScrollArea::vertical().stick_to_bottom(true).auto_shrink([false, false]).max_height(avail.max(120.0)).min_scrolled_height(avail.max(120.0)).show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    if self.chat.is_empty() {
                        ui.label(RichText::new("Ask about your terminals, logs or devices.\nTry: \"analyze the recent log\" or \"run uname -a in the terminal\".").color(theme::MUTE));
                    }
                    for l in &self.chat {
                        match l.kind {
                            Kind::User => {
                                ui.add_space(4.0);
                                ui.label(RichText::new("User:").color(theme::INFO).strong());
                                ui.label(&l.text);
                            }
                            Kind::AiHeader => {
                                ui.add_space(4.0);
                                ui.label(RichText::new(&l.text).color(theme::OK).strong());
                            }
                            Kind::Ai => {
                                ui.label(&l.text);
                            }
                            Kind::Note => {
                                ui.label(RichText::new(&l.text).italics().color(theme::TERM_ACCENT));
                            }
                            Kind::Warn => {
                                ui.label(RichText::new(&l.text).italics().color(theme::WARN));
                            }
                            Kind::Err => {
                                ui.label(RichText::new(&l.text).color(theme::ERR));
                            }
                            Kind::Action => {
                                egui::Frame::new().fill(theme::TERM_BG).inner_margin(6.0).corner_radius(6.0).show(ui, |ui| {
                                    ui.label(RichText::new(&l.text).monospace().color(theme::TERM_FG).size(12.0));
                                });
                            }
                        }
                    }
                });
            });
            ui.add_space(4.0);
            if !self.status.is_empty() {
                let dots = ".".repeat(((ui.input(|i| i.time) * 3.0) as usize % 4) + 1);
                ui.label(RichText::new(format!("{}{}", self.status.trim_end_matches('.'), dots)).color(theme::DIM).size(12.0));
                ui.ctx().request_repaint_after(std::time::Duration::from_millis(250));
            }
            ui.horizontal(|ui| {
                if ui.checkbox(&mut self.auto_mode, "LLM \u{2192} Terminal").on_hover_text("ON: the AI runs commands straight in the terminal. OFF: it asks permission first.").changed() {
                    self.cfg.set("llm_to_terminal", json!(self.auto_mode));
                    self.cfg.save();
                }
                ui.checkbox(&mut self.attach, "Attach terminal output");
            });
            ui.label(RichText::new(if self.auto_mode { "The AI runs the commands it decides on straight in the terminal." } else { "The AI prepares commands and asks your permission before sending them." }).color(theme::MUTE).size(11.5));
            let r = ui.add(egui::TextEdit::multiline(&mut self.input).desired_rows(3).desired_width(f32::INFINITY).hint_text("Message the AI (Enter to send, Shift+Enter for a new line)"));
            if self.focus_input {
                r.request_focus();
                self.focus_input = false;
            }
            if r.has_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter) && !i.modifiers.shift) {
                // remove the newline the text box just inserted
                while self.input.ends_with('\n') {
                    self.input.pop();
                }
                self.send();
            }
            ui.horizontal(|ui| {
                let running = self.cancel.is_some();
                if ui.add(if running { theme::danger_button("Stop") } else { theme::accent_button("Send Query") }).clicked() {
                    self.send();
                }
                if ui.add_enabled(!running, egui::Button::new("Analyze Log")).on_hover_text("Ask the AI to analyze the attached terminal output (suggestions only)").clicked() {
                    self.propose_only_next = true;
                    self.attach = true;
                    self.input = "Analyze the recent terminal log: summarize what happened, point out errors or warnings, and suggest next steps.".into();
                    self.send();
                }
                if ui.add_enabled(!running, egui::Button::new("Run in Terminal")).on_hover_text("Choose terminals and run the commands from the latest AI answer").clicked() {
                    let mut commands = self.latest_commands();
                    if commands.is_empty() {
                        commands = Vec::new();
                    }
                    let current = self.hub.active_session().map(|s| vec![s.id]).unwrap_or_default();
                    self.run_queue.push_back((commands, current, false));
                }
                if ui.button("Clear").clicked() {
                    self.clear();
                }
            });
        });
    }

    fn latest_commands(&self) -> Vec<String> {
        for m in self.history.iter().rev() {
            if m["role"] == "assistant" {
                let c = extract_commands(m["content"].as_str().unwrap_or(""));
                if !c.is_empty() {
                    return c;
                }
            }
        }
        self.last_commands.clone()
    }

    pub fn windows(&mut self, ctx: &egui::Context, hub: &Arc<Hub>) {
        self.poll();
        self.open_next_run_dialog();
        self.run_dialog(ctx, hub);
        self.settings_dialog(ctx);
        self.rag_dialog(ctx);
    }

    fn run_dialog(&mut self, ctx: &egui::Context, hub: &Arc<Hub>) {
        let Some(dlg) = self.run_dlg.as_mut() else { return };
        let sessions = hub.list();
        // tabs the user is connecting right now become ticked once live
        let mut newly: Vec<u64> = Vec::new();
        dlg.wanted.retain(|id| match sessions.iter().find(|s| s.id == *id) {
            Some(s) if s.is_connected() => {
                newly.push(*id);
                false
            }
            Some(_) => true,
            None => false,
        });
        dlg.checked.extend(newly);
        dlg.checked.retain(|id| sessions.iter().any(|s| s.id == *id && s.is_connected()));
        let mut result: Option<bool> = None;
        let mut connect: Option<(u64, &'static str)> = None;
        let mut new_terminal = false;
        egui::Window::new(if dlg.approval { "Approve AI commands" } else { "Run in Terminal" }).collapsible(false).resizable(true).default_size([560.0, 520.0]).anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0]).show(ctx, |ui| {
            ui.heading(if dlg.approval { "The AI wants to run these commands" } else { "Run commands in terminals" });
            ui.label(RichText::new("COMMANDS  (one per line)").size(11.5).color(theme::MUTE).strong());
            ui.add(egui::TextEdit::multiline(&mut dlg.commands).code_editor().desired_rows(6).desired_width(f32::INFINITY));
            ui.add_space(4.0);
            ui.label(RichText::new("TERMINALS").size(11.5).color(theme::MUTE).strong());
            egui::Frame::new().fill(theme::BG0).corner_radius(6.0).inner_margin(6.0).show(ui, |ui| {
                egui::ScrollArea::vertical().max_height(150.0).show(ui, |ui| {
                    for s in &sessions {
                        let live = s.is_connected();
                        ui.horizontal(|ui| {
                            let mut on = dlg.checked.contains(&s.id);
                            if ui.add_enabled(live, egui::Checkbox::new(&mut on, "")).changed() {
                                if on { dlg.checked.push(s.id) } else { dlg.checked.retain(|i| *i != s.id) }
                            }
                            ui.label(RichText::new(s.name()).strong());
                            ui.label(RichText::new(if live { s.label() } else { "not connected".into() }).color(if live { theme::OK } else { theme::MUTE }).size(12.0));
                            if !live {
                                ui.menu_button("Connect\u{2026}", |ui| {
                                    for (label, kind) in [("SSH...", "ssh"), ("Telnet...", "telnet"), ("Serial...", "serial")] {
                                        if ui.button(label).clicked() {
                                            dlg.wanted.push(s.id);
                                            connect = Some((s.id, kind));
                                            ui.close();
                                        }
                                    }
                                });
                            }
                        });
                    }
                });
            });
            ui.horizontal(|ui| {
                if ui.button("New terminal").clicked() {
                    new_terminal = true;
                }
                if ui.button("Select all connected").clicked() {
                    dlg.checked = sessions.iter().filter(|s| s.is_connected()).map(|s| s.id).collect();
                }
                ui.label("Wait between commands");
                ui.add(egui::DragValue::new(&mut dlg.delay).range(0.2..=30.0).speed(0.1).suffix(" s"));
            });
            ui.add_space(6.0);
            ui.horizontal(|ui| {
                let ok = !dlg.checked.is_empty() && !parse_command_lines(&dlg.commands).is_empty();
                if ui.add_enabled(ok, theme::accent_button("Run")).clicked() {
                    result = Some(true);
                }
                if ui.button(if dlg.approval { "Don't run" } else { "Cancel" }).clicked() {
                    result = Some(false);
                }
            });
        });
        if new_terminal {
            let s = self.hub.new_session(None);
            if let Some(d) = self.run_dlg.as_mut() {
                d.wanted.push(s.id);
            }
        }
        if connect.is_some() {
            self.connect_request = connect;
        }
        match result {
            Some(true) => {
                let d = self.run_dlg.take().unwrap();
                let chosen: Vec<Arc<Session>> = sessions.into_iter().filter(|s| d.checked.contains(&s.id)).collect();
                self.run_commands(parse_command_lines(&d.commands), chosen, d.delay);
            }
            Some(false) => {
                self.run_dlg = None;
                self.note("Nothing was sent to any terminal.", Kind::Note);
            }
            None => {}
        }
    }

    // --- LLM settings -------------------------------------------------------------------------------
    pub fn open_settings(&mut self) {
        let c = &self.cfg;
        self.settings = Some(SettingsForm {
            provider: ai::get_provider(&c.get_str("provider")).map(|p| p.label.to_string()).unwrap_or_else(|| c.get_str("provider")),
            endpoint: c.get_str("endpoint_url"),
            model: c.get_str("model_name"),
            api_key: c.get_str("api_key"),
            temperature: c.get_f64("temperature", 0.7),
            max_tokens: c.get_i64("max_tokens", 2048),
            context_length: c.get_i64("context_length", 8192),
            timeout: c.get_i64("request_timeout", 120),
            tool_mode: c.get_str("tool_mode"),
            gguf_path: c.get_str("gguf_path"),
            embedding_endpoint: c.get_str("embedding_endpoint_url"),
            redact: c.get_bool("redact_secrets", true),
            models: Vec::new(),
            status: String::new(),
            rx: None,
        });
    }

    fn settings_dialog(&mut self, ctx: &egui::Context) {
        let Some(f) = self.settings.as_mut() else { return };
        if let Some(rx) = &f.rx {
            if let Ok(r) = rx.try_recv() {
                match r {
                    Ok(m) => {
                        f.status = format!("{} model(s) found", m.len());
                        f.models = m;
                    }
                    Err(e) => f.status = format!("Could not list models: {e}"),
                }
                f.rx = None;
            }
        }
        let mut save = false;
        let mut cancel = false;
        let mut provider_changed: Option<String> = None;
        let cfg_snapshot = self.cfg.clone();
        egui::Window::new("LLM Settings").collapsible(false).resizable(false).anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0]).show(ctx, |ui| {
            egui::Grid::new("llm_grid").num_columns(2).spacing([10.0, 8.0]).show(ui, |ui| {
                ui.label("Provider");
                egui::ComboBox::from_id_salt("llm_provider").width(300.0).selected_text(f.provider.clone()).show_ui(ui, |ui| {
                    for p in PROVIDERS {
                        if ui.selectable_label(f.provider == p.label, p.label).clicked() && f.provider != p.label {
                            provider_changed = Some(p.label.to_string());
                        }
                    }
                });
                ui.end_row();
                ui.label("Endpoint URL");
                ui.add(egui::TextEdit::singleline(&mut f.endpoint).desired_width(300.0));
                ui.end_row();
                ui.label("Model");
                ui.horizontal(|ui| {
                    ui.add(egui::TextEdit::singleline(&mut f.model).desired_width(210.0));
                    if !f.models.is_empty() {
                        egui::ComboBox::from_id_salt("llm_models").width(70.0).selected_text("pick").show_ui(ui, |ui| {
                            for m in f.models.clone() {
                                if ui.selectable_label(f.model == m, &m).clicked() {
                                    f.model = m;
                                }
                            }
                        });
                    }
                });
                ui.end_row();
                ui.label("API key");
                ui.add(egui::TextEdit::singleline(&mut f.api_key).password(true).desired_width(300.0).hint_text("or set the provider's environment variable"));
                ui.end_row();
                ui.label("Temperature");
                ui.add(egui::Slider::new(&mut f.temperature, 0.0..=2.0));
                ui.end_row();
                ui.label("Max tokens");
                ui.add(egui::DragValue::new(&mut f.max_tokens).range(16..=131072));
                ui.end_row();
                ui.label("Context length (Ollama)");
                ui.add(egui::DragValue::new(&mut f.context_length).range(512..=1048576));
                ui.end_row();
                ui.label("Request timeout (s)");
                ui.add(egui::DragValue::new(&mut f.timeout).range(5..=3600));
                ui.end_row();
                ui.label("Tool calling");
                egui::ComboBox::from_id_salt("llm_tools").selected_text(f.tool_mode.clone()).show_ui(ui, |ui| {
                    for m in ai::TOOL_MODES {
                        ui.selectable_value(&mut f.tool_mode, m.to_string(), *m);
                    }
                });
                ui.end_row();
                ui.label("Embedding server");
                ui.add(egui::TextEdit::singleline(&mut f.embedding_endpoint).desired_width(300.0));
                ui.end_row();
                ui.label("Local GGUF path");
                ui.add(egui::TextEdit::singleline(&mut f.gguf_path).desired_width(300.0));
                ui.end_row();
            });
            ui.checkbox(&mut f.redact, "Mask passwords/tokens in terminal output sent to cloud models");
            if f.provider == "Local GGUF File" {
                ui.label(RichText::new("This build does not run .gguf files in-process. Serve the file with Ollama or llama-server and choose that provider.").color(theme::WARN).size(12.0));
            }
            if !f.status.is_empty() {
                ui.label(RichText::new(&f.status).color(theme::DIM).size(12.0));
            }
            ui.add_space(6.0);
            ui.horizontal(|ui| {
                if ui.add(theme::accent_button("Save")).clicked() {
                    save = true;
                }
                if ui.button("Fetch models").clicked() {
                    let mut c = cfg_snapshot.clone();
                    c.set("provider", json!(f.provider));
                    c.set("endpoint_url", json!(f.endpoint));
                    c.set("api_key", json!(f.api_key));
                    let (tx, rx) = unbounded();
                    f.rx = Some(rx);
                    f.status = "Contacting the server...".into();
                    std::thread::spawn(move || {
                        let _ = tx.send(ai::backend::list_models(&c).map_err(|e| e.to_string()));
                        crate::session::request_repaint();
                    });
                }
                if ui.button("Cancel").clicked() {
                    cancel = true;
                }
            });
        });
        if let Some(label) = provider_changed {
            // remember what was typed for the old provider, restore the new one's profile
            let mut tmp = self.cfg.clone();
            if let Some(old) = ai::get_provider(&f.provider) {
                let mut profiles = tmp.0.get("provider_profiles").and_then(|v| v.as_object()).cloned().unwrap_or_default();
                profiles.insert(old.key.to_string(), json!({"endpoint_url": f.endpoint, "api_key": f.api_key, "model_name": f.model, "gguf_path": f.gguf_path}));
                tmp.set("provider_profiles", Value::Object(profiles));
            }
            let prof = tmp.profile_for(&label);
            f.provider = label;
            f.endpoint = prof.get("endpoint_url").and_then(|v| v.as_str()).unwrap_or("").to_string();
            f.model = prof.get("model_name").and_then(|v| v.as_str()).unwrap_or("").to_string();
            f.api_key = prof.get("api_key").and_then(|v| v.as_str()).unwrap_or("").to_string();
            f.gguf_path = prof.get("gguf_path").and_then(|v| v.as_str()).unwrap_or("").to_string();
            f.models.clear();
            self.cfg = tmp;
        }
        if save {
            let f = self.settings.take().unwrap();
            self.cfg.set("provider", json!(f.provider));
            self.cfg.set("endpoint_url", json!(f.endpoint));
            self.cfg.set("model_name", json!(f.model));
            self.cfg.set("api_key", json!(f.api_key));
            self.cfg.set("temperature", json!(f.temperature));
            self.cfg.set("max_tokens", json!(f.max_tokens));
            self.cfg.set("context_length", json!(f.context_length));
            self.cfg.set("request_timeout", json!(f.timeout));
            self.cfg.set("tool_mode", json!(f.tool_mode));
            self.cfg.set("gguf_path", json!(f.gguf_path));
            self.cfg.set("embedding_endpoint_url", json!(f.embedding_endpoint));
            self.cfg.set("redact_secrets", json!(f.redact));
            self.cfg.save();
            self.note("LLM settings saved.", Kind::Note);
        } else if cancel {
            self.settings = None;
        }
    }

    // --- RAG manager -----------------------------------------------------------------------------------
    pub fn open_rag(&mut self) {
        self.rag_dlg = Some(RagForm { selected: String::new(), name: String::new(), model: "ollama/nomic-embed-text".into(), job: String::new(), new_text: String::new(), files: Vec::new(), status: String::new() });
    }

    fn rag_dialog(&mut self, ctx: &egui::Context) {
        let Some(f) = self.rag_dlg.as_mut() else { return };
        let rag = self.shared.rag.clone();
        let names = rag.lock().list_names();
        let mut close = false;
        let mut load: Option<String> = None;
        egui::Window::new("Project RAG Memories").collapsible(false).default_size([640.0, 480.0]).anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0]).show(ctx, |ui| {
            ui.horizontal_top(|ui| {
                ui.vertical(|ui| {
                    ui.set_width(200.0);
                    ui.label(RichText::new("MEMORIES").size(11.5).color(theme::MUTE).strong());
                    egui::ScrollArea::vertical().max_height(300.0).show(ui, |ui| {
                        if ui.selectable_label(f.selected.is_empty(), "+ New memory").clicked() {
                            load = Some(String::new());
                        }
                        for n in &names {
                            let built_in = n == APPLICATION_MEMORY_NAME;
                            if ui.selectable_label(&f.selected == n, if built_in { format!("{n}  \u{1F512}") } else { n.clone() }).clicked() {
                                load = Some(n.clone());
                            }
                        }
                    });
                });
                ui.separator();
                ui.vertical(|ui| {
                    let built_in = f.selected == APPLICATION_MEMORY_NAME;
                    egui::Grid::new("rag_grid").num_columns(2).spacing([10.0, 8.0]).show(ui, |ui| {
                        ui.label("Name");
                        ui.add_enabled(!built_in, egui::TextEdit::singleline(&mut f.name).desired_width(300.0));
                        ui.end_row();
                        ui.label("Embedding model");
                        egui::ComboBox::from_id_salt("rag_model").width(300.0).selected_text(f.model.clone()).show_ui(ui, |ui| {
                            for m in AVAILABLE_EMBEDDING_MODELS {
                                ui.selectable_value(&mut f.model, m.to_string(), *m);
                            }
                        });
                        ui.end_row();
                        ui.label("Assigned job");
                        ui.add_enabled(!built_in, egui::TextEdit::singleline(&mut f.job).desired_width(300.0).hint_text("e.g. Network test engineer"));
                        ui.end_row();
                    });
                    ui.label("Add text");
                    ui.add_enabled(!built_in, egui::TextEdit::multiline(&mut f.new_text).desired_rows(5).desired_width(f32::INFINITY));
                    ui.horizontal(|ui| {
                        if ui.add_enabled(!built_in, egui::Button::new("Add files...")).clicked() {
                            if let Some(files) = rfd::FileDialog::new().pick_files() {
                                f.files.extend(files);
                            }
                        }
                        ui.label(RichText::new(format!("{} file(s) queued", f.files.len())).color(theme::DIM).size(12.0));
                    });
                    if let Some(m) = rag.lock().memories.get(&f.selected).and_then(|v| v.as_object()) {
                        let n = m.get("text_entries").and_then(|v| v.as_array()).map(|a| a.len()).unwrap_or(0);
                        ui.label(RichText::new(format!("{n} stored entr{}", if n == 1 { "y" } else { "ies" })).color(theme::DIM).size(12.0));
                    }
                    if built_in {
                        ui.label(RichText::new("The built-in application guide cannot be modified.").color(theme::MUTE).size(12.0));
                    }
                    if !f.status.is_empty() {
                        ui.label(RichText::new(&f.status).color(theme::DIM).size(12.0));
                    }
                    ui.add_space(6.0);
                    ui.horizontal(|ui| {
                        if ui.add_enabled(!built_in, theme::accent_button("Save / Update")).clicked() {
                            let files = std::mem::take(&mut f.files);
                            match rag.lock().create_or_update(&f.name, &f.model, &f.job, &f.new_text, &files) {
                                Ok(()) => {
                                    f.status = format!("Saved '{}'", f.name.trim());
                                    f.selected = f.name.trim().to_string();
                                    f.new_text.clear();
                                }
                                Err(e) => f.status = e,
                            }
                        }
                        if ui.add_enabled(!built_in && !f.selected.is_empty(), theme::danger_button("Delete")).clicked() {
                            rag.lock().delete(&f.selected);
                            f.selected.clear();
                            f.name.clear();
                            f.status = "Deleted".into();
                        }
                        if ui.button("Close").clicked() {
                            close = true;
                        }
                    });
                });
            });
        });
        if let Some(n) = load {
            if let Some(f) = self.rag_dlg.as_mut() {
                f.selected = n.clone();
                f.name = n.clone();
                f.new_text.clear();
                f.files.clear();
                if let Some(m) = rag.lock().memories.get(&n).and_then(|v| v.as_object()) {
                    f.model = m.get("embedding_model").and_then(|v| v.as_str()).unwrap_or("ollama/nomic-embed-text").to_string();
                    f.job = m.get("assigned_job").and_then(|v| v.as_str()).unwrap_or("").to_string();
                } else {
                    f.job.clear();
                }
            }
        }
        if close {
            self.rag_dlg = None;
        }
        let _ = Color32::WHITE;
    }
}
