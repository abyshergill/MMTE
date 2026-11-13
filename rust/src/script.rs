//! Script execution: the Tera Term (.ttl) interpreter and the Python bridge.
//!
//! Python scripts and plugins run in a `python3` subprocess that talks to this process over
//! JSON lines (see `prelude.py`), so every script helper (send, wait, log_on ...) is implemented
//! once here and shared by both languages.

use crate::conn::ConnSpec;
use crate::macrorec::shell_split;
use crate::session::{Hub, ScriptStopped, Session};
use once_cell::sync::OnceCell;
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};

pub const PRELUDE: &str = include_str!("prelude.py");

#[derive(Debug, Clone, PartialEq)]
pub enum ScriptError {
    Stopped,
    Failed(String),
}

impl std::fmt::Display for ScriptError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ScriptError::Stopped => write!(f, "stopped by user"),
            ScriptError::Failed(m) => write!(f, "{m}"),
        }
    }
}

impl From<ScriptStopped> for ScriptError {
    fn from(_: ScriptStopped) -> Self {
        ScriptError::Stopped
    }
}

enum OpErr {
    Stopped,
    Msg(String),
}
impl From<ScriptStopped> for OpErr {
    fn from(_: ScriptStopped) -> Self {
        OpErr::Stopped
    }
}
impl From<ScriptError> for OpErr {
    fn from(e: ScriptError) -> Self {
        match e {
            ScriptError::Stopped => OpErr::Stopped,
            ScriptError::Failed(m) => OpErr::Msg(m),
        }
    }
}

pub fn find_python() -> Option<String> {
    static PY: OnceCell<Option<String>> = OnceCell::new();
    PY.get_or_init(|| {
        let mut candidates: Vec<String> = Vec::new();
        if let Ok(p) = std::env::var("MMTE_PYTHON") {
            candidates.push(p);
        }
        candidates.extend(["python3", "python", "py"].iter().map(|s| s.to_string()));
        candidates.into_iter().find(|c| {
            Command::new(c).arg("--version").stdout(Stdio::null()).stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false)
        })
    })
    .clone()
}

fn prelude_path() -> Result<PathBuf, String> {
    let dir = std::env::temp_dir().join(format!("mmte_bridge_{}", std::process::id()));
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let p = dir.join("prelude.py");
    let current = std::fs::read_to_string(&p).unwrap_or_default();
    if current != PRELUDE {
        std::fs::write(&p, PRELUDE).map_err(|e| e.to_string())?;
    }
    Ok(p)
}

pub struct ScriptHost {
    pub hub: Arc<Hub>,
    cur: parking_lot::Mutex<Arc<Session>>,
    pub plugin_dirs: Vec<PathBuf>,
    pub result: parking_lot::Mutex<Option<Value>>,
}

impl ScriptHost {
    pub fn new(hub: Arc<Hub>, session: Arc<Session>, plugin_dirs: Vec<PathBuf>) -> Self {
        Self { hub, cur: parking_lot::Mutex::new(session), plugin_dirs, result: parking_lot::Mutex::new(None) }
    }

    pub fn cur(&self) -> Arc<Session> {
        self.cur.lock().clone()
    }

    /// Run a `.ttl` or `.py` file on the current terminal.
    pub fn run_file(&self, path: &Path) -> Result<(), ScriptError> {
        let is_ttl = path.extension().map(|e| e.eq_ignore_ascii_case("ttl")).unwrap_or(false);
        if is_ttl {
            self.run_ttl(path)
        } else {
            self.run_python(&["script", &path.to_string_lossy()])
        }
    }

