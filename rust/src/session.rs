//! Terminal sessions and the hub that owns them.
//!
//! A [`Session`] is shared (`Arc`) between the UI thread, connection threads and script threads.
//! Everything a script needs (send, wait, log, screen text) is a method on it.

use crate::conn::{self, Conn, ConnSpec, Sink};
use crate::logclean::LogCleaner;
use crate::macrorec::MacroRecorder;
use once_cell::sync::OnceCell;
use parking_lot::{Condvar, Mutex};
use std::fs::File;
use std::io::Write;
use std::num::NonZeroU16;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

pub const DEFAULT_COLS: u16 = 80;
pub const DEFAULT_ROWS: u16 = 24;
pub const SCROLLBACK: usize = 5000;

static REPAINT: OnceCell<eframe::egui::Context> = OnceCell::new();

/// Terminal output events for plugin `on_output` hooks: (session name, label, connected, text).
pub type OutputEvent = (String, String, bool, String);
static OUTPUT_HOOK: parking_lot::Mutex<Option<crossbeam_channel::Sender<OutputEvent>>> = parking_lot::Mutex::new(None);

pub fn set_output_hook(tx: Option<crossbeam_channel::Sender<OutputEvent>>) {
    *OUTPUT_HOOK.lock() = tx;
}

pub fn set_repaint_context(ctx: eframe::egui::Context) {
    let _ = REPAINT.set(ctx);
}
pub fn request_repaint() {
    if let Some(c) = REPAINT.get() {
        c.request_repaint();
    }
}

/// Raised inside automation code when the user stops the running script.
#[derive(Debug, Clone, PartialEq)]
pub struct ScriptStopped;

impl std::fmt::Display for ScriptStopped {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "script stopped by user")
    }
}

fn nz(v: u16) -> NonZeroU16 {
    NonZeroU16::new(v.max(1)).unwrap()
}

pub struct TermState {
    pub parser: vt100::Parser,
    pub scroll: usize,
    pub cols: u16,
    pub rows: u16,
    pub highlight: bool,
}

impl TermState {
    fn new(cols: u16, rows: u16) -> Self {
        Self { parser: vt100::Parser::new(nz(rows), nz(cols), SCROLLBACK), scroll: 0, cols, rows, highlight: true }
    }
    pub fn alt_screen(&self) -> bool {
        self.parser.screen().alternate_screen()
    }
}

#[derive(Default)]
pub struct LogState {
    pub enabled: bool,
    pub paused: bool,
    pub path: Option<PathBuf>,
    file: Option<File>,
    cleaner: LogCleaner,
}

#[derive(Default)]
pub struct Typed {
    pub line: String,
    pub dirty: bool,
}

pub struct ScriptCtl {
    pub running: AtomicBool,
    pub name: Mutex<String>,
    stop: AtomicBool,
    paused: Mutex<bool>,
    pause_cv: Condvar,
}

impl ScriptCtl {
    fn new() -> Self {
        Self { running: AtomicBool::new(false), name: Mutex::new(String::new()), stop: AtomicBool::new(false), paused: Mutex::new(false), pause_cv: Condvar::new() }
    }
    pub fn is_paused(&self) -> bool {
        *self.paused.lock()
    }
}

pub struct Session {
    pub id: u64,
    pub name: Mutex<String>,
    pub label: Mutex<String>,
    pub connecting: AtomicBool,
    pub conn: Mutex<Option<Conn>>,
    pub spec: Mutex<Option<ConnSpec>>,
    pub term: Mutex<TermState>,
    pub buf: Mutex<String>,
    pub cv: Condvar,
    utf8_pending: Mutex<Vec<u8>>,
    pub log: Mutex<LogState>,
    pub script: ScriptCtl,
    pub recorder: Mutex<Option<MacroRecorder>>,
    pub typed: Mutex<Typed>,
    lost_notified: AtomicBool,
    pub closed: AtomicBool,
}

struct SessionSink(std::sync::Weak<Session>);

impl Sink for SessionSink {
    fn bytes(&self, data: &[u8]) {
        if let Some(s) = self.0.upgrade() {
            s.ingest(data);
        }
    }
    fn message(&self, text: &str) {
        if let Some(s) = self.0.upgrade() {
            s.local(text);
        }
    }
    fn closed(&self) {
        if let Some(s) = self.0.upgrade() {
            let mut c = s.conn.lock();
            if c.as_ref().map(|c| !c.alive.load(Ordering::SeqCst)).unwrap_or(false) {
                *c = None;
                *s.label.lock() = "Disconnected".into();
            }
            request_repaint();
        }
    }
}

