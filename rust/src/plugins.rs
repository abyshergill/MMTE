//! Plugin discovery. Plugins are Python files (see plugins/example_hello.py); they are loaded by
//! the Python bridge (prelude.py), which reports their menu actions and script functions back.

use crate::config;
use crate::script::ScriptHost;
use crate::session::{Hub, Session};
use std::path::PathBuf;
use std::sync::Arc;

#[derive(Clone, Debug, Default)]
pub struct PluginInfo {
    pub name: String,
    pub path: PathBuf,
    pub version: String,
    pub description: String,
    pub loaded: bool,
    pub error: String,
    pub actions: Vec<(String, Option<String>)>,
    #[allow(dead_code)]
    pub functions: Vec<String>,
    pub hooks: usize,
}

pub const TEMPLATE: &str = r#""""My plugin - describe what it does here."""

PLUGIN_NAME = "My Plugin"
PLUGIN_VERSION = "1.0"
PLUGIN_DESCRIPTION = "Example plugin created from the template."


def register(api):
    # 1) A menu entry under Plugins > My Plugin
    def say_hello():
        api.status("Hello from my plugin!")
        api.send("echo hello")          # types the command into the current terminal

    api.add_menu_action("Say hello", say_hello)

    # 2) A function you can call from Python scripts: hello("world")
    api.add_script_function("hello", lambda name="world": f"hello {name}")
"#;

/// Folders searched for plugins: next to the program, the project's `plugins/`, and the user folder.
pub fn plugin_dirs() -> Vec<PathBuf> {
    let mut dirs: Vec<PathBuf> = Vec::new();
    let mut add = |p: PathBuf| {
        let c = std::fs::canonicalize(&p).unwrap_or(p.clone());
        if !dirs.iter().any(|d| std::fs::canonicalize(d).unwrap_or(d.clone()) == c) {
            dirs.push(p);
        }
    };
    if let Ok(exe) = std::env::current_exe() {
        if let Some(d) = exe.parent() {
            add(d.join("plugins"));
            // cargo run: target/debug/mmte -> project root is three levels up
            if let Some(root) = d.parent().and_then(|p| p.parent()).and_then(|p| p.parent()) {
                add(root.join("plugins"));
                if let Some(repo) = root.parent() {
                    add(repo.join("plugins"));
                }
            }
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        add(cwd.join("plugins"));
        if let Some(p) = cwd.parent() {
            add(p.join("plugins"));
        }
    }
    add(config::plugin_user_dir());
    dirs.into_iter().filter(|d| d.is_dir() || *d == config::plugin_user_dir()).collect()
}

pub fn load(hub: &Arc<Hub>, dirs: &[PathBuf]) -> Vec<PluginInfo> {
    let dummy = Session::new(0, "plugins", false);
    let host = ScriptHost::new(hub.clone(), dummy, dirs.to_vec());
    if host.run_python(&["plugins"]).is_err() {
        return Vec::new();
    }
    let result = host.result.lock().clone();
    let Some(serde_json::Value::Array(list)) = result else { return Vec::new() };
    list.iter()
        .map(|p| PluginInfo {
            name: p["name"].as_str().unwrap_or("").into(),
            path: PathBuf::from(p["path"].as_str().unwrap_or("")),
            version: p["version"].as_str().unwrap_or("").into(),
            description: p["description"].as_str().unwrap_or("").into(),
            loaded: p["loaded"].as_bool().unwrap_or(false),
            error: p["error"].as_str().unwrap_or("").into(),
            actions: p["actions"].as_array().map(|a| a.iter().map(|x| (x["text"].as_str().unwrap_or("").to_string(), x["shortcut"].as_str().map(|s| s.to_string()))).collect()).unwrap_or_default(),
            hooks: p["hooks"].as_u64().unwrap_or(0) as usize,
            functions: p["functions"].as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect()).unwrap_or_default(),
        })
        .collect()
}

/// Run one menu action of a plugin on `session`.
pub fn run_action(hub: &Arc<Hub>, session: Arc<Session>, dirs: &[PathBuf], plugin: &PluginInfo, index: usize) -> Result<(), String> {
    let host = ScriptHost::new(hub.clone(), session, dirs.to_vec());
    host.run_python(&["action", &plugin.path.to_string_lossy(), &index.to_string()]).map_err(|e| e.to_string())
}

/// Background Python process that feeds terminal output to plugins' `on_output` hooks.
pub struct HookDaemon {
    child: Arc<parking_lot::Mutex<std::process::Child>>,
    stdin: Arc<parking_lot::Mutex<std::process::ChildStdin>>,
}

