//! RAG memories: chunking, embeddings (Ollama / OpenAI-compatible) and BM25 fallback.
//! Uses the same `rag_memories.json` format as the Python version.

use super::knowledge::{APPLICATION_EMBEDDING_MODEL, APPLICATION_MEMORY_NAME, CHUNKS};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::path::PathBuf;
use std::time::Duration;

pub const AVAILABLE_EMBEDDING_MODELS: &[&str] = &["openai/text-embedding-3-small", "openai/text-embedding-3-large", "ollama/nomic-embed-text", "ollama/mxbai-embed-large", "ollama/qwen3-embedding:0.6b"];

const CHUNK_CHARS: usize = 1200;
const CHUNK_OVERLAP: usize = 150;

pub fn rag_path() -> PathBuf {
    crate::config::config_dir().join("rag_memories.json")
}

pub fn chunk_text(text: &str, max_chars: usize, overlap: usize) -> Vec<String> {
    let chars: Vec<char> = text.trim().chars().collect();
    if chars.len() <= max_chars {
        return if chars.is_empty() { vec![] } else { vec![chars.iter().collect()] };
    }
    let mut chunks = Vec::new();
    let mut start = 0;
    while start < chars.len() {
        let mut end = (start + max_chars).min(chars.len());
        if end < chars.len() {
            let window: String = chars[start..end].iter().collect();
            let cut = ["\n\n", "\n", ". "].iter().filter_map(|p| window.rfind(p)).max();
            if let Some(c) = cut {
                let cut_chars = window[..c].chars().count();
                if cut_chars > max_chars / 2 {
                    end = start + cut_chars + 1;
                }
            }
        }
        let piece: String = chars[start..end].iter().collect::<String>().trim().to_string();
        if !piece.is_empty() {
            chunks.push(piece);
        }
        if end >= chars.len() {
            break;
        }
        start = end.saturating_sub(overlap).max(start + 1);
    }
    chunks
}

fn tokens(text: &str) -> Vec<String> {
    const STOP: &[&str] = &["the", "and", "for", "are", "but", "not", "you", "all", "can", "had", "her", "was", "one", "our", "out", "has", "his", "how", "its", "may", "new", "now", "see", "two", "way", "who", "did", "get", "let", "say", "she", "too", "use", "with", "this", "that", "from", "have", "what", "when", "where", "which", "does", "into", "your", "about"];
    text.to_lowercase().split(|c: char| !(c.is_ascii_alphanumeric() || c == '_')).filter(|t| t.len() > 2 && !STOP.contains(t)).map(|t| t.to_string()).collect()
}

/// BM25 ranking, the offline fallback when no embedding server is reachable.
pub fn lexical_scores(query: &str, entries: &[String]) -> Vec<f64> {
    let q: std::collections::HashSet<String> = tokens(query).into_iter().collect();
    if q.is_empty() || entries.is_empty() {
        return vec![0.0; entries.len()];
    }
    let docs: Vec<HashMap<String, usize>> = entries.iter().map(|e| {
        let mut m = HashMap::new();
        for t in tokens(e) {
            *m.entry(t).or_insert(0) += 1;
        }
        m
    }).collect();
    let lengths: Vec<usize> = docs.iter().map(|d| d.values().sum()).collect();
    let avg = (lengths.iter().sum::<usize>() as f64 / lengths.len() as f64).max(1.0);
    let n = entries.len() as f64;
    docs.iter().zip(lengths.iter()).map(|(doc, &len)| {
        let mut score = 0.0;
        for term in &q {
            let freq = *doc.get(term).unwrap_or(&0) as f64;
            if freq == 0.0 {
                continue;
            }
            let containing = docs.iter().filter(|d| d.contains_key(term)).count() as f64;
            let idf = (1.0 + (n - containing + 0.5) / (containing + 0.5)).ln();
            score += idf * (freq * 2.2) / (freq + 1.2 * (0.25 + 0.75 * len as f64 / avg));
        }
        score
    }).collect()
}

