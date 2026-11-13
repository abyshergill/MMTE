//! Tool schemas exposed to the model and helpers for prompt-based tool calling.

use serde_json::{json, Value};
use std::collections::HashSet;

fn tool(name: &str, description: &str, properties: Value, required: &[&str]) -> Value {
    json!({"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required}}})
}

pub fn app_tool_definitions() -> Vec<Value> {
    vec![
        tool("list_terminals", "List terminal tabs currently open inside Modern Multi Terminal. Use this to resolve names like the current tab before acting.", json!({}), &[]),
        tool("create_terminal", "Create and select a new terminal tab inside Modern Multi Terminal. This creates an application tab, not an OS shell window.", json!({"name": {"type": "string", "description": "Name for the new app terminal tab."}}), &["name"]),
        tool("rename_terminal", "Rename an existing terminal tab inside Modern Multi Terminal.", json!({"terminal_name": {"type": "string", "description": "Existing tab name, or 'current' for the selected tab."}, "new_name": {"type": "string", "description": "New tab name."}}), &["terminal_name", "new_name"]),
        tool("connect_terminal", "Connect an existing app terminal tab to a remote host using SSH or Telnet. SSH requires username/password when password authentication is used.", json!({"terminal_name": {"type": "string"}, "protocol": {"type": "string", "enum": ["ssh", "telnet"]}, "host": {"type": "string"}, "port": {"type": "integer"}, "username": {"type": "string"}, "password": {"type": "string"}}), &["terminal_name", "protocol", "host", "port"]),
        tool("send_terminal", "Send literal text or a command to an already-connected app terminal, then return the terminal output. Never use shell commands to control app tabs; use the dedicated app tools.", json!({"terminal_name": {"type": "string", "description": "Connected app tab name, 'current' for the selected tab, 'all' for every connected tab, or several names separated by commas."}, "text": {"type": "string", "description": "Exact text or command to send to the remote/device terminal (one command per call)."}}), &["terminal_name", "text"]),
        tool("read_terminal", "Read recent scrollback from an app terminal, usually to analyze logs.", json!({"terminal_name": {"type": "string", "description": "App tab name, or 'current'."}}), &["terminal_name"]),
        tool("sftp_upload", "Upload a user-specified local file to a remote path over the selected connected SSH app session. Do not use for Telnet or serial.", json!({"terminal_name": {"type": "string"}, "local_path": {"type": "string"}, "remote_path": {"type": "string"}}), &["terminal_name", "local_path", "remote_path"]),
        tool("sftp_download", "Download a remote file over the selected connected SSH app session to a user-specified local path. Do not use for Telnet or serial.", json!({"terminal_name": {"type": "string"}, "remote_path": {"type": "string"}, "local_path": {"type": "string"}}), &["terminal_name", "remote_path", "local_path"]),
    ]
}

pub fn tool_names(tools: &[Value]) -> HashSet<String> {
    tools.iter().filter(|t| t["type"] == "function").filter_map(|t| t["function"]["name"].as_str().map(|s| s.to_string())).collect()
}

fn tool_key(v: &Value) -> String {
    let name = v.get("name").and_then(|x| x.as_str()).or_else(|| v.get("tool").and_then(|x| x.as_str())).or_else(|| v.get("function").and_then(|f| f.get("name")).and_then(|x| x.as_str()));
    name.unwrap_or("").to_string()
}

fn arguments_key(v: &Value) -> Value {
    for k in ["arguments", "parameters", "args"] {
        if let Some(a) = v.get(k) {
            return a.clone();
        }
    }
    if let Some(f) = v.get("function") {
        for k in ["arguments", "parameters"] {
            if let Some(a) = f.get(k) {
                return a.clone();
            }
        }
    }
    json!({})
}

/// Recover a tool call the model wrote as JSON text (fenced or bare). Only known names are accepted.
pub fn extract_json_tool_call(text: &str, names: &HashSet<String>) -> Option<Value> {
    if text.is_empty() || names.is_empty() {
        return None;
    }
    let mut candidates: Vec<Value> = Vec::new();
    let bytes: Vec<(usize, char)> = text.char_indices().collect();
    for (i, c) in &bytes {
        if *c != '{' {
            continue;
        }
        let mut de = serde_json::Deserializer::from_str(&text[*i..]).into_iter::<Value>();
        if let Some(Ok(v)) = de.next() {
            if v.is_object() && names.contains(&tool_key(&v)) {
                candidates.push(v);
            }
        }
    }
    for v in candidates {
        let name = tool_key(&v);
        let args = arguments_key(&v);
        if names.contains(&name) && args.is_object() {
            return Some(json!({"id": "json-tool-call", "name": name, "arguments": args}));
        }
    }
    None
}

pub fn tool_prompt(tools: &[Value]) -> String {
    let mut lines = Vec::new();
    for t in tools {
        let f = &t["function"];
        let props = f["parameters"]["properties"].as_object().cloned().unwrap_or_default();
        let required: HashSet<String> = f["parameters"]["required"].as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect()).unwrap_or_default();
        let args: Vec<String> = props.iter().map(|(k, v)| format!("{k}{}: {}", if required.contains(k) { "*" } else { "" }, v["type"].as_str().unwrap_or("string"))).collect();
        lines.push(format!("- {}({}): {}", f["name"].as_str().unwrap_or(""), args.join(", "), f["description"].as_str().unwrap_or("")));
    }
    format!(
        "You can operate the application with these tools (* = required argument):\n{}\n\nTo use a tool, reply with ONLY one JSON object and nothing else, for example {{\"name\": \"list_terminals\", \"arguments\": {{}}}}. Use real values from the user's request, never placeholders. After the application returns the tool result, answer the user in plain language. If no tool is needed, answer normally without JSON.",
        lines.join("\n")
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_fallback() {
        let names = tool_names(&app_tool_definitions());
        let call = extract_json_tool_call("```json\n{\"name\": \"create_terminal\", \"arguments\": {\"name\": \"aby\"}}\n```", &names).unwrap();
        assert_eq!(call["name"], "create_terminal");
        assert_eq!(call["arguments"]["name"], "aby");
        assert!(extract_json_tool_call("{\"name\": \"rm_rf\", \"arguments\": {}}", &names).is_none());
        assert!(extract_json_tool_call("just prose", &names).is_none());
        let alt = extract_json_tool_call("{\"tool\": \"list_terminals\", \"parameters\": {}}", &names).unwrap();
        assert_eq!(alt["name"], "list_terminals");
    }

    #[test]
    fn prompt_lists_tools() {
        assert!(tool_prompt(&app_tool_definitions()).contains("send_terminal(terminal_name*: string, text*: string)"));
    }
}
