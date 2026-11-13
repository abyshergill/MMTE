//! The main window: menus, sidebar, tabs, terminal area, status bar, keyboard handling.

use super::dialogs::{Action, Dialogs, Dlg, PendingAlert, PendingConfirm, PendingGate, PendingInput};
use super::term_view::{self, TermUi};
use super::theme;
use crate::config::{self, ProfileStore, Settings};
use crate::conn::ConnSpec;
use crate::macrorec::{self, MacroRecorder};
use crate::plugins::{self, PluginInfo};
use crate::script::{ScriptError, ScriptHost};
use crate::session::{Hub, Session, UiRequest};
use eframe::egui::{self, Align, Color32, Event, Key, Layout, RichText, Sense};
use serde_json::json;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

pub struct App {
    pub hub: Arc<Hub>,
    pub ui_rx: crossbeam_channel::Receiver<UiRequest>,
    pub settings: Settings,
    pub ssh_profiles: ProfileStore,
    pub telnet_profiles: ProfileStore,
    pub plugin_dirs: Vec<PathBuf>,
    pub plugins: Vec<PluginInfo>,
    pub dialogs: Dialogs,
    pub tuis: HashMap<u64, TermUi>,
    pub sidebar: bool,
    pub inline_rename: Option<(u64, String, bool)>,
    pub extras: super::windows::Extras,
    hook_daemon: Option<plugins::HookDaemon>,
    pub shot: Option<(PathBuf, u32)>,
    icon: Option<egui::TextureHandle>,
}

impl App {
    pub fn new(cc: &eframe::CreationContext<'_>) -> Self {
        Self::with_parts(&cc.egui_ctx, Settings::load(), ProfileStore::ssh(), ProfileStore::telnet(), plugins::plugin_dirs())
    }

    /// Build the app from explicit parts (tests use temporary settings and no plugin folders).
    pub fn with_parts(ctx: &egui::Context, settings: Settings, ssh_profiles: ProfileStore, telnet_profiles: ProfileStore, plugin_dirs: Vec<PathBuf>) -> Self {
        theme::apply(ctx);
        if std::env::var("MMTE_SCREENSHOT").is_ok() {
            ctx.set_embed_viewports(true);
        }
        crate::session::set_repaint_context(ctx.clone());
        let (hub, ui_rx) = Hub::new();
        hub.highlight.store(settings.get_bool("syntax_highlighting", true), std::sync::atomic::Ordering::SeqCst);
        hub.intercept_editors.store(settings.get_bool("intercept_editors", true), std::sync::atomic::Ordering::SeqCst);
        let first = hub.new_session(None);
        if std::env::var("MMTE_DEMO").is_ok() {
            first.local("root@lab:~# ls -l /etc | head -3\r\n\x1b[1;34mdrwxr-xr-x\x1b[0m 3 root root 4096 Nov 12 10:01 \x1b[32mapt\x1b[0m\r\nping 10.0.0.1: 64 bytes, time=0.8 ms, status ok\r\nError: connection refused to https://example.com/x\r\nroot@lab:~# ");
        }
        let icon = super::icon::texture(ctx, 128);
        let mut app = Self {
            hub: hub.clone(),
            ui_rx,
            settings,
            ssh_profiles,
            telnet_profiles,
            plugin_dirs,
            plugins: Vec::new(),
            dialogs: { let mut d = Dialogs::new(); d.icon = icon.clone(); d },
            tuis: HashMap::new(),
            sidebar: true,
            inline_rename: None,
            extras: super::windows::Extras::new(hub),
            hook_daemon: None,
            icon,
            shot: std::env::var("MMTE_SCREENSHOT").ok().map(|p| (PathBuf::from(p), 0)),
        };
        app.reload_plugins();
        app.dev_hooks();
        app
    }

    /// Developer aids driven by environment variables (used for screenshots and smoke tests).
    fn dev_hooks(&mut self) {
        if let Ok(spec) = std::env::var("MMTE_AUTOTELNET") {
            if let Some((h, p)) = spec.rsplit_once(':') {
                if let Ok(port) = p.parse::<u16>() {
                    self.connect_current(ConnSpec::Telnet { host: h.to_string(), port });
                }
            }
        }
        if let Ok(w) = std::env::var("MMTE_DEMO_WINDOW") {
            match w.as_str() {
                "ssh" => self.dialogs.open_ssh(&self.ssh_profiles, None),
                "telnet" => self.dialogs.open_telnet(&self.telnet_profiles, None),
                "serial" => self.dialogs.open_serial(),
                "about" => self.dialogs.current = Dlg::About,
                "samples" => self.dialogs.current = Dlg::Samples,
                "plugins" => self.dialogs.current = Dlg::PluginManager,
                "suite" => self.extras.daw.open = true,
                "profiles" => self.extras.profiles_open = true,
                "ai" => self.extras.ai.demo(),
                "llm" => self.extras.ai.open_settings(),
                "rag" => self.extras.ai.open_rag(),
                "editor" => {
                    self.extras.editor.open_text("demo.py", "# demo\ndef hello(name):\n    \"\"\"Say hi\"\"\"\n    print('hello', name, 42)\n    return True\n".into(), "python", None);
                }
                _ => {}
            }
        }
    }

    pub fn reload_plugins(&mut self) {
        let hub = self.hub.clone();
        let dirs = self.plugin_dirs.clone();
        // loading runs Python once; do it on a thread so start-up is not delayed
        let (tx, rx) = crossbeam_channel::bounded(1);
        std::thread::spawn(move || {
            let _ = tx.send(plugins::load(&hub, &dirs));
        });
        self.extras.plugin_rx = Some(rx);
    }

    pub fn current(&self) -> Option<Arc<Session>> {
        self.hub.active_session()
    }

    pub fn status(&self, text: &str) {
        self.hub.set_status(text);
    }

    // --- connections ------------------------------------------------------------------------
    pub fn connect_current(&mut self, spec: ConnSpec) {
        let s = self.current().unwrap_or_else(|| self.hub.new_session(None));
        let label = spec.label();
        self.status(&format!("Connecting: {label}"));
        let hub = self.hub.clone();
        std::thread::spawn(move || {
            match s.connect(spec) {
                Ok(()) => {
                    s.local(&format!("\r\n[Active: {label}]\r\n"));
                    hub.set_status(&format!("Connected: {label}"));
                }
                Err(e) => hub.set_status(&format!("Connection failed: {e}")),
            }
        });
    }

    fn disconnect_current(&mut self) {
        if let Some(s) = self.current() {
            s.disconnect();
            s.local("\r\n[Disconnected]\r\n");
        }
    }

    // --- scripts ------------------------------------------------------------------------------
    pub fn run_script_file(&mut self, session: Arc<Session>, path: PathBuf) {
        if session.script_running() {
            self.dialogs.message("Script already running", &format!("'{}' is already running on this terminal. Stop it first, or switch to another terminal.", session.script.name.lock()));
            return;
        }
        let hub = self.hub.clone();
        let dirs = self.plugin_dirs.clone();
        let name = path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
        let is_ttl = path.extension().map(|e| e.eq_ignore_ascii_case("ttl")).unwrap_or(false);
        let kind = if is_ttl { "TTL" } else { "Python" };
        session.start_script(&name);
        std::thread::spawn(move || {
            hub.set_status(&format!("Running {kind}: {}", path.display()));
            let host = ScriptHost::new(hub.clone(), session.clone(), dirs);
            match host.run_file(&path) {
                Ok(()) => hub.set_status(&format!("{kind} completed: {name}")),
                Err(ScriptError::Stopped) => {
                    session.local(&format!("\r\n[{kind} script stopped by user]\r\n"));
                    hub.set_status(&format!("{kind} stopped: {name}"));
                }
                Err(ScriptError::Failed(m)) => {
                    session.local(&format!("\r\n[{kind} error: {m}]\r\n"));
                    hub.set_status(&format!("{kind} error: {m}"));
                }
            }
            session.finish_script();
        });
    }