impl Session {
    pub fn new(id: u64, name: &str, highlight: bool) -> Arc<Self> {
        let mut term = TermState::new(DEFAULT_COLS, DEFAULT_ROWS);
        term.highlight = highlight;
        Arc::new(Self {
            id,
            name: Mutex::new(name.to_string()),
            label: Mutex::new("Disconnected".into()),
            connecting: AtomicBool::new(false),
            conn: Mutex::new(None),
            spec: Mutex::new(None),
            term: Mutex::new(term),
            buf: Mutex::new(String::new()),
            cv: Condvar::new(),
            utf8_pending: Mutex::new(Vec::new()),
            log: Mutex::new(LogState::default()),
            script: ScriptCtl::new(),
            recorder: Mutex::new(None),
            typed: Mutex::new(Typed::default()),
            lost_notified: AtomicBool::new(false),
            closed: AtomicBool::new(false),
        })
    }

    pub fn name(&self) -> String {
        self.name.lock().clone()
    }
    pub fn label(&self) -> String {
        self.label.lock().clone()
    }
    pub fn is_connected(&self) -> bool {
        self.conn.lock().as_ref().map(|c| c.alive.load(Ordering::SeqCst)).unwrap_or(false)
    }
    pub fn conn_kind(&self) -> &'static str {
        self.conn.lock().as_ref().map(|c| c.spec.kind()).unwrap_or("")
    }

    // --- output path ---------------------------------------------------------------------
    /// Bytes received from the remote side.
    pub fn ingest(&self, data: &[u8]) {
        if data.is_empty() {
            return;
        }
        {
            self.term.lock().parser.process(data);
        }
        let text = {
            let mut pend = self.utf8_pending.lock();
            pend.extend_from_slice(data);
            let valid = match std::str::from_utf8(&pend) {
                Ok(s) => s.len(),
                Err(e) => {
                    if e.error_len().is_some() {
                        pend.len() // invalid bytes: decode lossily below
                    } else {
                        e.valid_up_to()
                    }
                }
            };
            let chunk: Vec<u8> = pend.drain(..valid).collect();
            String::from_utf8_lossy(&chunk).into_owned()
        };
        self.after_text(&text);
    }

    /// A message produced by this application (shown in the terminal and in the wait buffer).
    pub fn local(&self, text: &str) {
        self.term.lock().parser.process(text.as_bytes());
        self.after_text(text);
    }

    /// Text that goes only to the screen (script `print`): same as `local`.
    pub fn print(&self, text: &str) {
        let t = text.replace("\r\n", "\n").replace('\n', "\r\n");
        self.local(&t);
    }

    fn after_text(&self, text: &str) {
        if text.is_empty() {
            return;
        }
        if let Some(tx) = OUTPUT_HOOK.lock().as_ref() {
            let _ = tx.try_send((self.name(), self.label(), self.is_connected(), text.to_string()));
        }
        {
            let mut b = self.buf.lock();
            b.push_str(text);
            if b.len() > 250_000 {
                let cut = b.len() - 125_000;
                let mut at = cut;
                while !b.is_char_boundary(at) {
                    at += 1;
                }
                *b = b[at..].to_string();
            }
            self.cv.notify_all();
        }
        {
            let mut log = self.log.lock();
            if log.enabled && !log.paused {
                let clean = log.cleaner.feed(text);
                if !clean.is_empty() {
                    if let Some(f) = log.file.as_mut() {
                        let _ = f.write_all(clean.as_bytes());
                        let _ = f.flush();
                    }
                }
            }
        }
        request_repaint();
    }

    // --- input path ----------------------------------------------------------------------
    pub fn send(&self, data: &str) {
        self.send_bytes(data.as_bytes());
    }

    pub fn send_bytes(&self, data: &[u8]) {
        let res = {
            let c = self.conn.lock();
            match c.as_ref() {
                None => None,
                Some(conn) => Some(conn.send(data)),
            }
        };
        match res {
            None => {
                if !self.lost_notified.swap(true, Ordering::SeqCst) {
                    self.local("\r\n[No active connection]\r\n");
                }
            }
            Some(Err(e)) => {
                self.local(&format!("\r\n[Send error: {e}]\r\n"));
                *self.conn.lock() = None;
                *self.label.lock() = "Disconnected".into();
                self.lost_notified.store(true, Ordering::SeqCst);
            }
            Some(Ok(())) => {}
        }
    }

    // --- connections ---------------------------------------------------------------------
    /// Blocking: connect this session. Call from a worker thread.
    pub fn connect(self: &Arc<Self>, spec: ConnSpec) -> Result<(), String> {
        self.connecting.store(true, Ordering::SeqCst);
        self.disconnect();
        let (cols, rows) = {
            let t = self.term.lock();
            (t.cols, t.rows)
        };
        let sink: Arc<dyn Sink> = Arc::new(SessionSink(Arc::downgrade(self)));
        let result = conn::connect(spec.clone(), sink, cols, rows);
        let out = match result {
            Ok(c) => {
                *self.label.lock() = spec.label();
                *self.spec.lock() = Some(spec);
                *self.conn.lock() = Some(c);
                self.lost_notified.store(false, Ordering::SeqCst);
                Ok(())
            }
            Err(e) => {
                self.local(&format!("\r\n[Connection failed: {e}]\r\n"));
                Err(e)
            }
        };
        self.connecting.store(false, Ordering::SeqCst);
        request_repaint();
        out
    }

    pub fn disconnect(&self) {
        if let Some(c) = self.conn.lock().take() {
            c.close();
        }
        *self.label.lock() = "Disconnected".into();
    }

    pub fn resize(&self, cols: u16, rows: u16) {
        let (cols, rows) = (cols.clamp(20, 400), rows.clamp(5, 200));
        {
            let mut t = self.term.lock();
            if t.cols == cols && t.rows == rows {
                return;
            }
            t.cols = cols;
            t.rows = rows;
            t.parser.screen_mut().set_size(nz(rows), nz(cols));
        }
        if let Some(c) = self.conn.lock().as_ref() {
            c.resize(cols, rows);
        }
    }

    pub fn close(&self) {
        self.closed.store(true, Ordering::SeqCst);
        self.disable_logging();
        self.disconnect();
        self.request_stop_script();
    }

    // --- screen text ---------------------------------------------------------------------
    fn row_text(screen: &vt100::Screen, row: u16, cols: u16) -> String {
        let mut s = String::new();
        for c in 0..cols {
            match screen.cell(row, c) {
                Some(cell) if cell.is_wide_continuation() => {}
                Some(cell) if cell.has_contents() => s.push_str(cell.contents()),
                _ => s.push(' '),
            }
        }
        s
    }

    /// The visible screen, one string per row (right-trimmed).
    pub fn screen_lines(&self) -> Vec<String> {
        let mut t = self.term.lock();
        let (cols, rows) = (t.cols, t.rows);
        let prev = t.parser.screen().scrollback();
        t.parser.screen_mut().set_scrollback(0);
        let lines = (0..rows).map(|r| Self::row_text(t.parser.screen(), r, cols).trim_end().to_string()).collect();
        t.parser.screen_mut().set_scrollback(prev);
        lines
    }

    /// Sanitized recent terminal text including scrollback.
    pub fn recent_output(&self, max_chars: usize) -> String {
        if max_chars == 0 {
            return String::new();
        }
        let mut t = self.term.lock();
        let (cols, rows) = (t.cols, t.rows as usize);
        let prev = t.parser.screen().scrollback();
        let total = t.parser.screen().scrollback_len();
        let mut lines: Vec<String> = Vec::new();
        let mut off = total;
        while off >= rows {
            t.parser.screen_mut().set_scrollback(off);
            for r in 0..rows {
                lines.push(Self::row_text(t.parser.screen(), r as u16, cols).trim_end().to_string());
            }
            off -= rows;
        }
        if off > 0 {
            t.parser.screen_mut().set_scrollback(off);
            for r in 0..off {
                lines.push(Self::row_text(t.parser.screen(), r as u16, cols).trim_end().to_string());
            }
        }
        t.parser.screen_mut().set_scrollback(0);
        for r in 0..rows {
            lines.push(Self::row_text(t.parser.screen(), r as u16, cols).trim_end().to_string());
        }
        t.parser.screen_mut().set_scrollback(prev);
        let text = lines.join("\n");
        let text = text.trim_end();
        let chars: Vec<char> = text.chars().collect();
        if chars.len() <= max_chars {
            text.to_string()
        } else {
            chars[chars.len() - max_chars..].iter().collect()
        }
    }

    pub fn clear_screen(&self) {
        {
            let mut t = self.term.lock();
            let (c, r, h) = (t.cols, t.rows, t.highlight);
            *t = TermState::new(c, r);
            t.highlight = h;
        }
        self.buf.lock().clear();
        request_repaint();
    }

    #[allow(dead_code)]
    pub fn clear_wait_buffer(&self) {
        self.buf.lock().clear();
    }

    // --- waiting (scripts) -----------------------------------------------------------------
    /// Block until `pattern` appears in the received data or on the rendered screen.
    pub fn wait_for(&self, pattern: &str, timeout: f64, case_sensitive: bool) -> Result<bool, ScriptStopped> {
        let deadline = Instant::now() + Duration::from_secs_f64(timeout.max(0.0));
        let needle = if case_sensitive { pattern.to_string() } else { pattern.to_lowercase() };
        loop {
            self.script_checkpoint()?; // outside any lock: may block while paused
            {
                let b = self.buf.lock();
                let found = if case_sensitive { b.contains(&needle) } else { b.to_lowercase().contains(&needle) };
                if found {
                    return Ok(true);
                }
            }
            for line in self.screen_lines() {
                let hay = if case_sensitive { line } else { line.to_lowercase() };
                if hay.contains(&needle) {
                    return Ok(true);
                }
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Ok(false);
            }
            let mut b = self.buf.lock();
            self.cv.wait_for(&mut b, remaining.min(Duration::from_millis(200)));
        }
    }

    // --- script control ----------------------------------------------------------------------
    pub fn script_checkpoint(&self) -> Result<(), ScriptStopped> {
        if self.script.stop.load(Ordering::SeqCst) {
            return Err(ScriptStopped);
        }
        {
            let mut p = self.script.paused.lock();
            while *p {
                self.script.pause_cv.wait_for(&mut p, Duration::from_millis(200));
                if self.script.stop.load(Ordering::SeqCst) {
                    return Err(ScriptStopped);
                }
            }
        }
        if self.script.stop.load(Ordering::SeqCst) {
            return Err(ScriptStopped);
        }
        Ok(())
    }

    pub fn sleep_checkpoint(&self, seconds: f64) -> Result<(), ScriptStopped> {
        let deadline = Instant::now() + Duration::from_secs_f64(seconds.max(0.0));
        loop {
            self.script_checkpoint()?;
            let rem = deadline.saturating_duration_since(Instant::now());
            if rem.is_zero() {
                return Ok(());
            }
            std::thread::sleep(rem.min(Duration::from_millis(100)));
        }
    }

    pub fn start_script(&self, name: &str) {
        self.script.running.store(true, Ordering::SeqCst);
        *self.script.name.lock() = name.to_string();
        self.script.stop.store(false, Ordering::SeqCst);
        *self.script.paused.lock() = false;
        request_repaint();
    }

    pub fn finish_script(&self) {
        self.script.running.store(false, Ordering::SeqCst);
        self.script.name.lock().clear();
        self.script.stop.store(false, Ordering::SeqCst);
        *self.script.paused.lock() = false;
        self.script.pause_cv.notify_all();
        request_repaint();
    }

    pub fn request_stop_script(&self) {
        self.script.stop.store(true, Ordering::SeqCst);
        *self.script.paused.lock() = false;
        self.script.pause_cv.notify_all();
    }

    /// Returns true if now paused.
    pub fn toggle_pause_script(&self) -> bool {
        let mut p = self.script.paused.lock();
        *p = !*p;
        if !*p {
            self.script.pause_cv.notify_all();
        }
        *p
    }

    pub fn script_running(&self) -> bool {
        self.script.running.load(Ordering::SeqCst)
    }

    // --- logging -----------------------------------------------------------------------------
    pub fn enable_logging(&self, path: &Path) -> Result<(), String> {
        let mut log = self.log.lock();
        if log.enabled {
            return Ok(());
        }
        if let Some(p) = path.parent() {
            let _ = std::fs::create_dir_all(p);
        }
        let f = std::fs::OpenOptions::new().create(true).append(true).open(path).map_err(|e| e.to_string())?;
        log.file = Some(f);
        log.path = Some(path.to_path_buf());
        log.cleaner = LogCleaner::new();
        log.enabled = true;
        log.paused = false;
        Ok(())
    }

    pub fn pause_logging(&self) {
        let mut l = self.log.lock();
        if l.enabled {
            l.paused = true;
        }
    }
    pub fn resume_logging(&self) {
        let mut l = self.log.lock();
        if l.enabled {
            l.paused = false;
        }
    }

    pub fn disable_logging(&self) {
        self.disable_logging_path(None);
    }

    /// Stop logging; with `Some(path)` only when that exact file is the open log.
    pub fn disable_logging_path(&self, path: Option<&Path>) {
        let mut log = self.log.lock();
        if let (Some(want), Some(cur)) = (path, log.path.as_ref()) {
            let a = std::fs::canonicalize(want).unwrap_or_else(|_| want.to_path_buf());
            let b = std::fs::canonicalize(cur).unwrap_or_else(|_| cur.clone());
            if a != b {
                return;
            }
        }
        let tail = log.cleaner.flush();
        if let Some(f) = log.file.as_mut() {
            if !tail.is_empty() {
                let _ = f.write_all(tail.as_bytes());
            }
            let _ = f.flush();
        }
        log.file = None;
        log.enabled = false;
        log.paused = false;
        log.path = None;
    }
}

