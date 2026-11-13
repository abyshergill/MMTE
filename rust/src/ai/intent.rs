//! Natural-language intent helpers: which app tools a request may use, and direct parsing of
//! simple terminal-management requests (port of the helpers in gui/ai_assistant_panel.py).

use super::tools::app_tool_definitions;
use fancy_regex::Regex;
use serde_json::Value;

fn re(p: &str) -> Regex {
    Regex::new(&format!("(?i){p}")).unwrap_or_else(|e| panic!("{p}: {e}"))
}

pub fn is_advice_only_request(q: &str) -> bool {
    if re(r"^\s*(?:how|what|why|when|where|which|should I|can I|could I|would I)\b").is_match(q).unwrap_or(false) {
        return true;
    }
    re(r"\b(?:do not|don't|dont|without|explain only|just explain)\b").is_match(q).unwrap_or(false)
}

pub fn is_explicit_terminal_execution_request(q: &str) -> bool {
    if re(r"^\s*(?:what|which|how|why|should I|do I|can I)\b").is_match(q).unwrap_or(false) {
        return false;
    }
    let action = re(r"\b(?:(?:please|can you|could you|would you)\s+)?(?:write|type|enter|send|run|execute|print)\b").is_match(q).unwrap_or(false);
    let target = re(r"\b(?:in|inside|into|on|to)\s+(?:(?:the|this|these|those|active|selected)\s+)?terminals?(?:\s+\d+)?\b").is_match(q).unwrap_or(false);
    action && target
}

/// `(terminal, new_name)` for "rename terminal 1 to X".
pub fn parse_rename_request(q: &str) -> Option<(String, String)> {
    let r = re(r"\b(?:rename|change|set)\s+(?:the\s+)?(?:(?:current|active|selected)\s+)?(terminal\s+\d+|current\s+terminal|active\s+terminal)(?:\s+name)?\s+(?:(?:to|as)\s+)?([\w][\w .-]*?)\s*[.!?`]*\s*$");
    let c = r.captures(q).ok()??;
    Some((c.get(1)?.as_str().trim().to_string(), c.get(2)?.as_str().trim().trim_end_matches(['.', ',', '!', '?', '`']).to_string()))
}

pub fn parse_create_terminal_request(q: &str) -> Option<String> {
    let r = re(r"\b(?:launch|open|create|start|add)\s+(?:(?:a|the|new)\s+)*(?:application\s+)?terminal\b(?:\s+for\s+(?:the\s+)?application)?\s+(?:(?:and\s+)?give\s+(?:it\s+)?(?:the\s+)?name|name\s+it|named|called|with\s+(?:the\s+)?name)\s+([\w][\w .-]*?)\s*[.!?`]*\s*$");
    let c = r.captures(q).ok()??;
    Some(c.get(1)?.as_str().trim().trim_end_matches(['.', ',', '!', '?', '`']).to_string())
}

#[derive(Debug, Clone, PartialEq)]
pub struct ConnectionSpec {
    pub protocol: String,
    pub host: String,
    pub port: u16,
    pub username: String,
    pub password: String,
}

pub fn parse_terminal_connection_specs(q: &str) -> Vec<ConnectionSpec> {
    let proto = re(r"\b(ssh|telnet)\b");
    let matches: Vec<(usize, usize, String)> = proto.captures_iter(q).flatten().filter_map(|c| c.get(0).zip(c.get(1)).map(|(m, g)| (m.start(), m.end(), g.as_str().to_lowercase()))).collect();
    let host_port = re(r"\b([a-zA-Z0-9_.-]+)\s*(?::|[\s|]+)\s*(\d{2,5})\b");
    let mut specs = Vec::new();
    for (i, (_, end, protocol)) in matches.iter().enumerate() {
        let seg_end = matches.get(i + 1).map(|m| m.0).unwrap_or(q.len());
        let segment = &q[*end..seg_end];
        let Ok(Some(hp)) = host_port.captures(segment) else { continue };
        let (Some(h), Some(p), Some(whole)) = (hp.get(1), hp.get(2), hp.get(0)) else { continue };
        let Ok(port) = p.as_str().parse::<u16>() else { continue };
        let mut spec = ConnectionSpec { protocol: protocol.clone(), host: h.as_str().trim_matches(['`', '*']).to_string(), port, username: String::new(), password: String::new() };
        if protocol == "ssh" {
            let tail = &segment[whole.end()..];
            let token = Regex::new(r"[^\s|`*]+").unwrap();
            let values: Vec<String> = token
                .find_iter(tail)
                .flatten()
                .map(|m| m.as_str().trim_matches(['(', ')', '[', ']', '{', '}', ':', ',', ';', '.']).to_string())
                .filter(|v| !v.is_empty() && !["username", "password", "none"].contains(&v.to_lowercase().as_str()))
                .collect();
            if values.len() >= 2 {
                spec.username = values[0].clone();
                spec.password = values[1].clone();
            } else if values.len() == 1 {
                spec.username = values[0].clone();
            }
        }
        specs.push(spec);
    }
    specs
}

