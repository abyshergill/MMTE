//! Which terminals receive AI-generated commands (port of core/terminal_dispatch.py).

use crate::session::Session;
use fancy_regex::Regex;
use once_cell::sync::Lazy;
use std::sync::Arc;

pub const MAX_COMMANDS: usize = 50;
pub const MAX_COMMAND_CHARS: usize = 4000;

static FENCE: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?s)```[ \t]*([\w+.-]*)[ \t]*\r?\n(.*?)```").unwrap());
static CONTROL: Lazy<Regex> = Lazy::new(|| Regex::new(r"[\x00-\x08\x0b-\x1f\x7f]").unwrap());

const SHELL_LANGS: &[&str] = &["", "sh", "bash", "shell", "zsh", "cmd", "bat", "powershell", "pwsh", "ps1", "console", "terminal", "serial", "uart", "text-command"];
const CURRENT_WORDS: &[&str] = &["", "current", "active", "selected", "this", "here", "current terminal", "active terminal", "selected terminal", "this terminal"];
const ALL_WORDS: &[&str] = &["all", "all terminals", "every terminal", "each terminal", "all connected", "all active", "all active terminals", "all connected terminals", "*"];

pub fn connected_sessions(sessions: &[Arc<Session>]) -> Vec<Arc<Session>> {
    sessions.iter().filter(|s| s.is_connected()).cloned().collect()
}

/// Best terminal for "the terminal" when the user did not name one.
pub fn default_target(sessions: &[Arc<Session>], current: Option<&Arc<Session>>) -> Option<Arc<Session>> {
    if let Some(c) = current {
        if c.is_connected() {
            return Some(c.clone());
        }
    }
    let live = connected_sessions(sessions);
    if live.len() == 1 {
        return Some(live[0].clone());
    }
    current.cloned().or_else(|| sessions.first().cloned())
}

#[derive(Default)]
pub struct Targets {
    pub sessions: Vec<Arc<Session>>,
    pub not_connected: Vec<Arc<Session>>,
    pub missing: Vec<String>,
    pub note: String,
}

fn split_spec(spec: &str) -> Vec<String> {
    let re = Regex::new(r"(?i)\s*(?:,|;|&|\+|\band\b)\s*").unwrap();
    let mut parts = Vec::new();
    let mut last = 0;
    for m in re.find_iter(spec).flatten() {
        parts.push(spec[last..m.start()].to_string());
        last = m.end();
    }
    parts.push(spec[last..].to_string());
    parts
}

fn match_name(name: &str, sessions: &[Arc<Session>]) -> Option<Arc<Session>> {
    let key = name.trim().to_lowercase();
    if let Some(s) = sessions.iter().find(|s| s.name().to_lowercase() == key) {
        return Some(s.clone());
    }
    if !key.is_empty() && key.chars().all(|c| c.is_ascii_digit()) {
        return sessions.iter().find(|s| s.name().to_lowercase() == format!("terminal {key}")).cloned();
    }
    None
}

pub fn resolve_targets(spec: &str, sessions: &[Arc<Session>], current: Option<&Arc<Session>>) -> Targets {
    let spec = spec.trim();
    let mut r = Targets::default();
    let mut wanted: Vec<Arc<Session>> = Vec::new();
    let exact = if spec.is_empty() { None } else { match_name(spec, sessions) };
    let parts = if exact.is_some() { vec![spec.to_string()] } else { split_spec(spec) };
    for part in parts {
        let key = part.trim().to_lowercase();
        if ALL_WORDS.contains(&key.as_str()) {
            let live = connected_sessions(sessions);
            if live.is_empty() {
                r.note = "No terminal is connected.".into();
            }
            wanted.extend(live);
        } else if CURRENT_WORDS.contains(&key.as_str()) {
            match default_target(sessions, current) {
                Some(t) => wanted.push(t),
                None => r.note = "There are no terminal tabs.".into(),
            }
        } else {
            match match_name(&part, sessions) {
                Some(s) => wanted.push(s),
                None => r.missing.push(part.trim().to_string()),
            }
        }
    }
    let mut seen = std::collections::HashSet::new();
    for s in wanted {
        if !seen.insert(s.id) {
            continue;
        }
        if s.is_connected() { r.sessions.push(s) } else { r.not_connected.push(s) }
    }
    r
}

/// One line, no control characters.
pub fn sanitize_command(command: &str) -> String {
    let t = CONTROL.replace_all(command, "").trim().to_string();
    t.chars().take(MAX_COMMAND_CHARS).collect()
}