// ----------------------------------------------------------------------------------------
// Hub: owns all sessions and the cross-thread requests that need the UI
// ----------------------------------------------------------------------------------------

pub enum UiRequest {
    Alert { message: String, reply: crossbeam_channel::Sender<bool> },
    Input { prompt: String, reply: crossbeam_channel::Sender<String> },
    /// Playlist gate: reply is "EXTEND", "FORCE" or "ABORT".
    Gate { script: String, reason: String, tail: String, reply: crossbeam_channel::Sender<String> },
    Confirm { title: String, message: String, reply: crossbeam_channel::Sender<bool> },
    Info { title: String, message: String },
}

pub struct Hub {
    pub sessions: Mutex<Vec<Arc<Session>>>,
    pub active: AtomicU64,
    next_id: AtomicU64,
    pub highlight: AtomicBool,
    pub status: Mutex<String>,
    pub ui_tx: crossbeam_channel::Sender<UiRequest>,
}

impl Hub {
    pub fn new() -> (Arc<Hub>, crossbeam_channel::Receiver<UiRequest>) {
        let (tx, rx) = crossbeam_channel::unbounded();
        (
            Arc::new(Hub { sessions: Mutex::new(Vec::new()), active: AtomicU64::new(0), next_id: AtomicU64::new(1), highlight: AtomicBool::new(true), status: Mutex::new("Ready".into()), ui_tx: tx }),
            rx,
        )
    }

