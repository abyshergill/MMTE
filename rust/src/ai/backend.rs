//! Streaming chat backends: OpenAI-compatible, Ollama and Anthropic.
//!
//! Conversation messages use one canonical JSON shape (same as the Python version):
//! `{"role", "content", "tool_calls": [{"id","name","arguments"}], "tool_call_id", "name"}`.

use super::{resolve_provider, Config};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

#[derive(Debug, Clone, Default)]
pub struct ChatResult {
    pub content: String,
    pub tool_calls: Vec<Value>,
}

#[derive(Debug)]
pub struct LlmError {
    pub message: String,
    pub hint: String,
}

impl std::fmt::Display for LlmError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if self.hint.is_empty() {
            write!(f, "{}", self.message)
        } else {
            write!(f, "{}\n{}", self.message, self.hint)
        }
    }
}

fn err(message: impl Into<String>, hint: impl Into<String>) -> LlmError {
    LlmError { message: message.into(), hint: hint.into() }
}

pub fn native_tools(cfg: &Config) -> bool {
    resolve_provider(cfg).backend != "gguf"
}

// ----------------------------------------------------------------------------------------
// Endpoints
// ----------------------------------------------------------------------------------------

fn trim_url(s: &str) -> String {
    s.trim().trim_end_matches('/').to_string()
}

pub fn openai_url(cfg: &Config) -> String {
    let base = trim_url(&cfg.get_str("endpoint_url"));
    if base.ends_with("/chat/completions") { base } else { format!("{base}/chat/completions") }
}

pub fn ollama_root(endpoint: &str) -> String {
    let mut base = trim_url(endpoint);
    for suffix in ["/api/chat", "/api/generate", "/v1/chat/completions", "/v1", "/api"] {
        if let Some(b) = base.strip_suffix(suffix) {
            base = b.to_string();
            break;
        }
    }
    base
}

pub fn anthropic_url(cfg: &Config) -> String {
    let base = trim_url(&cfg.get_str("endpoint_url"));
    if base.ends_with("/messages") {
        base
    } else if base.ends_with("/v1") {
        format!("{base}/messages")
    } else {
        format!("{base}/v1/messages")
    }
}

// ----------------------------------------------------------------------------------------
// Message conversion
// ----------------------------------------------------------------------------------------

fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string()
}

fn to_openai(messages: &[Value]) -> Vec<Value> {
    messages
        .iter()
        .map(|m| match m["role"].as_str().unwrap_or("") {
            "assistant" if m["tool_calls"].as_array().map(|a| !a.is_empty()).unwrap_or(false) => {
                let calls: Vec<Value> = m["tool_calls"].as_array().unwrap().iter().map(|c| json!({"id": s(c, "id"), "type": "function", "function": {"name": s(c, "name"), "arguments": c["arguments"].to_string()}})).collect();
                let content = s(m, "content");
                json!({"role": "assistant", "content": if content.is_empty() { Value::Null } else { json!(content) }, "tool_calls": calls})
            }
            "tool" => json!({"role": "tool", "tool_call_id": s(m, "tool_call_id"), "content": s(m, "content")}),
            role => json!({"role": role, "content": s(m, "content")}),
        })
        .collect()
}

fn to_ollama(messages: &[Value]) -> Vec<Value> {
    messages
        .iter()
        .map(|m| match m["role"].as_str().unwrap_or("") {
            "assistant" if m["tool_calls"].as_array().map(|a| !a.is_empty()).unwrap_or(false) => {
                let calls: Vec<Value> = m["tool_calls"].as_array().unwrap().iter().map(|c| json!({"function": {"name": s(c, "name"), "arguments": c["arguments"]}})).collect();
                json!({"role": "assistant", "content": s(m, "content"), "tool_calls": calls})
            }
            "tool" => json!({"role": "tool", "content": s(m, "content"), "tool_name": s(m, "name")}),
            role => json!({"role": role, "content": s(m, "content")}),
        })
        .collect()
}

