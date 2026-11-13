//! SSH / Telnet / Serial connections. Each connection runs one worker thread that reads
//! incoming bytes (handed to a callback) and writes whatever arrives on the command channel.

use crossbeam_channel::{unbounded, Sender};
use std::collections::HashSet;
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

#[derive(Clone, Debug)]
pub enum ConnSpec {
    Ssh { host: String, username: String, password: String, port: u16, look_for_keys: bool },
    Telnet { host: String, port: u16 },
    Serial { port: String, baud: u32, bytesize: u8, parity: char, stopbits: f32 },
}

impl ConnSpec {
    pub fn kind(&self) -> &'static str {
        match self {
            ConnSpec::Ssh { .. } => "SSH",
            ConnSpec::Telnet { .. } => "Telnet",
            ConnSpec::Serial { .. } => "Serial",
        }
    }
    /// Label shown in the UI, e.g. `SSH 10.0.0.1`.
    pub fn label(&self) -> String {
        match self {
            ConnSpec::Ssh { host, .. } => format!("SSH {host}"),
            ConnSpec::Telnet { host, .. } => format!("Telnet {host}"),
            ConnSpec::Serial { port, baud, .. } => format!("Serial {port} @ {baud}"),
        }
    }
    /// A commented Tera Term `connect` line for `.ttl` macros.
    pub fn ttl_hint(&self) -> String {
        match self {
            ConnSpec::Ssh { host, username, port, .. } => format!("connect '{host} /ssh /user={username} /passwd=PASSWORD /port={port}'"),
            ConnSpec::Telnet { host, port } => format!("connect '{host} /telnet /port={port}'"),
            ConnSpec::Serial { port, baud, .. } => format!("(serial {port} @ {baud}: connect the terminal before replaying)"),
        }
    }

    /// A commented `connect_*` line for macro scripts.
    pub fn connect_hint(&self) -> String {
        match self {
            ConnSpec::Ssh { host, username, port, .. } => format!(
                "connect_ssh({}, {}, input('SSH password for {}@{}'), port={})",
                crate::macrorec::py_repr(host),
                crate::macrorec::py_repr(username),
                username,
                host,
                port
            ),
            ConnSpec::Telnet { host, port } => format!("connect_telnet({}, port={})", crate::macrorec::py_repr(host), port),
            ConnSpec::Serial { port, baud, .. } => format!("connect_serial({}, {})", crate::macrorec::py_repr(port), baud),
        }
    }
}

pub enum Cmd {
    Data(Vec<u8>),
    Resize(u16, u16),
    Close,
}

/// Handle used by the rest of the app to talk to a running connection.
pub struct Conn {
    pub spec: ConnSpec,
    tx: Sender<Cmd>,
    pub alive: Arc<AtomicBool>,
}

impl Conn {
    pub fn send(&self, data: &[u8]) -> Result<(), String> {
        if !self.alive.load(Ordering::SeqCst) {
            return Err("connection is closed".into());
        }
        self.tx.send(Cmd::Data(data.to_vec())).map_err(|_| "connection is closed".to_string())
    }
    pub fn resize(&self, cols: u16, rows: u16) {
        let _ = self.tx.send(Cmd::Resize(cols, rows));
    }
    pub fn close(&self) {
        let _ = self.tx.send(Cmd::Close);
    }
}

/// Receives everything a connection produces.
pub trait Sink: Send + Sync + 'static {
    fn bytes(&self, data: &[u8]);
    fn message(&self, text: &str);
    fn closed(&self);
}