impl HookDaemon {
    pub fn start(hub: &Arc<Hub>, dirs: &[PathBuf]) -> Option<Self> {
        use std::io::{BufRead, BufReader, Write};
        use std::process::{Command, Stdio};
        let py = crate::script::find_python()?;
        let prelude = crate::script::prelude_path().ok()?;
        let mut child = Command::new(py)
            .arg("-u")
            .arg(&prelude)
            .arg("hooks")
            .env("MMTE_PLUGIN_DIRS", std::env::join_paths(dirs).unwrap_or_default())
            .env("PYTHONIOENCODING", "utf-8")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .ok()?;
        let stdin = Arc::new(parking_lot::Mutex::new(child.stdin.take()?));
        let stdout = child.stdout.take()?;
        let host = Arc::new(ScriptHost::new(hub.clone(), hub.active_session().unwrap_or_else(|| Session::new(0, "plugins", false)), dirs.to_vec()));
        let (tx, rx) = crossbeam_channel::bounded::<crate::session::OutputEvent>(2000);
        // reader: answers the plugin's requests (send, status, ...)
        {
            let (host, hub, stdin, tx) = (host.clone(), hub.clone(), stdin.clone(), tx.clone());
            std::thread::spawn(move || {
                for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                    let Ok(v) = serde_json::from_str::<serde_json::Value>(&line) else { continue };
                    let op = v["op"].as_str().unwrap_or("").to_string();
                    if op == "hooks_ready" {
                        if v["count"].as_u64().unwrap_or(0) > 0 {
                            crate::session::set_output_hook(Some(tx.clone()));
                        }
                        continue;
                    }
                    if op == "error" || op == "result" {
                        continue;
                    }
                    if let Some(s) = hub.active_session() {
                        host.set_cur(s);
                    }
                    let reply = host.handle_op(&op, &v, std::path::Path::new("."));
                    let mut w = stdin.lock();
                    if writeln!(w, "{reply}").and_then(|_| w.flush()).is_err() {
                        break;
                    }
                }
                crate::session::set_output_hook(None);
            });
        }
        // forwarder: terminal output -> plugin
        {
            let stdin = stdin.clone();
            std::thread::spawn(move || {
                while let Ok((session, label, connected, text)) = rx.recv() {
                    let ev = serde_json::json!({"event": "output", "session": session, "label": label, "connected": connected, "text": text});
                    let mut w = stdin.lock();
                    if writeln!(w, "{ev}").and_then(|_| w.flush()).is_err() {
                        break;
                    }
                }
            });
        }
        Some(Self { child: Arc::new(parking_lot::Mutex::new(child)), stdin })
    }

    pub fn stop(&self) {
        use std::io::Write;
        crate::session::set_output_hook(None);
        let _ = writeln!(self.stdin.lock(), "{{\"event\": \"quit\"}}");
        let _ = self.child.lock().kill();
    }
}

pub fn create_from_template(name: &str) -> Result<PathBuf, String> {
    let safe: String = name.trim().chars().map(|c| if c.is_alphanumeric() || c == '_' { c } else { '_' }).collect::<String>().trim_matches('_').to_lowercase();
    let safe = if safe.is_empty() { "my_plugin".to_string() } else { safe };
    let dir = config::plugin_user_dir();
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let target = dir.join(format!("{safe}.py"));
    if target.exists() {
        return Err(format!("{} already exists", target.display()));
    }
    std::fs::write(&target, TEMPLATE).map_err(|e| e.to_string())?;
    Ok(target)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn loads_good_and_bad_plugins() {
        if crate::script::find_python().is_none() {
            return;
        }
        let dir = std::env::temp_dir().join(format!("mmte_plug_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("good.py"), "PLUGIN_NAME='Good'\ndef register(api):\n    api.add_menu_action('Hi', lambda: api.status('x'))\n    api.add_script_function('twice', lambda x: x*2)\n").unwrap();
        std::fs::write(dir.join("bad.py"), "def register(api):\n    raise RuntimeError('boom')\n").unwrap();
        let (hub, _rx) = Hub::new();
        let list = load(&hub, &[dir.clone()]);
        assert_eq!(list.len(), 2);
        let good = list.iter().find(|p| p.name == "Good").unwrap();
        assert!(good.loaded);
        assert_eq!(good.actions[0].0, "Hi");
        assert!(good.functions.contains(&"twice".to_string()));
        let bad = list.iter().find(|p| p.path.ends_with("bad.py")).unwrap();
        assert!(!bad.loaded && bad.error.contains("boom"));
        // run the action
        let s = hub.new_session(None);
        run_action(&hub, s, &[dir.clone()], good, 0).unwrap();
        assert_eq!(hub.status.lock().as_str(), "x");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn output_hooks_receive_terminal_text() {
        if crate::script::find_python().is_none() {
            return;
        }
        let dir = std::env::temp_dir().join(format!("mmte_hook_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("watch.py"), "def register(api):\n    def on_text(session, text):\n        if 'ALARM-7' in text:\n            api.status('alarm on ' + session.name)\n    api.on_output(on_text)\n").unwrap();
        let (hub, _rx) = Hub::new();
        let s = hub.new_session(Some("Rack 1"));
        let daemon = HookDaemon::start(&hub, &[dir.clone()]).expect("daemon");
        let mut ok = false;
        for _ in 0..80 {
            s.local("temperature ALARM-7 raised\r\n");
            std::thread::sleep(std::time::Duration::from_millis(100));
            if hub.status.lock().contains("alarm on Rack 1") {
                ok = true;
                break;
            }
        }
        daemon.stop();
        assert!(ok, "status was {:?}", hub.status.lock());
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn template_loads() {
        assert!(TEMPLATE.contains("def register(api)"));
    }
}