    pub fn new_session(&self, name: Option<&str>) -> Arc<Session> {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let mut sessions = self.sessions.lock();
        let title = match name {
            Some(n) if !n.trim().is_empty() => n.to_string(),
            _ => format!("Terminal {}", sessions.len() + 1),
        };
        let s = Session::new(id, &title, self.highlight.load(Ordering::SeqCst));
        s.local("\r\n[New terminal. Use SSH, Telnet or Serial, then type directly here.]\r\n");
        sessions.push(s.clone());
        self.active.store(id, Ordering::SeqCst);
        request_repaint();
        s
    }

    pub fn set_status(&self, text: &str) {
        *self.status.lock() = text.to_string();
        request_repaint();
    }

    pub fn list(&self) -> Vec<Arc<Session>> {
        self.sessions.lock().clone()
    }

    pub fn active_session(&self) -> Option<Arc<Session>> {
        let id = self.active.load(Ordering::SeqCst);
        let list = self.sessions.lock();
        list.iter().find(|s| s.id == id).cloned().or_else(|| list.first().cloned())
    }

    pub fn find(&self, name: &str) -> Option<Arc<Session>> {
        let list = self.sessions.lock();
        list.iter().find(|s| s.name() == name).cloned().or_else(|| list.iter().find(|s| s.name().to_lowercase() == name.to_lowercase()).cloned())
    }

