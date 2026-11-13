//! Modal-ish dialogs: connection forms, profiles, rename, alerts, about, samples, plugin manager.

use super::theme;
use crate::config::ProfileStore;
use crate::conn::{self, ConnSpec};
use crate::plugins::PluginInfo;
use eframe::egui::{self, Align2, RichText};
use serde_json::json;
use std::collections::VecDeque;

pub const BAUD_RATES: &[u32] = &[300, 1200, 2400, 4800, 9600, 14400, 19200, 38400, 57600, 115200, 230400, 460800, 921600, 1000000, 1500000, 2000000];

pub enum Action {
    Connect(ConnSpec),
    SaveSshProfile { name: String, host: String, user: String, port: u16 },
    SaveTelnetProfile { name: String, host: String, port: u16 },
    Rename(String),
    Error(String, String),
    OpenSample(&'static str, &'static str),
}

pub fn dialog_window(title: &str) -> egui::Window<'static> {
    egui::Window::new(title.to_string()).collapsible(false).resizable(false).anchor(Align2::CENTER_CENTER, [0.0, 0.0])
}

#[derive(Default)]
pub struct SshForm {
    profile: String,
    name: String,
    host: String,
    user: String,
    password: String,
    port: String,
    save: bool,
    first: bool,
}

#[derive(Default)]
pub struct TelnetForm {
    profile: String,
    name: String,
    host: String,
    port: String,
    save: bool,
}

pub struct SerialForm {
    ports: Vec<(String, String)>,
    port_idx: usize,
    custom: bool,
    baud: u32,
    custom_baud: String,
    bytesize: u8,
    parity: char,
    stopbits: f32,
}

pub enum Dlg {
    None,
    Ssh(SshForm),
    Telnet(TelnetForm),
    Serial(SerialForm),
    Rename(String, bool),
    About,
    Samples,
    PluginManager,
    NewPlugin(String),
    Message(String, String),
}

pub struct PendingAlert {
    pub message: String,
    pub reply: crossbeam_channel::Sender<bool>,
}
pub struct PendingInput {
    pub prompt: String,
    pub text: String,
    pub reply: crossbeam_channel::Sender<String>,
    pub first: bool,
}

pub struct PendingGate {
    pub script: String,
    pub reason: String,
    pub tail: String,
    pub reply: crossbeam_channel::Sender<String>,
}
pub struct PendingConfirm {
    pub title: String,
    pub message: String,
    pub reply: crossbeam_channel::Sender<bool>,
}

pub struct Dialogs {
    pub gates: VecDeque<PendingGate>,
    pub confirms: VecDeque<PendingConfirm>,
    pub current: Dlg,
    pub alerts: VecDeque<PendingAlert>,
    pub inputs: VecDeque<PendingInput>,
}

impl Dialogs {
    pub fn new() -> Self {
        Self { gates: VecDeque::new(), confirms: VecDeque::new(), current: Dlg::None, alerts: VecDeque::new(), inputs: VecDeque::new() }
    }

    pub fn open_ssh(&mut self, store: &ProfileStore, profile: Option<&str>) {
        let mut f = SshForm { port: "22".into(), save: true, first: true, ..Default::default() };
        if let Some(p) = profile {
            Self::load_ssh(&mut f, store, p);
        }
        self.current = Dlg::Ssh(f);
    }

    fn load_ssh(f: &mut SshForm, store: &ProfileStore, name: &str) {
        if let Some(p) = store.profiles.get(name) {
            f.profile = name.to_string();
            f.name = name.to_string();
            f.host = p["host"].as_str().unwrap_or("").to_string();
            f.user = p["username"].as_str().unwrap_or("").to_string();
            f.port = p["port"].as_u64().unwrap_or(22).to_string();
        }
    }