fn cosine(a: &[f64], b: &[f64]) -> f64 {
    if a.is_empty() || a.len() != b.len() {
        return 0.0;
    }
    let dot: f64 = a.iter().zip(b).map(|(x, y)| x * y).sum();
    let (na, nb) = (a.iter().map(|x| x * x).sum::<f64>().sqrt(), b.iter().map(|x| x * x).sum::<f64>().sqrt());
    if na == 0.0 || nb == 0.0 { 0.0 } else { dot / (na * nb) }
}

fn split_model(name: &str) -> (String, String) {
    let m = if name.trim().is_empty() { APPLICATION_EMBEDDING_MODEL } else { name.trim() };
    if let Some((p, rest)) = m.split_once('/') {
        if ["ollama", "openai", "local", "gguf"].contains(&p.to_lowercase().as_str()) {
            return (p.to_lowercase(), rest.to_string());
        }
    }
    ("ollama".into(), m.to_string())
}

pub fn embed_texts(texts: &[String], endpoint: &str, model_name: &str, api_key: &str) -> Result<Vec<Vec<f64>>, String> {
    if texts.is_empty() {
        return Ok(vec![]);
    }
    let (kind, model) = split_model(model_name);
    let client = reqwest::blocking::Client::builder().connect_timeout(Duration::from_secs(5)).timeout(Duration::from_secs(90)).build().map_err(|e| e.to_string())?;
    let post = |url: &str, body: Value, key: Option<&str>| -> Result<Value, String> {
        let mut rb = client.post(url).json(&body);
        if let Some(k) = key.filter(|k| !k.is_empty()) {
            rb = rb.header("Authorization", format!("Bearer {k}"));
        }
        let r = rb.send().map_err(|e| e.to_string())?;
        if !r.status().is_success() {
            return Err(format!("HTTP {}", r.status().as_u16()));
        }
        r.json::<Value>().map_err(|e| e.to_string())
    };
    match kind.as_str() {
        "ollama" => {
            let root = super::backend::ollama_root(if endpoint.is_empty() { "http://localhost:11434" } else { endpoint });
            let v = post(&format!("{root}/api/embed"), json!({"model": model, "input": texts}), None)?;
            let arr = v["embeddings"].as_array().ok_or("Ollama returned an unexpected embedding payload")?;
            if arr.len() != texts.len() {
                return Err("Ollama returned an unexpected embedding count".into());
            }
            Ok(arr.iter().map(|e| e.as_array().map(|x| x.iter().filter_map(|n| n.as_f64()).collect()).unwrap_or_default()).collect())
        }
        "openai" => {
            let key = if api_key.is_empty() { std::env::var("OPENAI_API_KEY").unwrap_or_default() } else { api_key.to_string() };
            let ep = endpoint.trim().trim_end_matches('/');
            let url = if ep.is_empty() || ep.contains(":11434") { "https://api.openai.com/v1/embeddings".to_string() } else { format!("{}/embeddings", ep.trim_end_matches("/chat/completions").trim_end_matches("/embeddings")) };
            let v = post(&url, json!({"model": model, "input": texts}), Some(&key))?;
            let mut items: Vec<Value> = v["data"].as_array().cloned().unwrap_or_default();
            items.sort_by_key(|i| i["index"].as_u64().unwrap_or(0));
            if items.len() != texts.len() {
                return Err("Embedding server returned an unexpected embedding count".into());
            }
            Ok(items.iter().map(|i| i["embedding"].as_array().map(|x| x.iter().filter_map(|n| n.as_f64()).collect()).unwrap_or_default()).collect())
        }
        other => Err(format!("{other} embeddings are not available in this build")),
    }
}

pub struct RagManager {
    path: PathBuf,
    pub memories: Map<String, Value>,
}

impl RagManager {
    pub fn load() -> Self {
        Self::load_from(rag_path())
    }

    pub fn load_from(path: PathBuf) -> Self {
        let mut memories = Map::new();
        if let Ok(t) = std::fs::read_to_string(&path) {
            if let Ok(Value::Object(m)) = serde_json::from_str::<Value>(&t) {
                memories = m;
            }
        }
        let mut me = Self { path, memories };
        me.ensure_application_memory();
        me
    }

    pub fn save(&self) {
        if let Some(p) = self.path.parent() {
            let _ = std::fs::create_dir_all(p);
        }
        let tmp = self.path.with_extension("tmp");
        if std::fs::write(&tmp, serde_json::to_string_pretty(&Value::Object(self.memories.clone())).unwrap_or_default()).is_ok() {
            let _ = std::fs::rename(&tmp, &self.path);
        }
    }

