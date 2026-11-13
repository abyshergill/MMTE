//! Secondary windows bundled in `Extras`: text editor, SFTP browser, suite (DAW), profiles, AI.

use super::dialogs::Dlg;
use super::editor::{Editor, EditorEvent, RemoteInfo};
use super::theme;
use crate::conn::ConnSpec;
use crate::session::{Hub, Session};
use crate::sftp::{self, Entry, HopSpec, SftpClient, SharedSftp};
use crate::suite::Suite;
use crossbeam_channel::{unbounded, Receiver, Sender};
use eframe::egui::{self, RichText};
use parking_lot::Mutex;
use serde_json::json;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;

// ------------------------------------------------------------------------------------------
// SFTP browser
// ------------------------------------------------------------------------------------------

enum SftpMsg {
    Connected { client: SharedSftp, home: String },
    Listed { path: String, result: Result<Vec<Entry>, String> },
    Status(String),
    Refresh,
    Hops(Vec<String>, usize),
}

#[derive(Default)]
struct HopForm {
    host: String,
    port: String,
    user: String,
    password: String,
}

pub struct SftpWin {
    pub open: bool,
    session: Option<Arc<Session>>,
    client: Option<SharedSftp>,
    labels: Vec<String>,
    active_hop: usize,
    path: String,
    path_input: String,
    entries: Vec<Entry>,
    error: Option<String>,
    selected: Option<usize>,
    status: String,
    rx: Receiver<SftpMsg>,
    tx: Sender<SftpMsg>,
    hop_form: Option<HopForm>,
    new_folder: Option<String>,
    confirm_delete: Option<usize>,
    editor_tx: Sender<EditorEvent>,
}

impl SftpWin {
    fn new(editor_tx: Sender<EditorEvent>) -> Self {
        let (tx, rx) = unbounded();
        Self { open: false, session: None, client: None, labels: vec!["Connecting...".into()], active_hop: 0, path: ".".into(), path_input: String::new(), entries: Vec::new(), error: None, selected: None, status: String::new(), rx, tx, hop_form: None, new_folder: None, confirm_delete: None, editor_tx }
    }

    fn start(&mut self, session: Arc<Session>, spec: HopSpec) {
        self.session = Some(session);
        self.open = true;
        self.client = None;
        self.entries.clear();
        self.labels = vec!["Connecting...".into()];
        self.status = "Connecting...".into();
        let tx = self.tx.clone();
        std::thread::spawn(move || {
            match SftpClient::open(spec) {
                Ok(c) => {
                    let home = c.realpath(".").unwrap_or_else(|_| "/".into());
                    let _ = tx.send(SftpMsg::Connected { client: Arc::new(Mutex::new(c)), home });
                }
                Err(e) => {
                    let _ = tx.send(SftpMsg::Status(format!("SFTP connect failed: {e}")));
                }
            }
            crate::session::request_repaint();
        });
    }

    fn refresh(&mut self) {
        let Some(c) = self.client.clone() else { return };
        let path = self.path.clone();
        let tx = self.tx.clone();
        self.status = "Loading...".into();
        std::thread::spawn(move || {
            let result = c.lock().list(&path);
            let _ = tx.send(SftpMsg::Listed { path, result });
            crate::session::request_repaint();
        });
    }