    pub fn open_telnet(&mut self, store: &ProfileStore, profile: Option<&str>) {
        let mut f = TelnetForm { port: "23".into(), save: true, ..Default::default() };
        if let Some(name) = profile {
            if let Some(p) = store.profiles.get(name) {
                f.profile = name.to_string();
                f.name = name.to_string();
                f.host = p["host"].as_str().unwrap_or("").to_string();
                f.port = p["port"].as_u64().unwrap_or(23).to_string();
            }
        }
        self.current = Dlg::Telnet(f);
    }

    pub fn open_serial(&mut self) {
        let mut ports = conn::list_serial_ports();
        if ports.is_empty() {
            let fallback = if cfg!(windows) { "COM1" } else { "/dev/ttyUSB0" };
            ports.push((fallback.to_string(), fallback.to_string()));
        }
        self.current = Dlg::Serial(SerialForm { ports, port_idx: 0, custom: false, baud: 115200, custom_baud: String::new(), bytesize: 8, parity: 'N', stopbits: 1.0 });
    }

    pub fn message(&mut self, title: &str, text: &str) {
        self.current = Dlg::Message(title.to_string(), text.to_string());
    }

    pub fn show(&mut self, ctx: &egui::Context, ssh: &ProfileStore, telnet: &ProfileStore, plugins: &[PluginInfo], plugin_dirs: &[std::path::PathBuf]) -> Vec<Action> {
        let mut actions = Vec::new();
        let mut close = false;
        match &mut self.current {
            Dlg::None => {}
            Dlg::Ssh(f) => {
                dialog_window("Connect SSH").show(ctx, |ui| {
                    ui.heading("SSH Connection");
                    ui.add_space(6.0);
                    egui::Grid::new("ssh_grid").num_columns(2).spacing([10.0, 8.0]).show(ui, |ui| {
                        ui.label("Saved profile");
                        let mut chosen: Option<String> = None;
                        egui::ComboBox::from_id_salt("ssh_profile").width(260.0).selected_text(if f.profile.is_empty() { "(none)".to_string() } else { f.profile.clone() }).show_ui(ui, |ui| {
                            if ui.selectable_label(f.profile.is_empty(), "(none)").clicked() {
                                chosen = Some(String::new());
                            }
                            let mut names = ssh.names();
                            names.sort();
                            for n in names {
                                if ui.selectable_label(f.profile == n, &n).clicked() {
                                    chosen = Some(n);
                                }
                            }
                        });
                        if let Some(n) = chosen {
                            if n.is_empty() {
                                f.profile.clear();
                            } else {
                                Self::load_ssh(f, ssh, &n);
                            }
                        }
                        ui.end_row();
                        ui.label("Profile name");
                        ui.add(egui::TextEdit::singleline(&mut f.name).desired_width(260.0));
                        ui.end_row();
                        ui.label("Host/IP");
                        let r = ui.add(egui::TextEdit::singleline(&mut f.host).desired_width(260.0));
                        if f.first {
                            r.request_focus();
                            f.first = false;
                        }
                        ui.end_row();
                        ui.label("Username");
                        ui.add(egui::TextEdit::singleline(&mut f.user).desired_width(260.0));
                        ui.end_row();
                        ui.label("Password");
                        ui.add(egui::TextEdit::singleline(&mut f.password).password(true).desired_width(260.0));
                        ui.end_row();
                        ui.label("Port");
                        ui.add(egui::TextEdit::singleline(&mut f.port).desired_width(80.0));
                        ui.end_row();
                    });
                    ui.checkbox(&mut f.save, "Save/update profile");
                    theme::hint(ui, "Leave the password empty to use your SSH agent or ~/.ssh keys.");
                    ui.add_space(8.0);
                    let mut go = ui.input(|i| i.key_pressed(egui::Key::Enter)) && !f.host.is_empty();
                    ui.horizontal(|ui| {
                        if ui.add(theme::accent_button("Connect")).clicked() {
                            go = true;
                        }
                        if ui.button("Cancel").clicked() {
                            close = true;
                        }
                    });
                    if go {
                        if f.host.trim().is_empty() {
                            actions.push(Action::Error("Missing host".into(), "Enter the SSH host/IP.".into()));
                        } else if let Ok(port) = f.port.trim().parse::<u16>() {
                            if f.save {
                                let name = if f.name.trim().is_empty() { f.host.trim().to_string() } else { f.name.trim().to_string() };
                                actions.push(Action::SaveSshProfile { name, host: f.host.trim().into(), user: f.user.trim().into(), port });
                            }
                            actions.push(Action::Connect(ConnSpec::Ssh { host: f.host.trim().into(), username: f.user.trim().into(), password: f.password.clone(), port, look_for_keys: true }));
                            close = true;
                        } else {
                            actions.push(Action::Error("Invalid port".into(), "Port must be numeric.".into()));
                        }
                    }
                });
            }
            Dlg::Telnet(f) => {
                dialog_window("Connect Telnet").show(ctx, |ui| {
                    ui.heading("Telnet Connection");
                    ui.add_space(6.0);
                    egui::Grid::new("tel_grid").num_columns(2).spacing([10.0, 8.0]).show(ui, |ui| {
                        ui.label("Saved profile");
                        let mut chosen: Option<String> = None;
                        egui::ComboBox::from_id_salt("tel_profile").width(260.0).selected_text(if f.profile.is_empty() { "(none)".to_string() } else { f.profile.clone() }).show_ui(ui, |ui| {
                            if ui.selectable_label(f.profile.is_empty(), "(none)").clicked() {
                                chosen = Some(String::new());
                            }
                            let mut names = telnet.names();
                            names.sort();
                            for n in names {
                                if ui.selectable_label(f.profile == n, &n).clicked() {
                                    chosen = Some(n);
                                }
                            }
                        });
                        if let Some(n) = chosen {
                            f.profile = n.clone();
                            if let Some(p) = telnet.profiles.get(&n) {
                                f.name = n;
                                f.host = p["host"].as_str().unwrap_or("").into();
                                f.port = p["port"].as_u64().unwrap_or(23).to_string();
                            }
                        }
                        ui.end_row();
                        ui.label("Profile name");
                        ui.add(egui::TextEdit::singleline(&mut f.name).desired_width(260.0));
                        ui.end_row();
                        ui.label("Host/IP");
                        ui.add(egui::TextEdit::singleline(&mut f.host).desired_width(260.0));
                        ui.end_row();
                        ui.label("Port");
                        ui.add(egui::TextEdit::singleline(&mut f.port).desired_width(80.0));
                        ui.end_row();
                    });
                    ui.checkbox(&mut f.save, "Save/update profile");
                    theme::hint(ui, "Press Ctrl+] in a Telnet terminal to disconnect.");
                    ui.add_space(8.0);
                    ui.horizontal(|ui| {
                        if ui.add(theme::accent_button("Connect")).clicked() {
                            if f.host.trim().is_empty() {
                                actions.push(Action::Error("Missing host".into(), "Enter the Telnet host/IP.".into()));
                            } else if let Ok(port) = f.port.trim().parse::<u16>() {
                                if f.save {
                                    let name = if f.name.trim().is_empty() { format!("{}:{}", f.host.trim(), port) } else { f.name.trim().to_string() };
                                    actions.push(Action::SaveTelnetProfile { name, host: f.host.trim().into(), port });
                                }
                                actions.push(Action::Connect(ConnSpec::Telnet { host: f.host.trim().into(), port }));
                                close = true;
                            } else {
                                actions.push(Action::Error("Invalid port".into(), "Port must be numeric.".into()));
                            }
                        }
                        if ui.button("Cancel").clicked() {
                            close = true;
                        }
                    });
                });
            }
            Dlg::Serial(f) => {
                dialog_window("Connect Serial").show(ctx, |ui| {
                    ui.heading("Serial Connection");
                    ui.add_space(6.0);
                    egui::Grid::new("ser_grid").num_columns(2).spacing([10.0, 8.0]).show(ui, |ui| {
                        ui.label("Port");
                        egui::ComboBox::from_id_salt("ser_port").width(320.0).selected_text(f.ports[f.port_idx].1.clone()).show_ui(ui, |ui| {
                            for (i, (_, desc)) in f.ports.iter().enumerate() {
                                ui.selectable_value(&mut f.port_idx, i, desc);
                            }
                        });
                        ui.end_row();
                        ui.label("Baud rate mode");
                        ui.horizontal(|ui| {
                            ui.radio_value(&mut f.custom, false, "Preset");
                            ui.radio_value(&mut f.custom, true, "Custom");
                        });
                        ui.end_row();
                        if f.custom {
                            ui.label("Custom baud");
                            ui.add(egui::TextEdit::singleline(&mut f.custom_baud).hint_text("Example: 250000").desired_width(140.0));
                        } else {
                            ui.label("Preset baud");
                            egui::ComboBox::from_id_salt("ser_baud").selected_text(f.baud.to_string()).show_ui(ui, |ui| {
                                for b in BAUD_RATES {
                                    ui.selectable_value(&mut f.baud, *b, b.to_string());
                                }
                            });
                        }
                        ui.end_row();
                        ui.label("Data bits");
                        egui::ComboBox::from_id_salt("ser_bits").selected_text(f.bytesize.to_string()).show_ui(ui, |ui| {
                            for b in [5u8, 6, 7, 8] {
                                ui.selectable_value(&mut f.bytesize, b, b.to_string());
                            }
                        });
                        ui.end_row();
                        ui.label("Parity");
                        egui::ComboBox::from_id_salt("ser_par").selected_text(match f.parity { 'E' => "Even", 'O' => "Odd", _ => "None" }).show_ui(ui, |ui| {
                            ui.selectable_value(&mut f.parity, 'N', "None");
                            ui.selectable_value(&mut f.parity, 'E', "Even");
                            ui.selectable_value(&mut f.parity, 'O', "Odd");
                        });
                        ui.end_row();
                        ui.label("Stop bits");
                        egui::ComboBox::from_id_salt("ser_stop").selected_text(f.stopbits.to_string()).show_ui(ui, |ui| {
                            ui.selectable_value(&mut f.stopbits, 1.0, "1");
                            ui.selectable_value(&mut f.stopbits, 2.0, "2");
                        });
                        ui.end_row();
                    });
                    ui.add_space(8.0);
                    ui.horizontal(|ui| {
                        if ui.add(theme::accent_button("Connect")).clicked() {
                            let baud = if f.custom { f.custom_baud.trim().parse::<u32>().ok().filter(|b| *b > 0) } else { Some(f.baud) };
                            match baud {
                                Some(baud) => {
                                    actions.push(Action::Connect(ConnSpec::Serial { port: f.ports[f.port_idx].0.clone(), baud, bytesize: f.bytesize, parity: f.parity, stopbits: f.stopbits }));
                                    close = true;
                                }
                                None => actions.push(Action::Error("Invalid baud".into(), "Enter a numeric baud rate.".into())),
                            }
                        }
                        if ui.button("Refresh ports").clicked() {
                            let ports = conn::list_serial_ports();
                            if !ports.is_empty() {
                                f.ports = ports;
                                f.port_idx = 0;
                            }
                        }
                        if ui.button("Cancel").clicked() {
                            close = true;
                        }
                    });
                });
            }
            Dlg::Rename(name, first) => {
                dialog_window("Rename Terminal").show(ctx, |ui| {
                    ui.label(RichText::new("Enter New Terminal Name:").strong());
                    let r = ui.add(egui::TextEdit::singleline(name).desired_width(280.0));
                    if *first {
                        r.request_focus();
                        *first = false;
                    }
                    let enter = r.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
                    ui.horizontal(|ui| {
                        if ui.add(theme::accent_button("Save")).clicked() || enter {
                            actions.push(Action::Rename(name.trim().to_string()));
                            close = true;
                        }
                        if ui.button("Cancel").clicked() {
                            close = true;
                        }
                    });
                });
            }
            Dlg::About => {
                dialog_window("About Modern Multi Terminal").show(ctx, |ui| {
                    ui.heading("Modern Multi Terminal");
                    ui.label("SSH / Serial / Telnet terminal with scripting (Python + Tera Term macros), SFTP and an AI assistant that can use Ollama or hosted LLM APIs.");
                    ui.add_space(4.0);
                    ui.label("Engineered by Aby Shergill · BSD License");
                    ui.hyperlink("https://github.com/abyshergill/modern_multi_terminal");
                    ui.add_space(4.0);
                    theme::hint(ui, &format!("Rust edition · v{}", env!("CARGO_PKG_VERSION")));
                    if ui.button("Close").clicked() {
                        close = true;
                    }
                });
            }
            Dlg::Samples => {
                egui::Window::new("Preset Script Templates").collapsible(false).default_size([640.0, 480.0]).anchor(Align2::CENTER_CENTER, [0.0, 0.0]).show(ctx, |ui| {
                    egui::ScrollArea::vertical().show(ui, |ui| {
                        for (title, desc, code, ext) in crate::samples::SAMPLES {
                            egui::Frame::group(ui.style()).show(ui, |ui| {
                                ui.set_width(ui.available_width());
                                ui.horizontal(|ui| {
                                    ui.vertical(|ui| {
                                        ui.label(RichText::new(*title).strong().color(theme::TERM_ACCENT));
                                        ui.label(RichText::new(*desc).color(theme::DIM));
                                    });
                                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                                        if ui.button("Open in editor").clicked() {
                                            actions.push(Action::OpenSample(code, ext));
                                        }
                                    });
                                });
                            });
                        }
                    });
                    if ui.button("Close").clicked() {
                        close = true;
                    }
                });
            }
            Dlg::PluginManager => {
                egui::Window::new("Plugin Manager").collapsible(false).default_size([620.0, 420.0]).anchor(Align2::CENTER_CENTER, [0.0, 0.0]).show(ctx, |ui| {
                    egui::ScrollArea::vertical().max_height(320.0).show(ui, |ui| {
                        if plugins.is_empty() {
                            ui.label("No plugins found.");
                        }
                        for p in plugins {
                            ui.horizontal(|ui| {
                                ui.label(RichText::new(if p.loaded { "OK" } else { "FAILED" }).color(if p.loaded { theme::OK } else { theme::ERR }).strong());
                                ui.label(RichText::new(format!("{} {}", p.name, p.version)).strong());
                            });
                            ui.label(RichText::new(p.path.display().to_string()).color(theme::MUTE).size(12.0));
                            if !p.description.is_empty() {
                                ui.label(&p.description);
                            }
                            if !p.error.is_empty() {
                                ui.label(RichText::new(format!("Error: {}", p.error)).color(theme::ERR));
                            }
                            ui.separator();
                        }
                    });
                    theme::hint(ui, "Plugin folders:");
                    for d in plugin_dirs {
                        theme::hint(ui, &format!("  {}", d.display()));
                    }
                    if ui.button("Close").clicked() {
                        close = true;
                    }
                });
            }
            Dlg::NewPlugin(name) => {
                dialog_window("New Plugin").show(ctx, |ui| {
                    ui.label("Plugin file name:");
                    ui.add(egui::TextEdit::singleline(name).desired_width(280.0));
                    ui.horizontal(|ui| {
                        if ui.add(theme::accent_button("Create")).clicked() {
                            match crate::plugins::create_from_template(name) {
                                Ok(path) => {
                                    actions.push(Action::OpenSample("", ""));
                                    actions.push(Action::Error("Plugin created".into(), format!("Created {}\nEdit it, then Plugins > Reload Plugins.", path.display())));
                                }
                                Err(e) => actions.push(Action::Error("New Plugin".into(), e)),
                            }
                            close = true;
                        }
                        if ui.button("Cancel").clicked() {
                            close = true;
                        }
                    });
                });
            }
            Dlg::Message(title, text) => {
                dialog_window(title).show(ctx, |ui| {
                    ui.label(text.as_str());
                    ui.add_space(6.0);
                    if ui.add(theme::accent_button("OK")).clicked() || ui.input(|i| i.key_pressed(egui::Key::Escape)) {
                        close = true;
                    }
                });
            }
        }
        if close {
            // an Error action opens a message dialog afterwards, so only clear when nothing replaces it
            self.current = Dlg::None;
        }

        // Script alerts and input prompts (from running scripts)
        if let Some(a) = self.alerts.front() {
            let mut answer: Option<bool> = None;
            dialog_window("Script Alert").show(ctx, |ui| {
                ui.label(&a.message);
                ui.label("Do you want to continue script execution?");
                ui.add_space(6.0);
                ui.horizontal(|ui| {
                    if ui.add(theme::accent_button("Yes")).clicked() {
                        answer = Some(true);
                    }
                    if ui.button("No").clicked() {
                        answer = Some(false);
                    }
                });
            });
            if let Some(ans) = answer {
                let a = self.alerts.pop_front().unwrap();
                let _ = a.reply.send(ans);
            }
        }
        if let Some(inp) = self.inputs.front_mut() {
            let mut done: Option<Option<String>> = None;
            dialog_window("Script Input").show(ctx, |ui| {
                ui.label(&inp.prompt);
                let r = ui.add(egui::TextEdit::singleline(&mut inp.text).desired_width(300.0));
                if inp.first {
                    r.request_focus();
                    inp.first = false;
                }
                let enter = r.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
                ui.horizontal(|ui| {
                    if ui.add(theme::accent_button("OK")).clicked() || enter {
                        done = Some(Some(inp.text.clone()));
                    }
                    if ui.button("Cancel").clicked() {
                        done = Some(None);
                    }
                });
            });
            if let Some(d) = done {
                let inp = self.inputs.pop_front().unwrap();
                let _ = inp.reply.send(d.unwrap_or_default());
            }
        }
        if let Some(g) = self.gates.front() {
            let mut choice: Option<&'static str> = None;
            egui::Window::new("\u{26A0} Playlist Gate Triggered").collapsible(false).resizable(false).anchor(Align2::CENTER_CENTER, [0.0, 0.0]).show(ctx, |ui| {
                ui.set_width(520.0);
                ui.label(RichText::new("Execution Handoff Failed").color(theme::ERR).heading());
                ui.label(RichText::new(format!("Script: '{}'\nReason: {}", g.script, g.reason)).color(theme::DIM));
                ui.label(RichText::new("Terminal Output Context (last 250 chars):").strong());
                egui::Frame::new().fill(theme::TERM_BG).inner_margin(6.0).show(ui, |ui| {
                    ui.set_min_height(80.0);
                    ui.label(RichText::new(if g.tail.trim().is_empty() { "[No recent terminal output]" } else { g.tail.as_str() }).monospace().color(theme::TERM_FG));
                });
                ui.horizontal(|ui| {
                    if ui.button("+15s More Time").clicked() {
                        choice = Some("EXTEND");
                    }
                    if ui.button("Force Next Script").clicked() {
                        choice = Some("FORCE");
                    }
                    if ui.add(theme::danger_button("Abort Playlist")).clicked() {
                        choice = Some("ABORT");
                    }
                });
            });
            if let Some(c) = choice {
                let g = self.gates.pop_front().unwrap();
                let _ = g.reply.send(c.to_string());
            }
        }
        if let Some(c) = self.confirms.front() {
            let mut ans: Option<bool> = None;
            dialog_window(&c.title).show(ctx, |ui| {
                ui.label(&c.message);
                ui.add_space(6.0);
                ui.horizontal(|ui| {
                    if ui.add(theme::accent_button("Yes")).clicked() {
                        ans = Some(true);
                    }
                    if ui.button("No").clicked() {
                        ans = Some(false);
                    }
                });
            });
            if let Some(a) = ans {
                let c = self.confirms.pop_front().unwrap();
                let _ = c.reply.send(a);
            }
        }
        let _ = json!(null);
        actions
    }
}