    // ---------------------------------------------------------------------------------
    // TTL
    // ---------------------------------------------------------------------------------
    pub fn run_ttl(&self, path: &Path) -> Result<(), ScriptError> {
        let text = std::fs::read(path).map_err(|e| ScriptError::Failed(e.to_string()))?;
        let text = String::from_utf8_lossy(&text).into_owned();
        let mut vars: std::collections::HashMap<String, String> = Default::default();
        for (idx, raw) in text.lines().enumerate() {
            let line_no = idx + 1;
            self.cur().script_checkpoint()?;
            let line = ttl_clean_line(raw);
            if line.is_empty() {
                continue;
            }
            if line.contains('=') && !line.to_lowercase().starts_with("connect") {
                let (name, value) = line.split_once('=').unwrap();
                let name = name.trim();
                if is_identifier(name) {
                    vars.insert(name.to_string(), ttl_unquote(value.trim()));
                    continue;
                }
            }
            let (cmd, mut arg) = ttl_split_command(&line);
            let cmd = cmd.to_lowercase();
            if let Some(v) = vars.get(&arg) {
                arg = v.clone();
            }
            match cmd.as_str() {
                "end" => break,
                "connect" => self.ttl_connect(&arg)?,
                "wait" => {
                    let pattern = ttl_unquote(&arg);
                    if !pattern.is_empty() && !self.cur().wait_for(&pattern, 60.0, true)? {
                        return Err(ScriptError::Failed(format!("wait timed out for: {pattern}")));
                    }
                }
                "sendln" => self.cur().send(&(ttl_unquote(&arg) + "\n")),
                "send" => self.cur().send(&ttl_unquote(&arg)),
                "pause" | "mpause" => {
                    let mut d: f64 = ttl_unquote(&arg).parse().unwrap_or(1.0);
                    if cmd == "mpause" {
                        d /= 1000.0;
                    }
                    self.cur().sleep_checkpoint(d)?;
                }
                _ => self.cur().local(&format!("\r\n[TTL warning: unsupported command on line {line_no}: {cmd}]\r\n")),
            }
        }
        Ok(())
    }

    fn ttl_connect(&self, arg: &str) -> Result<(), ScriptError> {
        let parts = shell_split(&ttl_unquote(arg));
        if parts.is_empty() {
            return Err(ScriptError::Failed("connect requires host".into()));
        }
        let host = parts[0].clone();
        let (mut user, mut pass, mut port, mut telnet) = (String::new(), String::new(), None::<u16>, false);
        for item in &parts[1..] {
            let lower = item.to_lowercase();
            if lower == "/telnet" || lower == "/t" {
                telnet = true;
            } else if lower.starts_with("/user=") {
                user = item[6..].to_string();
            } else if lower.starts_with("/passwd=") {
                pass = item[8..].to_string();
            } else if lower.starts_with("/password=") {
                pass = item[10..].to_string();
            } else if lower.starts_with("/port=") {
                port = item[6..].parse().ok();
            }
        }
        let spec = if telnet {
            ConnSpec::Telnet { host, port: port.unwrap_or(23) }
        } else {
            ConnSpec::Ssh { host, username: user, password: pass, port: port.unwrap_or(22), look_for_keys: false }
        };
        self.cur().connect(spec).map_err(ScriptError::Failed)?;
        self.cur().sleep_checkpoint(0.5)?;
        Ok(())
    }