pub fn connect(spec: ConnSpec, sink: Arc<dyn Sink>, cols: u16, rows: u16) -> Result<Conn, String> {
    let (tx, rx) = unbounded::<Cmd>();
    let alive = Arc::new(AtomicBool::new(true));
    match spec.clone() {
        ConnSpec::Ssh { host, username, password, port, look_for_keys } => {
            sink.message(&format!("\r\n[SSH connecting to {host}:{port} as {username}]\r\n"));
            let (sess, mut channel) = ssh_open(&host, port, &username, &password, look_for_keys, cols, rows)?;
            sink.message("\r\n[SSH connected with interactive PTY]\r\n");
            let alive2 = alive.clone();
            std::thread::spawn(move || {
                let _keep = sess;
                let mut buf = [0u8; 8192];
                let mut pending: Vec<u8> = Vec::new();
                'outer: loop {
                    // incoming
                    match channel.read(&mut buf) {
                        Ok(0) => {
                            if channel.eof() {
                                break;
                            }
                        }
                        Ok(n) => sink.bytes(&buf[..n]),
                        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                        Err(e) => {
                            sink.message(&format!("\r\n[SSH read error: {e}]\r\n"));
                            break;
                        }
                    }
                    if channel.eof() {
                        break;
                    }
                    // stderr is merged into the pty, nothing to do
                    // commands
                    while let Ok(cmd) = rx.try_recv() {
                        match cmd {
                            Cmd::Data(d) => pending.extend_from_slice(&d),
                            Cmd::Resize(c, r) => {
                                let _ = channel.request_pty_size(c.max(20) as u32, r.max(5) as u32, None, None);
                            }
                            Cmd::Close => break 'outer,
                        }
                    }
                    if !pending.is_empty() {
                        match channel.write(&pending) {
                            Ok(n) => {
                                pending.drain(..n);
                                let _ = channel.flush();
                            }
                            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                            Err(e) => {
                                sink.message(&format!("\r\n[SSH write error: {e}]\r\n"));
                                break;
                            }
                        }
                    }
                    std::thread::sleep(Duration::from_millis(8));
                }
                let _ = channel.close();
                alive2.store(false, Ordering::SeqCst);
                sink.message("\r\n[SSH session closed]\r\n");
                sink.closed();
            });
        }
        ConnSpec::Telnet { host, port } => {
            let addr = (host.as_str(), port).to_socket_addrs().map_err(|e| e.to_string())?.next().ok_or("host not found")?;
            let stream = TcpStream::connect_timeout(&addr, Duration::from_secs(10)).map_err(|e| e.to_string())?;
            stream.set_read_timeout(Some(Duration::from_millis(15))).ok();
            stream.set_nodelay(true).ok();
            sink.message(&format!("[Telnet connected to {host}:{port}]\r\n"));
            let alive2 = alive.clone();
            std::thread::spawn(move || {
                let mut stream = stream;
                let mut proto = TelnetProtocol::default();
                let mut buf = [0u8; 4096];
                'outer: loop {
                    match stream.read(&mut buf) {
                        Ok(0) => break,
                        Ok(n) => {
                            let (payload, replies) = proto.feed(&buf[..n]);
                            if !replies.is_empty() {
                                let _ = stream.write_all(&replies);
                            }
                            if !payload.is_empty() {
                                sink.bytes(&payload);
                            }
                        }
                        Err(e) if matches!(e.kind(), std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut) => {}
                        Err(e) => {
                            sink.message(&format!("\r\n[Telnet read error: {e}]\r\n"));
                            break;
                        }
                    }
                    while let Ok(cmd) = rx.try_recv() {
                        match cmd {
                            Cmd::Data(d) => {
                                if stream.write_all(&TelnetProtocol::escape(&d)).is_err() {
                                    break 'outer;
                                }
                            }
                            Cmd::Resize(..) => {}
                            Cmd::Close => break 'outer,
                        }
                    }
                }
                let _ = stream.shutdown(std::net::Shutdown::Both);
                alive2.store(false, Ordering::SeqCst);
                sink.message("\r\n[Telnet session closed]\r\n");
                sink.closed();
            });
        }
        ConnSpec::Serial { port, baud, bytesize, parity, stopbits } => {
            let data_bits = match bytesize {
                5 => serialport::DataBits::Five,
                6 => serialport::DataBits::Six,
                7 => serialport::DataBits::Seven,
                _ => serialport::DataBits::Eight,
            };
            let par = match parity.to_ascii_uppercase() {
                'E' => serialport::Parity::Even,
                'O' => serialport::Parity::Odd,
                _ => serialport::Parity::None,
            };
            let stop = if stopbits >= 2.0 { serialport::StopBits::Two } else { serialport::StopBits::One };
            let mut sp = serialport::new(&port, baud)
                .data_bits(data_bits)
                .parity(par)
                .stop_bits(stop)
                .timeout(Duration::from_millis(20))
                .open()
                .map_err(|e| e.to_string())?;
            sink.message(&format!("[Serial connected: {port} @ {baud}]\r\n"));
            let alive2 = alive.clone();
            std::thread::spawn(move || {
                let mut buf = [0u8; 4096];
                'outer: loop {
                    match sp.read(&mut buf) {
                        Ok(0) => {}
                        Ok(n) => sink.bytes(&buf[..n]),
                        Err(e) if matches!(e.kind(), std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock) => {}
                        Err(e) => {
                            sink.message(&format!("\r\n[Serial read error: {e}]\r\n"));
                            break;
                        }
                    }
                    while let Ok(cmd) = rx.try_recv() {
                        match cmd {
                            Cmd::Data(d) => {
                                if sp.write_all(&d).is_err() {
                                    break 'outer;
                                }
                                let _ = sp.flush();
                            }
                            Cmd::Resize(..) => {}
                            Cmd::Close => break 'outer,
                        }
                    }
                }
                alive2.store(false, Ordering::SeqCst);
                sink.message("\r\n[Serial session closed]\r\n");
                sink.closed();
            });
        }
    }
    Ok(Conn { spec, tx, alive })
}

