//! Built-in text editor (own OS window): tabs, line numbers, syntax colours, find / replace,
//! go to line, local files and files over SFTP.

use super::theme;
use crate::editlogic::{self as el, Class};
use crate::sftp::SharedSftp;
use crossbeam_channel::{unbounded, Receiver, Sender};
use eframe::egui::{self, text::LayoutJob, Color32, FontId, Key, Modifiers, RichText, TextFormat};
use std::hash::{Hash, Hasher};
use std::path::{Path, PathBuf};
use std::sync::Arc;

pub struct RemoteInfo {
    pub sftp: SharedSftp,
    pub path: String,
    pub label: String,
}

pub struct Doc {
    pub id: u64,
    pub title: String,
    pub local: Option<PathBuf>,
    pub remote: Option<RemoteInfo>,
    pub text: String,
    saved_hash: u64,
    pub lang: String,
    pub eol: &'static str,
    pub encoding: String,
    pub bom: bool,
    spans: Option<(u64, Arc<Vec<(usize, usize, Class)>>)>,
    pub cursor: (usize, usize), // (primary, secondary) char indices
    reveal: Option<usize>,
    pending_sel: Option<(usize, usize)>,
    pub suggested_path: Option<PathBuf>,
}

fn hash_str(s: &str) -> u64 {
    let mut h = std::collections::hash_map::DefaultHasher::new();
    s.hash(&mut h);
    h.finish()
}

impl Doc {
    pub fn dirty(&self) -> bool {
        hash_str(&self.text) != self.saved_hash
    }
    pub fn display_path(&self) -> String {
        if let Some(r) = &self.remote {
            if r.label.is_empty() { r.path.clone() } else { format!("{}:{}", r.label, r.path) }
        } else {
            self.local.as_ref().map(|p| p.display().to_string()).unwrap_or_else(|| self.title.clone())
        }
    }
    fn spans(&mut self) -> Arc<Vec<(usize, usize, Class)>> {
        if self.text.len() > 400_000 {
            return Arc::new(Vec::new());
        }
        let key = hash_str(&self.text) ^ hash_str(&self.lang).rotate_left(7);
        if let Some((k, s)) = &self.spans {
            if *k == key {
                return s.clone();
            }
        }
        let s = Arc::new(el::lex(&self.text, &self.lang));
        self.spans = Some((key, s.clone()));
        s
    }
}

pub enum EditorEvent {
    Loaded { remote: RemoteInfo, bytes: Vec<u8>, created: bool },
    Saved { doc_id: u64, path: String },
    Failed(String),
}

#[derive(Default)]
struct FindState {
    open: bool,
    replace: bool,
    query: String,
    with: String,
    case: bool,
    regex: bool,
    status: String,
    focus: bool,
}

pub struct Editor {
    pub visible: bool,
    pub docs: Vec<Doc>,
    active: usize,
    next_id: u64,
    find: FindState,
    goto: Option<(String, bool)>,
    pub wrap: bool,
    pub font_size: f32,
    pub status: String,
    tx: Sender<EditorEvent>,
    rx: Receiver<EditorEvent>,
    pub saved_remote: Vec<String>,
    confirm_close: Option<usize>,
    focus_editor: bool,
}

impl Editor {
    pub fn new() -> Self {
        let (tx, rx) = unbounded();
        Self { visible: false, docs: Vec::new(), active: 0, next_id: 1, find: FindState::default(), goto: None, wrap: false, font_size: 14.0, status: String::new(), tx, rx, saved_remote: Vec::new(), confirm_close: None, focus_editor: false }
    }

    pub fn sender(&self) -> Sender<EditorEvent> {
        self.tx.clone()
    }

    fn add(&mut self, mut doc: Doc, text: String, lang: Option<&str>) {
        doc.lang = lang.map(|l| l.to_string()).unwrap_or_else(|| el::detect_language(&doc.title, &text).to_string());
        doc.saved_hash = hash_str(&text);
        doc.text = text;
        self.docs.push(doc);
        self.active = self.docs.len() - 1;
        self.visible = true;
        self.focus_editor = true;
    }

    fn blank(&mut self, title: &str) -> Doc {
        let id = self.next_id;
        self.next_id += 1;
        Doc { id, title: title.to_string(), local: None, remote: None, text: String::new(), saved_hash: 0, lang: String::new(), eol: "\n", encoding: "utf-8".into(), bom: false, spans: None, cursor: (0, 0), reveal: None, pending_sel: None, suggested_path: None }
    }

    pub fn new_document(&mut self) {
        let n = self.docs.iter().filter(|d| d.local.is_none() && d.remote.is_none()).count() + 1;
        let d = self.blank(&format!("untitled-{n}"));
        self.add(d, String::new(), Some(""));
    }

    pub fn open_text(&mut self, title: &str, text: String, language: &str, suggested: Option<PathBuf>) {
        let mut d = self.blank(title);
        d.suggested_path = suggested;
        self.add(d, text, Some(language));
    }

