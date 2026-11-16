//! SFTP client with jump-host (hop) chains (port of core/sftp_hops.py).
//!
//! A hop beyond the first is reached by tunnelling a `direct-tcpip` channel through a *dedicated*
//! SSH session to the previous hop and bridging it to a loopback listener. The SSH session used
//! for SFTP then connects to that listener like any other TCP host.

use crate::conn::{ssh_session, ssh_session_over};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

#[derive(Clone)]
pub struct HopSpec {
    pub host: String,
    pub port: u16,
    pub username: String,
    pub password: String,
}

impl std::fmt::Debug for HopSpec {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "HopSpec {{ host: {:?}, port: {}, username: {:?}, password: <redacted> }}", self.host, self.port, self.username)
    }
}

impl HopSpec {
    pub fn label(&self) -> String {
        if self.username.is_empty() { self.host.clone() } else { format!("{}@{}", self.username, self.host) }
    }
}

#[derive(Clone, Debug)]
pub struct Entry {
    pub name: String,
    pub is_dir: bool,
    pub size: u64,
    /// The entry itself is a symbolic link (`is_dir` then describes its target).
    pub is_link: bool,
}

/// Recursion limit for folder transfers and deletes.
const MAX_DEPTH: usize = 64;
/// Largest file read into memory in one go (editor, downloads).
pub const MAX_FILE_BYTES: u64 = 512 * 1024 * 1024;

/// A name from the server is used as one path component only: nothing that could climb out of
/// the destination folder.
pub fn safe_name(name: &str) -> bool {
    !name.is_empty() && name != "." && name != ".." && !name.contains('/') && !name.contains('\\') && !name.contains('\0')
}

struct Hop {
    label: String,
    spec: HopSpec,
    sftp: ssh2::Sftp,
    _sess: ssh2::Session,
}

pub struct SftpClient {
    hops: Vec<Hop>,
    pub active: usize,
}

/// Open an authenticated SSH session to the last hop of `chain`.
fn connect_chain(chain: &[HopSpec]) -> Result<ssh2::Session, String> {
    let last = chain.last().ok_or("no hops")?;
    let look = last.password.is_empty();
    if chain.len() == 1 {
        return ssh_session(&last.host, last.port, &last.username, &last.password, look);
    }
    // dedicated session to the previous hop, used only to carry this tunnel
    let tunnel_sess = connect_chain(&chain[..chain.len() - 1])?;
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
    let local_port = listener.local_addr().map_err(|e| e.to_string())?.port();
    let channel = tunnel_sess.channel_direct_tcpip(&last.host, last.port, Some(("127.0.0.1", 0))).map_err(|e| format!("tunnel to {}: {e}", last.host))?;
    std::thread::spawn(move || {
        let Ok((stream, _)) = listener.accept() else { return };
        pump(tunnel_sess, channel, stream);
    });
    let tcp = TcpStream::connect(("127.0.0.1", local_port)).map_err(|e| e.to_string())?;
    ssh_session_over(tcp, &last.host, last.port, &last.username, &last.password, look)
}

/// Copy bytes both ways between a TCP stream and an SSH channel until either side closes.
fn pump(sess: ssh2::Session, mut channel: ssh2::Channel, mut stream: TcpStream) {
    sess.set_blocking(false);
    stream.set_read_timeout(Some(Duration::from_millis(2))).ok();
    let mut buf = [0u8; 16 * 1024];
    let mut to_chan: Vec<u8> = Vec::new();
    let mut to_tcp: Vec<u8> = Vec::new();
    loop {
        let mut idle = true;
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                to_chan.extend_from_slice(&buf[..n]);
                idle = false;
            }
            Err(e) if matches!(e.kind(), std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut) => {}
            Err(_) => break,
        }
        if !to_chan.is_empty() {
            match channel.write(&to_chan) {
                Ok(n) => {
                    to_chan.drain(..n);
                    idle = false;
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                Err(_) => break,
            }
        }
        match channel.read(&mut buf) {
            Ok(0) => {
                if channel.eof() {
                    break;
                }
            }
            Ok(n) => {
                to_tcp.extend_from_slice(&buf[..n]);
                idle = false;
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
            Err(_) => break,
        }
        if !to_tcp.is_empty() {
            stream.set_write_timeout(Some(Duration::from_secs(5))).ok();
            if stream.write_all(&to_tcp).is_err() {
                break;
            }
            to_tcp.clear();
        }
        if idle {
            std::thread::sleep(Duration::from_millis(2));
        }
    }
    let _ = channel.close();
}

impl SftpClient {
    pub fn open(first: HopSpec) -> Result<Self, String> {
        let sess = connect_chain(std::slice::from_ref(&first))?;
        let sftp = sess.sftp().map_err(|e| e.to_string())?;
        Ok(Self { hops: vec![Hop { label: format!("Hop 1: {}", first.label()), spec: first, sftp, _sess: sess }], active: 0 })
    }

