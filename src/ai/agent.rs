//! Backend-independent chat loop with app-tool execution (port of core/llm/agent.py).

use super::backend::{self, ChatResult, LlmError};
use super::tools::{app_tool_definitions, extract_json_tool_call, tool_names, tool_prompt};
use super::{resolve_provider, Config};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

pub type ToolHandler<'a> = dyn FnMut(&str, &Value) -> String + 'a;

pub fn context_budget_chars(cfg: &Config) -> usize {
    let spec = resolve_provider(cfg);
    let window = match spec.backend {
        "gguf" => cfg.get_i64("n_ctx", 4096),
        "ollama" => cfg.get_i64("context_length", 8192),
        _ => return 600_000,
    };
    let reserve = cfg.get_i64("max_tokens", 2048);
    ((window - reserve).max(1000) as usize * 3).max(3000)
}

/// Drop the oldest non-system turns until the prompt fits; system messages and the last message stay.
pub fn trim_history(messages: &[Value], budget: usize) -> Vec<Value> {
    let size = |m: &Value| m["content"].as_str().map(|s| s.chars().count()).unwrap_or(0) + 16;
    let mut kept: Vec<Value> = messages.to_vec();
    let mut total: usize = kept.iter().map(size).sum();
    if total <= budget {
        return kept;
    }
    let mut i = 0;
    while total > budget && i + 1 < kept.len() {
        if kept[i]["role"] == "system" {
            i += 1;
            continue;
        }
        total -= size(&kept[i]);
        kept.remove(i);
    }
    kept
}

// --- <think> handling ----------------------------------------------------------------------

pub fn strip_think_blocks(text: &str) -> String {
    let re = fancy_regex::Regex::new(r"(?is)<think>.*?</think>\s*").unwrap();
    let t = re.replace_all(text, "").into_owned();
    match t.to_lowercase().rfind("<think>") {
        Some(i) => t[..i].to_string(),
        None => t,
    }
}

#[derive(Default)]
pub struct ThinkFilter {
    buf: String,
    inside: bool,
}

impl ThinkFilter {
    const OPEN: &'static str = "<think>";
    const CLOSE: &'static str = "</think>";

    pub fn feed(&mut self, chunk: &str) -> String {
        self.buf.push_str(chunk);
        let mut out = String::new();
        loop {
            let lowered = self.buf.to_lowercase();
            if self.inside {
                match lowered.find(Self::CLOSE) {
                    None => {
                        let keep = Self::CLOSE.len() - 1;
                        if self.buf.len() > keep {
                            let mut at = self.buf.len() - keep;
                            while !self.buf.is_char_boundary(at) {
                                at += 1;
                            }
                            self.buf = self.buf[at..].to_string();
                        }
                        break;
                    }
                    Some(end) => {
                        self.buf = self.buf[end + Self::CLOSE.len()..].trim_start().to_string();
                        self.inside = false;
                    }
                }
            } else {
                match lowered.find(Self::OPEN) {
                    None => {
                        let keep = partial_suffix(&lowered, Self::OPEN);
                        let mut emit_to = self.buf.len() - keep;
                        while !self.buf.is_char_boundary(emit_to) {
                            emit_to -= 1;
                        }
                        out.push_str(&self.buf[..emit_to]);
                        self.buf = self.buf[emit_to..].to_string();
                        break;
                    }
                    Some(start) => {
                        out.push_str(&self.buf[..start]);
                        self.buf = self.buf[start + Self::OPEN.len()..].to_string();
                        self.inside = true;
                    }
                }
            }
        }
        out
    }

    pub fn flush(&mut self) -> String {
        let tail = if self.inside { String::new() } else { std::mem::take(&mut self.buf) };
        self.buf.clear();
        self.inside = false;
        tail
    }
}

fn partial_suffix(text: &str, token: &str) -> usize {
    for size in (1..=(token.len() - 1).min(text.len())).rev() {
        if text.is_char_boundary(text.len() - size) && token.starts_with(&text[text.len() - size..]) {
            return size;
        }
    }
    0
}