    pub fn open_local(&mut self, path: &Path) {
        if let Some(i) = self.docs.iter().position(|d| d.local.as_deref() == Some(path)) {
            self.active = i;
            self.visible = true;
            return;
        }
        match std::fs::read(path) {
            Ok(raw) => match el::decode_bytes(&raw) {
                Ok(dec) => {
                    let mut d = self.blank(&path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default());
                    d.local = Some(path.to_path_buf());
                    d.encoding = dec.encoding;
                    d.bom = dec.bom;
                    d.eol = dec.eol;
                    self.add(d, dec.text, None);
                }
                Err(e) => self.status = e,
            },
            Err(e) => self.status = format!("Could not open {}: {e}", path.display()),
        }
    }

    pub fn open_remote(&mut self, sftp: SharedSftp, path: String, label: String, create_if_missing: bool) {
        if let Some(i) = self.docs.iter().position(|d| d.remote.as_ref().map(|r| r.path == path && r.label == label).unwrap_or(false)) {
            self.active = i;
            self.visible = true;
            return;
        }
        self.status = format!("Opening {path} ...");
        load_remote(&self.tx, sftp, path, label, create_if_missing);
    }

    pub fn poll(&mut self) {
        while let Ok(ev) = self.rx.try_recv() {
            match ev {
                EditorEvent::Loaded { remote, bytes, created } => match el::decode_bytes(&bytes) {
                    Ok(dec) => {
                        let name = remote.path.rsplit('/').next().unwrap_or("file").to_string();
                        let msg = if created { format!("{} does not exist yet - it will be created when you save", remote.path) } else { format!("Opened {}", remote.path) };
                        let mut d = self.blank(&name);
                        d.encoding = dec.encoding;
                        d.bom = dec.bom;
                        d.eol = dec.eol;
                        d.remote = Some(remote);
                        self.add(d, dec.text, None);
                        self.status = msg;
                    }
                    Err(e) => self.status = e,
                },
                EditorEvent::Saved { doc_id, path } => {
                    if let Some(d) = self.docs.iter_mut().find(|d| d.id == doc_id) {
                        d.saved_hash = hash_str(&d.text);
                    }
                    self.status = format!("Saved {path}");
                    self.saved_remote.push(path);
                }
                EditorEvent::Failed(m) => self.status = m,
            }
        }
    }

    // --- saving ---------------------------------------------------------------------
    pub fn save_doc(&mut self, idx: usize) -> bool {
        let Some(d) = self.docs.get(idx) else { return false };
        let data = el::encode_text(&d.text, &d.encoding, d.bom, d.eol);
        if let Some(r) = &d.remote {
            let (sftp, path, id) = (r.sftp.clone(), r.path.clone(), d.id);
            let tx = self.tx.clone();
            self.status = format!("Saving {path} ...");
            std::thread::spawn(move || {
                let res = sftp.lock().write_file(&path, &data);
                let _ = tx.send(match res {
                    Ok(()) => EditorEvent::Saved { doc_id: id, path },
                    Err(e) => EditorEvent::Failed(format!("Save failed ({path}): {e}")),
                });
                crate::session::request_repaint();
            });
            return true;
        }
        let path = match &d.local {
            Some(p) => p.clone(),
            None => return self.save_as(idx),
        };
        match std::fs::write(&path, data) {
            Ok(()) => {
                self.docs[idx].saved_hash = hash_str(&self.docs[idx].text);
                self.status = format!("Saved {}", path.display());
                true
            }
            Err(e) => {
                self.status = format!("Save failed: {e}");
                false
            }
        }
    }

    pub fn save_as(&mut self, idx: usize) -> bool {
        let Some(d) = self.docs.get(idx) else { return false };
        let mut dlg = rfd::FileDialog::new().set_file_name(&d.title);
        if let Some(p) = d.suggested_path.as_ref().and_then(|p| p.parent()) {
            dlg = dlg.set_directory(p);
        }
        if let Some(path) = dlg.save_file() {
            let d = &mut self.docs[idx];
            d.title = path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
            d.local = Some(path);
            d.remote = None;
            d.lang = el::detect_language(&d.title, &d.text).to_string();
            return self.save_doc(idx);
        }
        false
    }

    pub fn close_doc(&mut self, idx: usize) {
        if idx < self.docs.len() {
            self.docs.remove(idx);
            if self.active >= self.docs.len() {
                self.active = self.docs.len().saturating_sub(1);
            }
        }
    }

    fn request_close(&mut self, idx: usize) {
        if self.docs.get(idx).map(|d| d.dirty()).unwrap_or(false) {
            self.confirm_close = Some(idx);
        } else {
            self.close_doc(idx);
        }
    }

    // --- text operations ---------------------------------------------------------------
    fn set_selection(&mut self, a: usize, b: usize, reveal: bool) {
        if let Some(d) = self.docs.get_mut(self.active) {
            d.pending_sel = Some((a, b));
            d.cursor = (a, b);
            if reveal {
                d.reveal = Some(b);
            }
        }
    }

