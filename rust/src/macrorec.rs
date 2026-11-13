//! Macro recorder + command-line helpers (port of core/macro_recorder.py).

use chrono::{DateTime, Local};
use fancy_regex::Regex;
use once_cell::sync::Lazy;
use std::time::Instant;

static PROMPT_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^.*?[$#>%]\s+").unwrap());
// For finding the prompt itself: it may also end the line (the device's echo has not arrived yet).
static PROMPT_ONLY_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^.*?[$#>%](?:\s+|$)").unwrap());
static SECRET_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)(pass(word|phrase|code)?|passwd|\bpin\b|secret|token)[^:]{0,40}:\s*$").unwrap());

const OPTIONS_WITH_VALUE: &[&str] = &[
    "-c", "--cmd", "-u", "-U", "-i", "-T", "-s", "-w", "-W", "-S", "-q", "-t", "--rcfile", "-Y", "--syntax", "--tabsize",
];
const EDITOR_COMMANDS: &[&str] = &["vi", "vim", "nvim", "nano", "view", "vim.tiny", "vim.basic", "pico", "edit", "sensible-editor"];

/// Strip the prompt from a rendered terminal line and return the typed command.
pub fn extract_command(screen_line: &str) -> String {
    let line = screen_line.trim_end();
    if let Ok(Some(m)) = PROMPT_RE.find(line) {
        return line[m.end()..].trim().to_string();
    }
    line.trim().to_string()
}

/// The prompt part of a rendered line (`root@a:~#`), or an empty string.
pub fn extract_prompt(screen_line: &str) -> String {
    let line = screen_line.trim_end();
    if let Ok(Some(m)) = PROMPT_ONLY_RE.find(line) {
        return m.as_str().trim().to_string();
    }
    String::new()
}

pub fn is_secret_prompt(screen_line: &str) -> bool {
    SECRET_RE.is_match(screen_line.trim_end()).unwrap_or(false)
}

/// `(uses_sudo, files)` when `command` opens vi / vim / nano, else None.
pub fn parse_editor_command(command: &str) -> Option<(bool, Vec<String>)> {
    let text = command.trim();
    if text.is_empty() || ["|", ";", "&&", "||", ">", "<", "`", "$("].iter().any(|t| text.contains(t)) {
        return None;
    }
    let mut parts = shell_split(text);
    if parts.is_empty() {
        return None;
    }
    let mut sudo = false;
    if parts[0] == "sudo" {
        sudo = true;
        parts.remove(0);
        while !parts.is_empty() && parts[0].starts_with('-') {
            let flag = parts.remove(0);
            if ["-u", "-g", "-C", "-p"].contains(&flag.as_str()) && !parts.is_empty() {
                parts.remove(0);
            }
        }
    }
    if !parts.is_empty() && parts[0] == "sudoedit" {
        sudo = true;
        parts[0] = "vim".into();
    }
    if parts.is_empty() || !EDITOR_COMMANDS.contains(&parts[0].rsplit('/').next().unwrap_or("")) {
        return None;
    }
    let mut files = Vec::new();
    let mut skip = false;
    for arg in &parts[1..] {
        if skip {
            skip = false;
        } else if OPTIONS_WITH_VALUE.contains(&arg.as_str()) {
            skip = true;
        } else if !arg.starts_with(['-', '+']) {
            files.push(arg.clone());
        }
    }
    if files.is_empty() {
        None
    } else {
        Some((sudo, files))
    }
}

/// Minimal POSIX-like word splitting (quotes and backslashes).
pub fn shell_split(s: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut in_word = false;
    let mut quote: Option<char> = None;
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        match quote {
            Some(q) => {
                if c == q {
                    quote = None;
                } else if c == '\\' && q == '"' {
                    if let Some(n) = chars.next() {
                        cur.push(n);
                    }
                } else {
                    cur.push(c);
                }
            }
            None => match c {
                '\'' | '"' => {
                    quote = Some(c);
                    in_word = true;
                }
                '\\' => {
                    if let Some(n) = chars.next() {
                        cur.push(n);
                        in_word = true;
                    }
                }
                c if c.is_whitespace() => {
                    if in_word {
                        out.push(std::mem::take(&mut cur));
                        in_word = false;
                    }
                }
                c => {
                    cur.push(c);
                    in_word = true;
                }
            },
        }
    }
    if in_word {
        out.push(cur);
    }
    out
}

pub fn control_key_name(k: &str) -> &'static str {
    match k {
        "\x03" => "Ctrl+C",
        "\x04" => "Ctrl+D",
        "\x1a" => "Ctrl+Z",
        "\x1c" => "Ctrl+\\",
        "\x0c" => "Ctrl+L",
        _ => "",
    }
}

pub struct MacroStep {
    pub delay: f64,
    pub kind: &'static str, // line | secret | keys | note
    pub text: String,
    pub comment: String,
    pub prompt: String,
}

pub struct MacroRecorder {
    pub session_name: String,
    pub connection_label: String,
    pub connect_hint: String,
    pub steps: Vec<MacroStep>,
    started: Instant,
    pub started_wall: DateTime<Local>,
    last: Instant,
}

impl MacroRecorder {
    pub fn new(session_name: &str, connection_label: &str, connect_hint: &str) -> Self {
        let now = Instant::now();
        Self {
            session_name: session_name.into(),
            connection_label: connection_label.into(),
            connect_hint: connect_hint.into(),
            steps: Vec::new(),
            started: now,
            started_wall: Local::now(),
            last: now,
        }
    }

