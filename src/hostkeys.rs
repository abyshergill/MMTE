//! SSH host key verification.
//!
//! Before any password or key is offered, the server's host key is checked against
//! `~/.ssh/known_hosts` and the app's own store (`~/.modern_multi_terminal_emulator/known_hosts`).
//! An unknown host must be approved once (trust on first use); a host whose key *changed* is
//! refused unless the user explicitly accepts the new key.

use parking_lot::Mutex;
use std::path::PathBuf;

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Question {
    /// First time this host is seen.
    Unknown,
    /// The host presented a different key than the one on record (possible interception).
    Changed,
}

/// `(host label, key type, SHA256 fingerprint, question) -> trust?`
pub type Prompt = Box<dyn Fn(&str, &str, &str, Question) -> bool + Send + Sync>;

static PROMPT: Mutex<Option<Prompt>> = Mutex::new(None);
static STORE: Mutex<Option<PathBuf>> = Mutex::new(None);
/// Serialises read-modify-write of the store: two connections verifying at the same time must
/// not overwrite each other's entry.
static STORE_LOCK: Mutex<()> = Mutex::new(());

#[cfg(test)]
thread_local! {
    /// Per-thread answer used by tests instead of the global prompt.
    pub static TEST_ANSWER: std::cell::Cell<Option<bool>> = const { std::cell::Cell::new(None) };
}

/// Install the question shown to the user. Without one, unknown hosts are accepted and recorded
/// (like OpenSSH's `accept-new`) and changed keys are always refused.
pub fn set_prompt(prompt: Prompt) {
    *PROMPT.lock() = Some(prompt);
}

pub fn store_path() -> PathBuf {
    if let Some(p) = STORE.lock().clone() {
        return p;
    }
    #[cfg(test)]
    {
        std::env::temp_dir().join(format!("mmte_known_hosts_{}", std::process::id()))
    }
    #[cfg(not(test))]
    {
        crate::config::config_dir().join("known_hosts")
    }
}

#[allow(dead_code)]
pub fn set_store_path(path: PathBuf) {
    *STORE.lock() = Some(path);
}

fn label(host: &str, port: u16) -> String {
    format!("[{}]:{}", host.to_lowercase(), port)
}

pub fn base64(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        out.push(T[(n >> 18) as usize & 63] as char);
        out.push(T[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 { T[(n >> 6) as usize & 63] as char } else { '=' });
        out.push(if chunk.len() > 2 { T[n as usize & 63] as char } else { '=' });
    }
    out
}

/// Entries of the app store: `[host]:port keytype base64key`.
fn load_store() -> Vec<(String, String, String)> {
    std::fs::read_to_string(store_path())
        .unwrap_or_default()
        .lines()
        .filter_map(|l| {
            let mut it = l.split_whitespace();
            Some((it.next()?.to_string(), it.next()?.to_string(), it.next()?.to_string()))
        })
        .collect()
}

fn save_store(entries: &[(String, String, String)]) -> Result<(), String> {
    let text: String = entries.iter().map(|(h, t, k)| format!("{h} {t} {k}\n")).collect();
    crate::config::write_private(&store_path(), text.as_bytes()).map_err(|e| format!("could not save the known hosts file: {e}"))
}

/// Remove the recorded key of a host (used by tests and by "forget host").
#[allow(dead_code)]
pub fn forget(host: &str, port: u16) {
    let _guard = STORE_LOCK.lock();
    let l = label(host, port);
    let entries: Vec<_> = load_store().into_iter().filter(|e| e.0 != l).collect();
    let _ = save_store(&entries);
}

#[derive(Debug, PartialEq)]
pub enum Verdict {
    Trusted,
    Unknown,
    Changed,
}

/// Pure decision against the app store.
pub fn check_store(entries: &[(String, String, String)], host: &str, port: u16, key_b64: &str) -> Verdict {
    let l = label(host, port);
    let mine: Vec<_> = entries.iter().filter(|e| e.0 == l).collect();
    if mine.iter().any(|e| e.2 == key_b64) {
        Verdict::Trusted
    } else if mine.is_empty() {
        Verdict::Unknown
    } else {
        Verdict::Changed
    }
}