    fn selection(&self) -> (usize, usize) {
        self.docs.get(self.active).map(|d| (d.cursor.0.min(d.cursor.1), d.cursor.0.max(d.cursor.1))).unwrap_or((0, 0))
    }

    fn find_next(&mut self, forward: bool) -> bool {
        let Some(d) = self.docs.get(self.active) else { return false };
        let re = match el::build_pattern(&self.find.query, self.find.case, self.find.regex) {
            Ok(Some(r)) => r,
            Ok(None) => {
                self.find.status.clear();
                return false;
            }
            Err(e) => {
                self.find.status = format!("Invalid pattern: {e}");
                return false;
            }
        };
        let matches = el::find_all(&d.text, &re);
        if matches.is_empty() {
            self.find.status = "No matches".into();
            return false;
        }
        let (s, e) = self.selection();
        let pick = if forward {
            matches.iter().find(|m| m.0 >= e && !(m.0 == s && m.1 == e && s != e && false)).or(matches.first()).copied()
        } else {
            matches.iter().rev().find(|m| m.1 <= s).or(matches.last()).copied()
        };
        if let Some((a, b)) = pick {
            let idx = matches.iter().position(|m| *m == (a, b)).unwrap_or(0) + 1;
            self.find.status = format!("{idx} of {}", matches.len());
            self.set_selection(a, b, true);
            return true;
        }
        false
    }

    fn replace_one(&mut self) {
        let (s, e) = self.selection();
        let Ok(Some(re)) = el::build_pattern(&self.find.query, self.find.case, self.find.regex) else { return };
        let (query_regex, with) = (self.find.regex, self.find.with.clone());
        if let Some(d) = self.docs.get_mut(self.active) {
            let bs = el::char_to_byte(&d.text, s);
            let be = el::char_to_byte(&d.text, e);
            let sel = d.text[bs..be].to_string();
            // only replace when the selection is itself a match
            if s != e && re.is_match(&sel).unwrap_or(false) {
                let (rep, _) = el::replace_all(&sel, &re, &with, query_regex);
                d.text.replace_range(bs..be, &rep);
                let ne = s + rep.chars().count();
                d.pending_sel = Some((ne, ne));
                d.cursor = (ne, ne);
            }
        }
        self.find_next(true);
    }

    fn replace_everything(&mut self) {
        let Ok(Some(re)) = el::build_pattern(&self.find.query, self.find.case, self.find.regex) else { return };
        let (rx, with) = (self.find.regex, self.find.with.clone());
        if let Some(d) = self.docs.get_mut(self.active) {
            let (t, n) = el::replace_all(&d.text, &re, &with, rx);
            d.text = t;
            self.find.status = format!("Replaced {n} match(es)");
        }
    }

    /// Apply `f(text, sel_start, sel_end) -> (new_text, new_start, new_end)` to the active doc.
    fn transform(&mut self, f: impl FnOnce(&str, usize, usize) -> (String, usize, usize)) {
        let (s, e) = self.selection();
        if let Some(d) = self.docs.get_mut(self.active) {
            let (t, a, b) = f(&d.text, s, e);
            d.text = t;
            d.pending_sel = Some((a, b));
            d.cursor = (a, b);
        }
    }

    fn line_range(text: &str, s: usize, e: usize) -> (usize, usize) {
        // char offsets of the first char of the first selected line and the end of the last
        let chars: Vec<char> = text.chars().collect();
        let mut a = s.min(chars.len());
        while a > 0 && chars[a - 1] != '\n' {
            a -= 1;
        }
        let mut end = e.min(chars.len());
        if end > s && end > 0 && chars[end - 1] == '\n' {
            end -= 1;
        }
        let mut b = end;
        while b < chars.len() && chars[b] != '\n' {
            b += 1;
        }
        (a, b)
    }

    fn indent(&mut self, dir: i32) {
        self.transform(|text, s, e| {
            let (a, b) = Self::line_range(text, s, e);
            let chars: Vec<char> = text.chars().collect();
            let block: String = chars[a..b].iter().collect();
            let new_block: Vec<String> = block
                .split('\n')
                .map(|l| {
                    if dir > 0 {
                        format!("    {l}")
                    } else {
                        let strip = l.chars().take(4).take_while(|c| *c == ' ').count();
                        if strip == 0 && l.starts_with('\t') { l[1..].to_string() } else { l[strip..].to_string() }
                    }
                })
                .collect();
            let nb = new_block.join("\n");
            let mut out: String = chars[..a].iter().collect();
            out.push_str(&nb);
            out.extend(chars[b..].iter());
            let na = a;
            let nbend = a + nb.chars().count();
            (out, na, nbend)
        });
    }

