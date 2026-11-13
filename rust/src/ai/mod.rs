//! AI assistant core: providers, settings, streaming backends, agent loop, RAG, dispatch helpers.

pub mod agent;
pub mod backend;
pub mod chat;
pub mod dispatch;
pub mod intent;
pub mod knowledge;
pub mod rag;
pub mod redact;
pub mod tools;

use serde_json::{json, Map, Value};
use std::path::PathBuf;

pub struct ProviderSpec {
    pub key: &'static str,
    pub label: &'static str,
    pub backend: &'static str, // openai | ollama | anthropic | gguf
    pub endpoint: &'static str,
    pub model: &'static str,
    pub key_env: &'static str,
    pub needs_key: bool,
    pub local: bool,
}

const fn p(key: &'static str, label: &'static str, backend: &'static str, endpoint: &'static str, model: &'static str, key_env: &'static str, needs_key: bool, local: bool) -> ProviderSpec {
    ProviderSpec { key, label, backend, endpoint, model, key_env, needs_key, local }
}

pub const PROVIDERS: &[ProviderSpec] = &[
    p("openai", "OpenAI", "openai", "https://api.openai.com/v1", "gpt-4o-mini", "OPENAI_API_KEY", true, false),
    p("anthropic", "Anthropic", "anthropic", "https://api.anthropic.com", "claude-3-5-haiku-latest", "ANTHROPIC_API_KEY", true, false),
    p("gemini", "Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash", "GEMINI_API_KEY", true, false),
    p("groq", "Groq", "openai", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "GROQ_API_KEY", true, false),
    p("mistral", "Mistral", "openai", "https://api.mistral.ai/v1", "mistral-small-latest", "MISTRAL_API_KEY", true, false),
    p("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1", "deepseek-chat", "DEEPSEEK_API_KEY", true, false),
    p("xai", "xAI (Grok)", "openai", "https://api.x.ai/v1", "grok-3-mini", "XAI_API_KEY", true, false),
    p("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", "openai/gpt-4o-mini", "OPENROUTER_API_KEY", true, false),
    p("together", "Together AI", "openai", "https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo", "TOGETHER_API_KEY", true, false),
    p("ollama", "Ollama (Local)", "ollama", "http://localhost:11434", "llama3.1", "", false, true),
    p("lmstudio", "LM Studio (Local)", "openai", "http://localhost:1234/v1", "local-model", "", false, true),
    p("llamacpp", "llama.cpp Server (Local)", "openai", "http://localhost:8080/v1", "local-model", "", false, true),
    p("gguf", "Local GGUF File", "gguf", "", "", "", false, true),
    p("custom", "Custom / Local HTTP", "openai", "http://localhost:1234/v1", "local-model", "", false, true),
];

pub fn get_provider(name: &str) -> Option<&'static ProviderSpec> {
    let t = name.trim().to_lowercase();
    if t.is_empty() {
        return None;
    }
    PROVIDERS.iter().find(|p| p.key == t || p.label.to_lowercase() == t).or_else(|| {
        let alias = match t.as_str() {
            "claude" => "anthropic",
            "google" => "gemini",
            "llama.cpp" => "llamacpp",
            "local" => "custom",
            _ => return None,
        };
        PROVIDERS.iter().find(|p| p.key == alias)
    })
}

pub fn is_local_endpoint(url: &str) -> bool {
    let rest = url.split("://").nth(1).unwrap_or(url);
    let host = rest.split(['/', ':']).next().unwrap_or("").to_lowercase();
    if ["localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"].contains(&host.as_str()) || host.ends_with(".local") {
        return true;
    }
    let parts: Vec<&str> = host.split('.').collect();
    if parts.len() == 4 && parts.iter().all(|p| p.parse::<u8>().is_ok()) {
        let (a, b): (u8, u8) = (parts[0].parse().unwrap(), parts[1].parse().unwrap());
        return a == 10 || a == 127 || (a == 192 && b == 168) || (a == 172 && (16..=31).contains(&b));
    }
    false
}