    // --- logging ------------------------------------------------------------------------------
    fn start_logging(&mut self) {
        let Some(s) = self.current() else { return };
        if s.log.lock().enabled {
            self.dialogs.message("Already logging", &format!("'{}' is already logging to a file. Stop it first to start a new one.", s.name()));
            return;
        }
        let dir = config::log_dir();
        let _ = std::fs::create_dir_all(&dir);
        let default = format!("{}_{}.log", sanitize(&s.name()), chrono::Local::now().format("%Y%m%d_%H%M%S"));
        if let Some(p) = rfd::FileDialog::new().set_directory(&dir).set_file_name(default).add_filter("Logs", &["log"]).add_filter("All Files", &["*"]).save_file() {
            match s.enable_logging(&p) {
                Ok(()) => self.status(&format!("Logging to {}", p.display())),
                Err(e) => self.dialogs.message("Logging failed", &e),
            }
        }
    }

    // --- macro recording --------------------------------------------------------------------------
    pub fn start_macro(&mut self, s: &Arc<Session>) {
        let mut rec = s.recorder.lock();
        if rec.is_some() {
            return;
        }
        let hint = s.spec.lock().as_ref().filter(|_| s.is_connected()).map(|sp| sp.connect_hint()).unwrap_or_default();
        *rec = Some(MacroRecorder::new(&s.name(), &s.label(), &hint));
        drop(rec);
        *s.typed.lock() = Default::default();
        self.hub.select(s);
        self.status(&format!("Recording macro on {}", s.name()));
    }

