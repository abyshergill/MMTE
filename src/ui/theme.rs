//! Single dark palette shared by the whole app (tokens from the Python version's ui_style.py).

use eframe::egui::{self, Color32, FontFamily, FontId, Stroke, TextStyle};

pub const BG0: Color32 = Color32::from_rgb(0x0b, 0x0e, 0x14);
pub const BG1: Color32 = Color32::from_rgb(0x11, 0x15, 0x1d);
pub const BG2: Color32 = Color32::from_rgb(0x17, 0x1c, 0x26);
pub const BG3: Color32 = Color32::from_rgb(0x21, 0x28, 0x38);
pub const LINE: Color32 = Color32::from_rgb(0x22, 0x2a, 0x38);
pub const LINE2: Color32 = Color32::from_rgb(0x32, 0x3c, 0x52);
pub const TEXT: Color32 = Color32::from_rgb(0xe6, 0xed, 0xf3);
pub const DIM: Color32 = Color32::from_rgb(0x9a, 0xa8, 0xb6);
pub const MUTE: Color32 = Color32::from_rgb(0x6b, 0x7a, 0x89);
pub const ACCENT: Color32 = Color32::from_rgb(0x7c, 0x8c, 0xff);
pub const ACCENT_HI: Color32 = Color32::from_rgb(0xa0, 0xac, 0xff);
pub const ACCENT_FG: Color32 = Color32::from_rgb(0x0a, 0x0d, 0x1f);
pub const INFO: Color32 = Color32::from_rgb(0x5a, 0xa9, 0xff);
pub const OK: Color32 = Color32::from_rgb(0x4c, 0xc3, 0x8a);
pub const WARN: Color32 = Color32::from_rgb(0xe0, 0xa9, 0x3b);
pub const ERR: Color32 = Color32::from_rgb(0xef, 0x6a, 0x73);
pub const ERR_BG: Color32 = Color32::from_rgb(0x33, 0x19, 0x1d);
pub const SELECT: Color32 = Color32::from_rgb(0x2a, 0x35, 0x66);
pub const TERM_BG: Color32 = Color32::from_rgb(0x02, 0x08, 0x0d);
pub const TERM_FG: Color32 = Color32::from_rgb(0xd8, 0xf3, 0xdc);
pub const TERM_ACCENT: Color32 = Color32::from_rgb(0x42, 0xc7, 0xb8);

pub fn apply(ctx: &egui::Context) {
    let mut v = egui::Visuals::dark();
    v.panel_fill = BG1;
    v.window_fill = BG1;
    v.extreme_bg_color = BG0;
    v.faint_bg_color = BG2;
    v.code_bg_color = BG2;
    v.override_text_color = Some(TEXT);
    v.hyperlink_color = INFO;
    v.selection.bg_fill = SELECT;
    v.selection.stroke = Stroke::new(1.0, ACCENT);
    v.window_stroke = Stroke::new(1.0, LINE2);
    v.window_corner_radius = egui::CornerRadius::same(10);
    v.menu_corner_radius = egui::CornerRadius::same(8);
    let r = egui::CornerRadius::same(7);
    for (w, fill) in [
        (&mut v.widgets.noninteractive, BG1),
        (&mut v.widgets.inactive, BG2),
        (&mut v.widgets.hovered, BG3),
        (&mut v.widgets.active, SELECT),
        (&mut v.widgets.open, BG3),
    ] {
        w.bg_fill = fill;
        w.weak_bg_fill = fill;
        w.corner_radius = r;
    }
    v.widgets.noninteractive.bg_stroke = Stroke::new(1.0, LINE);
    v.widgets.inactive.bg_stroke = Stroke::new(1.0, LINE2);
    v.widgets.hovered.bg_stroke = Stroke::new(1.0, ACCENT);
    v.widgets.active.bg_stroke = Stroke::new(1.0, ACCENT_HI);
    ctx.set_visuals(v);

    let mut style = (*ctx.global_style()).clone();
    style.spacing.item_spacing = egui::vec2(8.0, 6.0);
    style.spacing.button_padding = egui::vec2(12.0, 5.0);
    style.spacing.interact_size.y = 26.0;
    style.text_styles.insert(TextStyle::Body, FontId::new(14.0, FontFamily::Proportional));
    style.text_styles.insert(TextStyle::Button, FontId::new(14.0, FontFamily::Proportional));
    style.text_styles.insert(TextStyle::Heading, FontId::new(19.0, FontFamily::Proportional));
    style.text_styles.insert(TextStyle::Monospace, FontId::new(13.0, FontFamily::Monospace));
    ctx.set_global_style(style);
}

/// Accent (primary) button.
pub fn accent_button(text: &str) -> egui::Button<'static> {
    egui::Button::new(egui::RichText::new(text.to_string()).color(ACCENT_FG).strong()).fill(ACCENT)
}

pub fn danger_button(text: &str) -> egui::Button<'static> {
    egui::Button::new(egui::RichText::new(text.to_string()).color(Color32::from_rgb(0xff, 0xb4, 0xba))).fill(ERR_BG).stroke(Stroke::new(1.0, Color32::from_rgb(0x6b, 0x2f, 0x39)))
}

pub fn hint(ui: &mut egui::Ui, text: &str) {
    ui.label(egui::RichText::new(text).color(MUTE).size(12.5));
}