    fn spawn<F: FnOnce(&SftpClient, &Sender<SftpMsg>) + Send + 'static>(&self, f: F) {
        let Some(c) = self.client.clone() else { return };
        let tx = self.tx.clone();
        std::thread::spawn(move || {
            f(&c.lock(), &tx);
            crate::session::request_repaint();
        });
    }

    fn poll(&mut self) {
        while let Ok(m) = self.rx.try_recv() {
            match m {
                SftpMsg::Connected { client, home } => {
                    self.labels = client.lock().labels();
                    self.active_hop = 0;
                    self.client = Some(client);
                    self.path = home;
                    self.refresh();
                }
                SftpMsg::Listed { path, result } => {
                    if path == self.path {
                        self.path_input = path;
                        self.selected = None;
                        match result {
                            Ok(e) => {
                                self.status = format!("{} items", e.len());
                                self.entries = e;
                                self.error = None;
                            }
                            Err(e) => {
                                self.entries.clear();
                                self.error = Some(e);
                                self.status = "Error loading directory".into();
                            }
                        }
                    }
                }
                SftpMsg::Status(s) => self.status = s,
                SftpMsg::Refresh => self.refresh(),
                SftpMsg::Hops(labels, active) => {
                    self.labels = labels;
                    self.active_hop = active;
                    self.path = ".".into();
                    // resolve "." on the new hop
                    if let Some(c) = self.client.clone() {
                        let tx = self.tx.clone();
                        std::thread::spawn(move || {
                            let home = c.lock().realpath(".").unwrap_or_else(|_| "/".into());
                            let _ = tx.send(SftpMsg::Listed { path: home.clone(), result: c.lock().list(&home) });
                            crate::session::request_repaint();
                        });
                        self.path = "\u{0}".into(); // placeholder until Listed arrives
                    }
                }
            }
        }
        // the Listed message for a hop switch carries a path we have not set yet
    }

    fn selected_entry(&self) -> Option<&Entry> {
        self.selected.and_then(|i| self.entries.get(i))
    }

    fn show(&mut self, ctx: &egui::Context, editor: &mut Editor) {
        self.poll();
        // a hop switch leaves the placeholder path; accept the next Listed
        if !self.open {
            return;
        }
        let title = format!("Remote Files \u{2014} {}", self.session.as_ref().map(|s| s.name()).unwrap_or_default());
        let mut open = self.open;
        egui::Window::new(title).open(&mut open).default_size([940.0, 560.0]).show(ctx, |ui| {
            // hop bar
            ui.horizontal(|ui| {
                let mut chosen = None;
                egui::ComboBox::from_id_salt("sftp_hop").width(360.0).selected_text(self.labels.get(self.active_hop).cloned().unwrap_or_default()).show_ui(ui, |ui| {
                    for (i, l) in self.labels.iter().enumerate() {
                        if ui.selectable_label(i == self.active_hop, l).clicked() {
                            chosen = Some(i);
                        }
                    }
                });
                if let Some(i) = chosen {
                    if let Some(c) = &self.client {
                        c.lock().select_hop(i);
                        let (labels, active) = (c.lock().labels(), c.lock().active);
                        let _ = self.tx.send(SftpMsg::Hops(labels, active));
                    }
                }
                if ui.button("+ Add Jump Hop").clicked() {
                    self.hop_form = Some(HopForm { port: "22".into(), ..Default::default() });
                }
                if ui.add(theme::danger_button("Remove Last Hop")).clicked() {
                    if let Some(c) = &self.client {
                        c.lock().remove_last_hop();
                        let (labels, active) = (c.lock().labels(), c.lock().active);
                        let _ = self.tx.send(SftpMsg::Hops(labels, active));
                    }
                }
            });
            // path bar
            let mut go: Option<String> = None;
            ui.horizontal(|ui| {
                let r = ui.add(egui::TextEdit::singleline(&mut self.path_input).desired_width(ui.available_width() - 230.0));
                if r.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter)) {
                    go = Some(self.path_input.clone());
                }
                if ui.button("Go").clicked() {
                    go = Some(self.path_input.clone());
                }
                if ui.button("Up").clicked() {
                    go = Some(sftp::parent_remote(&self.path));
                }
                if ui.button("Refresh").clicked() {
                    self.refresh();
                }
            });
            if let Some(p) = go {
                self.path = p;
                self.refresh();
            }
            // actions
            let mut action: Option<&'static str> = None;
            ui.horizontal_wrapped(|ui| {
                for (label, key, accent) in [("New Folder...", "mkdir", false), ("Upload File...", "upload", true), ("Download Selected", "download", true), ("Upload Folder...", "upload_dir", true), ("Download Folder", "download_dir", true), ("Open in Editor", "edit", false)] {
                    let b = if accent { ui.add(theme::accent_button(label)) } else { ui.button(label) };
                    if b.clicked() {
                        action = Some(key);
                    }
                }
                if ui.add(theme::danger_button("Delete Selected")).clicked() {
                    action = Some("delete");
                }
            });
            self.do_action(action, editor);
            // list
            let mut open_idx: Option<usize> = None;
            egui::ScrollArea::vertical().auto_shrink([false, false]).max_height(ui.available_height() - 26.0).show(ui, |ui| {
                if let Some(e) = &self.error {
                    ui.label(RichText::new(format!("\u{26A0}  Failed to open this folder: {e}")).color(theme::ERR));
                } else if self.entries.is_empty() {
                    ui.label(RichText::new("(this folder is empty)").color(theme::MUTE));
                }
                for (i, e) in self.entries.iter().enumerate() {
                    let text = if e.is_dir { format!("\u{1F4C1}  {}", e.name) } else { format!("\u{1F4C4}  {}   ({} bytes)", e.name, e.size) };
                    let r = ui.selectable_label(self.selected == Some(i), text);
                    if r.clicked() {
                        self.selected = Some(i);
                    }
                    if r.double_clicked() {
                        open_idx = Some(i);
                    }
                }
            });
            ui.label(RichText::new(&self.status).color(theme::MUTE).size(12.5));
            if let Some(i) = open_idx {
                if let Some(e) = self.entries.get(i).cloned() {
                    if e.is_dir {
                        self.path = sftp::join_remote(&self.path, &e.name);
                        self.refresh();
                    } else {
                        self.selected = Some(i);
                        self.do_action(Some("edit"), editor);
                    }
                }
            }
        });
        // dropped local files upload into the current folder
        let dropped: Vec<PathBuf> = ctx.input(|i| i.raw.dropped_files.iter().map(|f| f.path().to_path_buf()).collect());
        if !dropped.is_empty() && open && self.client.is_some() {
            self.upload_paths(dropped);
        }
        self.dialogs(ctx);
        self.open = open;
    }

    fn upload_paths(&mut self, paths: Vec<PathBuf>) {
        let dir = self.path.clone();
        self.status = format!("Uploading {} item(s) ...", paths.len());
        self.spawn(move |c, tx| {
            for p in paths {
                let name = p.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
                let remote = sftp::join_remote(&dir, &name);
                let res = if p.is_dir() { c.upload_folder(&p, &remote, &mut |f| { let _ = tx.send(SftpMsg::Status(format!("Uploading {f} ..."))); }) } else { c.upload(&p, &remote) };
                if let Err(e) = res {
                    let _ = tx.send(SftpMsg::Status(format!("Upload failed ({name}): {e}")));
                    return;
                }
            }
            let _ = tx.send(SftpMsg::Status("Upload complete".into()));
            let _ = tx.send(SftpMsg::Refresh);
        });
    }

    fn do_action(&mut self, action: Option<&'static str>, editor: &mut Editor) {
        let Some(action) = action else { return };
        let dir = self.path.clone();
        let sel = self.selected_entry().cloned();
        match action {
            "mkdir" => self.new_folder = Some(String::new()),
            "upload" => {
                if let Some(files) = rfd::FileDialog::new().set_title("Select file(s) to upload").pick_files() {
                    self.upload_paths(files);
                }
            }
            "upload_dir" => {
                if let Some(d) = rfd::FileDialog::new().set_title("Select folder to upload").pick_folder() {
                    self.upload_paths(vec![d]);
                }
            }
            "download" => match sel {
                Some(e) if !e.is_dir => {
                    if let Some(dest) = rfd::FileDialog::new().set_file_name(&e.name).save_file() {
                        let remote = sftp::join_remote(&dir, &e.name);
                        self.status = format!("Downloading {} ...", e.name);
                        self.spawn(move |c, tx| {
                            let _ = tx.send(SftpMsg::Status(match c.download(&remote, &dest) {
                                Ok(()) => format!("Downloaded {} to {}", e.name, dest.display()),
                                Err(er) => format!("Download failed: {er}"),
                            }));
                        });
                    }
                }
                _ => self.status = "Select a file to download".into(),
            },
            "download_dir" => match sel {
                Some(e) if e.is_dir => {
                    if let Some(dest) = rfd::FileDialog::new().set_title("Choose where to save the folder").pick_folder() {
                        let remote = sftp::join_remote(&dir, &e.name);
                        let target = dest.join(&e.name);
                        self.status = format!("Downloading folder {} ...", e.name);
                        self.spawn(move |c, tx| {
                            let res = c.download_folder(&remote, &target, &mut |f| { let _ = tx.send(SftpMsg::Status(format!("Downloading {f} ..."))); });
                            let _ = tx.send(SftpMsg::Status(match res {
                                Ok(()) => format!("Downloaded folder to {}", target.display()),
                                Err(er) => format!("Folder download failed: {er}"),
                            }));
                        });
                    }
                }
                _ => self.status = "Select a folder to download".into(),
            },
            "edit" => match (sel, self.client.clone()) {
                (Some(e), Some(c)) if !e.is_dir => {
                    let remote = sftp::join_remote(&dir, &e.name);
                    let label = c.lock().label();
                    editor.open_remote(c, remote.clone(), label, false);
                    editor.visible = true;
                    self.status = format!("Opening {remote} in the editor ...");
                }
                _ => self.status = "Select a remote file (not a folder) to edit.".into(),
            },
            "delete" => {
                if self.selected.is_some() {
                    self.confirm_delete = self.selected;
                }
            }
            _ => {}
        }
    }

    fn dialogs(&mut self, ctx: &egui::Context) {
        if let Some(name) = self.new_folder.as_mut() {
            let mut result: Option<Option<String>> = None;
            egui::Window::new("New Folder").collapsible(false).resizable(false).anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0]).show(ctx, |ui| {
                ui.label("Folder name:");
                let r = ui.add(egui::TextEdit::singleline(name).desired_width(240.0));
                let enter = r.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
                ui.horizontal(|ui| {
                    if ui.add(theme::accent_button("Create")).clicked() || enter {
                        result = Some(Some(name.trim().to_string()));
                    }
                    if ui.button("Cancel").clicked() {
                        result = Some(None);
                    }
                });
            });
            if let Some(r) = result {
                self.new_folder = None;
                if let Some(n) = r.filter(|n| !n.is_empty()) {
                    if n.contains('/') || n == "." || n == ".." {
                        self.status = "Folder name cannot contain '/' or be '.' / '..'.".into();
                    } else {
                        let remote = sftp::join_remote(&self.path, &n);
                        self.spawn(move |c, tx| {
                            let _ = tx.send(SftpMsg::Status(match c.mkdir(&remote) {
                                Ok(()) => format!("Created folder {n}"),
                                Err(e) => format!("Create folder failed: {e}"),
                            }));
                            let _ = tx.send(SftpMsg::Refresh);
                        });
                    }
                }
            }
        }
        if let Some(i) = self.confirm_delete {
            let Some(e) = self.entries.get(i).cloned() else {
                self.confirm_delete = None;
                return;
            };
            let mut ans: Option<bool> = None;
            egui::Window::new("Delete").collapsible(false).resizable(false).anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0]).show(ctx, |ui| {
                ui.label(format!("Delete {} '{}'{}?", if e.is_dir { "folder" } else { "file" }, e.name, if e.is_dir { " and everything inside it" } else { "" }));
                ui.horizontal(|ui| {
                    if ui.add(theme::danger_button("Delete")).clicked() {
                        ans = Some(true);
                    }
                    if ui.button("Cancel").clicked() {
                        ans = Some(false);
                    }
                });
            });
            if let Some(a) = ans {
                self.confirm_delete = None;
                if a {
                    let remote = sftp::join_remote(&self.path, &e.name);
                    self.spawn(move |c, tx| {
                        let _ = tx.send(SftpMsg::Status(match c.remove(&remote, e.is_dir) {
                            Ok(()) => format!("Deleted {}", e.name),
                            Err(er) => format!("Delete failed: {er}"),
                        }));
                        let _ = tx.send(SftpMsg::Refresh);
                    });
                }
            }
        }
        if let Some(f) = self.hop_form.as_mut() {
            let mut result: Option<bool> = None;
            egui::Window::new("Add Jump Hop").collapsible(false).resizable(false).anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0]).show(ctx, |ui| {
                ui.heading("Tunnel to Next Server");
                egui::Grid::new("hop_grid").num_columns(2).show(ui, |ui| {
                    ui.label("Host/IP");
                    ui.add(egui::TextEdit::singleline(&mut f.host).desired_width(220.0));
                    ui.end_row();
                    ui.label("Port");
                    ui.add(egui::TextEdit::singleline(&mut f.port).desired_width(80.0));
                    ui.end_row();
                    ui.label("Username");
                    ui.add(egui::TextEdit::singleline(&mut f.user).desired_width(220.0));
                    ui.end_row();
                    ui.label("Password");
                    ui.add(egui::TextEdit::singleline(&mut f.password).password(true).desired_width(220.0));
                    ui.end_row();
                });
                ui.horizontal(|ui| {
                    if ui.add(theme::accent_button("Connect")).clicked() {
                        result = Some(true);
                    }
                    if ui.button("Cancel").clicked() {
                        result = Some(false);
                    }
                });
            });
            if let Some(go) = result {
                let form = self.hop_form.take().unwrap();
                if go && !form.host.trim().is_empty() {
                    if let (Some(c), Ok(port)) = (self.client.clone(), form.port.trim().parse::<u16>()) {
                        self.status = format!("Connecting to {} ...", form.host);
                        let tx = self.tx.clone();
                        let spec = HopSpec { host: form.host.trim().into(), port, username: form.user.trim().into(), password: form.password };
                        std::thread::spawn(move || {
                            let res = c.lock().add_hop(spec);
                            match res {
                                Ok(()) => {
                                    let (labels, active) = { let g = c.lock(); (g.labels(), g.active) };
                                    let _ = tx.send(SftpMsg::Hops(labels, active));
                                }
                                Err(e) => {
                                    let _ = tx.send(SftpMsg::Status(format!("Add hop failed: {e}")));
                                }
                            }
                            crate::session::request_repaint();
                        });
                    }
                }
            }
        }
    }
}

