//! Multi-Script Suite ("DAW"): run a playlist of scripts in order. After each script the
//! supervisor waits for a shell prompt; if none appears the operator decides (extend / force / abort).

use crate::script::{ScriptError, ScriptHost};
use crate::session::Hub;
use fancy_regex::Regex;
use parking_lot::Mutex;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

#[derive(Clone)]
pub struct SuiteItem {
    pub path: PathBuf,
    pub session_id: u64,
    pub timeout: f64,
    pub status: String,
}

impl SuiteItem {
    pub fn name(&self) -> String {
        self.path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default()
    }
}

#[derive(Clone)]
pub struct Suite {
    pub items: Arc<Mutex<Vec<SuiteItem>>>,
    pub running: Arc<AtomicBool>,
    pub stop: Arc<AtomicBool>,
    pub beep: Arc<AtomicBool>,
}

impl Suite {
    pub fn new() -> Self {
        Self { items: Arc::new(Mutex::new(Vec::new())), running: Arc::new(AtomicBool::new(false)), stop: Arc::new(AtomicBool::new(false)), beep: Arc::new(AtomicBool::new(true)) }
    }

    pub fn add(&self, path: PathBuf, session_id: u64) {
        self.items.lock().push(SuiteItem { path, session_id, timeout: 15.0, status: "Pending".into() });
    }

    pub fn current_running_session(&self, hub: &Hub) -> Option<Arc<crate::session::Session>> {
        let items = self.items.lock();
        for it in items.iter() {
            if let Some(s) = hub.list().into_iter().find(|s| s.id == it.session_id) {
                if s.script_running() {
                    return Some(s);
                }
            }
        }
        None
    }

    pub fn stop_all(&self, hub: &Hub) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(s) = self.current_running_session(hub) {
            s.request_stop_script();
        }
    }

    /// Start running `indices` of the playlist in a background thread.
    pub fn launch(&self, hub: Arc<Hub>, dirs: Vec<PathBuf>, indices: Vec<usize>) {
        if self.running.swap(true, Ordering::SeqCst) {
            return;
        }
        self.stop.store(false, Ordering::SeqCst);
        let me = self.clone();
        for &i in &indices {
            if let Some(it) = me.items.lock().get_mut(i) {
                it.status = "Pending".into();
            }
        }
        std::thread::spawn(move || {
            me.run(&hub, &dirs, &indices);
            me.running.store(false, Ordering::SeqCst);
            crate::session::request_repaint();
        });
    }

    fn set_status(&self, i: usize, text: &str) {
        if let Some(it) = self.items.lock().get_mut(i) {
            it.status = text.to_string();
        }
        crate::session::request_repaint();
    }

    fn run(&self, hub: &Arc<Hub>, dirs: &[PathBuf], indices: &[usize]) {
        let prompt = Regex::new(r"[\$#>\]]\s*$").unwrap();
        let mut stopped = false;
        let mut ran: Vec<(String, String)> = Vec::new();
        for &idx in indices {
            if self.stop.load(Ordering::SeqCst) {
                break;
            }
            let Some(item) = self.items.lock().get(idx).cloned() else { continue };
            let sess = hub.list().into_iter().find(|s| s.id == item.session_id).or_else(|| hub.list().into_iter().next());
            let Some(sess) = sess else { break };
            hub.select(&sess);
            self.set_status(idx, "Running");
            sess.start_script(&item.name());
            let start_marker = sess.buf.lock().len();
            let host = ScriptHost::new(hub.clone(), sess.clone(), dirs.to_vec());
            let mut code_failed = false;
            let mut error_reason = String::new();
            match host.run_file(&item.path) {
                Ok(()) => {}
                Err(ScriptError::Stopped) => {
                    self.set_status(idx, "Stopped");
                    self.stop.store(true, Ordering::SeqCst);
                    sess.local("[Playlist] Script stopped by user.\r\n");
                    sess.finish_script();
                    stopped = true;
                    break;
                }
                Err(ScriptError::Failed(m)) => {
                    code_failed = true;
                    error_reason = format!("Script Exception: {m}");
                }
            }
            sess.finish_script();
            let tail = |sess: &crate::session::Session| -> String {
                let b = sess.buf.lock();
                let s = b.get(start_marker.min(b.len())..).unwrap_or("");
                let chars: Vec<char> = s.chars().collect();
                chars[chars.len().saturating_sub(250)..].iter().collect()
            };
            let mut timeout = item.timeout;
            let mut passed = false;
            while !passed && !self.stop.load(Ordering::SeqCst) {
                let choice;
                if code_failed {
                    choice = self.gate(hub, &sess, idx, &item, &error_reason, &tail(&sess));
                } else {
                    let deadline = Instant::now() + Duration::from_secs_f64(timeout);
                    let mut detected = false;
                    while Instant::now() < deadline {
                        if self.stop.load(Ordering::SeqCst) {
                            break;
                        }
                        std::thread::sleep(Duration::from_millis(100));
                        let left = deadline.saturating_duration_since(Instant::now()).as_secs_f64();
                        self.set_status(idx, &format!("Sniffing ({left:.1}s)"));
                        let found = {
                            let b = sess.buf.lock();
                            let s = b.get(start_marker.min(b.len())..).unwrap_or("");
                            prompt.is_match(s).unwrap_or(false)
                        };
                        if found {
                            detected = true;
                            break;
                        }
                    }
                    if detected {
                        passed = true;
                        self.set_status(idx, "Passed");
                        break;
                    }
                    if self.stop.load(Ordering::SeqCst) {
                        break;
                    }
                    choice = self.gate(hub, &sess, idx, &item, &format!("Deadline expired ({timeout}s) without prompt."), &tail(&sess));
                }
                match choice.as_str() {
                    "EXTEND" => {
                        code_failed = false;
                        timeout = 15.0;
                        sess.local("[Playlist] Extension granted...\r\n");
                    }
                    "FORCE" => {
                        passed = true;
                        self.set_status(idx, "Passed (Forced)");
                        sess.local("[Playlist] Forced progression.\r\n");
                    }
                    _ => {
                        self.set_status(idx, "Aborted");
                        self.stop.store(true, Ordering::SeqCst);
                        sess.local("[Playlist] Aborted.\r\n");
                        stopped = true;
                        break;
                    }
                }
            }
            if passed {
                ran.push((item.name(), sess.name()));
            }
        }
        if !stopped && !self.stop.load(Ordering::SeqCst) && !ran.is_empty() {
            let names: Vec<String> = ran.iter().map(|(n, s)| format!("\u{2714}  {n}   [on {s}]")).collect();
            let title = if indices.len() == 1 { "Single Passed" } else { "Suite Completed" };
            hub.info(title, &format!("All {} scripts ran successfully:\n\n{}", ran.len(), names.join("\n")));
        }
    }

    fn gate(&self, hub: &Arc<Hub>, _sess: &Arc<crate::session::Session>, idx: usize, item: &SuiteItem, reason: &str, tail: &str) -> String {
        self.set_status(idx, "GATE TRIGGERED");
        if self.beep.load(Ordering::SeqCst) {
            print!("\x07");
        }
        hub.gate(&item.name(), reason, tail)
    }
}