fn to_anthropic(messages: &[Value]) -> (String, Vec<Value>) {
    let mut system = Vec::new();
    let mut out: Vec<Value> = Vec::new();
    for m in messages {
        match m["role"].as_str().unwrap_or("") {
            "system" => system.push(s(m, "content")),
            "assistant" => {
                let mut blocks: Vec<Value> = Vec::new();
                let text = s(m, "content");
                if !text.is_empty() {
                    blocks.push(json!({"type": "text", "text": text}));
                }
                for c in m["tool_calls"].as_array().cloned().unwrap_or_default() {
                    blocks.push(json!({"type": "tool_use", "id": s(&c, "id"), "name": s(&c, "name"), "input": c["arguments"]}));
                }
                if blocks.is_empty() {
                    blocks.push(json!({"type": "text", "text": "..."}));
                }
                out.push(json!({"role": "assistant", "content": blocks}));
            }
            "tool" => {
                let block = json!({"type": "tool_result", "tool_use_id": s(m, "tool_call_id"), "content": s(m, "content")});
                if let Some(last) = out.last_mut() {
                    if last["role"] == "user" && last["content"].as_array().map(|a| a.iter().all(|b| b["type"] == "tool_result")).unwrap_or(false) {
                        last["content"].as_array_mut().unwrap().push(block);
                        continue;
                    }
                }
                out.push(json!({"role": "user", "content": [block]}));
            }
            _ => out.push(json!({"role": "user", "content": s(m, "content")})),
        }
    }
    (system.join("\n\n"), out)
}

// ----------------------------------------------------------------------------------------
// HTTP streaming
// ----------------------------------------------------------------------------------------

enum Line {
    Text(String),
    End,
    Error(String),
}

fn http_error(status: u16, body: &str) -> LlmError {
    let mut detail = String::new();
    if let Ok(v) = serde_json::from_str::<Value>(body) {
        let e = v.get("error").unwrap_or(&v);
        detail = e.get("message").or_else(|| e.get("detail")).and_then(|x| x.as_str()).map(|x| x.to_string()).unwrap_or_else(|| if e.is_string() { e.as_str().unwrap().to_string() } else { String::new() });
    }
    if detail.is_empty() {
        detail = body.trim().chars().take(300).collect();
    }
    let hint = match status {
        401 | 403 => "Check the API key (or the matching environment variable) in AI > LLM Settings.",
        404 => "Check the endpoint URL and that the model name is installed/available for this provider.",
        429 => "Rate limit or quota reached; wait a moment or choose another model/provider.",
        _ => "",
    };
    err(if detail.is_empty() { format!("HTTP {status}") } else { format!("HTTP {status}: {detail}") }, hint)
}

fn client() -> Result<reqwest::blocking::Client, LlmError> {
    reqwest::blocking::Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(3600))
        .build()
        .map_err(|e| err(e.to_string(), ""))
}

/// POST and stream the response body line by line. Calls `on_line` for every non-empty line.
fn post_lines(url: &str, payload: &Value, headers: &[(&str, String)], cfg: &Config, cancel: &Arc<AtomicBool>, mut on_line: impl FnMut(&str) -> Result<(), LlmError>) -> Result<(), LlmError> {
    let client = client()?;
    let mut attempt = 0;
    let resp = loop {
        let mut rb = client.post(url).json(payload);
        for (k, v) in headers {
            rb = rb.header(*k, v.as_str());
        }
        match rb.send() {
            Ok(r) => {
                let status = r.status().as_u16();
                if [429, 500, 502, 503, 504].contains(&status) && attempt < 2 && !cancel.load(Ordering::SeqCst) {
                    attempt += 1;
                    std::thread::sleep(Duration::from_secs(2u64.pow(attempt)));
                    continue;
                }
                if !r.status().is_success() {
                    let body = r.text().unwrap_or_default();
                    return Err(http_error(status, &body));
                }
                break r;
            }
            Err(e) => {
                if attempt < 2 && !cancel.load(Ordering::SeqCst) {
                    attempt += 1;
                    std::thread::sleep(Duration::from_secs(2u64.pow(attempt)));
                    continue;
                }
                return Err(err(format!("Could not reach the model server at {url}: {e}"), "Is the server running, and is the endpoint URL correct?"));
            }
        }
    };
    // read on a helper thread so a stalled stream can still be cancelled
    let (tx, rx) = crossbeam_channel::unbounded::<Line>();
    std::thread::spawn(move || {
        let reader = BufReader::new(resp);
        for line in reader.lines() {
            match line {
                Ok(l) => {
                    if tx.send(Line::Text(l)).is_err() {
                        return;
                    }
                }
                Err(e) => {
                    let _ = tx.send(Line::Error(e.to_string()));
                    return;
                }
            }
        }
        let _ = tx.send(Line::End);
    });
    let idle_limit = Duration::from_secs(cfg.get_i64("request_timeout", 120).max(5) as u64);
    let mut last = std::time::Instant::now();
    loop {
        if cancel.load(Ordering::SeqCst) {
            return Ok(());
        }
        if last.elapsed() > idle_limit {
            return Err(err(format!("The model server stopped responding for {} s.", idle_limit.as_secs()), "Increase the request timeout in AI > LLM Settings, or check the server."));
        }
        match rx.recv_timeout(Duration::from_millis(100)) {
            Ok(Line::Text(l)) => {
                last = std::time::Instant::now();
                let t = l.trim();
                if !t.is_empty() {
                    on_line(t)?;
                }
            }
            Ok(Line::End) => return Ok(()),
            Ok(Line::Error(e)) => return Err(err(format!("Connection to the model server was interrupted: {e}"), "")),
            Err(crossbeam_channel::RecvTimeoutError::Timeout) => {}
            Err(crossbeam_channel::RecvTimeoutError::Disconnected) => return Ok(()),
        }
    }
}