    pub fn ensure_application_memory(&mut self) {
        let mut m = self.memories.get(APPLICATION_MEMORY_NAME).and_then(|v| v.as_object()).cloned().unwrap_or_default();
        m.insert("name".into(), json!(APPLICATION_MEMORY_NAME));
        m.insert("embedding_model".into(), json!(APPLICATION_EMBEDDING_MODEL));
        m.insert("assigned_job".into(), json!("Application-only AI assistant engineer for Modern Multi Terminal"));
        m.insert("text_entries".into(), json!(CHUNKS));
        m.insert("file_sources".into(), json!(["built-in"]));
        m.insert("built_in".into(), json!(true));
        self.memories.insert(APPLICATION_MEMORY_NAME.into(), Value::Object(m));
        self.save();
    }

    pub fn list_names(&self) -> Vec<String> {
        let mut v: Vec<String> = self.memories.keys().cloned().collect();
        v.sort();
        v
    }

    pub fn create_or_update(&mut self, name: &str, embedding_model: &str, job: &str, new_text: &str, files: &[PathBuf]) -> Result<(), String> {
        let name = name.trim();
        if name == APPLICATION_MEMORY_NAME {
            return Err(format!("'{APPLICATION_MEMORY_NAME}' is the built-in application guide and cannot be modified."));
        }
        if name.is_empty() {
            return Err("Enter a memory name.".into());
        }
        let mut m = self.memories.get(name).and_then(|v| v.as_object()).cloned().unwrap_or_else(|| {
            let mut m = Map::new();
            m.insert("name".into(), json!(name));
            m.insert("text_entries".into(), json!([]));
            m.insert("file_sources".into(), json!([]));
            m
        });
        m.insert("embedding_model".into(), json!(embedding_model));
        m.insert("assigned_job".into(), json!(job));
        let mut entries: Vec<Value> = m["text_entries"].as_array().cloned().unwrap_or_default();
        let mut sources: Vec<Value> = m["file_sources"].as_array().cloned().unwrap_or_default();
        if !new_text.trim().is_empty() {
            entries.push(json!(new_text.trim()));
        }
        for f in files {
            let fs = f.to_string_lossy().to_string();
            if f.exists() && !sources.iter().any(|s| s == &json!(fs)) {
                let content = String::from_utf8_lossy(&std::fs::read(f).map_err(|e| e.to_string())?).into_owned();
                let mut pieces = chunk_text(&content, CHUNK_CHARS, CHUNK_OVERLAP);
                if pieces.is_empty() {
                    pieces.push(String::new());
                }
                let total = pieces.len();
                let fname = f.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
                for (i, p) in pieces.iter().enumerate() {
                    let suffix = if total > 1 { format!(" (part {}/{total})", i + 1) } else { String::new() };
                    entries.push(json!(format!("--- FILE: {fname}{suffix} ---\n{p}")));
                }
                sources.push(json!(fs));
            }
        }
        m.insert("text_entries".into(), Value::Array(entries));
        m.insert("file_sources".into(), Value::Array(sources));
        m.remove("embeddings");
        m.remove("embedding_hash");
        self.memories.insert(name.to_string(), Value::Object(m));
        self.save();
        Ok(())
    }

    pub fn delete(&mut self, name: &str) {
        if name != APPLICATION_MEMORY_NAME {
            self.memories.remove(name);
            self.save();
        }
    }

    fn content_hash(entries: &[String], model: &str) -> String {
        use std::hash::{Hash, Hasher};
        let mut h = std::collections::hash_map::DefaultHasher::new();
        model.hash(&mut h);
        entries.hash(&mut h);
        format!("{:016x}", h.finish())
    }

