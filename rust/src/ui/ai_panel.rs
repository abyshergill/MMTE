//! AI assistant dock (placeholder until the assistant is wired in).
use crate::session::Hub;
use eframe::egui;
use std::sync::Arc;

pub struct AiPanel {
    pub open: bool,
}
impl AiPanel {
    pub fn new(_hub: Arc<Hub>) -> Self {
        Self { open: false }
    }
    pub fn menu(&mut self, ui: &mut egui::Ui) {
        ui.menu_button("AI", |_ui| {});
    }
    pub fn right_panel(&mut self, _ui: &mut egui::Ui, _hub: &Arc<Hub>) {}
    pub fn windows(&mut self, _ctx: &egui::Context, _hub: &Arc<Hub>) {}
    pub fn wants_keyboard(&self) -> bool {
        false
    }
}