fn ask(host_label: &str, key_type: &str, fingerprint: &str, q: Question) -> Option<bool> {
    #[cfg(test)]
    if let Some(a) = TEST_ANSWER.with(|c| c.get()) {
        return Some(a);
    }
    PROMPT.lock().as_ref().map(|p| p(host_label, key_type, fingerprint, q))
}

/// Verify the host key of a session whose handshake just completed. Must run before authentication.
pub fn verify(sess: &ssh2::Session, host: &str, port: u16) -> Result<(), String> {
    let (key, kind) = sess.host_key().ok_or("the server did not present a host key")?;
    let key_b64 = base64(key);
    let key_type = format!("{kind:?}");
    let fingerprint = sess.host_key_hash(ssh2::HashType::Sha256).map(|h| format!("SHA256:{}", base64(h).trim_end_matches('='))).unwrap_or_else(|| "unknown".into());
    let mut verdict = check_store(&load_store(), host, port, &key_b64);
    // the user's OpenSSH file is honoured too (read-only); the app store wins when it has an answer
    if verdict == Verdict::Unknown && !cfg!(test) {
        if let (Some(home), Ok(mut kh)) = (dirs::home_dir(), sess.known_hosts()) {
            let file = home.join(".ssh").join("known_hosts");
            if file.exists() && kh.read_file(&file, ssh2::KnownHostFileKind::OpenSSH).is_ok() {
                match kh.check_port(host, port, key) {
                    ssh2::CheckResult::Match => verdict = Verdict::Trusted,
                    ssh2::CheckResult::Mismatch => verdict = Verdict::Changed,
                    _ => {}
                }
            }
        }
    }
    let host_label = if port == 22 { host.to_string() } else { format!("{host}:{port}") };
    match verdict {
        Verdict::Trusted => Ok(()),
        Verdict::Unknown => {
            // no prompt installed (headless use): accept and record, like OpenSSH accept-new
            if ask(&host_label, &key_type, &fingerprint, Question::Unknown).unwrap_or(true) {
                // the question may have been open for a while: re-read under the lock
                let _guard = STORE_LOCK.lock();
                let mut entries = load_store();
                let l = label(host, port);
                entries.retain(|e| e.0 != l);
                entries.push((l, key_type, key_b64));
                save_store(&entries)
            } else {
                Err(format!("the host key of {host_label} was not trusted ({fingerprint})"))
            }
        }
        Verdict::Changed => {
            if ask(&host_label, &key_type, &fingerprint, Question::Changed).unwrap_or(false) {
                let _guard = STORE_LOCK.lock();
                let mut entries = load_store();
                let l = label(host, port);
                entries.retain(|e| e.0 != l);
                entries.push((l, key_type, key_b64));
                save_store(&entries)
            } else {
                Err(format!(
                    "HOST KEY CHANGED for {host_label} ({fingerprint}). The connection was refused because someone may be intercepting it. If the server was reinstalled, remove its line from {} and connect again.",
                    store_path().display()
                ))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_matches_reference_vectors() {
        assert_eq!(base64(b""), "");
        assert_eq!(base64(b"f"), "Zg==");
        assert_eq!(base64(b"fo"), "Zm8=");
        assert_eq!(base64(b"foo"), "Zm9v");
        assert_eq!(base64(b"foobar"), "Zm9vYmFy");
    }

    #[test]
    fn store_decisions() {
        let entries = vec![("[10.0.0.1]:22".to_string(), "Rsa".to_string(), "AAAA".to_string())];
        assert_eq!(check_store(&entries, "10.0.0.1", 22, "AAAA"), Verdict::Trusted);
        assert_eq!(check_store(&entries, "10.0.0.1", 22, "BBBB"), Verdict::Changed);
        assert_eq!(check_store(&entries, "10.0.0.1", 2222, "AAAA"), Verdict::Unknown);
        assert_eq!(check_store(&entries, "10.0.0.2", 22, "AAAA"), Verdict::Unknown);
        // host names compare case-insensitively
        let e2 = vec![("[router.lab]:22".to_string(), "Rsa".to_string(), "K".to_string())];
        assert_eq!(check_store(&e2, "Router.LAB", 22, "K"), Verdict::Trusted);
    }
}