/// Open an SSH session with an interactive shell on a PTY (non-blocking afterwards).
fn ssh_open(
    host: &str,
    port: u16,
    username: &str,
    password: &str,
    look_for_keys: bool,
    cols: u16,
    rows: u16,
) -> Result<(ssh2::Session, ssh2::Channel), String> {
    let sess = ssh_session(host, port, username, password, look_for_keys)?;
    let mut channel = sess.channel_session().map_err(|e| e.to_string())?;
    channel
        .request_pty("xterm", None, Some((cols.max(20) as u32, rows.max(5) as u32, 0, 0)))
        .map_err(|e| e.to_string())?;
    channel.shell().map_err(|e| e.to_string())?;
    sess.set_blocking(false);
    Ok((sess, channel))
}

/// Authenticated blocking SSH session (also used for SFTP).
pub fn ssh_session(host: &str, port: u16, username: &str, password: &str, look_for_keys: bool) -> Result<ssh2::Session, String> {
    let addr = (host, port).to_socket_addrs().map_err(|e| format!("{host}: {e}"))?.next().ok_or("host not found")?;
    let tcp = TcpStream::connect_timeout(&addr, Duration::from_secs(15)).map_err(|e| e.to_string())?;
    ssh_session_over(tcp, host, username, password, look_for_keys)
}