    pub fn labels(&self) -> Vec<String> {
        self.hops.iter().map(|h| h.label.clone()).collect()
    }

    pub fn add_hop(&mut self, spec: HopSpec) -> Result<(), String> {
        let mut chain: Vec<HopSpec> = self.hops.iter().map(|h| h.spec.clone()).collect();
        chain.push(spec.clone());
        let sess = connect_chain(&chain)?;
        let sftp = sess.sftp().map_err(|e| e.to_string())?;
        let label = format!("Hop {}: {}", self.hops.len() + 1, spec.label());
        self.hops.push(Hop { label, spec, sftp, _sess: sess });
        self.active = self.hops.len() - 1;
        Ok(())
    }

    pub fn remove_last_hop(&mut self) {
        if self.hops.len() > 1 {
            self.hops.pop();
            self.active = self.hops.len() - 1;
        }
    }

    pub fn select_hop(&mut self, i: usize) {
        if i < self.hops.len() {
            self.active = i;
        }
    }

    fn sftp(&self) -> &ssh2::Sftp {
        &self.hops[self.active].sftp
    }

    pub fn label(&self) -> String {
        self.hops[self.active].label.split(": ").nth(1).unwrap_or("SFTP").to_string()
    }

    pub fn realpath(&self, p: &str) -> Result<String, String> {
        self.sftp().realpath(Path::new(p)).map(|p| p.to_string_lossy().to_string()).map_err(|e| e.to_string())
    }

    pub fn list(&self, path: &str) -> Result<Vec<Entry>, String> {
        let items = self.sftp().readdir(Path::new(path)).map_err(|e| e.to_string())?;
        let mut out: Vec<Entry> = items
            .into_iter()
            .filter_map(|(p, st)| {
                let name = p.file_name()?.to_string_lossy().to_string();
                if name == "." || name == ".." {
                    return None;
                }
                // symlinks to directories count as directories
                let mut is_dir = st.is_dir();
                let is_link = st.file_type().is_symlink();
                if is_link {
                    if let Ok(real) = self.sftp().stat(&p) {
                        is_dir = real.is_dir();
                    }
                }
                Some(Entry { name, is_dir, size: st.size.unwrap_or(0), is_link })
            })
            .collect();
        out.sort_by(|a, b| b.is_dir.cmp(&a.is_dir).then(a.name.to_lowercase().cmp(&b.name.to_lowercase())));
        Ok(out)
    }

    #[allow(dead_code)]
    pub fn is_dir(&self, path: &str) -> bool {
        self.sftp().stat(Path::new(path)).map(|s| s.is_dir()).unwrap_or(false)
    }

    pub fn exists(&self, path: &str) -> bool {
        self.sftp().stat(Path::new(path)).is_ok()
    }

    pub fn mkdir(&self, path: &str) -> Result<(), String> {
        self.sftp().mkdir(Path::new(path), 0o755).map_err(|e| e.to_string())
    }

    #[allow(dead_code)]
    pub fn rename(&self, from: &str, to: &str) -> Result<(), String> {
        self.sftp().rename(Path::new(from), Path::new(to), None).map_err(|e| e.to_string())
    }

    pub fn remove(&self, path: &str, is_dir: bool) -> Result<(), String> {
        self.remove_depth(path, is_dir, 0)
    }

    fn remove_depth(&self, path: &str, is_dir: bool, depth: usize) -> Result<(), String> {
        // A symbolic link is removed as a link. Its target is never entered: deleting a link that
        // points at / or at another user's folder must not delete what it points to.
        let is_link = self.sftp().lstat(Path::new(path)).map(|s| s.file_type().is_symlink()).unwrap_or(false);
        if !is_dir || is_link {
            return self.sftp().unlink(Path::new(path)).map_err(|e| e.to_string());
        }
        if depth >= MAX_DEPTH {
            return Err(format!("{path}: folders are nested too deeply"));
        }
        for e in self.list(path)? {
            let child = format!("{}/{}", path.trim_end_matches('/'), e.name);
            self.remove_depth(&child, e.is_dir && !e.is_link, depth + 1)?;
        }
        self.sftp().rmdir(Path::new(path)).map_err(|e| e.to_string())
    }

    pub fn read_file(&self, path: &str) -> Result<Vec<u8>, String> {
        self.read_file_limited(path, MAX_FILE_BYTES)
    }

    /// Read a remote file, refusing anything larger than `limit` (a server cannot exhaust memory).
    pub fn read_file_limited(&self, path: &str, limit: u64) -> Result<Vec<u8>, String> {
        let f = self.sftp().open(Path::new(path)).map_err(|e| e.to_string())?;
        let mut buf = Vec::new();
        f.take(limit + 1).read_to_end(&mut buf).map_err(|e| e.to_string())?;
        if buf.len() as u64 > limit {
            return Err(format!("{path} is larger than {} MB and was not opened", limit / (1024 * 1024)));
        }
        Ok(buf)
    }