    fn toggle_comment(&mut self) {
        let prefix = self.docs.get(self.active).map(|d| el::comment_prefix(&d.lang)).unwrap_or("#");
        self.transform(|text, s, e| {
            let (a, b) = Self::line_range(text, s, e);
            let chars: Vec<char> = text.chars().collect();
            let block: String = chars[a..b].iter().collect();
            let lines: Vec<&str> = block.split('\n').collect();
            let all = lines.iter().filter(|l| !l.trim().is_empty()).all(|l| l.trim_start().starts_with(prefix));
            let new_lines: Vec<String> = lines
                .iter()
                .map(|l| {
                    if l.trim().is_empty() {
                        l.to_string()
                    } else if all {
                        let indent = l.len() - l.trim_start().len();
                        let rest = &l.trim_start()[prefix.len()..];
                        let rest = rest.strip_prefix(' ').unwrap_or(rest);
                        format!("{}{}", &l[..indent], rest)
                    } else {
                        let indent = l.len() - l.trim_start().len();
                        format!("{}{} {}", &l[..indent], prefix, l.trim_start())
                    }
                })
                .collect();
            let nb = new_lines.join("\n");
            let mut out: String = chars[..a].iter().collect();
            out.push_str(&nb);
            out.extend(chars[b..].iter());
            (out, a, a + nb.chars().count())
        });
    }

    fn duplicate_line(&mut self) {
        self.transform(|text, s, e| {
            let (a, b) = Self::line_range(text, s, e);
            let chars: Vec<char> = text.chars().collect();
            let block: String = chars[a..b].iter().collect();
            let mut out: String = chars[..b].iter().collect();
            out.push('\n');
            out.push_str(&block);
            out.extend(chars[b..].iter());
            let shift = block.chars().count() + 1;
            (out, s + shift, e + shift)
        });
    }

    fn goto_line(&mut self, line: usize) {
        if let Some(d) = self.docs.get(self.active) {
            let total = d.text.matches('\n').count() + 1;
            let l = line.clamp(1, total);
            let off = el::line_start_offset(&d.text, l);
            self.set_selection(off, off, true);
        }
    }

    // --- UI --------------------------------------------------------------------------------
    pub fn show(&mut self, ctx: &egui::Context) {
        self.poll();
        if !self.visible {
            return;
        }
        let builder = egui::ViewportBuilder::default().with_title("Text Editor").with_inner_size([1000.0, 700.0]).with_min_inner_size([640.0, 400.0]);
        let mut want_close = false;
        ctx.show_viewport_immediate(egui::ViewportId::from_hash_of("mmte_text_editor"), builder, |ui, _class| {
            let ctx = ui.ctx().clone();
            if ctx.input(|i| i.viewport().close_requested()) {
                if self.docs.iter().any(|d| d.dirty()) {
                    ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
                    self.confirm_close = Some(usize::MAX);
                } else {
                    want_close = true;
                }
            }
            self.shortcuts(&ctx);
            egui::Panel::top("ed_menu").show_inside(ui, |ui| self.menu(ui));
            egui::Panel::bottom("ed_status").show_inside(ui, |ui| self.status_bar(ui));
            egui::Panel::top("ed_tabs").show_inside(ui, |ui| self.tabs(ui));
            if self.find.open {
                egui::Panel::top("ed_find").show_inside(ui, |ui| self.find_bar(ui));
            }
            egui::CentralPanel::default().frame(egui::Frame::default().fill(theme::BG0)).show_inside(ui, |ui| {
                if self.docs.is_empty() {
                    ui.centered_and_justified(|ui| {
                        ui.label(RichText::new("No document open - File > New or Open").color(theme::MUTE));
                    });
                } else {
                    self.editor_area(ui);
                }
            });
            self.dialogs(&ctx);
        });
        if want_close {
            self.visible = false;
        }
    }

    fn shortcuts(&mut self, ctx: &egui::Context) {
        let cmd = Modifiers::COMMAND;
        let pressed = |ctx: &egui::Context, m: Modifiers, k: Key| ctx.input_mut(|i| i.consume_key(m, k));
        if pressed(ctx, cmd, Key::S) {
            let a = self.active;
            self.save_doc(a);
        }
        if pressed(ctx, cmd | Modifiers::SHIFT, Key::S) {
            let a = self.active;
            self.save_as(a);
        }
        if pressed(ctx, cmd, Key::N) {
            self.new_document();
        }
        if pressed(ctx, cmd, Key::O) {
            self.open_dialog();
        }
        if pressed(ctx, cmd, Key::W) {
            let a = self.active;
            self.request_close(a);
        }
        if pressed(ctx, cmd, Key::F) {
            self.find.open = true;
            self.find.replace = false;
            self.find.focus = true;
        }
        if pressed(ctx, cmd, Key::H) {
            self.find.open = true;
            self.find.replace = true;
            self.find.focus = true;
        }
        if pressed(ctx, cmd, Key::G) {
            self.goto = Some((String::new(), true));
        }
        if pressed(ctx, Modifiers::NONE, Key::F3) {
            self.find_next(true);
        }
        if pressed(ctx, Modifiers::SHIFT, Key::F3) {
            self.find_next(false);
        }
        if pressed(ctx, cmd, Key::Slash) {
            self.toggle_comment();
        }
        if pressed(ctx, cmd, Key::D) {
            self.duplicate_line();
        }
        if pressed(ctx, cmd, Key::Equals) || pressed(ctx, cmd, Key::Plus) {
            self.font_size = (self.font_size + 1.0).min(40.0);
        }
        if pressed(ctx, cmd, Key::Minus) {
            self.font_size = (self.font_size - 1.0).max(8.0);
        }
        if pressed(ctx, cmd, Key::Num0) {
            self.font_size = 14.0;
        }
        if self.find.open && pressed(ctx, Modifiers::NONE, Key::Escape) {
            self.find.open = false;
            self.focus_editor = true;
        }
        // Tab / Shift+Tab indent (consumed so the text box does not insert a tab character)
        let has_sel = {
            let (s, e) = self.selection();
            s != e
        };
        if !self.find.focus && ctx.memory(|m| m.focused()).map(|f| f == self.editor_id()).unwrap_or(false) {
            if pressed(ctx, Modifiers::SHIFT, Key::Tab) {
                self.indent(-1);
            } else if pressed(ctx, Modifiers::NONE, Key::Tab) {
                if has_sel {
                    self.indent(1);
                } else {
                    self.insert_at_cursor("    ");
                }
            }
        }
    }