// ------------------------------------------------------------------------------------------
// Extras
// ------------------------------------------------------------------------------------------

pub struct Daw {
    pub open: bool,
    pub suite: Suite,
}

pub struct Extras {
    pub hub: Arc<Hub>,
    pub plugin_rx: Option<Receiver<Vec<crate::plugins::PluginInfo>>>,
    pub editor: Editor,
    pub daw: Daw,
    pub sftp: SftpWin,
    pub profiles_open: bool,
    profile_tab: usize,
    profile_edit: Option<(bool, String, String, String, String, String)>, // (is_ssh, orig_name, name, host, user, port)
    sftp_cache: Arc<Mutex<HashMap<u64, SharedSftp>>>,
    pub ai: super::ai_panel::AiPanel,
}

impl Extras {
    pub fn new(hub: Arc<Hub>) -> Self {
        let editor = Editor::new();
        let sftp = SftpWin::new(editor.sender());
        Self { ai: super::ai_panel::AiPanel::new(hub.clone()), hub, plugin_rx: None, editor, daw: Daw { open: false, suite: Suite::new() }, sftp, profiles_open: false, profile_tab: 0, profile_edit: None, sftp_cache: Arc::new(Mutex::new(HashMap::new())) }
    }

    pub fn ai_menu(&mut self, ui: &mut egui::Ui) {
        self.ai.menu(ui);
    }