    fn delay(&mut self) -> f64 {
        let now = Instant::now();
        let d = now.duration_since(self.last).as_secs_f64();
        self.last = now;
        d
    }

    pub fn record_line(&mut self, text: &str, secret: bool, prompt: &str, where_: &str) {
        let delay = self.delay();
        if secret {
            self.steps.push(MacroStep { delay, kind: "secret", text: String::new(), comment: prompt.trim().into(), prompt: where_.trim().into() });
        } else {
            self.steps.push(MacroStep { delay, kind: "line", text: text.into(), comment: String::new(), prompt: where_.trim().into() });
        }
    }

    pub fn record_keys(&mut self, keys: &str) {
        let delay = self.delay();
        self.steps.push(MacroStep { delay, kind: "keys", text: keys.into(), comment: control_key_name(keys).into(), prompt: String::new() });
    }

    pub fn record_note(&mut self, note: &str) {
        let delay = self.delay();
        self.steps.push(MacroStep { delay, kind: "note", text: note.into(), comment: String::new(), prompt: String::new() });
    }

    pub fn command_count(&self) -> usize {
        self.steps.iter().filter(|s| matches!(s.kind, "line" | "secret" | "keys")).count()
    }

    pub fn elapsed(&self) -> f64 {
        self.started.elapsed().as_secs_f64()
    }

    pub fn to_python(&self) -> String {
        let total = self.elapsed();
        let mut l: Vec<String> = vec![
            "# Macro recorded with Modern Multi Terminal Emulator".into(),
            format!("# Terminal : {} ({})", self.session_name, self.connection_label),
            format!("# Recorded : {}  -  {} command(s)  -  {:.1} s", self.started_wall.format("%Y-%m-%d %H:%M:%S"), self.command_count(), total),
            "#".into(),
            "# Replay: Scripting > Run Script... (or add it to the Multi-Script Suite).".into(),
            "# Each command is preceded by a comment with the prompt it was typed at, e.g. # [root@a:~#]".into(),
            "#   send(text)     types the text and presses Enter".into(),
            "#   pause(seconds) waits as long as you did while recording".into(),
            "# Tip: swap a pause(...) for wait(\"$ \") to continue as soon as the prompt appears.".into(),
            "".into(),
            "TIMING_SCALE = 1.0  # 0.5 = replay twice as fast, 0 = no pauses".into(),
            "".into(),
            "".into(),
            "def pause(seconds):".into(),
            "    if TIMING_SCALE > 0 and seconds > 0:".into(),
            "        sleep(seconds * TIMING_SCALE)".into(),
            "".into(),
            "".into(),
        ];
        if !self.connect_hint.is_empty() {
            l.push("# The terminal was already connected when recording started. To connect from the".into());
            l.push("# script instead, uncomment the next line:".into());
            l.push(format!("# {}", self.connect_hint));
            l.push("".into());
        }
        for step in &self.steps {
            let delay = (step.delay * 10.0).round() / 10.0;
            if delay >= 0.1 {
                l.push(format!("pause({delay:?})"));
            }
            if matches!(step.kind, "line" | "secret") && !step.prompt.is_empty() {
                l.push(format!("# [{}]", step.prompt));
            }
            match step.kind {
                "line" => l.push(format!("send({})", py_repr(&step.text))),
                "secret" => {
                    let label = if step.comment.is_empty() { "Password" } else { &step.comment };
                    l.push(format!("send(input({}))  # hidden input - not stored in the macro", py_repr(label)));
                }
                "keys" => {
                    let note = if step.comment.is_empty() { String::new() } else { format!("  # {}", step.comment) };
                    l.push(format!("send({}, newline=False){}", py_repr(&step.text), note));
                }
                "note" => l.push(format!("# {}", step.text)),
                _ => {}
            }
        }
        l.join("\n") + "\n"
    }
}

/// Python-style repr of a string (single quotes unless the text has a single quote and no double).
pub fn py_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::new();
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts() {
        assert_eq!(extract_command("root@a:~# ls -l"), "ls -l");
        assert_eq!(extract_prompt("root@a:~# ls -l"), "root@a:~#");
        // the echo of the typed command has not arrived yet: the line is only the prompt
        assert_eq!(extract_prompt("root@a:~#      "), "root@a:~#");
        assert_eq!(extract_prompt("no prompt here"), "");
        assert!(is_secret_prompt("Password: "));
        assert!(!is_secret_prompt("root@a:~# "));
    }

    #[test]
    fn editor_commands() {
        assert_eq!(parse_editor_command("sudo vim -R /etc/hosts"), Some((true, vec!["/etc/hosts".into()])));
        assert_eq!(parse_editor_command("nano a.txt b.txt").unwrap().1.len(), 2);
        assert!(parse_editor_command("vim a | cat").is_none());
        assert!(parse_editor_command("ls").is_none());
        assert!(parse_editor_command("vim").is_none());
    }

    #[test]
    fn script_keeps_prompt() {
        let mut r = MacroRecorder::new("t", "SSH", "");
        r.record_line("ls -l", false, "", "root@a:~#");
        let s = r.to_python();
        assert!(s.contains("# [root@a:~#]"));
        assert!(s.contains("send('ls -l')"));
        assert!(!s.contains("Replaying"));
    }
}