/// Same as [`ssh_session`] but over an already connected stream (used for jump hops).
pub fn ssh_session_over(tcp: TcpStream, host: &str, username: &str, password: &str, look_for_keys: bool) -> Result<ssh2::Session, String> {
    tcp.set_nodelay(true).ok();
    let mut sess = ssh2::Session::new().map_err(|e| e.to_string())?;
    sess.set_tcp_stream(tcp);
    sess.set_timeout(15_000);
    sess.handshake().map_err(|e| e.to_string())?;
    let user = if username.is_empty() { std::env::var("USER").unwrap_or_default() } else { username.to_string() };
    let mut last_err = String::from("authentication failed");
    if !password.is_empty() {
        match sess.userauth_password(&user, password) {
            Ok(()) => {}
            Err(e) => last_err = e.to_string(),
        }
        if !sess.authenticated() {
            // keyboard-interactive is what many servers (and some devices) expect
            struct P(String);
            impl ssh2::KeyboardInteractivePrompt for P {
                fn prompt(&mut self, _u: &str, _i: &str, prompts: &[ssh2::Prompt<'_>]) -> Vec<String> {
                    prompts.iter().map(|_| self.0.clone()).collect()
                }
            }
            if sess.userauth_keyboard_interactive(&user, &mut P(password.to_string())).is_ok() {}
        }
    }
    if !sess.authenticated() && (look_for_keys || password.is_empty()) {
        if sess.userauth_agent(&user).is_ok() && sess.authenticated() {
            // agent worked
        } else if let Some(home) = dirs::home_dir() {
            for name in ["id_ed25519", "id_rsa", "id_ecdsa"] {
                let key = home.join(".ssh").join(name);
                if key.exists() && sess.userauth_pubkey_file(&user, None, &key, if password.is_empty() { None } else { Some(password) }).is_ok() && sess.authenticated() {
                    break;
                }
            }
        }
    }
    if !sess.authenticated() {
        return Err(format!("SSH authentication failed for {user}@{host}: {last_err}"));
    }
    sess.set_timeout(0);
    Ok(sess)
}

// ----------------------------------------------------------------------------------------
// Telnet IAC handling (port of TelnetProtocol)
// ----------------------------------------------------------------------------------------

const IAC: u8 = 255;
const DONT: u8 = 254;
const DO: u8 = 253;
const WONT: u8 = 252;
const WILL: u8 = 251;
const SB: u8 = 250;
const SE: u8 = 240;
const OPT_ECHO: u8 = 1;
const OPT_SGA: u8 = 3;

#[derive(Default, Clone, Copy, PartialEq)]
enum TState {
    #[default]
    Data,
    Iac,
    Option,
    Sub,
    SubIac,
}

#[derive(Default)]
pub struct TelnetProtocol {
    state: TState,
    cmd: u8,
    answered: HashSet<(u8, u8)>,
}

impl TelnetProtocol {
    /// Returns `(payload, replies)`: application bytes with protocol commands removed, and the
    /// negotiation answers that must be sent back.
    pub fn feed(&mut self, data: &[u8]) -> (Vec<u8>, Vec<u8>) {
        let mut out = Vec::new();
        let mut replies = Vec::new();
        for &b in data {
            match self.state {
                TState::Data => {
                    if b == IAC {
                        self.state = TState::Iac
                    } else {
                        out.push(b)
                    }
                }
                TState::Iac => {
                    if b == IAC {
                        out.push(IAC);
                        self.state = TState::Data;
                    } else if matches!(b, WILL | WONT | DO | DONT) {
                        self.cmd = b;
                        self.state = TState::Option;
                    } else if b == SB {
                        self.state = TState::Sub;
                    } else {
                        self.state = TState::Data;
                    }
                }
                TState::Option => {
                    replies.extend(self.negotiate(self.cmd, b));
                    self.state = TState::Data;
                }
                TState::Sub => {
                    if b == IAC {
                        self.state = TState::SubIac
                    }
                }
                TState::SubIac => self.state = if b == SE { TState::Data } else { TState::Sub },
            }
        }
        (out, replies)
    }

    fn negotiate(&mut self, cmd: u8, option: u8) -> Vec<u8> {
        if self.answered.contains(&(cmd, option)) || matches!(cmd, WONT | DONT) {
            return vec![];
        }
        self.answered.insert((cmd, option));
        if cmd == WILL {
            vec![IAC, if option == OPT_ECHO || option == OPT_SGA { DO } else { DONT }, option]
        } else {
            vec![IAC, WONT, option]
        }
    }

    pub fn escape(data: &[u8]) -> Vec<u8> {
        let mut out = Vec::with_capacity(data.len());
        for &b in data {
            out.push(b);
            if b == IAC {
                out.push(IAC);
            }
        }
        out
    }
}

/// Friendly serial port names, e.g. `/dev/cu.usbserial-1410 (USB Serial)`.
pub fn list_serial_ports() -> Vec<(String, String)> {
    serialport::available_ports()
        .unwrap_or_default()
        .into_iter()
        .map(|p| {
            let desc = match &p.port_type {
                serialport::SerialPortType::UsbPort(u) => {
                    u.product.clone().map(|pr| format!("{pr} ({})", p.port_name)).unwrap_or_else(|| p.port_name.clone())
                }
                _ => p.port_name.clone(),
            };
            (p.port_name, desc)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn telnet_strips_iac() {
        let mut p = TelnetProtocol::default();
        let (out, replies) = p.feed(&[b'h', IAC, WILL, OPT_ECHO, b'i', IAC, DO, 31, IAC, IAC]);
        assert_eq!(out, vec![b'h', b'i', IAC]);
        assert_eq!(replies, vec![IAC, DO, OPT_ECHO, IAC, WONT, 31]);
    }

    #[test]
    fn telnet_split_sequence() {
        let mut p = TelnetProtocol::default();
        let (a, _) = p.feed(&[b'a', IAC]);
        let (b, _) = p.feed(&[WILL, 24, b'b']);
        assert_eq!([a, b].concat(), b"ab".to_vec());
    }

    #[test]
    fn telnet_escape() {
        assert_eq!(TelnetProtocol::escape(&[1, IAC, 2]), vec![1, IAC, IAC, 2]);
    }
}