    pub fn select(&self, s: &Arc<Session>) {
        self.active.store(s.id, Ordering::SeqCst);
        request_repaint();
    }

    pub fn close(&self, s: &Arc<Session>) {
        s.close();
        let mut list = self.sessions.lock();
        list.retain(|x| x.id != s.id);
        if self.active.load(Ordering::SeqCst) == s.id {
            let next = list.last().map(|x| x.id).unwrap_or(0);
            self.active.store(next, Ordering::SeqCst);
        }
        request_repaint();
    }

    pub fn rename(&self, current: &str, new_name: &str) -> bool {
        if new_name.trim().is_empty() {
            return false;
        }
        if let Some(s) = self.find(current) {
            *s.name.lock() = new_name.to_string();
            self.select(&s);
            return true;
        }
        false
    }

    pub fn set_highlight(&self, on: bool) {
        self.highlight.store(on, Ordering::SeqCst);
        for s in self.list() {
            s.term.lock().highlight = on;
        }
        request_repaint();
    }

    /// Ask the UI for a yes/no answer (blocks the calling script thread).
    pub fn alert(&self, message: &str) -> bool {
        let (tx, rx) = crossbeam_channel::bounded(1);
        if self.ui_tx.send(UiRequest::Alert { message: message.to_string(), reply: tx }).is_err() {
            return false;
        }
        request_repaint();
        rx.recv().unwrap_or(false)
    }

