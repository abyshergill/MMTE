//! End-to-end tests against a real (paramiko) SSH server: shell, SFTP, jump hops, remote editing.
//! Skipped automatically when Python with paramiko is not available.

use crate::conn::ConnSpec;
use crate::session::Session;
use crate::sftp::{HopSpec, SftpClient};
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Arc;

pub struct TestServer {
    child: Child,
    pub port: u16,
    pub root: PathBuf,
}

impl Drop for TestServer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

fn python() -> Option<String> {
    let candidates = [
        std::env::var("MMTE_TEST_PYTHON").ok(),
        Some(concat!(env!("CARGO_MANIFEST_DIR"), "/.venv/bin/python").to_string()),
        Some("python3".to_string()),
    ];
    candidates.into_iter().flatten().find(|p| Command::new(p).args(["-c", "import paramiko"]).stdout(Stdio::null()).stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false))
}

pub fn start_server(port: u16, tag: &str) -> Option<TestServer> {
    // every test server has a fresh host key: forget whatever an earlier one left for this port
    crate::hostkeys::forget("127.0.0.1", port);
    start_server_keep(port, tag)
}

/// Start a server without touching the recorded host keys.
pub fn start_server_keep(port: u16, tag: &str) -> Option<TestServer> {
    let py = python().or_else(|| { eprintln!("E2E SKIPPED: no python+paramiko"); None })?;
    let root = std::env::temp_dir().join(format!("mmte_ssh_{}_{}_{}", tag, std::process::id(), port));
    std::fs::create_dir_all(&root).unwrap();
    let script = concat!(env!("CARGO_MANIFEST_DIR"), "/tests_support/ssh_server.py");
    let mut child = Command::new(py).args([script, &port.to_string(), &root.to_string_lossy()]).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().ok()?;
    eprintln!("E2E server up on {port}");
    let mut line = String::new();
    BufReader::new(child.stdout.take()?).read_line(&mut line).ok()?;
    if !line.contains("READY") {
        return None;
    }
    Some(TestServer { child, port, root })
}

pub fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

fn hop(port: u16) -> HopSpec {
    HopSpec { host: "127.0.0.1".into(), port, username: "tester".into(), password: "secret".into() }
}