/// Streams visible text to the UI while holding back anything that looks like a JSON tool call.
struct LiveEmitter<'a> {
    on_chunk: &'a mut dyn FnMut(&str),
    filter: ThinkFilter,
    pending: String,
    streaming: Option<bool>,
}

impl<'a> LiveEmitter<'a> {
    fn new(on_chunk: &'a mut dyn FnMut(&str), always: bool) -> Self {
        Self { on_chunk, filter: ThinkFilter::default(), pending: String::new(), streaming: if always { Some(true) } else { None } }
    }

    fn feed(&mut self, chunk: &str) {
        let visible = self.filter.feed(chunk);
        if visible.is_empty() {
            return;
        }
        match self.streaming {
            None => {
                self.pending.push_str(&visible);
                let stripped = self.pending.trim_start().to_string();
                if stripped.is_empty() {
                    return;
                }
                let s = !(stripped.starts_with('{') || stripped.starts_with("```") || stripped.starts_with('['));
                self.streaming = Some(s);
                if s {
                    let text = std::mem::take(&mut self.pending);
                    (self.on_chunk)(&text);
                }
            }
            Some(true) => (self.on_chunk)(&visible),
            Some(false) => {}
        }
    }

    fn finish(&mut self, final_text: &str) {
        let tail = self.filter.flush();
        match self.streaming {
            Some(true) => {
                if !tail.is_empty() {
                    (self.on_chunk)(&tail);
                }
            }
            _ => {
                if !final_text.is_empty() {
                    (self.on_chunk)(final_text);
                }
            }
        }
    }
}

/// Plain streamed chat (no tools). Returns the full visible reply.
pub fn stream_plain(cfg: &Config, messages: &[Value], cancel: &Arc<AtomicBool>, on_chunk: &mut dyn FnMut(&str)) -> Result<String, LlmError> {
    let prompt = trim_history(messages, context_budget_chars(cfg));
    let mut emitter = LiveEmitter::new(on_chunk, true);
    let result = backend::stream(cfg, &prompt, None, cancel, &mut |c| emitter.feed(c))?;
    let text = strip_think_blocks(&result.content);
    emitter.finish(&text);
    Ok(text)
}

fn with_tool_prompt(mut conv: Vec<Value>, tools: &[Value]) -> Vec<Value> {
    let addendum = tool_prompt(tools);
    for m in conv.iter_mut() {
        if m["role"] == "system" {
            let existing = m["content"].as_str().unwrap_or("").to_string();
            m["content"] = json!(format!("{existing}\n\n{addendum}").trim().to_string());
            return conv;
        }
    }
    let mut out = vec![json!({"role": "system", "content": addendum})];
    out.extend(conv);
    out
}