/// Provider for a config, inferring one for legacy / partial configs.
pub fn resolve_provider(cfg: &Config) -> &'static ProviderSpec {
    if let Some(s) = get_provider(&cfg.get_str("provider")) {
        return s;
    }
    let ep = cfg.get_str("endpoint_url").to_lowercase();
    if ep.contains("11434") {
        return get_provider("ollama").unwrap();
    }
    if ep.contains("anthropic.com") {
        return get_provider("anthropic").unwrap();
    }
    if !cfg.get_str("gguf_path").is_empty() {
        return get_provider("gguf").unwrap();
    }
    get_provider("custom").unwrap()
}

pub fn is_cloud(cfg: &Config) -> bool {
    let spec = resolve_provider(cfg);
    !spec.local && !is_local_endpoint(&cfg.get_str("endpoint_url"))
}

// ----------------------------------------------------------------------------------------
// Config (JSON, same file/keys as the Python version)
// ----------------------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub struct Config(pub Map<String, Value>);

pub const TOOL_MODES: &[&str] = &["auto", "native", "prompt", "off"];

pub fn llm_config_path() -> PathBuf {
    crate::config::config_dir().join("llm_config.json")
}

fn defaults() -> Map<String, Value> {
    let v = json!({
        "provider": "Ollama (Local)", "api_key": "", "endpoint_url": "http://localhost:11434", "model_name": "llama3.1",
        "temperature": 0.7, "max_tokens": 2048, "gguf_path": "", "n_ctx": 4096, "n_gpu_layers": 0, "n_threads": 0,
        "chat_format": "", "context_length": 8192, "tool_mode": "auto", "request_timeout": 120,
        "embedding_endpoint_url": "http://localhost:11434", "redact_secrets": true, "provider_profiles": {},
        "llm_to_terminal": false
    });
    v.as_object().unwrap().clone()
}

impl Config {
    pub fn load() -> Self {
        Self::load_from(&llm_config_path())
    }

    pub fn load_from(path: &std::path::Path) -> Self {
        let mut cfg = Config(defaults());
        if let Ok(text) = std::fs::read_to_string(path) {
            if let Ok(Value::Object(m)) = serde_json::from_str::<Value>(&text) {
                for (k, v) in m {
                    if !v.is_null() {
                        cfg.0.insert(k, v);
                    }
                }
            }
        }
        cfg.sanitize();
        cfg
    }

    pub fn get_str(&self, k: &str) -> String {
        self.0.get(k).and_then(|v| v.as_str()).unwrap_or("").trim().to_string()
    }
    pub fn get_f64(&self, k: &str, d: f64) -> f64 {
        self.0.get(k).and_then(|v| v.as_f64().or_else(|| v.as_str().and_then(|s| s.parse().ok()))).unwrap_or(d)
    }
    pub fn get_i64(&self, k: &str, d: i64) -> i64 {
        self.get_f64(k, d as f64) as i64
    }
    pub fn get_bool(&self, k: &str, d: bool) -> bool {
        self.0.get(k).and_then(|v| v.as_bool()).unwrap_or(d)
    }
    pub fn set(&mut self, k: &str, v: Value) {
        self.0.insert(k.to_string(), v);
    }