    fn insert_at_cursor(&mut self, s: &str) {
        let (a, b) = self.selection();
        let ins = s.to_string();
        self.transform(|text, _, _| {
            let ba = el::char_to_byte(text, a);
            let bb = el::char_to_byte(text, b);
            let mut out = text.to_string();
            out.replace_range(ba..bb, &ins);
            let np = a + ins.chars().count();
            (out, np, np)
        });
    }

    fn editor_id(&self) -> egui::Id {
        egui::Id::new(("ed_text", self.docs.get(self.active).map(|d| d.id).unwrap_or(0)))
    }

    fn open_dialog(&mut self) {
        if let Some(files) = rfd::FileDialog::new().pick_files() {
            for f in files {
                self.open_local(&f);
            }
        }
    }

    fn menu(&mut self, ui: &mut egui::Ui) {
        egui::MenuBar::new().ui(ui, |ui| {
            ui.menu_button("File", |ui| {
                if ui.add(egui::Button::new("New").shortcut_text("Ctrl+N")).clicked() {
                    self.new_document();
                    ui.close();
                }
                if ui.add(egui::Button::new("Open...").shortcut_text("Ctrl+O")).clicked() {
                    self.open_dialog();
                    ui.close();
                }
                ui.separator();
                if ui.add(egui::Button::new("Save").shortcut_text("Ctrl+S")).clicked() {
                    let a = self.active;
                    self.save_doc(a);
                    ui.close();
                }
                if ui.add(egui::Button::new("Save As...").shortcut_text("Ctrl+Shift+S")).clicked() {
                    let a = self.active;
                    self.save_as(a);
                    ui.close();
                }
                if ui.button("Save All").clicked() {
                    for i in 0..self.docs.len() {
                        if self.docs[i].dirty() {
                            self.save_doc(i);
                        }
                    }
                    ui.close();
                }
                if ui.button("Reload from Disk").clicked() {
                    if let Some(p) = self.docs.get(self.active).and_then(|d| d.local.clone()) {
                        let a = self.active;
                        self.docs.remove(a);
                        self.open_local(&p);
                    }
                    ui.close();
                }
                ui.separator();
                if ui.add(egui::Button::new("Close Tab").shortcut_text("Ctrl+W")).clicked() {
                    let a = self.active;
                    self.request_close(a);
                    ui.close();
                }
            });
            ui.menu_button("Edit", |ui| {
                if ui.add(egui::Button::new("Toggle Comment").shortcut_text("Ctrl+/")).clicked() {
                    self.toggle_comment();
                    ui.close();
                }
                if ui.add(egui::Button::new("Duplicate Line").shortcut_text("Ctrl+D")).clicked() {
                    self.duplicate_line();
                    ui.close();
                }
                if ui.button("Indent").clicked() {
                    self.indent(1);
                    ui.close();
                }
                if ui.button("Unindent").clicked() {
                    self.indent(-1);
                    ui.close();
                }
                ui.separator();
                ui.menu_button("Line Endings", |ui| {
                    for (label, eol) in [("Unix (LF)", "\n"), ("Windows (CRLF)", "\r\n")] {
                        if ui.button(label).clicked() {
                            if let Some(d) = self.docs.get_mut(self.active) {
                                d.eol = if eol == "\n" { "\n" } else { "\r\n" };
                            }
                            ui.close();
                        }
                    }
                });
            });
            ui.menu_button("Search", |ui| {
                if ui.add(egui::Button::new("Find...").shortcut_text("Ctrl+F")).clicked() {
                    self.find.open = true;
                    self.find.replace = false;
                    self.find.focus = true;
                    ui.close();
                }
                if ui.add(egui::Button::new("Replace...").shortcut_text("Ctrl+H")).clicked() {
                    self.find.open = true;
                    self.find.replace = true;
                    self.find.focus = true;
                    ui.close();
                }
                if ui.add(egui::Button::new("Find Next").shortcut_text("F3")).clicked() {
                    self.find_next(true);
                    ui.close();
                }
                if ui.add(egui::Button::new("Find Previous").shortcut_text("Shift+F3")).clicked() {
                    self.find_next(false);
                    ui.close();
                }
                if ui.add(egui::Button::new("Go to Line...").shortcut_text("Ctrl+G")).clicked() {
                    self.goto = Some((String::new(), true));
                    ui.close();
                }
            });
            ui.menu_button("View", |ui| {
                ui.checkbox(&mut self.wrap, "Word Wrap");
                if ui.add(egui::Button::new("Zoom In").shortcut_text("Ctrl++")).clicked() {
                    self.font_size = (self.font_size + 1.0).min(40.0);
                }
                if ui.add(egui::Button::new("Zoom Out").shortcut_text("Ctrl+-")).clicked() {
                    self.font_size = (self.font_size - 1.0).max(8.0);
                }
                if ui.add(egui::Button::new("Reset Zoom").shortcut_text("Ctrl+0")).clicked() {
                    self.font_size = 14.0;
                }
            });
            ui.menu_button("Language", |ui| {
                for (label, alias) in el::LANGUAGES {
                    let current = self.docs.get(self.active).map(|d| d.lang == *alias).unwrap_or(false);
                    if ui.selectable_label(current, *label).clicked() {
                        if let Some(d) = self.docs.get_mut(self.active) {
                            d.lang = alias.to_string();
                        }
                        ui.close();
                    }
                }
            });
        });
    }