    /// "python" or "ttl": the saved choice, otherwise Python when it is installed and Tera Term if not.
    pub fn macro_format(&self) -> &'static str {
        match self.settings.get_str("macro_format").as_deref() {
            Some("python") => "python",
            Some("ttl") => "ttl",
            _ => if crate::script::find_python().is_some() { "python" } else { "ttl" },
        }
    }

    pub fn stop_macro(&mut self, s: &Arc<Session>, ask_to_save: bool) {
        let Some(rec) = s.recorder.lock().take() else { return };
        let ttl = self.macro_format() == "ttl";
        let (script, ext, language) = if ttl {
            let hint = if rec.connect_hint.is_empty() { String::new() } else { s.spec.lock().as_ref().map(|sp| sp.ttl_hint()).unwrap_or_default() };
            (rec.to_ttl(&hint), "ttl", "teraterm")
        } else {
            (rec.to_python(), "py", "python")
        };
        let dir = config::macro_dir();
        let _ = std::fs::create_dir_all(&dir);
        let file = format!("macro_{}_{}.{ext}", sanitize(&s.name()), rec.started_wall.format("%Y%m%d_%H%M%S"));
        let default_path = dir.join(&file);
        let mut saved: Option<PathBuf> = None;
        if ask_to_save {
            if let Some(p) = rfd::FileDialog::new().set_directory(&dir).set_file_name(&file).add_filter(if ttl { "Tera Term Macro" } else { "Python Script" }, &[ext]).add_filter("All Files", &["*"]).save_file() {
                match std::fs::write(&p, &script) {
                    Ok(()) => {
                        self.status(&format!("Macro saved to {}", p.display()));
                        saved = Some(p);
                    }
                    Err(e) => self.dialogs.message("Save failed", &format!("Could not save the macro:\n\n{e}")),
                }
            }
        } else if std::fs::write(&default_path, &script).is_ok() {
            self.status(&format!("Macro saved to {}", default_path.display()));
            saved = Some(default_path.clone());
        }
        match saved {
            Some(p) => self.extras.editor.open_local(&p),
            None => self.extras.editor.open_text(&file, script, language, Some(default_path)),
        }
        self.extras.editor.visible = true;
    }

    // --- keyboard ---------------------------------------------------------------------------------
    fn cursor_line(s: &Session) -> String {
        let t = s.term.lock();
        let (r, _) = t.parser.screen().cursor_position();
        let mut line = String::new();
        for c in 0..t.cols {
            match t.parser.screen().cell(r, c) {
                Some(cell) if cell.is_wide_continuation() => {}
                Some(cell) if cell.has_contents() => line.push_str(cell.contents()),
                _ => line.push(' '),
            }
        }
        line
    }

    fn track_control_key(s: &Session, ch: &str) {
        if ch == "\x15" {
            *s.typed.lock() = Default::default();
            return;
        }
        if ["\x03", "\x04", "\x1a", "\x1c", "\x0c"].contains(&ch) {
            if !s.term.lock().alt_screen() {
                if let Some(r) = s.recorder.lock().as_mut() {
                    r.record_keys(ch);
                }
            }
            if ch != "\x0c" {
                *s.typed.lock() = Default::default();
            }
            return;
        }
        s.typed.lock().dirty = true;
    }

    /// Runs before Enter is sent. Records the command; may redirect vi/nano to the built-in editor.
    fn on_enter(&mut self, s: &Arc<Session>) -> bool {
        if s.term.lock().alt_screen() {
            *s.typed.lock() = Default::default();
            return false;
        }
        let screen_line = Self::cursor_line(s);
        let secret = macrorec::is_secret_prompt(&screen_line);
        let (typed_line, dirty) = {
            let t = s.typed.lock();
            (t.line.clone(), t.dirty)
        };
        let command = if secret {
            String::new()
        } else if !dirty {
            typed_line.trim().to_string()
        } else {
            macrorec::extract_command(&screen_line)
        };
        *s.typed.lock() = Default::default();

        if !secret && !command.is_empty() && self.settings.get_bool("intercept_editors", true) && s.conn_kind() == "SSH" {
            if let Some((sudo, files)) = macrorec::parse_editor_command(&command) {
                if let Some(r) = s.recorder.lock().as_mut() {
                    r.record_note(&format!("opened in the built-in editor: {command}"));
                }
                self.extras.open_files_from_terminal(s, &command, sudo, files, true);
                return true;
            }
        }
        if let Some(r) = s.recorder.lock().as_mut() {
            let tail: String = {
                let t = screen_line.trim();
                let chars: Vec<char> = t.chars().collect();
                chars[chars.len().saturating_sub(60)..].iter().collect()
            };
            r.record_line(&command, secret, &tail, &macrorec::extract_prompt(&screen_line));
        }
        false
    }

    fn track_pasted(&mut self, s: &Arc<Session>, text: &str) {
        let norm = text.replace("\r\n", "\n").replace('\r', "\n");
        let pieces: Vec<&str> = norm.split('\n').collect();
        for complete in &pieces[..pieces.len().saturating_sub(1)] {
            s.typed.lock().line.push_str(complete);
            if !s.term.lock().alt_screen() {
                let (line, dirty) = {
                    let t = s.typed.lock();
                    (t.line.clone(), t.dirty)
                };
                let command = if !dirty { line } else { macrorec::extract_command(&Self::cursor_line(s)) };
                if let Some(r) = s.recorder.lock().as_mut() {
                    r.record_line(&command, false, "", &macrorec::extract_prompt(&Self::cursor_line(s)));
                }
            }
            *s.typed.lock() = Default::default();
        }
        if let Some(last) = pieces.last() {
            s.typed.lock().line.push_str(last);
        }
    }

    fn paste(&mut self, s: &Arc<Session>, text: &str) {
        if text.is_empty() {
            return;
        }
        if s.is_connected() {
            self.track_pasted(s, text);
            let bracketed = s.term.lock().parser.screen().bracketed_paste();
            let body = text.replace("\r\n", "\n").replace('\n', "\r");
            if bracketed {
                s.send(&format!("\x1b[200~{body}\x1b[201~"));
            } else {
                s.send(&body);
            }
        } else {
            s.local(&text.replace("\r\n", "\n").replace('\n', "\r\n"));
        }
    }

    fn copy_selection(&mut self, ctx: &egui::Context, s: &Arc<Session>) -> bool {
        let Some(tui) = self.tuis.get(&s.id) else { return false };
        let Some(sel) = tui.sel else { return false };
        let text = term_view::selected_text(s, sel);
        if text.is_empty() {
            return false;
        }
        ctx.copy_text(text);
        true
    }

    fn process_keyboard(&mut self, ctx: &egui::Context, s: &Arc<Session>) {
        let events = ctx.input(|i| i.events.clone());
        let mac = cfg!(target_os = "macos");
        for ev in events {
            match ev {
                Event::Text(t) => {
                    let mut scroll_reset = false;
                    for ch in t.chars() {
                        if (ch as u32) < 32 || ch == '\u{7f}' {
                            continue;
                        }
                        scroll_reset = true;
                        if s.is_connected() {
                            s.typed.lock().line.push(ch);
                            s.send(&ch.to_string());
                        } else {
                            s.local(&ch.to_string());
                        }
                    }
                    if scroll_reset {
                        s.term.lock().scroll = 0;
                    }
                }
                Event::Paste(t) => {
                    s.term.lock().scroll = 0;
                    self.paste(s, &t)
                }
                Event::Copy => {
                    if !self.copy_selection(ctx, s) && !mac {
                        Self::track_control_key(s, "\x03");
                        s.send("\x03");
                    }
                }
                Event::Cut => {
                    if !self.copy_selection(ctx, s) && !mac {
                        Self::track_control_key(s, "\x18");
                        s.send("\x18");
                    }
                }
                Event::Key { key, pressed: true, modifiers: m, .. } => self.on_key(ctx, s, key, m),
                _ => {}
            }
        }
    }

    fn on_key(&mut self, ctx: &egui::Context, s: &Arc<Session>, key: Key, m: egui::Modifiers) {
        let connected = s.is_connected();
        let mac = cfg!(target_os = "macos");
        let letter = {
            let n = format!("{key:?}");
            if n.len() == 1 { n.chars().next().map(|c| c.to_ascii_lowercase()) } else { None }
        };
        // keys that scroll the history
        if m.shift && key == Key::PageUp {
            let rows = s.term.lock().rows as usize;
            let mut t = s.term.lock();
            let max = t.parser.screen().scrollback_len();
            t.scroll = (t.scroll + rows.saturating_sub(2)).min(max);
            return;
        }
        if m.shift && key == Key::PageDown {
            let rows = s.term.lock().rows as usize;
            let mut t = s.term.lock();
            t.scroll = t.scroll.saturating_sub(rows.saturating_sub(2));
            return;
        }
        if m.mac_cmd || (m.command && mac) {
            return; // Cmd+C/V arrive as Copy/Paste events; other Cmd shortcuts belong to the app
        }
        if !matches!(key, Key::F1 | Key::F2) {
            s.term.lock().scroll = 0;
        }
        if m.ctrl && m.shift {
            match letter {
                Some('c') => {
                    self.copy_selection(ctx, s);
                    return;
                }
                Some('x') => {
                    self.copy_selection(ctx, s);
                    return;
                }
                Some('v') => {
                    ctx.send_viewport_cmd(egui::ViewportCommand::RequestPaste);
                    return;
                }
                Some('t') => {
                    self.hub.new_session(None);
                    return;
                }
                _ => {}
            }
        }
        // Ctrl + ]  : Telnet escape
        if m.ctrl && !m.shift && key == Key::CloseBracket {
            if s.conn_kind() == "Telnet" {
                s.disconnect();
                s.local("\r\n[Telnet session disconnected via Ctrl + ] escape sequence]\r\n");
                return;
            }
            if connected {
                s.send("\x1d");
            }
            return;
        }
        if !connected {
            match key {
                Key::Enter => s.local("\r\n"),
                Key::Backspace => s.local("\x08 \x08"),
                _ => {}
            }
            return;
        }
        if m.ctrl && !m.shift {
            if let Some(l) = letter {
                if l.is_ascii_lowercase() {
                    let ch = ((l as u8 - b'a') + 1) as char;
                    if l == 'c' && self.tuis.get(&s.id).map(|t| t.sel.is_some()).unwrap_or(false) && self.copy_selection(ctx, s) {
                        return;
                    }
                    let st = ch.to_string();
                    Self::track_control_key(s, &st);
                    s.send(&st);
                    return;
                }
            }
            let special = match key {
                Key::OpenBracket => Some("\x1b"),
                Key::Backslash => Some("\x1c"),
                Key::Space => Some("\x00"),
                Key::Slash => Some("\x1f"),
                _ => None,
            };
            if let Some(sp) = special {
                Self::track_control_key(s, sp);
                s.send(sp);
                return;
            }
        }
        let app_cursor = s.term.lock().parser.screen().application_cursor();
        let arrow = |c: char| if app_cursor { format!("\x1bO{c}") } else { format!("\x1b[{c}") };
        let seq: Option<String> = match key {
            Key::ArrowUp => Some(arrow('A')),
            Key::ArrowDown => Some(arrow('B')),
            Key::ArrowRight => Some(arrow('C')),
            Key::ArrowLeft => Some(arrow('D')),
            Key::Home => Some("\x1b[H".into()),
            Key::End => Some("\x1b[F".into()),
            Key::PageUp => Some("\x1b[5~".into()),
            Key::PageDown => Some("\x1b[6~".into()),
            Key::Delete => Some("\x1b[3~".into()),
            Key::Insert => Some("\x1b[2~".into()),
            Key::Tab => Some(if m.shift { "\x1b[Z".into() } else { "\t".into() }),
            Key::Escape => Some("\x1b".into()),
            Key::F1 => Some("\x1bOP".into()),
            Key::F2 => Some("\x1bOQ".into()),
            Key::F3 => Some("\x1bOR".into()),
            Key::F4 => Some("\x1bOS".into()),
            Key::F5 => Some("\x1b[15~".into()),
            Key::F6 => Some("\x1b[17~".into()),
            Key::F7 => Some("\x1b[18~".into()),
            Key::F8 => Some("\x1b[19~".into()),
            Key::F9 => Some("\x1b[20~".into()),
            Key::F10 => Some("\x1b[21~".into()),
            Key::F11 => Some("\x1b[23~".into()),
            Key::F12 => Some("\x1b[24~".into()),
            _ => None,
        };
        if let Some(seq) = seq {
            s.typed.lock().dirty = true;
            s.send(&seq);
            return;
        }
        match key {
            Key::Backspace => {
                s.typed.lock().line.pop();
                s.send("\x7f");
            }
            Key::Enter => {
                if self.on_enter(s) {
                    return;
                }
                s.send("\r");
            }
            _ => {
                if m.alt {
                    if let Some(l) = letter {
                        s.send(&format!("\x1b{}", if m.shift { l.to_ascii_uppercase() } else { l }));
                    }
                }
            }
        }
    }

    // --- actions from dialogs ------------------------------------------------------------------------
    fn apply_actions(&mut self, actions: Vec<Action>) {
        for a in actions {
            match a {
                Action::Connect(spec) => self.connect_current(spec),
                Action::SaveSshProfile { name, host, user, port } => self.ssh_profiles.upsert(&name, json!({"host": host, "username": user, "port": port})),
                Action::SaveTelnetProfile { name, host, port } => self.telnet_profiles.upsert(&name, json!({"host": host, "port": port})),
                Action::Rename(name) => {
                    if let (Some(s), false) = (self.current(), name.is_empty()) {
                        *s.name.lock() = name;
                    }
                }
                Action::Error(t, m) => self.dialogs.message(&t, &m),
                Action::OpenSample(code, ext) => {
                    if !code.is_empty() {
                        let name = format!("template{ext}");
                        let lang = if ext == ".py" { "python" } else { "text" };
                        self.extras.editor.open_text(&name, code.trim().to_string() + "\n", lang, None);
                        self.extras.editor.visible = true;
                    }
                }
            }
        }
    }

    fn poll_requests(&mut self) {
        while let Ok(req) = self.ui_rx.try_recv() {
            match req {
                UiRequest::Alert { message, reply } => self.dialogs.alerts.push_back(PendingAlert { message, reply }),
                UiRequest::Input { prompt, reply } => self.dialogs.inputs.push_back(PendingInput { prompt, text: String::new(), reply, first: true }),
                UiRequest::Gate { script, reason, tail, reply } => self.dialogs.gates.push_back(PendingGate { script, reason, tail, reply }),
                UiRequest::Confirm { title, message, reply } => self.dialogs.confirms.push_back(PendingConfirm { title, message, reply }),
                UiRequest::Info { title, message } => self.dialogs.message(&title, &message),
                UiRequest::OpenRemote { session_id, command, sudo, files, wipe } => {
                    if let Some(s) = self.hub.list().into_iter().find(|s| s.id == session_id) {
                        self.extras.open_files_from_terminal(&s, &command, sudo, files, wipe);
                    }
                }
                UiRequest::OpenLocal { path } => {
                    self.extras.editor.open_local(std::path::Path::new(&path));
                    self.extras.editor.visible = true;
                }
            }
        }
        if let Some(rx) = &self.extras.plugin_rx {
            if let Ok(list) = rx.try_recv() {
                let any_hooks = list.iter().any(|p| p.loaded && p.hooks > 0);
                self.plugins = list;
                self.extras.plugin_rx = None;
                if let Some(d) = self.hook_daemon.take() {
                    d.stop();
                }
                if any_hooks {
                    self.hook_daemon = plugins::HookDaemon::start(&self.hub, &self.plugin_dirs);
                }
            }
        }
    }

    // --- UI pieces -----------------------------------------------------------------------------------------
    fn menu_bar(&mut self, ui: &mut egui::Ui) {
        let cur = self.current();
        let recording = cur.as_ref().map(|s| s.recorder.lock().is_some()).unwrap_or(false);
        let (log_on, log_paused) = cur.as_ref().map(|s| { let l = s.log.lock(); (l.enabled, l.paused) }).unwrap_or((false, false));
        let (running, paused) = cur.as_ref().map(|s| (s.script_running(), s.script.is_paused())).unwrap_or((false, false));
        egui::MenuBar::new().ui(ui, |ui| {
            ui.menu_button("Connection", |ui| {
                if ui.button("SSH...").clicked() {
                    self.dialogs.open_ssh(&self.ssh_profiles, None);
                    ui.close();
                }
                if ui.button("Telnet...").clicked() {
                    self.dialogs.open_telnet(&self.telnet_profiles, None);
                    ui.close();
                }
                if ui.button("Serial...").clicked() {
                    self.dialogs.open_serial();
                    ui.close();
                }
                ui.separator();
                if ui.button("Disconnect").clicked() {
                    self.disconnect_current();
                    ui.close();
                }
            });
            ui.menu_button("Scripting", |ui| {
                if ui.button("Run Script...").clicked() {
                    ui.close();
                    if let (Some(s), Some(p)) = (cur.clone(), rfd::FileDialog::new().add_filter("Scripts", &["ttl", "py"]).add_filter("All Files", &["*"]).pick_file()) {
                        self.run_script_file(s, p);
                    }
                }
                if ui.add_enabled(running, egui::Button::new(if paused { "\u{25B6} Resume Script" } else { "\u{23F8} Pause Script" })).clicked() {
                    if let Some(s) = &cur {
                        s.toggle_pause_script();
                    }
                    ui.close();
                }
                if ui.add_enabled(running, egui::Button::new("\u{23F9} Stop Script")).clicked() {
                    if let Some(s) = &cur {
                        s.request_stop_script();
                    }
                    ui.close();
                }
                ui.separator();
                if ui.button("Multi-Script Suite...").clicked() {
                    self.extras.daw.open = true;
                    ui.close();
                }
            });
            ui.menu_button("Utility", |ui| {
                if ui.button("Files (SFTP Browser)...").clicked() {
                    if let Err(e) = self.extras.open_sftp(cur.clone()) {
                        self.dialogs.message("Remote files", &e);
                    }
                    ui.close();
                }
                if ui.button("Text Editor...").clicked() {
                    self.extras.editor.visible = true;
                    if self.extras.editor.docs.is_empty() {
                        self.extras.editor.new_document();
                    }
                    ui.close();
                }
                if ui.button("Saved Profiles...").clicked() {
                    self.extras.profiles_open = true;
                    ui.close();
                }
                ui.separator();
                if ui.add_enabled(cur.is_some(), egui::Button::new("\u{25CF} Record Actions | Macro...")).clicked() {
                    if let Some(s) = cur.clone() {
                        if recording {
                            self.stop_macro(&s, true);
                        } else {
                            self.start_macro(&s);
                        }
                    }
                    ui.close();
                }
                if ui.add_enabled(recording, egui::Button::new("\u{23F9} Stop Macro Recording")).clicked() {
                    if let Some(s) = cur.clone() {
                        self.stop_macro(&s, true);
                    }
                    ui.close();
                }
                ui.menu_button("Macro format", |ui| {
                    let current = self.macro_format();
                    for (label, key) in [("Python script (.py)", "python"), ("Tera Term macro (.ttl)", "ttl")] {
                        if ui.selectable_label(current == key, label).clicked() {
                            self.settings.set("macro_format", json!(key));
                            ui.close();
                        }
                    }
                    ui.separator();
                    ui.label(egui::RichText::new(if crate::script::find_python().is_some() { "Python 3 found: both formats replay." } else { "Python 3 not found: use Tera Term." }).size(12.0).color(theme::MUTE));
                });
                ui.separator();
                if ui.button("Clear Terminal").clicked() {
                    if let Some(s) = &cur {
                        s.clear_screen();
                    }
                    ui.close();
                }
                let mut hl = self.settings.get_bool("syntax_highlighting", true);
                if ui.checkbox(&mut hl, "Terminal Syntax Highlighting").changed() {
                    self.settings.set("syntax_highlighting", json!(hl));
                    self.hub.set_highlight(hl);
                }
                let mut ic = self.settings.get_bool("intercept_editors", true);
                if ui.checkbox(&mut ic, "Open vi / vim / nano in Built-in Editor").changed() {
                    self.settings.set("intercept_editors", json!(ic));
                    self.hub.intercept_editors.store(ic, std::sync::atomic::Ordering::SeqCst);
                }
                ui.separator();
                if ui.add_enabled(!log_on, egui::Button::new("Start Logging...")).clicked() {
                    self.start_logging();
                    ui.close();
                }
                if ui.add_enabled(log_on, egui::Button::new(if log_paused { "\u{25B6} Resume Logging" } else { "\u{23F8} Pause Logging" })).clicked() {
                    if let Some(s) = &cur {
                        if log_paused { s.resume_logging() } else { s.pause_logging() }
                    }
                    ui.close();
                }
                if ui.add_enabled(log_on, egui::Button::new("\u{23F9} Stop Logging")).clicked() {
                    if let Some(s) = &cur {
                        s.disable_logging();
                    }
                    ui.close();
                }
                ui.separator();
                if ui.add(egui::Button::new("Show/Hide Sidebar").shortcut_text("Ctrl+B")).clicked() {
                    self.sidebar = !self.sidebar;
                    ui.close();
                }
            });
            self.extras.ai_menu(ui);
            ui.menu_button("Plugins", |ui| {
                let mut any = false;
                for p in self.plugins.clone() {
                    if p.loaded && !p.actions.is_empty() {
                        any = true;
                        ui.menu_button(&p.name, |ui| {
                            for (i, (text, _)) in p.actions.iter().enumerate() {
                                if ui.button(text).clicked() {
                                    ui.close();
                                    if let Some(s) = cur.clone() {
                                        let hub = self.hub.clone();
                                        let dirs = self.plugin_dirs.clone();
                                        let p2 = p.clone();
                                        std::thread::spawn(move || {
                                            if let Err(e) = plugins::run_action(&hub, s, &dirs, &p2, i) {
                                                hub.set_status(&format!("Plugin error: {e}"));
                                            }
                                        });
                                    }
                                }
                            }
                        });
                    }
                }
                if any {
                    ui.separator();
                }
                if ui.button("Plugin Manager...").clicked() {
                    self.dialogs.current = Dlg::PluginManager;
                    ui.close();
                }
                if ui.button("New Plugin from Template...").clicked() {
                    self.dialogs.current = Dlg::NewPlugin("my_plugin".into());
                    ui.close();
                }
                if ui.button("Open Plugins Folder").clicked() {
                    let d = config::plugin_user_dir();
                    let _ = std::fs::create_dir_all(&d);
                    open_in_file_manager(&d);
                    ui.close();
                }
                if ui.button("Reload Plugins").clicked() {
                    self.reload_plugins();
                    self.status("Reloading plugins...");
                    ui.close();
                }
            });
            ui.menu_button("Help", |ui| {
                if ui.button("Sample Scripts & Templates...").clicked() {
                    self.dialogs.current = Dlg::Samples;
                    ui.close();
                }
                if ui.button("Developer Info / About...").clicked() {
                    self.dialogs.current = Dlg::About;
                    ui.close();
                }
            });
        });
    }

    fn sidebar_ui(&mut self, ui: &mut egui::Ui) {
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            if let Some(tex) = &self.icon {
                ui.add(egui::Image::new((tex.id(), egui::vec2(24.0, 24.0))));
            }
            ui.label(RichText::new("TERMINALS").strong().size(14.0).color(theme::TEXT));
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if ui.add(egui::Button::new("\u{00AB}").frame(false)).on_hover_text("Hide sidebar (Ctrl+B)").clicked() {
                    self.sidebar = false;
                }
            });
        });
        ui.add_space(6.0);
        let w = ui.available_width();
        if ui.add_sized([w, 30.0], theme::accent_button("+ New Terminal")).clicked() {
            self.hub.new_session(None);
        }
        if ui.add_sized([w, 28.0], egui::Button::new("Rename Selected")).clicked() {
            if let Some(s) = self.current() {
                self.dialogs.current = Dlg::Rename(s.name(), true);
            }
        }
        if ui.add_sized([w, 28.0], theme::danger_button("Close Selected")).clicked() {
            if let Some(s) = self.current() {
                self.close_session(&s);
            }
        }
        ui.add_space(10.0);
        ui.label(RichText::new("OPEN SESSIONS").size(11.5).color(theme::MUTE).strong());
        let active = self.hub.active.load(std::sync::atomic::Ordering::SeqCst);
        let mut select: Option<Arc<Session>> = None;
        let mut close: Option<Arc<Session>> = None;
        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
            for s in self.hub.list() {
                let selected = s.id == active;
                let (rect, resp) = ui.allocate_exact_size(egui::vec2(ui.available_width(), 46.0), Sense::click());
                let p = ui.painter();
                if selected {
                    p.rect(rect, 8.0, theme::SELECT, egui::Stroke::new(1.0, theme::ACCENT), egui::StrokeKind::Inside);
                } else if resp.hovered() {
                    p.rect_filled(rect, 8.0, theme::BG3);
                }
                let connected = s.is_connected();
                let dot = if connected { theme::OK } else { theme::MUTE };
                p.circle_filled(rect.left_center() + egui::vec2(14.0, 0.0), 4.0, dot);
                let title = format!("{}{}", if s.recorder.lock().is_some() { "\u{25CF} " } else { "" }, s.name());
                p.text(rect.left_top() + egui::vec2(28.0, 8.0), egui::Align2::LEFT_TOP, title, egui::FontId::proportional(14.0), theme::TEXT);
                p.text(rect.left_top() + egui::vec2(28.0, 27.0), egui::Align2::LEFT_TOP, s.label(), egui::FontId::proportional(11.5), theme::MUTE);
                let x_rect = egui::Rect::from_center_size(rect.right_center() - egui::vec2(16.0, 0.0), egui::vec2(20.0, 20.0));
                let x_hover = ui.rect_contains_pointer(x_rect);
                close_x(ui, x_rect, x_hover);
                if x_hover && resp.clicked() {
                    close = Some(s.clone());
                } else if resp.clicked() {
                    select = Some(s.clone());
                }
            }
        });
        if let Some(s) = select {
            self.hub.select(&s);
        }
        if let Some(s) = close {
            self.close_session(&s);
        }
    }

    fn close_session(&mut self, s: &Arc<Session>) {
        if s.recorder.lock().is_some() {
            self.stop_macro(s, false);
        }
        self.tuis.remove(&s.id);
        self.hub.close(s);
        if self.hub.list().is_empty() {
            self.hub.new_session(None);
        }
    }

    fn tab_bar(&mut self, ui: &mut egui::Ui) {
        let active = self.hub.active.load(std::sync::atomic::Ordering::SeqCst);
        let mut select = None;
        let mut close = None;
        let mut start_rename = None;
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = 2.0;
            for s in self.hub.list() {
                let selected = s.id == active;
                if let Some((id, text, first)) = self.inline_rename.as_mut() {
                    if *id == s.id {
                        let r = ui.add(egui::TextEdit::singleline(text).desired_width(120.0));
                        if *first {
                            r.request_focus();
                            *first = false;
                        }
                        if r.lost_focus() {
                            let apply = ui.input(|i| i.key_pressed(egui::Key::Enter));
                            if apply && !text.trim().is_empty() {
                                *s.name.lock() = text.trim().to_string();
                            }
                            self.inline_rename = None;
                        }
                        continue;
                    }
                }
                let title = format!("{}{}", if s.recorder.lock().is_some() { "\u{25CF} " } else { "" }, s.name());
                let galley = ui.painter().layout_no_wrap(title.clone(), egui::FontId::proportional(13.5), theme::TEXT);
                let size = egui::vec2(galley.size().x + 46.0, 28.0);
                let (rect, resp) = ui.allocate_exact_size(size, Sense::click());
                let p = ui.painter();
                let fill = if selected { theme::TERM_BG } else if resp.hovered() { theme::BG3 } else { theme::BG2 };
                p.rect(rect, egui::CornerRadius { nw: 8, ne: 8, sw: 0, se: 0 }, fill, egui::Stroke::new(1.0, if selected { theme::ACCENT } else { theme::LINE }), egui::StrokeKind::Inside);
                p.galley(rect.left_center() + egui::vec2(12.0, -galley.size().y / 2.0), galley, if selected { theme::TEXT } else { theme::DIM });
                let x_rect = egui::Rect::from_center_size(rect.right_center() - egui::vec2(15.0, 0.0), egui::vec2(18.0, 18.0));
                let hover_x = ui.rect_contains_pointer(x_rect);
                close_x(ui, x_rect, hover_x);
                if hover_x && resp.clicked() {
                    close = Some(s.clone());
                } else if resp.double_clicked() {
                    start_rename = Some(s.clone());
                } else if resp.clicked() {
                    select = Some(s.clone());
                }
                resp.on_hover_text("Double-click to rename");
            }
            if ui.add_sized([28.0, 28.0], egui::Button::new("+").frame(true)).on_hover_text("New terminal (Ctrl+Shift+T)").clicked() {
                self.hub.new_session(None);
            }
        });
        if let Some(s) = select {
            self.hub.select(&s);
        }
        if let Some(s) = start_rename {
            self.inline_rename = Some((s.id, s.name(), true));
        }
        if let Some(s) = close {
            self.close_session(&s);
        }
    }

    fn status_text(s: &Session) -> String {
        let log = {
            let l = s.log.lock();
            if l.enabled { if l.paused { "PAUSED" } else { "ON" } } else { "OFF" }
        };
        let mut base = format!("{}  \u{00B7}  {}  \u{00B7}  Logging: {}", s.name(), s.label(), log);
        if s.recorder.lock().is_some() {
            base.push_str("  \u{00B7}  Macro: REC");
        }
        base
    }
}