/// Chat with the configured model, executing only the supplied app-native tools.
pub fn run_chat(cfg: &Config, messages: &[Value], cancel: &Arc<AtomicBool>, on_chunk: &mut dyn FnMut(&str), handler: &mut ToolHandler<'_>, tools: Option<Vec<Value>>, max_rounds: usize) -> Result<String, LlmError> {
    let mode = {
        let m = cfg.get_str("tool_mode");
        if m.is_empty() { "auto".to_string() } else { m }
    };
    let mut available: Vec<Value> = if mode == "off" { Vec::new() } else { tools.unwrap_or_else(app_tool_definitions) };
    let use_native = mode == "native" || (mode == "auto" && backend::native_tools(cfg));
    let mut names = tool_names(&available);
    let mut conversation = trim_history(messages, context_budget_chars(cfg));
    if !available.is_empty() && !use_native {
        conversation = with_tool_prompt(conversation, &available);
    }
    let mut completed_fallback: HashMap<String, String> = HashMap::new();

    for _ in 0..=max_rounds {
        if cancel.load(Ordering::SeqCst) {
            return Ok(String::new());
        }
        let mut emitter = LiveEmitter::new(on_chunk, false);
        let wire: Option<&[Value]> = if use_native && mode != "off" && !available.is_empty() { Some(&available) } else { None };
        let result: ChatResult = backend::stream(cfg, &conversation, wire, cancel, &mut |c| emitter.feed(c))?;
        if cancel.load(Ordering::SeqCst) {
            return Ok(String::new());
        }
        let content = strip_think_blocks(&result.content);
        let mut tool_calls: Vec<Value> = if use_native { result.tool_calls.clone() } else { Vec::new() };
        let mut json_fallback: Option<Value> = None;
        if tool_calls.is_empty() {
            json_fallback = extract_json_tool_call(&content, &names);
            if let Some(c) = &json_fallback {
                tool_calls = vec![c.clone()];
            }
        }
        if tool_calls.is_empty() {
            let text = if content.is_empty() { "No output generated.".to_string() } else { content.clone() };
            emitter.finish(&text);
            if content.is_empty() {
                drop(emitter);
                on_chunk(&text);
            }
            return Ok(text);
        }
        drop(emitter);

        if let Some(call) = json_fallback {
            let name = call["name"].as_str().unwrap_or("").to_string();
            let args = if call["arguments"].is_object() { call["arguments"].clone() } else { json!({}) };
            let key = json!([name, args]).to_string();
            let repeated = completed_fallback.contains_key(&key);
            let tool_result = if repeated {
                format!("This exact app tool call already ran. Its actual result was:\n{}", completed_fallback[&key])
            } else {
                let r = handler(&name, &args);
                completed_fallback.insert(key, r.clone());
                r
            };
            conversation.push(json!({"role": "assistant", "content": content}));
            conversation.push(json!({"role": "user", "content": format!(
                "The application {} {} with the requested values. Actual application result:\n{}\n\nIf this completes the user's request, give a concise, truthful summary. If another distinct app operation is needed to finish the request, call the appropriate next tool. Do not repeat an app operation that already succeeded.",
                if repeated { "already executed" } else { "has now executed" }, name, tool_result)}));
            if repeated {
                available.clear();
                names.clear();
            }
            continue;
        }

        let calls_json: Vec<Value> = tool_calls.iter().enumerate().map(|(i, c)| json!({"id": c["id"].as_str().filter(|s| !s.is_empty()).map(|s| s.to_string()).unwrap_or(format!("call_{i}")), "name": c["name"], "arguments": c["arguments"]})).collect();
        conversation.push(json!({"role": "assistant", "content": content, "tool_calls": calls_json}));
        for (i, call) in tool_calls.iter().enumerate() {
            if cancel.load(Ordering::SeqCst) {
                return Ok(String::new());
            }
            let name = call["name"].as_str().unwrap_or("").to_string();
            let args = match &call["arguments"] {
                Value::String(s) => serde_json::from_str::<Value>(s).unwrap_or(json!({})),
                v => v.clone(),
            };
            let result = if !names.contains(&name) || !args.is_object() { format!("Error: invalid app tool call {name:?}.") } else { handler(&name, &args) };
            let id = call["id"].as_str().filter(|s| !s.is_empty()).map(|s| s.to_string()).unwrap_or(format!("call_{i}"));
            conversation.push(json!({"role": "tool", "tool_call_id": id, "name": name, "content": result}));
        }
    }
    let msg = "The model exceeded the application tool-call limit without completing a response.".to_string();
    on_chunk(&msg);
    Ok(msg)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn think_filter_hides_reasoning() {
        let mut f = ThinkFilter::default();
        let mut out = String::new();
        for chunk in ["Hel", "lo <th", "ink>secret", " stuff</thi", "nk> world"] {
            out.push_str(&f.feed(chunk));
        }
        out.push_str(&f.flush());
        assert_eq!(out, "Hello world");
        assert_eq!(strip_think_blocks("a<think>x</think> b"), "ab");
        assert_eq!(strip_think_blocks("a<think>cut"), "a");
    }

    #[test]
    fn history_trimming() {
        let msgs = vec![json!({"role": "system", "content": "S"}), json!({"role": "user", "content": "a".repeat(100)}), json!({"role": "assistant", "content": "b".repeat(100)}), json!({"role": "user", "content": "last"})];
        let t = trim_history(&msgs, 150);
        assert_eq!(t.first().unwrap()["role"], "system");
        assert_eq!(t.last().unwrap()["content"], "last");
        assert!(t.len() < 4);
    }
}