pub fn clean_commands<'a>(lines: impl IntoIterator<Item = &'a str>) -> Vec<String> {
    let mut out = Vec::new();
    for l in lines {
        for part in l.lines() {
            let c = sanitize_command(part);
            if !c.is_empty() {
                out.push(c);
            }
        }
    }
    out.truncate(MAX_COMMANDS);
    out
}

/// Commands typed/edited in the Run dialog (one per line; `#` lines are notes).
pub fn parse_command_lines(text: &str) -> Vec<String> {
    clean_commands(text.lines().filter(|l| !l.trim().starts_with('#')))
}

/// Commands from fenced shell blocks in a model reply.
pub fn extract_commands(text: &str) -> Vec<String> {
    let mut cmds: Vec<String> = Vec::new();
    for caps in FENCE.captures_iter(text).flatten() {
        let lang = caps.get(1).map(|m| m.as_str().to_lowercase()).unwrap_or_default();
        if !SHELL_LANGS.contains(&lang.as_str()) {
            continue;
        }
        for line in caps.get(2).map(|m| m.as_str()).unwrap_or("").lines() {
            let t = line.trim();
            if t.is_empty() || t.starts_with('#') {
                continue;
            }
            cmds.push(t.strip_prefix("$ ").unwrap_or(t).trim().to_string());
        }
    }
    clean_commands(cmds.iter().map(|s| s.as_str()))
}

/// Commands the model wants to run while "LLM -> Terminal" is off (awaiting approval).
#[derive(Default)]
pub struct ProposalQueue {
    items: parking_lot::Mutex<Vec<(String, String)>>,
}

impl ProposalQueue {
    pub fn add(&self, target: &str, command: &str) -> bool {
        let c = sanitize_command(command);
        if c.is_empty() {
            return false;
        }
        let mut items = self.items.lock();
        if items.len() >= MAX_COMMANDS {
            return false;
        }
        let t = target.trim();
        items.push((if t.is_empty() { "current".into() } else { t.to_string() }, c));
        true
    }
    pub fn clear(&self) {
        self.items.lock().clear();
    }
    pub fn len(&self) -> usize {
        self.items.lock().len()
    }
    pub fn groups(&self) -> Vec<(String, Vec<String>)> {
        let items = self.items.lock().clone();
        let mut order: Vec<String> = Vec::new();
        let mut map: std::collections::HashMap<String, (String, Vec<String>)> = Default::default();
        for (t, c) in items {
            let key = t.to_lowercase();
            if !map.contains_key(&key) {
                order.push(key.clone());
                map.insert(key.clone(), (t.clone(), Vec::new()));
            }
            map.get_mut(&key).unwrap().1.push(c);
        }
        order.into_iter().map(|k| map.remove(&k).unwrap()).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::session::Hub;

    #[test]
    fn extracts_shell_blocks_only() {
        let t = "Try:\n```bash\n# comment\n$ ls -l\nuname -a\n```\nand\n```python\nprint(1)\n```\n";
        assert_eq!(extract_commands(t), vec!["ls -l".to_string(), "uname -a".to_string()]);
    }

    #[test]
    fn sanitizes() {
        assert_eq!(sanitize_command("ls\x1b[31m -l\x00"), "ls[31m -l");
        assert_eq!(parse_command_lines("# note\nls\n\npwd"), vec!["ls", "pwd"]);
    }

    #[test]
    fn target_resolution() {
        let (hub, _rx) = Hub::new();
        let a = hub.new_session(Some("Terminal 1"));
        let b = hub.new_session(Some("Router"));
        let all = hub.list();
        let r = resolve_targets("Router", &all, Some(&a));
        assert!(r.sessions.is_empty() && r.not_connected.len() == 1 && r.not_connected[0].id == b.id);
        let r = resolve_targets("1, nope", &all, Some(&a));
        assert_eq!(r.not_connected.len(), 1);
        assert_eq!(r.missing, vec!["nope".to_string()]);
        let r = resolve_targets("all", &all, Some(&a));
        assert!(r.note.contains("No terminal is connected"));
        assert_eq!(default_target(&all, Some(&a)).unwrap().id, a.id);
    }

    #[test]
    fn proposals_group_by_target() {
        let q = ProposalQueue::default();
        q.add("current", "ls");
        q.add("Router", "show ver");
        q.add("current", "pwd");
        assert!(!q.add("x", "\x01"));
        let g = q.groups();
        assert_eq!(g.len(), 2);
        assert_eq!(g[0].1, vec!["ls", "pwd"]);
    }
}