    fn tabs(&mut self, ui: &mut egui::Ui) {
        let mut select = None;
        let mut close = None;
        egui::ScrollArea::horizontal().id_salt("ed_tab_scroll").show(ui, |ui| {
            ui.horizontal(|ui| {
                for (i, d) in self.docs.iter().enumerate() {
                    let title = format!("{}{}", d.title, if d.dirty() { " \u{25CF}" } else { "" });
                    let sel = i == self.active;
                    let r = ui.selectable_label(sel, title).on_hover_text(d.display_path());
                    if r.clicked() {
                        select = Some(i);
                    }
                    if r.middle_clicked() {
                        close = Some(i);
                    }
                    if ui.add(egui::Button::new("\u{00D7}").frame(false)).clicked() {
                        close = Some(i);
                    }
                    ui.add_space(6.0);
                }
            });
        });
        if let Some(i) = select {
            self.active = i;
            self.focus_editor = true;
        }
        if let Some(i) = close {
            self.request_close(i);
        }
    }

    fn find_bar(&mut self, ui: &mut egui::Ui) {
        let mut next = false;
        let mut prev = false;
        let mut rep = false;
        let mut rep_all = false;
        let mut close = false;
        ui.horizontal(|ui| {
            ui.label("Find");
            let r = ui.add(egui::TextEdit::singleline(&mut self.find.query).desired_width(240.0));
            if self.find.focus {
                r.request_focus();
                self.find.focus = false;
            }
            if r.changed() {
                self.find.status.clear();
            }
            if r.lost_focus() && ui.input(|i| i.key_pressed(Key::Enter)) {
                if ui.input(|i| i.modifiers.shift) { prev = true } else { next = true }
                r.request_focus();
            }
            if ui.button("\u{2191}").on_hover_text("Previous (Shift+F3)").clicked() {
                prev = true;
            }
            if ui.button("\u{2193}").on_hover_text("Next (F3)").clicked() {
                next = true;
            }
            ui.toggle_value(&mut self.find.case, "Aa").on_hover_text("Match case");
            ui.toggle_value(&mut self.find.regex, ".*").on_hover_text("Regular expression");
            let matches = self.docs.get(self.active).and_then(|d| el::build_pattern(&self.find.query, self.find.case, self.find.regex).ok().flatten().map(|re| el::find_all(&d.text, &re).len()));
            if let Some(n) = matches {
                ui.label(RichText::new(format!("{n} match(es)")).color(theme::DIM));
            }
            if !self.find.status.is_empty() {
                ui.label(RichText::new(&self.find.status).color(theme::WARN));
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("\u{00D7}").clicked() {
                    close = true;
                }
            });
        });
        if self.find.replace {
            ui.horizontal(|ui| {
                ui.label("Replace");
                ui.add(egui::TextEdit::singleline(&mut self.find.with).desired_width(240.0));
                if ui.button("Replace").clicked() {
                    rep = true;
                }
                if ui.button("Replace All").clicked() {
                    rep_all = true;
                }
            });
        }
        if next {
            self.find_next(true);
        }
        if prev {
            self.find_next(false);
        }
        if rep {
            self.replace_one();
        }
        if rep_all {
            self.replace_everything();
        }
        if close {
            self.find.open = false;
        }
    }

    fn status_bar(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            if let Some(d) = self.docs.get(self.active) {
                let (l, c) = el::line_col(&d.text, d.cursor.0);
                ui.label(RichText::new(format!("Ln {l}, Col {c}")).color(theme::DIM));
                ui.separator();
                ui.label(RichText::new(el::language_label(&d.lang)).color(theme::DIM));
                ui.separator();
                ui.label(RichText::new(if d.eol == "\r\n" { "CRLF" } else { "LF" }).color(theme::DIM));
                ui.separator();
                ui.label(RichText::new(format!("{}{}", d.encoding.to_uppercase(), if d.bom { " BOM" } else { "" })).color(theme::DIM));
                ui.separator();
                ui.label(RichText::new(format!("{} chars", d.text.chars().count())).color(theme::DIM));
                if d.remote.is_some() {
                    ui.separator();
                    ui.label(RichText::new("remote").color(theme::INFO));
                }
            }
            if !self.status.is_empty() {
                ui.separator();
                ui.label(RichText::new(&self.status).color(theme::MUTE));
            }
        });
    }

    fn editor_area(&mut self, ui: &mut egui::Ui) {
        let active = self.active.min(self.docs.len() - 1);
        self.active = active;
        let font = FontId::monospace(self.font_size);
        let wrap = self.wrap;
        let id = self.editor_id();
        let want_focus = std::mem::take(&mut self.focus_editor);
        let doc = &mut self.docs[active];
        let spans = doc.spans();
        let text_len_before = doc.text.len();
        let pending = doc.pending_sel.take();
        let reveal = doc.reveal.take();
        if let Some((a, b)) = pending {
            let mut st = egui::TextEdit::load_state(ui.ctx(), id).unwrap_or_default();
            st.cursor.set_char_range(Some(egui::text::CCursorRange::two(egui::text::CCursor::new(a), egui::text::CCursor::new(b))));
            st.store(ui.ctx(), id);
            ui.ctx().memory_mut(|m| m.request_focus(id));
        }
        let lines = doc.text.matches('\n').count() + 1;
        let digits = lines.to_string().len().max(3);
        let gutter: String = (1..=lines).map(|n| format!("{n:>digits$}")).collect::<Vec<_>>().join("\n");
        let mut cursor_out: Option<(usize, usize)> = None;
        let mut reveal_rect: Option<egui::Rect> = None;
        egui::ScrollArea::both().auto_shrink([false, false]).show(ui, |ui| {
            ui.horizontal_top(|ui| {
                ui.spacing_mut().item_spacing.x = 8.0;
                if !wrap {
                    ui.add(egui::Label::new(RichText::new(gutter).font(font.clone()).color(theme::MUTE)).wrap_mode(egui::TextWrapMode::Extend));
                }
                let spans2 = spans.clone();
                let font2 = font.clone();
                let mut layouter = move |ui: &egui::Ui, text: &dyn egui::TextBuffer, wrap_width: f32| {
                    let s = text.as_str();
                    let mut job = LayoutJob::default();
                    job.wrap.max_width = if wrap { wrap_width } else { f32::INFINITY };
                    build_job(&mut job, s, &spans2, &font2);
                    ui.fonts_mut(|f| f.layout_job(job))
                };
                let out = egui::TextEdit::multiline(&mut doc.text)
                    .id(id)
                    .font(font.clone())
                    .code_editor()
                    .lock_focus(true)
                    .desired_width(if wrap { ui.available_width() } else { f32::INFINITY })
                    .desired_rows(40)
                    .frame(egui::Frame::NONE)
                    .layouter(&mut layouter)
                    .show(ui);
                if want_focus {
                    out.response.request_focus();
                }
                if let Some(r) = out.cursor_range {
                    cursor_out = Some((usize::from(r.primary.index), usize::from(r.secondary.index)));
                }
                if let Some(ci) = reveal {
                    let cur = out.galley.pos_from_cursor(egui::text::CCursor::new(ci));
                    reveal_rect = Some(cur.translate(out.galley_pos.to_vec2()));
                }
            });
            if let Some(r) = reveal_rect {
                ui.scroll_to_rect(r.expand2(egui::vec2(40.0, 40.0)), Some(egui::Align::Center));
            }
        });
        if let Some(c) = cursor_out {
            doc.cursor = c;
        }
        let _ = text_len_before;
    }

    fn dialogs(&mut self, ctx: &egui::Context) {
        // go to line
        if let Some((text, first)) = self.goto.as_mut() {
            let mut go: Option<usize> = None;
            let mut cancel = false;
            egui::Window::new("Go to Line").collapsible(false).resizable(false).anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0]).show(ctx, |ui| {
                let r = ui.add(egui::TextEdit::singleline(text).desired_width(140.0).hint_text("line number"));
                if *first {
                    r.request_focus();
                    *first = false;
                }
                let enter = r.lost_focus() && ui.input(|i| i.key_pressed(Key::Enter));
                ui.horizontal(|ui| {
                    if ui.add(theme::accent_button("Go")).clicked() || enter {
                        go = text.trim().parse().ok();
                        cancel = go.is_none();
                    }
                    if ui.button("Cancel").clicked() {
                        cancel = true;
                    }
                });
            });
            if let Some(l) = go {
                self.goto_line(l);
                self.goto = None;
            } else if cancel {
                self.goto = None;
            }
        }
        // unsaved changes
        if let Some(idx) = self.confirm_close {
            let mut act: Option<u8> = None;
            let closing_all = idx == usize::MAX;
            let names: Vec<String> = if closing_all { self.docs.iter().filter(|d| d.dirty()).map(|d| d.title.clone()).collect() } else { self.docs.get(idx).map(|d| vec![d.title.clone()]).unwrap_or_default() };
            egui::Window::new("Unsaved changes").collapsible(false).resizable(false).anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0]).show(ctx, |ui| {
                ui.label(format!("Save changes to {}?", names.join(", ")));
                ui.horizontal(|ui| {
                    if ui.add(theme::accent_button("Save")).clicked() {
                        act = Some(0);
                    }
                    if ui.add(theme::danger_button("Discard")).clicked() {
                        act = Some(1);
                    }
                    if ui.button("Cancel").clicked() {
                        act = Some(2);
                    }
                });
            });
            match act {
                Some(0) => {
                    if closing_all {
                        for i in 0..self.docs.len() {
                            if self.docs[i].dirty() {
                                self.save_doc(i);
                            }
                        }
                        self.docs.retain(|d| !d.dirty() || d.remote.is_some());
                        self.visible = false;
                    } else if self.save_doc(idx) {
                        self.close_doc(idx);
                    }
                    self.confirm_close = None;
                }
                Some(1) => {
                    if closing_all {
                        self.docs.clear();
                        self.visible = false;
                    } else {
                        self.close_doc(idx);
                    }
                    self.confirm_close = None;
                }
                Some(_) => self.confirm_close = None,
                None => {}
            }
        }
        // dropped files
        let dropped: Vec<PathBuf> = ctx.input(|i| i.raw.dropped_files.iter().map(|f| f.path().to_path_buf()).collect());
        for p in dropped {
            if p.is_file() {
                self.open_local(&p);
            }
        }
    }
}