fn close_x(ui: &egui::Ui, rect: egui::Rect, hover: bool) {
    let c = if hover { theme::ERR } else { theme::MUTE };
    let r = rect.shrink(6.0);
    let st = egui::Stroke::new(1.6, c);
    if hover {
        ui.painter().rect_filled(rect, 5.0, theme::ERR_BG);
    }
    ui.painter().line_segment([r.left_top(), r.right_bottom()], st);
    ui.painter().line_segment([r.right_top(), r.left_bottom()], st);
}

pub fn sanitize(name: &str) -> String {
    let s: String = name.chars().map(|c| if c.is_ascii_alphanumeric() || c == '_' || c == '.' || c == '-' { c } else { '_' }).collect();
    let t = s.trim_matches('_').to_string();
    if t.is_empty() { "terminal".into() } else { t }
}

pub fn open_in_file_manager(path: &Path) {
    #[cfg(target_os = "macos")]
    let _ = std::process::Command::new("open").arg(path).spawn();
    #[cfg(target_os = "windows")]
    let _ = std::process::Command::new("explorer").arg(path).spawn();
    #[cfg(all(unix, not(target_os = "macos")))]
    let _ = std::process::Command::new("xdg-open").arg(path).spawn();
}

impl eframe::App for App {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let ctx = ui.ctx().clone();
        self.poll_requests();
        self.screenshot_hook(&ctx);
        ctx.request_repaint_after(std::time::Duration::from_millis(400));