    pub fn right_panel(&mut self, ui: &mut egui::Ui, hub: &Arc<Hub>) {
        self.ai.right_panel(ui, hub);
    }

    pub fn any_modal_open(&self) -> bool {
        self.sftp.new_folder.is_some() || self.sftp.hop_form.is_some() || self.sftp.confirm_delete.is_some() || self.ai.wants_keyboard()
    }

    fn spec_for(session: &Session) -> Option<HopSpec> {
        match session.spec.lock().clone() {
            Some(ConnSpec::Ssh { host, username, password, port, .. }) if session.conn_kind() == "SSH" => Some(HopSpec { host, port, username, password }),
            _ => None,
        }
    }

    pub fn open_sftp(&mut self, session: Option<Arc<Session>>) -> Result<(), String> {
        let Some(s) = session else { return Err("Open a terminal first".into()) };
        let Some(spec) = Self::spec_for(&s) else { return Err("Active connection is not SSH".into()) };
        if self.sftp.open && self.sftp.session.as_ref().map(|x| x.id) == Some(s.id) {
            return Ok(());
        }
        self.sftp.start(s, spec);
        Ok(())
    }

    /// `vim file` typed in an SSH terminal: edit the file(s) in the built-in editor over SFTP.
    pub fn open_files_from_terminal(&mut self, s: &Arc<Session>, command: &str, sudo: bool, files: Vec<String>) {
        let Some(spec) = Self::spec_for(s) else { return };
        s.send("\x15"); // wipe the typed command from the remote shell's input line
        let (session, hub, tx, cache) = (s.clone(), self.hub.clone(), self.editor.sender(), self.sftp_cache.clone());
        let command = command.to_string();
        let label = spec.label();
        std::thread::spawn(move || {
            let fallback = |why: String| {
                let msg = format!("The file could not be opened in the built-in editor:\n\n{why}\n\nRun '{command}' in the terminal instead?");
                hub.set_status(&format!("Built-in editor: {why}"));
                if session.is_connected() && hub.confirm("Open in terminal instead?", &msg) {
                    session.send(&format!("{command}\r"));
                }
            };
            let client = {
                let existing = cache.lock().get(&session.id).cloned();
                match existing {
                    Some(c) => c,
                    None => match SftpClient::open(spec) {
                        Ok(c) => {
                            let c = Arc::new(Mutex::new(c));
                            cache.lock().insert(session.id, c.clone());
                            c
                        }
                        Err(e) => return fallback(e),
                    },
                }
            };
            let home = client.lock().realpath(".").unwrap_or_else(|_| "/".into());
            let needs_cwd = files.iter().any(|f| !f.starts_with('/') && !f.starts_with('~'));
            let mut cwd = home.clone();
            if needs_cwd {
                let token: String = format!("{:x}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0) & 0xffffff);
                session.send(&format!(" printf '__MMTE_CWD_{token}__%s__END\\n' \"$PWD\"\r"));
                let re = fancy_regex::Regex::new(&format!(r"__MMTE_CWD_{token}__(/[^\r\n]*?)__END")).unwrap();
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(6);
                let mut found = None;
                while std::time::Instant::now() < deadline {
                    if let Ok(Some(c)) = re.captures(&session.buf.lock().clone()) {
                        found = c.get(1).map(|m| m.as_str().to_string());
                        break;
                    }
                    std::thread::sleep(std::time::Duration::from_millis(100));
                }
                match found {
                    Some(f) => cwd = f,
                    None => return fallback("could not read the shell's working directory".into()),
                }
            }
            for f in files {
                let path = resolve_remote_path(&f, &cwd, &home);
                crate::ui::editor::load_remote(&tx, client.clone(), path, label.clone(), true);
            }
            let note = if sudo { "  (opened with your SSH login; saving may need write permission)" } else { "" };
            hub.set_status(&format!("Editing in the built-in editor{note}"));
        });
    }

    pub fn show_all(&mut self, ctx: &egui::Context) {
        self.editor.show(ctx);
        self.sftp.show(ctx, &mut self.editor);
        for p in self.editor.saved_remote.drain(..) {
            let _ = p;
            let _ = self.sftp.tx.send(SftpMsg::Refresh);
        }
        let _ = &self.sftp.editor_tx;
    }
}

pub fn resolve_remote_path(path: &str, cwd: &str, home: &str) -> String {
    let norm = |p: String| -> String {
        let mut parts: Vec<&str> = Vec::new();
        for seg in p.split('/') {
            match seg {
                "" | "." => {}
                ".." => {
                    parts.pop();
                }
                s => parts.push(s),
            }
        }
        format!("/{}", parts.join("/"))
    };
    if path == "~" {
        home.to_string()
    } else if let Some(rest) = path.strip_prefix("~/") {
        norm(format!("{home}/{rest}"))
    } else if path.starts_with('/') {
        norm(path.to_string())
    } else {
        norm(format!("{cwd}/{path}"))
    }
}

impl super::app::App {
    pub fn extras_windows(&mut self, ctx: &egui::Context) {
        self.extras.show_all(ctx);
        self.daw_window(ctx);
        self.profiles_window(ctx);
        self.extras.ai.windows(ctx, &self.hub);
    }