fn build_job(job: &mut LayoutJob, text: &str, spans: &[(usize, usize, Class)], font: &FontId) {
    let color = |c: Class| -> (Color32, bool) {
        match c {
            Class::Plain => (theme::TEXT, false),
            Class::Comment => (Color32::from_rgb(0x7f, 0x8c, 0x98), true),
            Class::Str => (Color32::from_rgb(0x98, 0xc3, 0x79), false),
            Class::Number => (Color32::from_rgb(0xd1, 0x9a, 0x66), false),
            Class::Keyword => (Color32::from_rgb(0xc6, 0x78, 0xdd), false),
            Class::Type => (Color32::from_rgb(0xe5, 0xc0, 0x7b), false),
            Class::Tag => (Color32::from_rgb(0xe0, 0x6c, 0x75), false),
            Class::Heading => (Color32::from_rgb(0x61, 0xaf, 0xef), false),
            Class::Error => (Color32::from_rgb(0xff, 0x7b, 0x72), false),
            Class::Warn => (Color32::from_rgb(0xe3, 0xb3, 0x41), false),
        }
    };
    let fmt = |c: Class| {
        let (col, italic) = color(c);
        TextFormat { font_id: font.clone(), color: col, italics: italic, ..Default::default() }
    };
    // char index -> byte offsets
    let mut offsets: Vec<usize> = text.char_indices().map(|(b, _)| b).collect();
    offsets.push(text.len());
    let byte = |ci: usize| offsets.get(ci).copied().unwrap_or(text.len());
    let mut pos = 0usize; // byte position
    for &(s, e, c) in spans {
        let (bs, be) = (byte(s), byte(e));
        if bs < pos || be <= bs {
            continue;
        }
        if bs > pos {
            job.append(&text[pos..bs], 0.0, fmt(Class::Plain));
        }
        job.append(&text[bs..be], 0.0, fmt(c));
        pos = be;
    }
    if pos < text.len() {
        job.append(&text[pos..], 0.0, fmt(Class::Plain));
    }
    if text.is_empty() {
        job.append("", 0.0, fmt(Class::Plain));
    }
}

/// Read a remote file on a worker thread and hand it to the editor through `tx`.
pub fn load_remote(tx: &Sender<EditorEvent>, sftp: SharedSftp, path: String, label: String, create_if_missing: bool) {
    let tx = tx.clone();
    std::thread::spawn(move || {
        let result = {
            let c = sftp.lock();
            match c.read_file(&path) {
                Ok(b) => Ok((b, false)),
                Err(_) if create_if_missing && !c.exists(&path) => Ok((Vec::new(), true)),
                Err(e) => Err(e),
            }
        };
        match result {
            Ok((bytes, created)) => {
                let _ = tx.send(EditorEvent::Loaded { remote: RemoteInfo { sftp, path, label }, bytes, created });
            }
            Err(e) => {
                let _ = tx.send(EditorEvent::Failed(format!("{path}: {e}")));
            }
        }
        crate::session::request_repaint();
    });
}