    pub fn write_file(&self, path: &str, data: &[u8]) -> Result<(), String> {
        let mut f = self.sftp().create(Path::new(path)).map_err(|e| e.to_string())?;
        f.write_all(data).map_err(|e| e.to_string())?;
        Ok(())
    }

    pub fn upload(&self, local: &Path, remote: &str) -> Result<(), String> {
        let data = std::fs::read(local).map_err(|e| e.to_string())?;
        self.write_file(remote, &data)
    }

    pub fn download(&self, remote: &str, local: &Path) -> Result<(), String> {
        let data = self.read_file(remote)?;
        if let Some(p) = local.parent() {
            let _ = std::fs::create_dir_all(p);
        }
        std::fs::write(local, data).map_err(|e| e.to_string())
    }

    pub fn upload_folder(&self, local_dir: &Path, remote_dir: &str, progress: &mut dyn FnMut(&str)) -> Result<(), String> {
        self.upload_folder_depth(local_dir, remote_dir, progress, 0)
    }

    fn upload_folder_depth(&self, local_dir: &Path, remote_dir: &str, progress: &mut dyn FnMut(&str), depth: usize) -> Result<(), String> {
        if depth >= MAX_DEPTH {
            return Err(format!("{}: folders are nested too deeply", local_dir.display()));
        }
        let _ = self.mkdir(remote_dir); // may already exist
        for entry in std::fs::read_dir(local_dir).map_err(|e| e.to_string())? {
            let entry = entry.map_err(|e| e.to_string())?;
            let name = entry.file_name().to_string_lossy().to_string();
            let remote = format!("{}/{}", remote_dir.trim_end_matches('/'), name);
            // symbolic links are skipped: following them can loop forever or upload files from
            // outside the chosen folder
            let meta = std::fs::symlink_metadata(entry.path()).map_err(|e| e.to_string())?;
            if meta.file_type().is_symlink() {
                continue;
            }
            if meta.is_dir() {
                self.upload_folder_depth(&entry.path(), &remote, progress, depth + 1)?;
            } else {
                progress(&entry.path().to_string_lossy());
                self.upload(&entry.path(), &remote)?;
            }
        }
        Ok(())
    }

    pub fn download_folder(&self, remote_dir: &str, local_dir: &Path, progress: &mut dyn FnMut(&str)) -> Result<(), String> {
        self.download_folder_depth(remote_dir, local_dir, progress, 0)
    }

    fn download_folder_depth(&self, remote_dir: &str, local_dir: &Path, progress: &mut dyn FnMut(&str), depth: usize) -> Result<(), String> {
        if depth >= MAX_DEPTH {
            return Err(format!("{remote_dir}: folders are nested too deeply"));
        }
        std::fs::create_dir_all(local_dir).map_err(|e| e.to_string())?;
        for e in self.list(remote_dir)? {
            // a hostile server must not be able to write outside the chosen folder
            if !safe_name(&e.name) {
                return Err(format!("the server sent an unsafe file name: {:?}", e.name));
            }
            let remote = format!("{}/{}", remote_dir.trim_end_matches('/'), e.name);
            let local = local_dir.join(&e.name);
            if e.is_dir && e.is_link {
                continue; // linked folders are not followed (they can loop back on themselves)
            }
            if e.is_dir {
                self.download_folder_depth(&remote, &local, progress, depth + 1)?;
            } else {
                progress(&remote);
                self.download(&remote, &local)?;
            }
        }
        Ok(())
    }
}

pub type SharedSftp = Arc<parking_lot::Mutex<SftpClient>>;

pub fn join_remote(dir: &str, name: &str) -> String {
    let base = if dir.is_empty() { "/" } else { dir.trim_end_matches('/') };
    format!("{}/{}", if base.is_empty() { "" } else { base }, name)
}

pub fn parent_remote(path: &str) -> String {
    let t = path.trim_end_matches('/');
    match t.rfind('/') {
        Some(0) | None => "/".to_string(),
        Some(i) => t[..i].to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn server_names_cannot_climb_out() {
        assert!(safe_name("notes.txt") && safe_name(".bashrc"));
        for bad in ["..", ".", "", "../x", "a/b", "a\\b", "x\0y"] {
            assert!(!safe_name(bad), "{bad:?}");
        }
    }

    #[test]
    fn path_helpers() {
        assert_eq!(join_remote("/home/a", "x"), "/home/a/x");
        assert_eq!(join_remote("/", "x"), "/x");
        assert_eq!(parent_remote("/home/a"), "/home");
        assert_eq!(parent_remote("/home"), "/");
        assert_eq!(parent_remote("/"), "/");
    }
}