    // ---------------------------------------------------------------------------------
    // Python bridge
    // ---------------------------------------------------------------------------------
    pub fn run_python(&self, args: &[&str]) -> Result<(), ScriptError> {
        let py = find_python().ok_or_else(|| {
            ScriptError::Failed("Python 3 was not found. Install it (or set MMTE_PYTHON) to run .py scripts and plugins.".into())
        })?;
        let prelude = prelude_path().map_err(ScriptError::Failed)?;
        let dirs = std::env::join_paths(&self.plugin_dirs).unwrap_or_default();
        let mut child = Command::new(py)
            .arg("-u")
            .arg(&prelude)
            .args(args)
            .env("MMTE_PLUGIN_DIRS", dirs)
            .env("PYTHONIOENCODING", "utf-8")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| ScriptError::Failed(format!("could not start Python: {e}")))?;
        let mut stdin = child.stdin.take().unwrap();
        let stdout = child.stdout.take().unwrap();
        let stderr = child.stderr.take().unwrap();
        let (tx, rx) = crossbeam_channel::unbounded::<String>();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if tx.send(line).is_err() {
                    break;
                }
            }
        });
        let err_buf = Arc::new(parking_lot::Mutex::new(String::new()));
        {
            let err_buf = err_buf.clone();
            std::thread::spawn(move || {
                for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                    let mut b = err_buf.lock();
                    b.push_str(&line);
                    b.push('\n');
                }
            });
        }
        let mut error_text: Option<String> = None;
        let base = args.get(1).and_then(|p| Path::new(p).parent().map(|p| p.to_path_buf())).unwrap_or_default();
        let outcome: Result<(), ScriptError> = loop {
            match rx.recv_timeout(Duration::from_millis(100)) {
                Ok(line) => {
                    let Ok(v) = serde_json::from_str::<Value>(&line) else { continue };
                    let op = v["op"].as_str().unwrap_or("").to_string();
                    if op == "error" {
                        error_text = v["text"].as_str().map(|s| s.to_string());
                        continue;
                    }
                    if op == "result" {
                        *self.result.lock() = Some(v["value"].clone());
                        continue;
                    }
                    let reply = match self.op(&op, &v, &base) {
                        Ok(val) => json!({"ok": true, "value": val}),
                        Err(OpErr::Stopped) => json!({"ok": false, "error": "__stopped__"}),
                        Err(OpErr::Msg(m)) => json!({"ok": false, "error": m}),
                    };
                    if writeln!(stdin, "{reply}").and_then(|_| stdin.flush()).is_err() {
                        break Ok(());
                    }
                }
                Err(crossbeam_channel::RecvTimeoutError::Timeout) => {
                    if self.cur().script_checkpoint().is_err() {
                        // stopped while Python is busy outside of a script call
                        let _ = child.kill();
                        break Err(ScriptError::Stopped);
                    }
                }
                Err(crossbeam_channel::RecvTimeoutError::Disconnected) => break Ok(()),
            }
        };
        drop(stdin);
        let status = {
            let deadline = Instant::now() + Duration::from_secs(3);
            loop {
                match child.try_wait() {
                    Ok(Some(s)) => break Some(s),
                    Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(20)),
                    _ => {
                        let _ = child.kill();
                        break child.wait().ok();
                    }
                }
            }
        };
        if let Err(e) = outcome {
            return Err(e);
        }
        match status.and_then(|s| s.code()) {
            Some(0) | None => Ok(()),
            Some(3) => Err(ScriptError::Stopped),
            Some(_) => {
                let text = error_text.unwrap_or_else(|| err_buf.lock().clone());
                let last = text.trim().lines().last().unwrap_or("script failed").to_string();
                Err(ScriptError::Failed(last))
            }
        }
    }

    fn target(&self, v: &Value) -> Option<Arc<Session>> {
        match v["target"].as_str() {
            Some(name) => self.hub.find(name),
            None => Some(self.cur()),
        }
    }

    fn op(&self, op: &str, v: &Value, base: &Path) -> Result<Value, OpErr> {
        let s = self.cur();
        let text_of = |k: &str| v[k].as_str().unwrap_or("").to_string();
        match op {
            "print" => {
                s.print(&format!("{}{}", text_of("text"), v["end"].as_str().unwrap_or("")));
                Ok(Value::Null)
            }
            "send" => {
                s.script_checkpoint()?;
                let delay = v["delay"].as_f64().unwrap_or(0.0);
                if delay > 0.0 {
                    s.sleep_checkpoint(delay)?;
                }
                let nl = v["newline"].as_bool().unwrap_or(true);
                self.cur().send(&format!("{}{}", text_of("text"), if nl { "\n" } else { "" }));
                Ok(Value::Null)
            }
            "send_to" => {
                let t = self.target(v).ok_or_else(|| OpErr::Msg("terminal not found".into()))?;
                let nl = v["newline"].as_bool().unwrap_or(true);
                t.send(&format!("{}{}", text_of("text"), if nl { "\n" } else { "" }));
                Ok(Value::Null)
            }
            "sleep" => {
                s.sleep_checkpoint(v["seconds"].as_f64().unwrap_or(0.0))?;
                Ok(Value::Null)
            }
            "wait" => {
                let pattern = text_of("pattern");
                let ok = s.wait_for(&pattern, v["timeout"].as_f64().unwrap_or(60.0), v["case_sensitive"].as_bool().unwrap_or(true))?;
                if !ok {
                    s.local(&format!("\r\n[wait timed out for: '{pattern}']\r\n"));
                }
                Ok(json!(ok))
            }
            "wait_connected" => {
                let deadline = Instant::now() + Duration::from_secs_f64(v["timeout"].as_f64().unwrap_or(30.0));
                loop {
                    let c = self.cur();
                    c.script_checkpoint()?;
                    if c.is_connected() {
                        return Ok(json!(true));
                    }
                    if !c.connecting.load(std::sync::atomic::Ordering::SeqCst) {
                        return Err(OpErr::Msg(format!("{} is not connected", c.name())));
                    }
                    if Instant::now() >= deadline {
                        return Err(OpErr::Msg(format!("Timed out connecting {}", c.name())));
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }
            }
            "alert" => {
                let message = text_of("message");
                if !self.hub.alert(&message) {
                    s.local(&format!("\r\n[Script stopped by user alert: {message}]\r\n"));
                    return Err(OpErr::Msg(format!("Script execution stopped by user at alert: '{message}'")));
                }
                Ok(json!(true))
            }
            "input" => Ok(json!(self.hub.input(&text_of("prompt")))),
            "connect_ssh" => {
                let spec = ConnSpec::Ssh {
                    host: text_of("host"),
                    username: text_of("username"),
                    password: text_of("password"),
                    port: v["port"].as_u64().unwrap_or(22) as u16,
                    look_for_keys: false,
                };
                s.connect(spec).map_err(OpErr::Msg)?;
                Ok(Value::Null)
            }
            "connect_telnet" => {
                s.connect(ConnSpec::Telnet { host: text_of("host"), port: v["port"].as_u64().unwrap_or(23) as u16 }).map_err(OpErr::Msg)?;
                Ok(Value::Null)
            }
            "connect_serial" => {
                let spec = ConnSpec::Serial {
                    port: text_of("port"),
                    baud: v["baudrate"].as_u64().unwrap_or(115200) as u32,
                    bytesize: v["bytesize"].as_u64().unwrap_or(8) as u8,
                    parity: text_of("parity").chars().next().unwrap_or('N'),
                    stopbits: v["stopbits"].as_f64().unwrap_or(1.0) as f32,
                };
                s.connect(spec).map_err(OpErr::Msg)?;
                Ok(Value::Null)
            }
            "log_on" => {
                let p = resolve(base, &text_of("path"));
                s.enable_logging(&p).map_err(OpErr::Msg)?;
                Ok(Value::Null)
            }
            "log_off" => {
                match v["path"].as_str() {
                    Some(p) => s.disable_logging_path(Some(&resolve(base, p))),
                    None => s.disable_logging(),
                }
                Ok(Value::Null)
            }
            "terminal_new" => {
                let n = self.hub.new_session(v["name"].as_str());
                *self.cur.lock() = n.clone();
                Ok(json!(n.name()))
            }
            "terminal_close" => {
                let target = match v["name"].as_str() {
                    Some(n) => self.hub.find(n),
                    None => Some(s.clone()),
                };
                if let Some(t) = target {
                    self.hub.close(&t);
                }
                Ok(Value::Null)
            }
            "terminal_rename" => Ok(json!(self.hub.rename(&text_of("current"), &text_of("new")))),
            "terminal_list" => Ok(json!(self.hub.list().iter().map(|x| x.name()).collect::<Vec<_>>())),
            "terminal_select" => match self.hub.find(&text_of("name")) {
                Some(t) => {
                    self.hub.select(&t);
                    *self.cur.lock() = t;
                    Ok(json!(true))
                }
                None => Ok(json!(false)),
            },
            "terminal_name" => Ok(json!(s.name())),
            "session_info" => Ok(match self.target(v) {
                Some(t) => json!({"name": t.name(), "label": t.label(), "connected": t.is_connected()}),
                None => Value::Null,
            }),
            "session_list" => Ok(json!(self
                .hub
                .list()
                .iter()
                .map(|t| json!({"name": t.name(), "label": t.label(), "connected": t.is_connected()}))
                .collect::<Vec<_>>())),
            "recent_output" => {
                let t = self.target(v).ok_or_else(|| OpErr::Msg("terminal not found".into()))?;
                Ok(json!(t.recent_output(v["max_chars"].as_u64().unwrap_or(6000) as usize)))
            }
            "status" => {
                self.hub.set_status(&text_of("text"));
                Ok(Value::Null)
            }
            "run_script" => {
                let p = resolve(Path::new(&text_of("base")), &text_of("path"));
                if !p.exists() {
                    return Err(OpErr::Msg(format!("Script file not found: {}", p.display())));
                }
                self.run_file(&p)?;
                Ok(Value::Null)
            }
            other => Err(OpErr::Msg(format!("unknown script operation: {other}"))),
        }
    }
}