    pub fn confirm(&self, title: &str, message: &str) -> bool {
        let (tx, rx) = crossbeam_channel::bounded(1);
        if self.ui_tx.send(UiRequest::Confirm { title: title.into(), message: message.into(), reply: tx }).is_err() {
            return false;
        }
        request_repaint();
        rx.recv().unwrap_or(false)
    }

    pub fn info(&self, title: &str, message: &str) {
        let _ = self.ui_tx.send(UiRequest::Info { title: title.into(), message: message.into() });
        request_repaint();
    }

    pub fn gate(&self, script: &str, reason: &str, tail: &str) -> String {
        let (tx, rx) = crossbeam_channel::bounded(1);
        if self.ui_tx.send(UiRequest::Gate { script: script.into(), reason: reason.into(), tail: tail.into(), reply: tx }).is_err() {
            return "ABORT".into();
        }
        request_repaint();
        rx.recv().unwrap_or_else(|_| "ABORT".into())
    }

    pub fn input(&self, prompt: &str) -> String {
        let (tx, rx) = crossbeam_channel::bounded(1);
        if self.ui_tx.send(UiRequest::Input { prompt: prompt.to_string(), reply: tx }).is_err() {
            return String::new();
        }
        request_repaint();
        rx.recv().unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ingest_and_wait() {
        let s = Session::new(1, "t", true);
        s.ingest(b"hello \xe2\x82");
        s.ingest(b"\xac world\r\nroot@a:~# ");
        assert!(s.buf.lock().contains("hello \u{20ac} world"));
        assert!(s.wait_for("root@a:~#", 0.2, true).unwrap());
        assert!(!s.wait_for("nothing", 0.1, true).unwrap());
        assert!(s.screen_lines()[0].contains("hello"));
        assert!(s.recent_output(1000).contains("hello"));
    }

    #[test]
    fn stop_script_interrupts_wait() {
        let s = Session::new(1, "t", true);
        s.start_script("x");
        s.request_stop_script();
        assert_eq!(s.wait_for("zzz", 5.0, true), Err(ScriptStopped));
        s.finish_script();
        assert!(s.script_checkpoint().is_ok());
    }

    #[test]
    fn logging_is_clean() {
        let dir = std::env::temp_dir().join(format!("mmte_log_{}", std::process::id()));
        let path = dir.join("a.log");
        let s = Session::new(1, "t", true);
        s.enable_logging(&path).unwrap();
        s.ingest(b"\x1b[1;32muart:~$ \x1b[m\x1b[8D\x1b[Jhello\r\n");
        s.disable_logging();
        let text = std::fs::read_to_string(&path).unwrap();
        assert_eq!(text, "hello\n");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn hub_lifecycle() {
        let (hub, _rx) = Hub::new();
        let a = hub.new_session(None);
        assert_eq!(a.name(), "Terminal 1");
        let b = hub.new_session(Some("second"));
        assert_eq!(hub.active_session().unwrap().id, b.id);
        assert!(hub.rename("SECOND", "x"));
        hub.close(&b);
        assert_eq!(hub.list().len(), 1);
    }
}