        if ctx.input(|i| i.viewport().close_requested()) {
            for s in self.hub.list() {
                if s.recorder.lock().is_some() {
                    self.stop_macro(&s, false);
                }
                s.close();
            }
        }
        // global shortcuts
        let toggle_sidebar = egui::KeyboardShortcut::new(egui::Modifiers::COMMAND, Key::B);
        if ctx.input_mut(|i| i.consume_shortcut(&toggle_sidebar)) {
            self.sidebar = !self.sidebar;
        }

        egui::Panel::top("menu").show(ui, |ui| {
            self.menu_bar(ui);
        });
        egui::Panel::bottom("status").show(ui, |ui| {
            ui.horizontal(|ui| {
                ui.label(RichText::new(self.hub.status.lock().clone()).color(theme::DIM).size(12.5));
            });
        });
        if self.sidebar {
            egui::Panel::left("sidebar").exact_size(245.0).resizable(false).frame(egui::Frame::default().fill(theme::BG1).inner_margin(egui::Margin::symmetric(14, 10))).show(ui, |ui| {
                self.sidebar_ui(ui);
            });
        }
        // AI panel / other side docks
        self.extras.right_panel(ui, &self.hub);

        let cur = self.current();
        egui::CentralPanel::default().frame(egui::Frame::default().fill(theme::TERM_BG).inner_margin(egui::Margin::symmetric(12, 8))).show(ui, |ui| {
            ui.horizontal(|ui| {
                if !self.sidebar && ui.add_sized([28.0, 28.0], egui::Button::new("\u{00BB}")).on_hover_text("Show the sidebar (Ctrl+B)").clicked() {
                    self.sidebar = true;
                }
                let header = cur.as_ref().map(|s| Self::status_text(s)).unwrap_or_else(|| "No terminal".into());
                ui.label(RichText::new(header).strong().size(14.0));
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if let Some(s) = &cur {
                        if s.script_running() {
                            let paused = s.script.is_paused();
                            ui.label(RichText::new(format!("{} {}", if paused { "\u{23F8} Paused:" } else { "\u{25B6} Running:" }, s.script.name.lock())).color(theme::WARN).strong());
                        }
                        if let Some(r) = s.recorder.lock().as_ref() {
                            let secs = r.elapsed() as u64;
                            let on = (ui.input(|i| i.time) * 1.6) as u64 % 2 == 0;
                            let badge = format!("{}  REC  {}:{:02}  \u{00B7}  {} cmd", if on { "\u{25CF}" } else { "\u{25CB}" }, secs / 60, secs % 60, r.command_count());
                            ui.label(RichText::new(badge).color(Color32::from_rgb(0xff, 0x8a, 0x93)).background_color(Color32::from_rgb(0x3a, 0x12, 0x18)).strong());
                        }
                    }
                });
            });
            self.tab_bar(ui);
            ui.add_space(2.0);
            if let Some(s) = cur.clone() {
                let id = egui::Id::unique(("term", s.id));
                let tui = self.tuis.entry(s.id).or_default();
                let resp = term_view::show(ui, &s, tui, id);
                let r = &resp.response;
                // focus handling: keep the terminal focused unless a text field is being used
                if r.clicked() || r.dragged() {
                    r.request_focus();
                }
                let other_focus = ctx.memory(|m| m.focused()).map(|f| f != id).unwrap_or(false);
                if !other_focus && !r.has_focus() && !self.dialog_open() {
                    r.request_focus();
                }
                if r.has_focus() && !self.dialog_open() {
                    ctx.memory_mut(|m| {
                        m.set_focus_lock_filter(id, egui::EventFilter { tab: true, horizontal_arrows: true, vertical_arrows: true, escape: true });
                    });
                    self.process_keyboard(&ctx, &s);
                }
                r.context_menu(|ui| {
                    let has_sel = self.tuis.get(&s.id).map(|t| t.sel.is_some()).unwrap_or(false);
                    if ui.add_enabled(has_sel, egui::Button::new("Copy")).clicked() {
                        self.copy_selection(&ctx, &s);
                        ui.close();
                    }
                    if ui.button("Paste").clicked() {
                        ctx.send_viewport_cmd(egui::ViewportCommand::RequestPaste);
                        ui.close();
                    }
                    if ui.button("Select All").clicked() {
                        let (cols, rows) = { let t = s.term.lock(); (t.cols, t.rows) };
                        if let Some(t) = self.tuis.get_mut(&s.id) {
                            t.sel = Some(((0, 0), (rows - 1, cols - 1)));
                        }
                        ui.close();
                    }
                    ui.separator();
                    if ui.button("Clear Terminal").clicked() {
                        s.clear_screen();
                        ui.close();
                    }
                });
            }
        });

        // dialogs and extra windows
        let actions = self.dialogs.show(&ctx, &self.ssh_profiles, &self.telnet_profiles, &self.plugins, &self.plugin_dirs);
        self.apply_actions(actions);
        self.extras_windows(&ctx);
    }
}