fn resolve(base: &Path, p: &str) -> PathBuf {
    let path = PathBuf::from(p);
    if path.is_absolute() {
        path
    } else {
        base.join(path)
    }
}

// --- TTL helpers (ported 1:1) ---------------------------------------------------------------

pub fn ttl_clean_line(raw: &str) -> String {
    let line = raw.trim();
    if line.is_empty() || line.starts_with(';') {
        return String::new();
    }
    let mut out = String::new();
    let mut quote: Option<char> = None;
    for ch in line.chars() {
        if ch == '\'' || ch == '"' {
            match quote {
                None => quote = Some(ch),
                Some(q) if q == ch => quote = None,
                _ => {}
            }
        } else if ch == ';' && quote.is_none() {
            break;
        }
        out.push(ch);
    }
    out.trim().to_string()
}

pub fn ttl_split_command(line: &str) -> (String, String) {
    let line = line.trim_start();
    match line.split_once(char::is_whitespace) {
        Some((c, rest)) => (c.to_string(), rest.trim().to_string()),
        None => (line.to_string(), String::new()),
    }
}

pub fn ttl_unquote(text: &str) -> String {
    let t = text.trim();
    let b: Vec<char> = t.chars().collect();
    if b.len() >= 2 && b[0] == b[b.len() - 1] && (b[0] == '\'' || b[0] == '"') {
        b[1..b.len() - 1].iter().collect()
    } else {
        t.to_string()
    }
}