pub fn is_multi_terminal_setup_request(q: &str, specs: &[ConnectionSpec]) -> bool {
    re(r"\b(?:launch|open|create|start|connect)\b").is_match(q).unwrap_or(false) && specs.len() >= 2
}

pub fn order_terminal_specs(q: &str, specs: Vec<ConnectionSpec>) -> Vec<ConnectionSpec> {
    let r = re(r"\bterminal\s*1\b.{0,40}?\b(ssh|telnet)\b|\b1\s+with\s+(ssh|telnet)\b");
    if let Ok(Some(c)) = r.captures(q) {
        let proto = c.get(1).or_else(|| c.get(2)).map(|m| m.as_str().to_lowercase()).unwrap_or_default();
        if let Some(pos) = specs.iter().position(|s| s.protocol == proto) {
            let mut out = specs.clone();
            let first = out.remove(pos);
            out.insert(0, first);
            return out;
        }
    }
    specs
}

/// Read tools are free; mutating tools only when this turn asks for them.
pub fn tools_for_request(q: &str, auto_send: bool) -> Vec<Value> {
    let mut allowed: Vec<&str> = vec!["list_terminals", "read_terminal"];
    if !is_advice_only_request(q) {
        let specs = parse_terminal_connection_specs(q);
        let launch = re(r"\b(?:launch|open|create|start)\b").is_match(q).unwrap_or(false);
        if parse_create_terminal_request(q).is_some() || (specs.len() >= 2 && launch) {
            allowed.push("create_terminal");
        }
        if parse_rename_request(q).is_some() {
            allowed.push("rename_terminal");
        }
        if auto_send || is_explicit_terminal_execution_request(q) || re(r"\b(?:print|type|write|send|enter)\b.{0,60}\b(?:terminals?|those|them)\b").is_match(q).unwrap_or(false) {
            allowed.push("send_terminal");
        }
        if !specs.is_empty() && re(r"\b(?:connect|launch|open|start)\b").is_match(q).unwrap_or(false) {
            allowed.push("connect_terminal");
        }
        if re(r"\b(?:upload|put)\b").is_match(q).unwrap_or(false) {
            allowed.push("sftp_upload");
        }
        if re(r"\b(?:download|get|retrieve)\b").is_match(q).unwrap_or(false) {
            allowed.push("sftp_download");
        }
    }
    app_tool_definitions().into_iter().filter(|t| t["function"]["name"].as_str().map(|n| allowed.contains(&n)).unwrap_or(false)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rename_and_create() {
        assert_eq!(parse_rename_request("rename the current Terminal 1 name to Terminal 2"), Some(("Terminal 1".into(), "Terminal 2".into())));
        assert_eq!(parse_create_terminal_request("launch a new terminal and give name aby"), Some("aby".into()));
        assert_eq!(parse_create_terminal_request("hello"), None);
    }

    #[test]
    fn advice_vs_action() {
        assert!(is_advice_only_request("How do I rename a tab?"));
        assert!(!is_advice_only_request("run ls in terminal 1"));
        assert!(is_explicit_terminal_execution_request("run ls in terminal 1"));
        assert!(!is_explicit_terminal_execution_request("what should I run in the terminal"));
    }

    #[test]
    fn connection_specs() {
        let s = parse_terminal_connection_specs("open terminal 1 with ssh 10.0.0.5:22 admin secret and terminal 2 with telnet 10.0.0.6 23 then print hello");
        assert_eq!(s.len(), 2);
        assert_eq!((s[0].protocol.as_str(), s[0].host.as_str(), s[0].port, s[0].username.as_str(), s[0].password.as_str()), ("ssh", "10.0.0.5", 22, "admin", "secret"));
        assert_eq!(s[1].protocol, "telnet");
        assert!(is_multi_terminal_setup_request("open terminal 1 ssh a 22 and terminal 2 telnet b 23", &s));
    }

    #[test]
    fn tool_gating() {
        let names = |q: &str, auto: bool| -> Vec<String> { tools_for_request(q, auto).iter().map(|t| t["function"]["name"].as_str().unwrap().to_string()).collect() };
        assert_eq!(names("How do I connect?", true), vec!["list_terminals", "read_terminal"]);
        assert!(names("check the uptime", true).contains(&"send_terminal".to_string()));
        assert!(!names("check the uptime", false).contains(&"send_terminal".to_string()));
        assert!(names("create a terminal named x", false).contains(&"create_terminal".to_string()));
    }
}