fn parse_args(v: &Value) -> Value {
    match v {
        Value::String(s) => serde_json::from_str::<Value>(s).ok().filter(|x| x.is_object()).unwrap_or(json!({})),
        Value::Object(_) => v.clone(),
        _ => json!({}),
    }
}

/// Stream one assistant turn. `on_chunk` receives text deltas as they arrive.
pub fn stream(cfg: &Config, messages: &[Value], tools: Option<&[Value]>, cancel: &Arc<AtomicBool>, on_chunk: &mut dyn FnMut(&str)) -> Result<ChatResult, LlmError> {
    let spec = resolve_provider(cfg);
    let model = cfg.get_str("model_name");
    let temperature = cfg.get_f64("temperature", 0.7);
    let max_tokens = cfg.get_i64("max_tokens", 2048);
    let mut content = String::new();
    match spec.backend {
        "openai" => {
            let mut payload = json!({"model": if model.is_empty() { "local-model".to_string() } else { model }, "messages": to_openai(messages), "stream": true, "temperature": temperature, "max_tokens": max_tokens});
            if let Some(t) = tools {
                if !t.is_empty() {
                    payload["tools"] = json!(t);
                }
            }
            let mut headers = vec![("Content-Type", "application/json".to_string()), ("Accept", "text/event-stream".to_string())];
            let key = cfg.api_key();
            if !key.is_empty() {
                headers.push(("Authorization", format!("Bearer {key}")));
            }
            // accumulate tool calls by index
            let mut acc: Vec<(String, String, String)> = Vec::new();
            post_lines(&openai_url(cfg), &payload, &headers, cfg, cancel, |line| {
                let Some(data) = line.strip_prefix("data:") else { return Ok(()) };
                let data = data.trim();
                if data == "[DONE]" {
                    return Ok(());
                }
                let Ok(v) = serde_json::from_str::<Value>(data) else { return Ok(()) };
                if let Some(e) = v.get("error") {
                    return Err(err(e["message"].as_str().unwrap_or(&e.to_string()).to_string(), ""));
                }
                let Some(choice) = v["choices"].get(0) else { return Ok(()) };
                let delta = &choice["delta"];
                if let Some(t) = delta["content"].as_str() {
                    if !t.is_empty() {
                        content.push_str(t);
                        on_chunk(t);
                    }
                }
                for tc in delta["tool_calls"].as_array().cloned().unwrap_or_default() {
                    let idx = tc["index"].as_u64().unwrap_or(0) as usize;
                    while acc.len() <= idx {
                        acc.push(Default::default());
                    }
                    if let Some(id) = tc["id"].as_str() {
                        acc[idx].0 = id.to_string();
                    }
                    if let Some(n) = tc["function"]["name"].as_str() {
                        acc[idx].1.push_str(n);
                    }
                    if let Some(a) = tc["function"]["arguments"].as_str() {
                        acc[idx].2.push_str(a);
                    }
                }
                Ok(())
            })?;
            let calls = acc.into_iter().filter(|c| !c.1.is_empty()).enumerate().map(|(i, c)| json!({"id": if c.0.is_empty() { format!("call_{i}") } else { c.0 }, "name": c.1, "arguments": parse_args(&json!(c.2))})).collect();
            Ok(ChatResult { content, tool_calls: calls })
        }
        "ollama" => {
            let mut payload = json!({"model": if model.is_empty() { "llama3.1".to_string() } else { model }, "messages": to_ollama(messages), "stream": true,
                "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": cfg.get_i64("context_length", 8192)}});
            if let Some(t) = tools {
                if !t.is_empty() {
                    payload["tools"] = json!(t);
                }
            }
            let mut headers = vec![("Content-Type", "application/json".to_string())];
            let key = cfg.api_key();
            if !key.is_empty() {
                headers.push(("Authorization", format!("Bearer {key}")));
            }
            let mut calls: Vec<Value> = Vec::new();
            post_lines(&format!("{}/api/chat", ollama_root(&cfg.get_str("endpoint_url"))), &payload, &headers, cfg, cancel, |line| {
                let Ok(v) = serde_json::from_str::<Value>(line) else { return Ok(()) };
                if let Some(e) = v.get("error") {
                    return Err(err(e.as_str().map(|x| x.to_string()).unwrap_or_else(|| e.to_string()), ""));
                }
                let m = &v["message"];
                if let Some(t) = m["content"].as_str() {
                    if !t.is_empty() {
                        content.push_str(t);
                        on_chunk(t);
                    }
                }
                for c in m["tool_calls"].as_array().cloned().unwrap_or_default() {
                    if let Some(name) = c["function"]["name"].as_str() {
                        calls.push(json!({"id": format!("call_{}", calls.len()), "name": name, "arguments": parse_args(&c["function"]["arguments"])}));
                    }
                }
                Ok(())
            })?;
            Ok(ChatResult { content, tool_calls: calls })
        }
        "anthropic" => {
            let (system, msgs) = to_anthropic(messages);
            let mut payload = json!({"model": if model.is_empty() { "claude-3-5-haiku-latest".to_string() } else { model }, "max_tokens": max_tokens, "temperature": temperature.min(1.0), "stream": true, "messages": msgs});
            if !system.is_empty() {
                payload["system"] = json!(system);
            }
            if let Some(t) = tools {
                if !t.is_empty() {
                    let at: Vec<Value> = t.iter().map(|x| json!({"name": x["function"]["name"], "description": x["function"]["description"], "input_schema": x["function"]["parameters"]})).collect();
                    payload["tools"] = json!(at);
                }
            }
            let headers = vec![("Content-Type", "application/json".to_string()), ("x-api-key", cfg.api_key()), ("anthropic-version", "2023-06-01".to_string())];
            let mut blocks: Vec<(String, String, String)> = Vec::new(); // id, name, json
            let mut current: Option<usize> = None;
            post_lines(&anthropic_url(cfg), &payload, &headers, cfg, cancel, |line| {
                let Some(data) = line.strip_prefix("data:") else { return Ok(()) };
                let Ok(v) = serde_json::from_str::<Value>(data.trim()) else { return Ok(()) };
                match v["type"].as_str().unwrap_or("") {
                    "error" => return Err(err(v["error"]["message"].as_str().unwrap_or("Anthropic error").to_string(), "")),
                    "content_block_start" => {
                        let b = &v["content_block"];
                        if b["type"] == "tool_use" {
                            blocks.push((s(b, "id"), s(b, "name"), String::new()));
                            current = Some(blocks.len() - 1);
                        } else {
                            current = None;
                        }
                    }
                    "content_block_delta" => {
                        let d = &v["delta"];
                        match d["type"].as_str().unwrap_or("") {
                            "text_delta" => {
                                if let Some(t) = d["text"].as_str() {
                                    content.push_str(t);
                                    on_chunk(t);
                                }
                            }
                            "input_json_delta" => {
                                if let (Some(i), Some(p)) = (current, d["partial_json"].as_str()) {
                                    blocks[i].2.push_str(p);
                                }
                            }
                            _ => {}
                        }
                    }
                    _ => {}
                }
                Ok(())
            })?;
            let calls = blocks.into_iter().map(|b| json!({"id": b.0, "name": b.1, "arguments": if b.2.is_empty() { json!({}) } else { parse_args(&json!(b.2)) }})).collect();
            Ok(ChatResult { content, tool_calls: calls })
        }
        _ => Err(err(
            "Local .gguf files are not run in-process by this build.",
            "Serve the model with Ollama or llama.cpp's server (llama-server -m model.gguf) and pick \"Ollama (Local)\" or \"llama.cpp Server (Local)\" in AI > LLM Settings.",
        )),
    }
}