    pub fn sanitize(&mut self) {
        let d = defaults();
        for (k, v) in &d {
            self.0.entry(k.clone()).or_insert_with(|| v.clone());
        }
        for k in ["provider", "api_key", "endpoint_url", "model_name", "gguf_path", "chat_format", "embedding_endpoint_url"] {
            let s = self.get_str(k);
            self.0.insert(k.into(), json!(s));
        }
        let clamp = |v: f64, lo: f64, hi: f64| v.max(lo).min(hi);
        let t = clamp(self.get_f64("temperature", 0.7), 0.0, 2.0);
        self.0.insert("temperature".into(), json!(t));
        for (k, d, lo, hi) in [("max_tokens", 2048.0, 16.0, 131072.0), ("n_ctx", 4096.0, 512.0, 1048576.0), ("n_gpu_layers", 0.0, -1.0, 1024.0), ("n_threads", 0.0, 0.0, 512.0), ("context_length", 8192.0, 512.0, 1048576.0), ("request_timeout", 120.0, 5.0, 3600.0)] {
            let v = clamp(self.get_f64(k, d), lo, hi) as i64;
            self.0.insert(k.into(), json!(v));
        }
        if !TOOL_MODES.contains(&self.get_str("tool_mode").as_str()) {
            self.0.insert("tool_mode".into(), json!("auto"));
        }
        if !self.0.get("provider_profiles").map(|v| v.is_object()).unwrap_or(false) {
            self.0.insert("provider_profiles".into(), json!({}));
        }
    }

    pub fn save(&mut self) {
        self.sanitize();
        if let Some(spec) = get_provider(&self.get_str("provider")) {
            let mut profiles = self.0.get("provider_profiles").and_then(|v| v.as_object()).cloned().unwrap_or_default();
            let mut prof = Map::new();
            for k in ["endpoint_url", "api_key", "model_name", "gguf_path"] {
                prof.insert(k.into(), json!(self.get_str(k)));
            }
            profiles.insert(spec.key.to_string(), Value::Object(prof));
            self.0.insert("provider_profiles".into(), Value::Object(profiles));
        }
        let path = llm_config_path();
        if let Some(p) = path.parent() {
            let _ = std::fs::create_dir_all(p);
        }
        let tmp = path.with_extension("tmp");
        if std::fs::write(&tmp, serde_json::to_string_pretty(&Value::Object(self.0.clone())).unwrap_or_default()).is_ok() {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600));
            }
            let _ = std::fs::rename(&tmp, &path);
        }
    }

    pub fn profile_for(&self, provider: &str) -> Map<String, Value> {
        let Some(spec) = get_provider(provider) else { return Map::new() };
        let mut saved = self.0.get("provider_profiles").and_then(|v| v.get(spec.key)).and_then(|v| v.as_object()).cloned().unwrap_or_default();
        saved.entry("endpoint_url").or_insert(json!(spec.endpoint));
        saved.entry("model_name").or_insert(json!(spec.model));
        saved.entry("api_key").or_insert(json!(""));
        saved.entry("gguf_path").or_insert(json!(""));
        saved
    }

    pub fn api_key(&self) -> String {
        let k = self.get_str("api_key");
        if !k.is_empty() {
            return k;
        }
        let spec = resolve_provider(self);
        if spec.key_env.is_empty() { String::new() } else { std::env::var(spec.key_env).unwrap_or_default().trim().to_string() }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn providers_and_endpoints() {
        assert_eq!(get_provider("Ollama (Local)").unwrap().key, "ollama");
        assert_eq!(get_provider("claude").unwrap().key, "anthropic");
        assert!(is_local_endpoint("http://192.168.1.5:8080/v1"));
        assert!(!is_local_endpoint("https://api.openai.com/v1"));
        let mut c = Config(defaults());
        assert!(!is_cloud(&c));
        c.set("provider", json!("OpenAI"));
        c.set("endpoint_url", json!("https://api.openai.com/v1"));
        assert!(is_cloud(&c));
    }

    #[test]
    fn sanitize_clamps() {
        let mut c = Config(defaults());
        c.set("temperature", json!(9));
        c.set("max_tokens", json!("abc"));
        c.set("tool_mode", json!("weird"));
        c.sanitize();
        assert_eq!(c.get_f64("temperature", 0.0), 2.0);
        assert_eq!(c.get_i64("max_tokens", 0), 2048);
        assert_eq!(c.get_str("tool_mode"), "auto");
    }
}
