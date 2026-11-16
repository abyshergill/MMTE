//! Paths, JSON-backed settings and saved connection profiles.
//! Uses the same folder and file names as the Python version, so existing data keeps working.

use serde_json::{json, Map, Value};
use std::fs;
use std::path::PathBuf;

pub const APP_NAME: &str = "Modern Multi Terminal Emulator";

pub fn config_dir() -> PathBuf {
    let d = dirs::home_dir().unwrap_or_else(|| PathBuf::from(".")).join(".modern_multi_terminal_emulator");
    let _ = fs::create_dir_all(&d);
    // the folder holds API keys, logs and known host keys: owner only
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&d, fs::Permissions::from_mode(0o700));
    }
    d
}

/// Write a file that only the owner can read (created with mode 0600, replaced atomically).
pub fn write_private(path: &std::path::Path, data: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    if let Some(p) = path.parent() {
        fs::create_dir_all(p)?;
    }
    // unique per call: several threads may save different files (or the same one) at once
    static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let n = COUNTER.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let tmp = path.with_extension(format!("tmp{}_{n}", std::process::id()));
    let _ = fs::remove_file(&tmp);
    let mut opts = fs::OpenOptions::new();
    opts.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut f = opts.open(&tmp)?;
    f.write_all(data)?;
    f.sync_all().ok();
    drop(f);
    fs::rename(&tmp, path).inspect_err(|_| {
        let _ = fs::remove_file(&tmp);
    })
}
pub fn log_dir() -> PathBuf {
    config_dir().join("logs")
}
pub fn macro_dir() -> PathBuf {
    config_dir().join("macros")
}
pub fn plugin_user_dir() -> PathBuf {
    config_dir().join("plugins")
}

// ----------------------------------------------------------------------------------------
// Settings
// ----------------------------------------------------------------------------------------

pub struct Settings {
    path: PathBuf,
    data: Map<String, Value>,
}

impl Settings {
    pub fn load() -> Self {
        Self::load_from(config_dir().join("app_settings.json"))
    }

    pub fn load_from(path: PathBuf) -> Self {
        let mut data = Map::new();
        data.insert("syntax_highlighting".into(), json!(true));
        data.insert("intercept_editors".into(), json!(true));
        data.insert("editor_word_wrap".into(), json!(false));
        data.insert("editor_font_size".into(), json!(12));
        if let Ok(text) = fs::read_to_string(&path) {
            if let Ok(Value::Object(m)) = serde_json::from_str::<Value>(&text) {
                for (k, v) in m {
                    data.insert(k, v);
                }
            }
        }
        Self { path, data }
    }

    pub fn get_bool(&self, key: &str, default: bool) -> bool {
        self.data.get(key).and_then(|v| v.as_bool()).unwrap_or(default)
    }
    #[allow(dead_code)]
    pub fn get_f64(&self, key: &str, default: f64) -> f64 {
        self.data.get(key).and_then(|v| v.as_f64()).unwrap_or(default)
    }
    #[allow(dead_code)]
    pub fn get_str(&self, key: &str) -> Option<String> {
        self.data.get(key).and_then(|v| v.as_str()).map(|s| s.to_string())
    }
    #[allow(dead_code)]
    pub fn get(&self, key: &str) -> Option<&Value> {
        self.data.get(key)
    }
    pub fn set(&mut self, key: &str, value: Value) {
        self.data.insert(key.to_string(), value);
        let _ = write_private(&self.path, serde_json::to_string_pretty(&Value::Object(self.data.clone())).unwrap_or_default().as_bytes());
    }
}

// ----------------------------------------------------------------------------------------
// Profiles
// ----------------------------------------------------------------------------------------

pub struct ProfileStore {
    path: PathBuf,
    pub profiles: Map<String, Value>,
}

impl ProfileStore {
    pub fn load(path: PathBuf) -> Self {
        let mut profiles = Map::new();
        if let Ok(text) = fs::read_to_string(&path) {
            if let Ok(Value::Object(m)) = serde_json::from_str::<Value>(&text) {
                profiles = m;
            }
        }
        Self { path, profiles }
    }
    pub fn ssh() -> Self {
        Self::load(config_dir().join("ssh_profiles.json"))
    }
    pub fn telnet() -> Self {
        Self::load(config_dir().join("telnet_profiles.json"))
    }
    fn save(&self) {
        let _ = write_private(&self.path, serde_json::to_string_pretty(&Value::Object(self.profiles.clone())).unwrap_or_default().as_bytes());
    }
    pub fn upsert(&mut self, name: &str, data: Value) {
        self.profiles.insert(name.to_string(), data);
        self.save();
    }
    pub fn delete(&mut self, name: &str) {
        self.profiles.shift_remove_compat(name);
        self.save();
    }
    pub fn names(&self) -> Vec<String> {
        self.profiles.keys().cloned().collect()
    }
}

trait RemoveCompat {
    fn shift_remove_compat(&mut self, key: &str);
}
impl RemoveCompat for Map<String, Value> {
    fn shift_remove_compat(&mut self, key: &str) {
        self.remove(key);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    #[cfg(unix)]
    fn private_files_are_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!("mmte_priv_{}", std::process::id()));
        let p = dir.join("secret.json");
        write_private(&p, b"one").unwrap();
        write_private(&p, b"two").unwrap();
        assert_eq!(fs::read(&p).unwrap(), b"two");
        assert_eq!(fs::metadata(&p).unwrap().permissions().mode() & 0o777, 0o600);
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn settings_roundtrip() {
        let dir = std::env::temp_dir().join(format!("mmte_cfg_{}", std::process::id()));
        let p = dir.join("s.json");
        let mut s = Settings::load_from(p.clone());
        assert!(s.get_bool("syntax_highlighting", false));
        s.set("syntax_highlighting", json!(false));
        let s2 = Settings::load_from(p);
        assert!(!s2.get_bool("syntax_highlighting", true));
        let _ = fs::remove_dir_all(dir);
    }
}