/// Model names offered by the provider (for the settings dialog).
pub fn list_models(cfg: &Config) -> Result<Vec<String>, LlmError> {
    let spec = resolve_provider(cfg);
    let c = client()?;
    let get = |url: String, headers: Vec<(&str, String)>| -> Result<Value, LlmError> {
        let mut rb = c.get(&url).timeout(Duration::from_secs(10));
        for (k, v) in headers {
            rb = rb.header(k, v);
        }
        let r = rb.send().map_err(|e| err(format!("Could not reach {url}: {e}"), "Is the server running?"))?;
        let status = r.status().as_u16();
        let body = r.text().unwrap_or_default();
        if status >= 400 {
            return Err(http_error(status, &body));
        }
        serde_json::from_str(&body).map_err(|_| err(format!("Unexpected (non-JSON) reply from {url}"), ""))
    };
    let key = cfg.api_key();
    let mut names: Vec<String> = match spec.backend {
        "ollama" => {
            let v = get(format!("{}/api/tags", ollama_root(&cfg.get_str("endpoint_url"))), vec![])?;
            v["models"].as_array().map(|a| a.iter().filter_map(|m| m["name"].as_str().map(|x| x.to_string())).collect()).unwrap_or_default()
        }
        "anthropic" => {
            let base = trim_url(&cfg.get_str("endpoint_url"));
            let base = base.strip_suffix("/v1/messages").or_else(|| base.strip_suffix("/v1")).unwrap_or(&base).to_string();
            let v = get(format!("{base}/v1/models"), vec![("x-api-key", key), ("anthropic-version", "2023-06-01".into())])?;
            v["data"].as_array().map(|a| a.iter().filter_map(|m| m["id"].as_str().map(|x| x.to_string())).collect()).unwrap_or_default()
        }
        "openai" => {
            let base = trim_url(&cfg.get_str("endpoint_url"));
            let base = base.strip_suffix("/chat/completions").unwrap_or(&base).to_string();
            let mut h = vec![];
            if !key.is_empty() {
                h.push(("Authorization", format!("Bearer {key}")));
            }
            let v = get(format!("{base}/models"), h)?;
            v["data"].as_array().map(|a| a.iter().filter_map(|m| m["id"].as_str().map(|x| x.trim_start_matches("models/").to_string())).collect()).unwrap_or_default()
        }
        _ => return Err(err("Model listing is not available for this provider.", "")),
    };
    names.sort();
    names.dedup();
    Ok(names)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};

    /// Tiny HTTP server answering one request with a canned streaming body.
    fn serve(body: String, content_type: &'static str) -> u16 {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            if let Ok((mut s, _)) = listener.accept() {
                let mut buf = [0u8; 8192];
                let mut req = Vec::new();
                loop {
                    let n = s.read(&mut buf).unwrap_or(0);
                    req.extend_from_slice(&buf[..n]);
                    let text = String::from_utf8_lossy(&req).to_string();
                    if let Some(h) = text.find("\r\n\r\n") {
                        let len = text[..h].lines().find_map(|l| l.to_lowercase().strip_prefix("content-length:").map(|v| v.trim().parse::<usize>().unwrap_or(0))).unwrap_or(0);
                        if req.len() >= h + 4 + len {
                            break;
                        }
                    }
                    if n == 0 {
                        break;
                    }
                }
                let resp = format!("HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
                let _ = s.write_all(resp.as_bytes());
            }
        });
        port
    }

    fn cfg(provider: &str, port: u16, path: &str) -> Config {
        let mut c = Config::load_from(std::path::Path::new("/nonexistent"));
        c.set("provider", json!(provider));
        c.set("endpoint_url", json!(format!("http://127.0.0.1:{port}{path}")));
        c.set("model_name", json!("m"));
        c
    }

    #[test]
    fn openai_stream_with_tool_call() {
        let body = "data: {\"choices\":[{\"delta\":{\"content\":\"Hel\"}}]}\n\ndata: {\"choices\":[{\"delta\":{\"content\":\"lo\"}}]}\n\ndata: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"c1\",\"function\":{\"name\":\"list_terminals\",\"arguments\":\"{\"}}]}}]}\n\ndata: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"arguments\":\"}\"}}]}}]}\n\ndata: [DONE]\n\n".to_string();
        let port = serve(body, "text/event-stream");
        let c = cfg("Custom / Local HTTP", port, "/v1");
        let mut text = String::new();
        let r = stream(&c, &[json!({"role": "user", "content": "hi"})], None, &Arc::new(AtomicBool::new(false)), &mut |t| text.push_str(t)).unwrap();
        assert_eq!(text, "Hello");
        assert_eq!(r.content, "Hello");
        assert_eq!(r.tool_calls[0]["name"], "list_terminals");
        assert_eq!(r.tool_calls[0]["id"], "c1");
    }

    #[test]
    fn ollama_stream() {
        let body = "{\"message\":{\"content\":\"A\"}}\n{\"message\":{\"content\":\"B\",\"tool_calls\":[{\"function\":{\"name\":\"read_terminal\",\"arguments\":{\"terminal_name\":\"current\"}}}]}}\n{\"done\":true}\n".to_string();
        let port = serve(body, "application/x-ndjson");
        let c = cfg("Ollama (Local)", port, "");
        let r = stream(&c, &[json!({"role": "user", "content": "hi"})], None, &Arc::new(AtomicBool::new(false)), &mut |_| {}).unwrap();
        assert_eq!(r.content, "AB");
        assert_eq!(r.tool_calls[0]["arguments"]["terminal_name"], "current");
    }

    #[test]
    fn anthropic_stream_and_conversion() {
        let body = "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"content_block\":{\"type\":\"text\"}}\n\ndata: {\"type\":\"content_block_delta\",\"delta\":{\"type\":\"text_delta\",\"text\":\"Hi\"}}\n\ndata: {\"type\":\"content_block_start\",\"content_block\":{\"type\":\"tool_use\",\"id\":\"t1\",\"name\":\"list_terminals\"}}\n\ndata: {\"type\":\"content_block_delta\",\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{}\"}}\n\n".to_string();
        let port = serve(body, "text/event-stream");
        let mut c = cfg("Anthropic", port, "");
        c.set("api_key", json!("k"));
        let r = stream(&c, &[json!({"role": "system", "content": "s"}), json!({"role": "user", "content": "hi"})], None, &Arc::new(AtomicBool::new(false)), &mut |_| {}).unwrap();
        assert_eq!(r.content, "Hi");
        assert_eq!(r.tool_calls[0]["id"], "t1");
        let (sys, msgs) = to_anthropic(&[
            json!({"role": "system", "content": "S"}),
            json!({"role": "user", "content": "q"}),
            json!({"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "x", "arguments": {}}]}),
            json!({"role": "tool", "tool_call_id": "a", "name": "x", "content": "r1"}),
            json!({"role": "tool", "tool_call_id": "b", "name": "y", "content": "r2"}),
        ]);
        assert_eq!(sys, "S");
        assert_eq!(msgs.len(), 3);
        assert_eq!(msgs[2]["content"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn http_errors_are_actionable() {
        let e = http_error(401, "{\"error\":{\"message\":\"bad key\"}}");
        assert!(e.message.contains("bad key") && e.hint.contains("API key"));
    }

    #[test]
    fn ollama_root_strips_suffixes() {
        assert_eq!(ollama_root("http://h:11434/v1/"), "http://h:11434");
        assert_eq!(ollama_root("http://h:11434/api/chat"), "http://h:11434");
    }

    #[test]
    fn gguf_reports_guidance() {
        let c = cfg("Local GGUF File", 1, "");
        let e = stream(&c, &[], None, &Arc::new(AtomicBool::new(false)), &mut |_| {}).unwrap_err();
        assert!(e.hint.contains("llama-server"));
    }
}