impl App {
    /// Developer aid: `MMTE_SCREENSHOT=out.png` saves the window after a moment and quits.
    fn screenshot_hook(&mut self, ctx: &egui::Context) {
        let Some((path, frame)) = self.shot.as_mut() else { return };
        *frame += 1;
        ctx.request_repaint();
        if *frame == 40 {
            let path = path.clone();
            ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(egui::ScreenshotCallback::new(move |img| {
                let (w, h) = (img.size[0] as u32, img.size[1] as u32);
                let raw: Vec<u8> = img.pixels.iter().flat_map(|c| c.to_array()).collect();
                if let Some(buf) = image::RgbaImage::from_raw(w, h, raw) {
                    let _ = buf.save(&path);
                }
                std::process::exit(0);
            })));
        }
    }

    fn dialog_open(&self) -> bool {
        !matches!(self.dialogs.current, Dlg::None) || !self.dialogs.alerts.is_empty() || !self.dialogs.inputs.is_empty() || !self.dialogs.gates.is_empty() || !self.dialogs.confirms.is_empty() || self.extras.any_modal_open()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};

    fn test_app() -> (App, egui::Context) {
        let ctx = egui::Context::default();
        let dir = std::env::temp_dir().join(format!("mmte_app_{}_{}", std::process::id(), std::thread::current().name().unwrap_or("t").replace("::", "_")));
        let _ = std::fs::create_dir_all(&dir);
        let app = App::with_parts(&ctx, Settings::load_from(dir.join("s.json")), ProfileStore::load(dir.join("ssh.json")), ProfileStore::load(dir.join("tel.json")), vec![]);
        (app, ctx)
    }

    fn key(key: Key, modifiers: egui::Modifiers) -> Event {
        Event::Key { key, physical_key: Some(key), pressed: true, repeat: false, modifiers }
    }

    fn feed(app: &mut App, ctx: &egui::Context, events: Vec<Event>) {
        let mut raw = egui::RawInput::default();
        raw.events = events;
        let s = app.current().unwrap();
        ctx.run_pass(raw, |ctx| app.process_keyboard(ctx, &s)).drop_without_applying_deltas();
    }

    /// Telnet "device": prints a prompt, echoes characters, and records everything it receives.
    fn device() -> (u16, std::sync::Arc<parking_lot::Mutex<Vec<u8>>>) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let got = std::sync::Arc::new(parking_lot::Mutex::new(Vec::new()));
        let got2 = got.clone();
        std::thread::spawn(move || {
            let Ok((mut s, _)) = listener.accept() else { return };
            let _ = s.write_all(b"root@dev:~# ");
            let mut buf = [0u8; 256];
            let mut skip = 0;
            while let Ok(n) = s.read(&mut buf) {
                if n == 0 {
                    break;
                }
                got2.lock().extend_from_slice(&buf[..n]);
                for &b in &buf[..n] {
                    if skip > 0 {
                        skip -= 1; // the two bytes after ESC (cursor keys) are not echoed
                    } else if b == 27 {
                        skip = 2;
                    } else if b == b'\r' {
                        let _ = s.write_all(b"\r\nroot@dev:~# ");
                    } else if b >= 32 {
                        let _ = s.write_all(&[b]);
                    }
                }
            }
        });
        (port, got)
    }

    fn connect(app: &mut App, port: u16) -> Arc<Session> {
        let s = app.current().unwrap();
        s.connect(ConnSpec::Telnet { host: "127.0.0.1".into(), port }).unwrap();
        assert!(s.wait_for("root@dev:~#", 5.0, true).unwrap());
        s
    }

    #[test]
    fn typed_command_is_sent_and_recorded_with_its_prompt() {
        let (mut app, ctx) = test_app();
        let (port, got) = device();
        let s = connect(&mut app, port);
        app.start_macro(&s);
        feed(&mut app, &ctx, vec![Event::Text("ls -l".into()), key(Key::Enter, Default::default())]);
        assert!(s.wait_for("ls -l\r\nroot@dev:~#", 5.0, true).unwrap(), "{}", s.buf.lock());
        assert_eq!(got.lock().as_slice(), b"ls -l\r");
        let script = s.recorder.lock().as_ref().unwrap().to_python();
        assert!(script.contains("# [root@dev:~#]"), "{script}");
        assert!(script.contains("send('ls -l')"), "{script}");
        assert_eq!(s.recorder.lock().as_ref().unwrap().command_count(), 1);
    }

    #[test]
    fn backspace_and_history_keys_still_record_the_visible_command() {
        let (mut app, ctx) = test_app();
        let (port, _got) = device();
        let s = connect(&mut app, port);
        app.start_macro(&s);
        // typo fixed with backspace: the model of the typed line follows
        feed(&mut app, &ctx, vec![Event::Text("pwdd".into()), key(Key::Backspace, Default::default()), key(Key::Enter, Default::default())]);
        // arrow key edits are not modelled, so the command is read back from the screen
        s.wait_for("root@dev:~#", 5.0, true).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(200));
        feed(&mut app, &ctx, vec![Event::Text("whoami".into()), key(Key::ArrowLeft, Default::default()), key(Key::ArrowRight, Default::default())]);
        std::thread::sleep(std::time::Duration::from_millis(300));
        feed(&mut app, &ctx, vec![key(Key::Enter, Default::default())]);
        let script = s.recorder.lock().as_ref().unwrap().to_python();
        assert!(script.contains("send('pwd')"), "{script}");
        assert!(script.contains("send('whoami')"), "{script}");
    }

    #[test]
    fn control_keys_are_sent_and_recorded() {
        let (mut app, ctx) = test_app();
        let (port, got) = device();
        let s = connect(&mut app, port);
        app.start_macro(&s);
        feed(&mut app, &ctx, vec![key(Key::C, egui::Modifiers::CTRL)]);
        std::thread::sleep(std::time::Duration::from_millis(200));
        assert!(got.lock().contains(&3u8));
        let script = s.recorder.lock().as_ref().unwrap().to_python();
        assert!(script.contains("send('\\x03', newline=False)  # Ctrl+C"), "{script}");
    }

    #[test]
    fn ttl_macro_replays_without_python() {
        let (mut app, ctx) = test_app();
        app.settings.set("macro_format", json!("ttl"));
        assert_eq!(app.macro_format(), "ttl");
        let (port, _got) = device();
        let s = connect(&mut app, port);
        app.start_macro(&s);
        feed(&mut app, &ctx, vec![Event::Text("echo it's ok".into()), key(Key::Enter, Default::default())]);
        s.wait_for("root@dev:~#", 5.0, true).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(200));
        let ttl = s.recorder.lock().as_ref().unwrap().to_ttl("");
        assert!(ttl.contains("sendln 'echo it'#39's ok'"), "{ttl}");
        s.disconnect();
        // replay on a second device and check the exact bytes it receives
        let (port2, got2) = device();
        let s2 = app.hub.new_session(Some("replay"));
        s2.connect(ConnSpec::Telnet { host: "127.0.0.1".into(), port: port2 }).unwrap();
        let dir = std::env::temp_dir().join(format!("mmte_ttl_replay_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("m.ttl");
        std::fs::write(&file, ttl).unwrap();
        let host = crate::script::ScriptHost::new(app.hub.clone(), s2.clone(), vec![]);
        host.run_file(&file).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(300));
        assert_eq!(got2.lock().as_slice(), b"echo it's ok\n");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn ttl_macro_asks_for_the_password_when_replayed() {
        let (mut app, ctx) = test_app();
        app.settings.set("macro_format", json!("ttl"));
        let (port, _got) = device();
        let s = connect(&mut app, port);
        s.local("\r\nPassword: ");
        app.start_macro(&s);
        feed(&mut app, &ctx, vec![Event::Text("hunter2".into()), key(Key::Enter, Default::default())]);
        let ttl = s.recorder.lock().as_ref().unwrap().to_ttl("");
        assert!(!ttl.contains("hunter2"), "{ttl}");
        s.disconnect();
        let (port2, got2) = device();
        let s2 = app.hub.new_session(Some("replay"));
        s2.connect(ConnSpec::Telnet { host: "127.0.0.1".into(), port: port2 }).unwrap();
        // answer the password dialog the way the UI would
        let rx = app.ui_rx.clone();
        std::thread::spawn(move || {
            if let Ok(crate::session::UiRequest::Input { reply, .. }) = rx.recv_timeout(std::time::Duration::from_secs(5)) {
                let _ = reply.send("s3cret".to_string());
            }
        });
        let dir = std::env::temp_dir().join(format!("mmte_ttl_pw_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("m.ttl");
        std::fs::write(&file, ttl).unwrap();
        crate::script::ScriptHost::new(app.hub.clone(), s2.clone(), vec![]).run_file(&file).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(300));
        assert_eq!(got2.lock().as_slice(), b"s3cret\n");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn passwords_are_never_recorded() {
        let (mut app, ctx) = test_app();
        let (port, _got) = device();
        let s = connect(&mut app, port);
        s.local("\r\nPassword: ");
        app.start_macro(&s);
        feed(&mut app, &ctx, vec![Event::Text("hunter2".into()), key(Key::Enter, Default::default())]);
        let script = s.recorder.lock().as_ref().unwrap().to_python();
        assert!(!script.contains("hunter2"), "{script}");
        assert!(script.contains("send(input("), "{script}");
    }

    #[test]
    fn pasted_multiline_text_is_recorded_line_by_line() {
        let (mut app, ctx) = test_app();
        let (port, got) = device();
        let s = connect(&mut app, port);
        app.start_macro(&s);
        feed(&mut app, &ctx, vec![Event::Paste("echo a\necho b\n".into())]);
        std::thread::sleep(std::time::Duration::from_millis(300));
        assert_eq!(got.lock().as_slice(), b"echo a\recho b\r");
        assert_eq!(s.recorder.lock().as_ref().unwrap().command_count(), 2);
    }

    #[test]
    fn disconnected_terminal_echoes_locally() {
        let (mut app, ctx) = test_app();
        let s = app.current().unwrap();
        feed(&mut app, &ctx, vec![Event::Text("abc".into()), key(Key::Backspace, Default::default()), key(Key::Enter, Default::default())]);
        let lines = s.screen_lines();
        assert!(lines.iter().any(|l| l.trim() == "ab"), "{lines:?}");
    }

    #[test]
    fn ctrl_bracket_disconnects_telnet() {
        let (mut app, ctx) = test_app();
        let (port, _got) = device();
        let s = connect(&mut app, port);
        feed(&mut app, &ctx, vec![key(Key::CloseBracket, egui::Modifiers::CTRL)]);
        assert!(!s.is_connected());
        assert!(s.buf.lock().contains("Ctrl + ] escape"));
    }

    #[test]
    fn vim_on_ssh_opens_the_builtin_editor() {
        let Some(srv) = crate::e2e_tests::start_server(crate::e2e_tests::free_port(), "vim") else { return };
        std::fs::write(srv.root.join("conf.txt"), "hello\n").unwrap();
        let (mut app, ctx) = test_app();
        let s = app.current().unwrap();
        s.connect(ConnSpec::Ssh { host: "127.0.0.1".into(), username: "tester".into(), password: "secret".into(), port: srv.port, look_for_keys: false }).unwrap();
        assert!(s.wait_for("root@ssh:~#", 10.0, true).unwrap());
        app.start_macro(&s);
        feed(&mut app, &ctx, vec![Event::Text("vim /conf.txt".into()), key(Key::Enter, Default::default())]);
        for _ in 0..100 {
            app.extras.editor.poll();
            if !app.extras.editor.docs.is_empty() {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        assert_eq!(app.extras.editor.docs.len(), 1);
        assert_eq!(app.extras.editor.docs[0].text, "hello\n");
        let script = s.recorder.lock().as_ref().unwrap().to_python();
        assert!(script.contains("# opened in the built-in editor: vim /conf.txt"), "{script}");
        // with interception off the command would have run in the shell instead
        app.settings.set("intercept_editors", json!(false));
        s.clear_wait_buffer();
        feed(&mut app, &ctx, vec![Event::Text("vim /x".into()), key(Key::Enter, Default::default())]);
        assert!(s.wait_for("sh: vim /x: not found", 10.0, true).unwrap());
    }
}