fn is_identifier(s: &str) -> bool {
    let mut chars = s.chars();
    match chars.next() {
        Some(c) if c.is_alphabetic() || c == '_' => chars.all(|c| c.is_alphanumeric() || c == '_'),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ttl_helpers() {
        assert_eq!(ttl_clean_line("; comment"), "");
        assert_eq!(ttl_clean_line("sendln 'hi' ; trailing"), "sendln 'hi'");
        assert_eq!(ttl_clean_line("sendln 'a;b'"), "sendln 'a;b'");
        assert_eq!(ttl_split_command("sendln  'x y'"), ("sendln".into(), "'x y'".into()));
        assert_eq!(ttl_unquote("'abc'"), "abc");
        assert_eq!(ttl_unquote("abc"), "abc");
    }

    #[test]
    fn ttl_runs_against_a_session() {
        let (hub, _rx) = Hub::new();
        let s = hub.new_session(None);
        let host = ScriptHost::new(hub.clone(), s.clone(), vec![]);
        let dir = std::env::temp_dir().join(format!("mmte_ttl_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("t.ttl");
        std::fs::write(&p, "; hi\ncmd = 'ls -l'\nsendln cmd\nmpause 10\nunknown thing\nend\nsend 'never'\n").unwrap();
        host.run_ttl(&p).unwrap();
        assert!(s.buf.lock().contains("unsupported command on line 5"));
        assert!(s.buf.lock().contains("No active connection"));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn python_bridge_runs_a_script() {
        if find_python().is_none() {
            return;
        }
        let (hub, _rx) = Hub::new();
        let s = hub.new_session(Some("main"));
        let host = ScriptHost::new(hub.clone(), s.clone(), vec![]);
        let dir = std::env::temp_dir().join(format!("mmte_py_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("t.py");
        std::fs::write(
            &p,
            "print('hello', terminal_name)\nterminal_new('second')\nprint(terminal_list())\nlog_on('x.log')\nlog_off\nok = wait('nothing', timeout=0.2)\nprint('ok=', ok)\n",
        )
        .unwrap();
        host.run_file(&p).unwrap();
        let main_text = s.buf.lock().clone();
        assert!(main_text.contains("hello main"), "{main_text}");
        let second = hub.find("second").unwrap();
        let t = second.buf.lock().clone();
        assert!(t.contains("['main', 'second']"), "{t}");
        assert!(t.contains("ok= False"), "{t}");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn python_error_is_reported() {
        if find_python().is_none() {
            return;
        }
        let (hub, _rx) = Hub::new();
        let s = hub.new_session(None);
        let host = ScriptHost::new(hub, s, vec![]);
        let dir = std::env::temp_dir().join(format!("mmte_pyerr_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("e.py");
        std::fs::write(&p, "raise ValueError('boom')\n").unwrap();
        let err = host.run_file(&p).unwrap_err();
        assert!(matches!(err, ScriptError::Failed(m) if m.contains("boom")));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn stop_kills_python() {
        if find_python().is_none() {
            return;
        }
        let (hub, _rx) = Hub::new();
        let s = hub.new_session(None);
        let host = ScriptHost::new(hub, s.clone(), vec![]);
        let dir = std::env::temp_dir().join(format!("mmte_pystop_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("s.py");
        std::fs::write(&p, "sleep(30)\n").unwrap();
        s.start_script("s.py");
        let s2 = s.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(500));
            s2.request_stop_script();
        });
        let r = host.run_file(&p);
        assert_eq!(r, Err(ScriptError::Stopped));
        s.finish_script();
    }
}