#[test]
fn ssh_shell_session_works() {
    let Some(srv) = start_server(free_port(), "shell") else { return };
    let s = Session::new(1, "ssh", true);
    s.connect(ConnSpec::Ssh { host: "127.0.0.1".into(), username: "tester".into(), password: "secret".into(), port: srv.port, look_for_keys: false }).unwrap();
    assert!(s.is_connected());
    assert_eq!(s.conn_kind(), "SSH");
    assert!(s.wait_for("root@ssh:~#", 10.0, true).unwrap(), "no prompt: {}", s.buf.lock());
    s.clear_wait_buffer();
    s.send("echo hello-rust\r");
    assert!(s.wait_for("hello-rust\r\nroot@ssh:~#", 10.0, true).unwrap(), "{}", s.buf.lock());
    s.resize(120, 40);
    s.clear_wait_buffer();
    s.send("pwd\r");
    assert!(s.wait_for("/srv/data", 10.0, true).unwrap());
    // logging through a real session produces clean text
    let log = std::env::temp_dir().join(format!("mmte_e2e_{}.log", std::process::id()));
    s.enable_logging(&log).unwrap();
    s.send("echo logged-line\r");
    assert!(s.wait_for("logged-line\r\nroot", 10.0, true).unwrap());
    s.disable_logging();
    let text = std::fs::read_to_string(&log).unwrap();
    assert!(text.contains("logged-line") && !text.contains('\x1b'), "{text:?}");
    let _ = std::fs::remove_file(log);
    s.send("exit\r");
    assert!(s.wait_for("logout", 10.0, true).unwrap());
    for _ in 0..50 {
        if !s.is_connected() {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    assert!(!s.is_connected());
    assert_eq!(s.label(), "Disconnected");
}

#[test]
fn ssh_wrong_password_fails_cleanly() {
    let Some(srv) = start_server(free_port(), "auth") else { return };
    let s = Session::new(1, "ssh", true);
    let err = s.connect(ConnSpec::Ssh { host: "127.0.0.1".into(), username: "tester".into(), password: "nope".into(), port: srv.port, look_for_keys: false }).unwrap_err();
    assert!(err.contains("authentication failed"), "{err}");
    assert!(!s.is_connected());
    assert!(s.buf.lock().contains("[Connection failed"));
}

#[test]
fn sftp_operations() {
    let Some(srv) = start_server(free_port(), "sftp") else { return };
    let c = SftpClient::open(hop(srv.port)).unwrap();
    assert_eq!(c.realpath(".").unwrap(), "/");
    c.mkdir("/docs").unwrap();
    c.write_file("/docs/a.txt", b"hello sftp").unwrap();
    assert_eq!(c.read_file("/docs/a.txt").unwrap(), b"hello sftp");
    let list = c.list("/docs").unwrap();
    assert_eq!(list.len(), 1);
    assert!(!list[0].is_dir && list[0].size == 10);
    c.rename("/docs/a.txt", "/docs/b.txt").unwrap();
    assert!(c.exists("/docs/b.txt") && !c.exists("/docs/a.txt"));
    // folders: upload then download round trip
    let local = std::env::temp_dir().join(format!("mmte_up_{}", std::process::id()));
    std::fs::create_dir_all(local.join("sub")).unwrap();
    std::fs::write(local.join("x.txt"), "x").unwrap();
    std::fs::write(local.join("sub/y.txt"), "yy").unwrap();
    let mut seen = Vec::new();
    c.upload_folder(&local, "/up", &mut |f| seen.push(f.to_string())).unwrap();
    assert_eq!(seen.len(), 2);
    assert_eq!(c.read_file("/up/sub/y.txt").unwrap(), b"yy");
    let back = std::env::temp_dir().join(format!("mmte_down_{}", std::process::id()));
    c.download_folder("/up", &back, &mut |_| {}).unwrap();
    assert_eq!(std::fs::read(back.join("sub/y.txt")).unwrap(), b"yy");
    // recursive delete
    c.remove("/up", true).unwrap();
    assert!(!c.exists("/up"));
    c.remove("/docs/b.txt", false).unwrap();
    assert!(c.list("/docs").unwrap().is_empty());
    assert!(c.list("/missing").is_err());
    let _ = std::fs::remove_dir_all(local);
    let _ = std::fs::remove_dir_all(back);
}

#[test]
fn sftp_jump_hops() {
    let (Some(a), Some(b)) = (start_server(free_port(), "hopa"), start_server(free_port(), "hopb")) else { return };
    std::fs::write(a.root.join("on_a.txt"), "a").unwrap();
    std::fs::write(b.root.join("on_b.txt"), "b").unwrap();
    let mut c = SftpClient::open(hop(a.port)).unwrap();
    assert_eq!(c.list("/").unwrap()[0].name, "on_a.txt");
    c.add_hop(hop(b.port)).unwrap();
    assert_eq!(c.labels().len(), 2);
    assert_eq!(c.active, 1);
    let names: Vec<String> = c.list("/").unwrap().into_iter().map(|e| e.name).collect();
    assert_eq!(names, vec!["on_b.txt".to_string()]);
    assert_eq!(c.read_file("/on_b.txt").unwrap(), b"b");
    c.select_hop(0);
    assert_eq!(c.list("/").unwrap()[0].name, "on_a.txt");
    c.select_hop(1);
    c.remove_last_hop();
    assert_eq!(c.labels().len(), 1);
    assert_eq!(c.list("/").unwrap()[0].name, "on_a.txt");
}

#[test]
fn editor_opens_and_saves_remote_file() {
    let Some(srv) = start_server(free_port(), "editor") else { return };
    std::fs::write(srv.root.join("conf.txt"), "line1\r\nline2\r\n").unwrap();
    let client: crate::sftp::SharedSftp = Arc::new(parking_lot::Mutex::new(SftpClient::open(hop(srv.port)).unwrap()));
    let mut ed = crate::ui::editor::Editor::new();
    ed.open_remote(client.clone(), "/conf.txt".into(), "SSH test".into(), false);
    for _ in 0..100 {
        ed.poll();
        if !ed.docs.is_empty() {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    assert_eq!(ed.docs.len(), 1);
    assert_eq!(ed.docs[0].text, "line1\nline2\n");
    assert_eq!(ed.docs[0].eol, "\r\n");
    ed.docs[0].text.push_str("line3\n");
    assert!(ed.docs[0].dirty());
    ed.save_doc(0);
    for _ in 0..100 {
        ed.poll();
        if !ed.docs[0].dirty() {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    assert!(!ed.docs[0].dirty());
    // CRLF line endings were preserved on save
    assert_eq!(std::fs::read(srv.root.join("conf.txt")).unwrap(), b"line1\r\nline2\r\nline3\r\n");
    // a missing file is offered as a new document when asked
    ed.open_remote(client, "/new.txt".into(), "SSH test".into(), true);
    for _ in 0..100 {
        ed.poll();
        if ed.docs.len() == 2 {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    assert_eq!(ed.docs.len(), 2);
    assert!(ed.docs[1].text.is_empty());
}

#[test]
fn python_script_drives_a_real_ssh_session() {
    let Some(srv) = start_server(free_port(), "script") else { return };
    if crate::script::find_python().is_none() {
        return;
    }
    let (hub, _rx) = crate::session::Hub::new();
    let s = hub.new_session(Some("main"));
    let host = crate::script::ScriptHost::new(hub.clone(), s.clone(), vec![]);
    let dir = std::env::temp_dir().join(format!("mmte_e2e_script_{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("t.py");
    std::fs::write(&p, format!("connect_ssh('127.0.0.1', 'tester', 'secret', port={})\nwait('root@ssh:~#', timeout=10)\nsend('echo from-script')\nok = wait('from-script', timeout=10)\nassert ok\n", srv.port)).unwrap();
    host.run_file(&p).unwrap();
    assert!(s.buf.lock().contains("from-script"));
    s.close();
    // the same through the TTL interpreter (it connects with /user and /passwd)
    let s2 = hub.new_session(Some("ttl"));
    let host2 = crate::script::ScriptHost::new(hub.clone(), s2.clone(), vec![]);
    let t = dir.join("t.ttl");
    std::fs::write(&t, format!("connect '127.0.0.1 /ssh /user=tester /passwd=secret /port={}'\nwait 'root@ssh:~#'\nsendln 'echo from-ttl'\nwait 'from-ttl'\nend\n", srv.port)).unwrap();
    host2.run_file(&t).unwrap();
    assert!(s2.buf.lock().contains("from-ttl"));
    s2.close();
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn ai_editor_commands_open_the_builtin_editor() {
    use crate::session::UiRequest;
    use std::sync::atomic::AtomicBool;
    let Some(srv) = start_server(free_port(), "aiedit") else { return };
    let (hub, rx) = crate::session::Hub::new();
    let s = hub.new_session(None);
    s.connect(ConnSpec::Ssh { host: "127.0.0.1".into(), username: "tester".into(), password: "secret".into(), port: srv.port, look_for_keys: false }).unwrap();
    assert!(s.wait_for("root@ssh:~#", 10.0, true).unwrap());
    s.clear_wait_buffer();
    let out = crate::ai::chat::send_commands(&hub, &[s.clone()], &["sudo vim -R /etc/hosts".to_string(), "echo after".to_string()], 0.4, &Arc::new(AtomicBool::new(false)));
    // the editor command went to the editor, the next one to the shell
    match rx.try_recv() {
        Ok(UiRequest::OpenRemote { files, sudo, wipe, .. }) => {
            assert_eq!(files, vec!["/etc/hosts".to_string()]);
            assert!(sudo && !wipe);
        }
        _ => panic!("expected an OpenRemote request"),
    }
    assert!(out.contains("Opened /etc/hosts in the built-in editor"), "{out}");
    assert!(out.contains("after"), "{out}");
    assert!(!s.buf.lock().contains("vim"), "vim must not be typed into the terminal: {}", s.buf.lock());
    // with interception off the command is typed as before
    hub.intercept_editors.store(false, std::sync::atomic::Ordering::SeqCst);
    s.clear_wait_buffer();
    let out = crate::ai::chat::send_commands(&hub, &[s.clone()], &["vim x".to_string()], 0.5, &Arc::new(AtomicBool::new(false)));
    assert!(out.contains("sh: vim x: not found"), "{out}");
    s.close();
}

#[test]
fn host_key_is_verified_before_authentication() {
    use crate::hostkeys::TEST_ANSWER;
    let port = free_port();
    let Some(first) = start_server(port, "hk1") else { return };
    let spec = || ConnSpec::Ssh { host: "127.0.0.1".into(), username: "tester".into(), password: "secret".into(), port, look_for_keys: false };

    // unknown host, user says no: refused, and nothing is recorded
    TEST_ANSWER.with(|c| c.set(Some(false)));
    let s = Session::new(1, "hk", true);
    let err = s.connect(spec()).unwrap_err();
    assert!(err.contains("was not trusted"), "{err}");

    // user says yes: connects and the key is remembered
    TEST_ANSWER.with(|c| c.set(Some(true)));
    s.connect(spec()).unwrap();
    s.close();
    let stored = std::fs::read_to_string(crate::hostkeys::store_path()).unwrap();
    assert!(stored.contains(&format!("[127.0.0.1]:{port} ")), "{stored}");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(crate::hostkeys::store_path()).unwrap().permissions().mode() & 0o777, 0o600);
    }

    // a known host needs no question at all (an answer of "no" would fail here)
    TEST_ANSWER.with(|c| c.set(Some(false)));
    let s2 = Session::new(2, "hk", true);
    s2.connect(spec()).unwrap();
    s2.close();

    // the server is replaced by an impostor with another key on the same address
    drop(first);
    std::thread::sleep(std::time::Duration::from_millis(300));
    let Some(_impostor) = start_server_keep(port, "hk2") else { return };
    TEST_ANSWER.with(|c| c.set(None)); // no one to ask: must be refused
    let s3 = Session::new(3, "hk", true);
    let err = s3.connect(spec()).unwrap_err();
    assert!(err.contains("HOST KEY CHANGED"), "{err}");
    assert!(!s3.is_connected());
    // SFTP goes through the same check
    assert!(SftpClient::open(hop(port)).err().unwrap_or_default().contains("HOST KEY CHANGED"));
    // only an explicit yes replaces the saved key
    TEST_ANSWER.with(|c| c.set(Some(true)));
    s3.connect(spec()).unwrap();
    s3.close();
    TEST_ANSWER.with(|c| c.set(None));
}

#[test]
fn password_is_forgotten_after_disconnect() {
    let Some(srv) = start_server(free_port(), "pwclr") else { return };
    let s = Session::new(1, "pw", true);
    s.connect(ConnSpec::Ssh { host: "127.0.0.1".into(), username: "tester".into(), password: "secret".into(), port: srv.port, look_for_keys: false }).unwrap();
    assert!(matches!(s.spec.lock().as_ref(), Some(ConnSpec::Ssh { password, .. }) if password == "secret"));
    assert!(!format!("{:?}", s.spec.lock()).contains("secret"));
    s.disconnect();
    assert!(matches!(s.spec.lock().as_ref(), Some(ConnSpec::Ssh { password, .. }) if password.is_empty()));
}

#[test]
#[cfg(unix)]
fn sftp_never_follows_symbolic_links_when_deleting_or_copying() {
    let Some(srv) = start_server(free_port(), "links") else { return };
    // outside/ holds data that must survive; tree/ is what the user works on
    std::fs::create_dir_all(srv.root.join("outside")).unwrap();
    std::fs::write(srv.root.join("outside/keep.txt"), "precious").unwrap();
    std::fs::create_dir_all(srv.root.join("tree/sub")).unwrap();
    std::fs::write(srv.root.join("tree/sub/a.txt"), "a").unwrap();
    std::os::unix::fs::symlink(srv.root.join("outside"), srv.root.join("tree/escape")).unwrap();
    std::os::unix::fs::symlink(srv.root.join("tree"), srv.root.join("tree/loop")).unwrap();
    let c = SftpClient::open(hop(srv.port)).unwrap();
    let entries = c.list("/tree").unwrap();
    let link = entries.iter().find(|e| e.name == "escape").unwrap();
    assert!(link.is_link && link.is_dir);

    // download: finishes (no endless loop) and does not pull in the linked folders
    let dest = std::env::temp_dir().join(format!("mmte_links_{}", std::process::id()));
    c.download_folder("/tree", &dest, &mut |_| {}).unwrap();
    assert!(dest.join("sub/a.txt").exists());
    assert!(!dest.join("escape").exists() && !dest.join("loop").exists());

    // delete: the links go, what they point to stays
    c.remove("/tree", true).unwrap();
    assert!(!srv.root.join("tree").exists());
    assert_eq!(std::fs::read_to_string(srv.root.join("outside/keep.txt")).unwrap(), "precious");

    // upload: local links are skipped too
    let up = std::env::temp_dir().join(format!("mmte_uplinks_{}", std::process::id()));
    std::fs::create_dir_all(&up).unwrap();
    std::fs::write(up.join("f.txt"), "f").unwrap();
    std::os::unix::fs::symlink(&up, up.join("self")).unwrap();
    c.upload_folder(&up, "/up", &mut |_| {}).unwrap();
    let names: Vec<String> = c.list("/up").unwrap().into_iter().map(|e| e.name).collect();
    assert_eq!(names, vec!["f.txt".to_string()]);
    // oversized files are refused instead of filling memory
    std::fs::write(srv.root.join("big.bin"), vec![b'x'; 5000]).unwrap();
    assert!(c.read_file_limited("/big.bin", 1000).unwrap_err().contains("larger than"));
    let _ = std::fs::remove_dir_all(dest);
    let _ = std::fs::remove_dir_all(up);
}