    /// Relevant entries for `query`; falls back to BM25 when embeddings are unavailable.
    pub fn retrieve_relevant(&mut self, name: &str, query: &str, endpoint: &str, api_key: &str, top_k: usize) -> (String, String) {
        let Some(mem) = self.memories.get(name).and_then(|v| v.as_object()).cloned() else { return (String::new(), "memory not found".into()) };
        let entries: Vec<String> = mem.get("text_entries").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|x| x.as_str()).filter(|s| !s.trim().is_empty()).map(|s| s.to_string()).collect()).unwrap_or_default();
        if entries.is_empty() {
            return (String::new(), "memory is empty".into());
        }
        let model = mem.get("embedding_model").and_then(|v| v.as_str()).unwrap_or(APPLICATION_EMBEDDING_MODEL).to_string();
        let hash = Self::content_hash(&entries, &model);
        let cached: Option<Vec<Vec<f64>>> = if mem.get("embedding_hash").and_then(|v| v.as_str()) == Some(hash.as_str()) {
            mem.get("embeddings").and_then(|v| v.as_array()).map(|a| a.iter().map(|e| e.as_array().map(|x| x.iter().filter_map(|n| n.as_f64()).collect()).unwrap_or_default()).collect())
        } else {
            None
        };
        let mut mode = format!("{} embeddings", split_model(&model).0);
        let scores: Vec<f64> = (|| -> Result<Vec<f64>, String> {
            let vectors = match cached.filter(|c| c.len() == entries.len()) {
                Some(c) => c,
                None => {
                    let v = embed_texts(&entries, endpoint, &model, api_key)?;
                    if let Some(m) = self.memories.get_mut(name).and_then(|v| v.as_object_mut()) {
                        m.insert("embeddings".into(), json!(v));
                        m.insert("embedding_hash".into(), json!(hash));
                        m.insert("embedding_dimensions".into(), json!(v.first().map(|x| x.len()).unwrap_or(0)));
                    }
                    self.save();
                    v
                }
            };
            let q = embed_texts(&[query.to_string()], endpoint, &model, api_key)?;
            Ok(vectors.iter().map(|v| cosine(&q[0], v)).collect())
        })()
        .unwrap_or_else(|e| {
            mode = format!("keyword fallback ({})", e.chars().take(40).collect::<String>());
            lexical_scores(query, &entries)
        });
        let mut ranked: Vec<(usize, f64)> = scores.into_iter().enumerate().collect();
        ranked.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        ranked.truncate(top_k.max(1));
        let mut relevant: Vec<(usize, f64)> = ranked.iter().copied().filter(|(_, s)| *s > 0.0).collect();
        if relevant.is_empty() {
            relevant = ranked.iter().copied().take(2).collect();
        }
        let ctx = relevant.iter().enumerate().map(|(i, (idx, s))| format!("[Knowledge {}; relevance={:.3}]\n{}", i + 1, s, entries[*idx])).collect::<Vec<_>>().join("\n\n");
        (ctx, mode)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chunking() {
        let text = "para one. ".repeat(300);
        let c = chunk_text(&text, 1200, 150);
        assert!(c.len() >= 2 && c.iter().all(|x| x.chars().count() <= 1200));
        assert!(chunk_text("   ", 10, 2).is_empty());
        assert_eq!(chunk_text("short", 100, 10), vec!["short".to_string()]);
    }

    #[test]
    fn bm25_prefers_matching_entry() {
        let entries = vec!["SSH uses libssh2 for interactive shells".to_string(), "Cooking pasta takes ten minutes".to_string()];
        let s = lexical_scores("how does ssh work", &entries);
        assert!(s[0] > s[1]);
    }

    #[test]
    fn retrieval_falls_back_offline() {
        let dir = std::env::temp_dir().join(format!("mmte_rag_{}", std::process::id()));
        let mut rag = RagManager::load_from(dir.join("rag.json"));
        rag.create_or_update("proj", "ollama/nomic-embed-text", "tester", "The device reboots with the command reload now", &[]).unwrap();
        // port 9 (discard) is not an embedding server -> keyword fallback
        let (ctx, mode) = rag.retrieve_relevant("proj", "how do I reboot the device", "http://127.0.0.1:9", "", 3);
        assert!(ctx.contains("reload now"), "{ctx}");
        assert!(mode.starts_with("keyword fallback"), "{mode}");
        assert!(rag.create_or_update(APPLICATION_MEMORY_NAME, "x", "y", "z", &[]).is_err());
        rag.delete("proj");
        assert!(!rag.memories.contains_key("proj"));
        let _ = std::fs::remove_dir_all(dir);
    }
}