    fn daw_window(&mut self, ctx: &egui::Context) {
        if !self.extras.daw.open {
            return;
        }
        let mut open = true;
        let hub = self.hub.clone();
        let suite = self.extras.daw.suite.clone();
        let dirs = self.plugin_dirs.clone();
        let sessions = hub.list();
        let running = suite.running.load(std::sync::atomic::Ordering::SeqCst);
        let mut launch: Option<Vec<usize>> = None;
        let mut swap: Option<(usize, usize)> = None;
        let mut remove: Option<usize> = None;
        let mut add_files = false;
        egui::Window::new("DevOps Suite DAW Manager").open(&mut open).default_size([880.0, 520.0]).show(ctx, |ui| {
            ui.horizontal(|ui| {
                ui.heading("Multi-Script DAW Suite");
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    let mut beep = suite.beep.load(std::sync::atomic::Ordering::SeqCst);
                    if ui.checkbox(&mut beep, "Audio chime on Human Gate").changed() {
                        suite.beep.store(beep, std::sync::atomic::Ordering::SeqCst);
                    }
                });
            });
            ui.separator();
            let count = suite.items.lock().len();
            egui::ScrollArea::vertical().max_height(ui.available_height() - 50.0).auto_shrink([false, false]).show(ui, |ui| {
                if count == 0 {
                    ui.add_space(20.0);
                    ui.centered_and_justified(|ui| ui.label(RichText::new("[ Suite is empty. Click 'Add Script Files' below. ]").color(theme::MUTE)));
                }
                let mut items = suite.items.lock();
                let n = items.len();
                for (idx, item) in items.iter_mut().enumerate() {
                    egui::Frame::new().fill(theme::BG2).stroke(egui::Stroke::new(1.0, theme::LINE)).corner_radius(8.0).inner_margin(8.0).show(ui, |ui| {
                        ui.horizontal(|ui| {
                            ui.label(RichText::new(format!("{}.", idx + 1)).strong());
                            ui.label(RichText::new(item.name()).monospace()).on_hover_text(item.path.display().to_string());
                            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                                if ui.add(egui::Button::new("\u{00D7}").min_size(egui::vec2(28.0, 28.0))).on_hover_text("Remove script from suite").clicked() {
                                    remove = Some(idx);
                                }
                                if ui.add_enabled(!running, egui::Button::new("\u{25B6} Run")).on_hover_text("Run only this script (single-bench debug)").clicked() {
                                    launch = Some(vec![idx]);
                                }
                                if ui.add_enabled(idx + 1 < n, egui::Button::new("\u{2193}").min_size(egui::vec2(28.0, 28.0))).clicked() {
                                    swap = Some((idx, idx + 1));
                                }
                                if ui.add_enabled(idx > 0, egui::Button::new("\u{2191}").min_size(egui::vec2(28.0, 28.0))).clicked() {
                                    swap = Some((idx, idx - 1));
                                }
                                let st = item.status.split('(').next().unwrap_or("").trim().to_string();
                                let color = match st.as_str() {
                                    "Pending" => theme::MUTE,
                                    "Running" => egui::Color32::from_rgb(0xff, 0xd5, 0x4f),
                                    "Passed" | "Passed" => theme::TERM_ACCENT,
                                    s if s.starts_with("GATE") => egui::Color32::from_rgb(0xff, 0x57, 0x22),
                                    _ => theme::DIM,
                                };
                                ui.add_sized([120.0, 20.0], egui::Label::new(RichText::new(&item.status).color(color)));
                                ui.label("s");
                                let mut t = format!("{}", item.timeout as i64);
                                if ui.add(egui::TextEdit::singleline(&mut t).desired_width(40.0)).changed() {
                                    if let Ok(v) = t.parse::<f64>() {
                                        item.timeout = v;
                                    }
                                }
                                ui.label("Wait:");
                                let cur = sessions.iter().find(|s| s.id == item.session_id).map(|s| s.name()).unwrap_or_else(|| sessions.first().map(|s| s.name()).unwrap_or_default());
                                egui::ComboBox::from_id_salt(("daw_sess", idx)).width(110.0).selected_text(cur).show_ui(ui, |ui| {
                                    for s in &sessions {
                                        if ui.selectable_label(s.id == item.session_id, s.name()).clicked() {
                                            item.session_id = s.id;
                                        }
                                    }
                                });
                            });
                        });
                    });
                }
            });
            ui.separator();
            ui.horizontal(|ui| {
                if ui.button("+ Add Script Files").clicked() {
                    add_files = true;
                }
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if ui.add_enabled(count > 0 && !running, theme::accent_button("\u{25B6} Launch Full Suite")).clicked() {
                        launch = Some((0..count).collect());
                    }
                    if ui.add_enabled(running, theme::danger_button("\u{23F9} Stop Suite")).clicked() {
                        suite.stop_all(&hub);
                    }
                    let rs = suite.current_running_session(&hub);
                    let paused = rs.as_ref().map(|s| s.script.is_paused()).unwrap_or(false);
                    if ui.add_enabled(rs.is_some(), egui::Button::new(if paused { "\u{25B6} Resume" } else { "\u{23F8} Pause" })).clicked() {
                        if let Some(s) = &rs {
                            s.toggle_pause_script();
                        }
                    }
                });
            });
        });
        if add_files {
            let sid = self.current().map(|s| s.id).unwrap_or(0);
            if hub.list().is_empty() {
                self.dialogs.message("No terminal", "Open a terminal tab first; scripts run inside a terminal.");
            } else if let Some(files) = rfd::FileDialog::new().add_filter("Scripts", &["ttl", "py"]).add_filter("All Files", &["*"]).pick_files() {
                for f in files {
                    suite.add(f, sid);
                }
            }
        }
        if let Some((a, b)) = swap {
            suite.items.lock().swap(a, b);
        }
        if let Some(i) = remove {
            suite.items.lock().remove(i);
        }
        if let Some(idx) = launch {
            suite.launch(hub, dirs, idx);
        }
        self.extras.daw.open = open;
    }

    fn profiles_window(&mut self, ctx: &egui::Context) {
        if !self.extras.profiles_open {
            return;
        }
        let mut open = true;
        let mut connect: Option<(bool, String)> = None;
        let mut delete: Option<(bool, String)> = None;
        let mut edit: Option<(bool, String)> = None;
        let mut add = false;
        egui::Window::new("Saved Profiles").open(&mut open).default_size([620.0, 420.0]).show(ctx, |ui| {
            ui.horizontal(|ui| {
                ui.selectable_value(&mut self.extras.profile_tab, 0, "SSH");
                ui.selectable_value(&mut self.extras.profile_tab, 1, "Telnet");
            });
            ui.separator();
            let ssh = self.extras.profile_tab == 0;
            let store = if ssh { &self.ssh_profiles } else { &self.telnet_profiles };
            let mut names = store.names();
            names.sort();
            egui::ScrollArea::vertical().max_height(300.0).show(ui, |ui| {
                if names.is_empty() {
                    ui.label(RichText::new("No saved profiles yet.").color(theme::MUTE));
                }
                for n in &names {
                    let p = &store.profiles[n];
                    ui.horizontal(|ui| {
                        ui.label(RichText::new(n).strong());
                        let who = if ssh { format!("{}@{}:{}", p["username"].as_str().unwrap_or(""), p["host"].as_str().unwrap_or(""), p["port"].as_u64().unwrap_or(22)) } else { format!("{}:{}", p["host"].as_str().unwrap_or(""), p["port"].as_u64().unwrap_or(23)) };
                        ui.label(RichText::new(who).color(theme::DIM));
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if ui.add(theme::danger_button("Delete")).clicked() {
                                delete = Some((ssh, n.clone()));
                            }
                            if ui.button("Edit").clicked() {
                                edit = Some((ssh, n.clone()));
                            }
                            if ui.add(theme::accent_button("Connect")).clicked() {
                                connect = Some((ssh, n.clone()));
                            }
                        });
                    });
                }
            });
            ui.separator();
            if ui.button("+ Add Profile").clicked() {
                add = true;
            }
        });
        if add {
            let ssh = self.extras.profile_tab == 0;
            self.extras.profile_edit = Some((ssh, String::new(), String::new(), String::new(), String::new(), if ssh { "22".into() } else { "23".into() }));
        }
        if let Some((ssh, n)) = edit {
            let store = if ssh { &self.ssh_profiles } else { &self.telnet_profiles };
            if let Some(p) = store.profiles.get(&n) {
                self.extras.profile_edit = Some((ssh, n.clone(), n.clone(), p["host"].as_str().unwrap_or("").into(), p["username"].as_str().unwrap_or("").into(), p["port"].as_u64().unwrap_or(22).to_string()));
            }
        }
        if let Some((ssh, n)) = delete {
            if ssh { self.ssh_profiles.delete(&n) } else { self.telnet_profiles.delete(&n) }
        }
        if let Some((ssh, n)) = connect {
            if ssh { self.dialogs.open_ssh(&self.ssh_profiles, Some(&n)) } else { self.dialogs.open_telnet(&self.telnet_profiles, Some(&n)) }
        }
        let mut save: Option<(bool, String, String, String, String, String)> = None;
        let mut cancel = false;
        if let Some((ssh, orig, name, host, user, port)) = self.extras.profile_edit.as_mut() {
            egui::Window::new(if orig.is_empty() { "New Profile" } else { "Edit Profile" }).collapsible(false).resizable(false).anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0]).show(ctx, |ui| {
                egui::Grid::new("pf_grid").num_columns(2).show(ui, |ui| {
                    ui.label("Profile name");
                    ui.add(egui::TextEdit::singleline(name).desired_width(240.0));
                    ui.end_row();
                    ui.label("Host/IP");
                    ui.add(egui::TextEdit::singleline(host).desired_width(240.0));
                    ui.end_row();
                    if *ssh {
                        ui.label("Username");
                        ui.add(egui::TextEdit::singleline(user).desired_width(240.0));
                        ui.end_row();
                    }
                    ui.label("Port");
                    ui.add(egui::TextEdit::singleline(port).desired_width(80.0));
                    ui.end_row();
                });
                theme::hint(ui, "Passwords are never saved; you are asked when you connect.");
                ui.horizontal(|ui| {
                    if ui.add(theme::accent_button("Save")).clicked() {
                        save = Some((*ssh, orig.clone(), name.clone(), host.clone(), user.clone(), port.clone()));
                    }
                    if ui.button("Cancel").clicked() {
                        cancel = true;
                    }
                });
            });
        }
        if let Some((ssh, orig, name, host, user, port)) = save {
            let name = name.trim().to_string();
            if name.is_empty() || host.trim().is_empty() {
                self.dialogs.message("Missing information", "Enter a profile name and a host.");
            } else if let Ok(p) = port.trim().parse::<u16>() {
                let store = if ssh { &mut self.ssh_profiles } else { &mut self.telnet_profiles };
                if !orig.is_empty() && orig != name {
                    store.delete(&orig);
                }
                if ssh {
                    store.upsert(&name, json!({"host": host.trim(), "username": user.trim(), "port": p}));
                } else {
                    store.upsert(&name, json!({"host": host.trim(), "port": p}));
                }
                self.extras.profile_edit = None;
            } else {
                self.dialogs.message("Invalid port", "Port must be numeric.");
            }
        } else if cancel {
            self.extras.profile_edit = None;
        }
        self.extras.profiles_open = open;
        let _ = Dlg::None;
        let _: Option<RemoteInfo> = None;
        let _: Option<PathBuf> = None;
    }
}
