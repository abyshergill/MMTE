//! Draws a terminal session (vt100 screen) with egui and handles mouse selection / scrolling.

use super::theme;
use crate::highlight::{self, Tag};
use crate::session::Session;
use eframe::egui::{self, Align2, Color32, FontId, Pos2, Rect, Sense, Stroke, Vec2};
use std::sync::Arc;

#[derive(Default)]
pub struct TermUi {
    /// Selection as (row, col) start/end in screen coordinates (inclusive), un-normalised.
    pub sel: Option<((u16, u16), (u16, u16))>,
    pub cell: Vec2,
    pub origin: Pos2,
    pub dragging: bool,
}

pub struct TermResponse {
    pub response: egui::Response,
    pub rect: Rect,
}

const ANSI: [[u8; 3]; 16] = [
    [0x1c, 0x1c, 0x1c],
    [0xe0, 0x55, 0x61],
    [0x8c, 0xc2, 0x65],
    [0xd2, 0xb9, 0x67],
    [0x4a, 0xa5, 0xf0],
    [0xc1, 0x62, 0xde],
    [0x42, 0xc7, 0xb8],
    [0xd8, 0xf3, 0xdc],
    [0x5c, 0x63, 0x70],
    [0xff, 0x6e, 0x7a],
    [0xa8, 0xe0, 0x7f],
    [0xf0, 0xd4, 0x7d],
    [0x6c, 0xb8, 0xff],
    [0xdd, 0x84, 0xf2],
    [0x6a, 0xe0, 0xd2],
    [0xff, 0xff, 0xff],
];

pub fn idx_color(i: u8) -> Color32 {
    let rgb = match i {
        0..=15 => ANSI[i as usize],
        16..=231 => {
            let n = i - 16;
            let lv = |v: u8| if v == 0 { 0 } else { 55 + v * 40 };
            [lv(n / 36), lv((n / 6) % 6), lv(n % 6)]
        }
        _ => {
            let g = 8 + (i - 232) * 10;
            [g, g, g]
        }
    };
    Color32::from_rgb(rgb[0], rgb[1], rgb[2])
}

fn resolve(c: vt100::Color, default: Color32, bold: bool) -> Color32 {
    match c {
        vt100::Color::Default => default,
        vt100::Color::Idx(i) => idx_color(if bold && i < 8 { i + 8 } else { i }),
        vt100::Color::Rgb(r, g, b) => Color32::from_rgb(r, g, b),
    }
}

pub fn font() -> FontId {
    FontId::monospace(14.5)
}

pub fn cell_size(ui: &egui::Ui) -> Vec2 {
    let galley = ui.painter().layout_no_wrap("M".to_string(), font(), Color32::WHITE);
    Vec2::new(galley.size().x.max(1.0), galley.size().y.max(1.0).ceil())
}

/// Normalised selection (start <= end in reading order).
pub fn norm(sel: ((u16, u16), (u16, u16))) -> ((u16, u16), (u16, u16)) {
    let (a, b) = sel;
    if (a.0, a.1) <= (b.0, b.1) {
        (a, b)
    } else {
        (b, a)
    }
}

pub fn selected_text(session: &Session, sel: ((u16, u16), (u16, u16))) -> String {
    let ((r1, c1), (r2, c2)) = norm(sel);
    let mut t = session.term.lock();
    let scroll = t.scroll;
    let cols = t.cols;
    t.parser.screen_mut().set_scrollback(scroll);
    let mut out = String::new();
    for r in r1..=r2 {
        let (start, end) = (if r == r1 { c1 } else { 0 }, if r == r2 { c2 } else { cols.saturating_sub(1) });
        let mut line = String::new();
        for c in start..=end.min(cols.saturating_sub(1)) {
            match t.parser.screen().cell(r, c) {
                Some(cell) if cell.is_wide_continuation() => {}
                Some(cell) if cell.has_contents() => line.push_str(cell.contents()),
                _ => line.push(' '),
            }
        }
        out.push_str(line.trim_end());
        if r != r2 {
            out.push('\n');
        }
    }
    t.parser.screen_mut().set_scrollback(0);
    out
}

pub fn show(ui: &mut egui::Ui, session: &Arc<Session>, tui: &mut TermUi, id: egui::Id) -> TermResponse {
    let avail = ui.available_size();
    let (rect, _) = ui.allocate_exact_size(avail, Sense::hover());
    let response = ui.interact(rect, id, Sense::click_and_drag());
    let painter = ui.painter_at(rect);
    painter.rect_filled(rect, 0.0, theme::TERM_BG);

    let cell = cell_size(ui);
    tui.cell = cell;
    let pad = 8.0;
    let inner = rect.shrink(pad);
    let cols = (inner.width() / cell.x).floor().max(1.0) as u16;
    let rows = (inner.height() / cell.y).floor().max(1.0) as u16;
    session.resize(cols, rows);
    tui.origin = inner.min;

    // mouse wheel scrolls history
    if response.hovered() {
        let dy = ui.input(|i| i.smooth_scroll_delta.y);
        if dy.abs() > 0.5 {
            let lines = ((dy / cell.y).round() as i64).clamp(-50, 50);
            let mut t = session.term.lock();
            let max = t.parser.screen().scrollback_len();
            t.scroll = (t.scroll as i64 + lines).clamp(0, max as i64) as usize;
        }
    }

    // mouse selection
    let to_cell = |p: Pos2| -> (u16, u16) {
        let c = (((p.x - tui.origin.x) / cell.x).floor().max(0.0) as u16).min(cols - 1);
        let r = (((p.y - tui.origin.y) / cell.y).floor().max(0.0) as u16).min(rows - 1);
        (r, c)
    };
    if response.drag_started_by(egui::PointerButton::Primary) {
        if let Some(p) = response.interact_pointer_pos() {
            let c = to_cell(p);
            tui.sel = Some((c, c));
            tui.dragging = true;
        }
    } else if response.dragged_by(egui::PointerButton::Primary) {
        if let (Some(p), Some((a, _))) = (response.interact_pointer_pos(), tui.sel) {
            tui.sel = Some((a, to_cell(p)));
        }
    }
    if response.drag_stopped() {
        tui.dragging = false;
        if let Some((a, b)) = tui.sel {
            if a == b {
                tui.sel = None;
            }
        }
    }
    if response.clicked() && !tui.dragging {
        tui.sel = None;
    }
    if response.double_clicked() {
        if let Some(p) = response.interact_pointer_pos() {
            let (r, c) = to_cell(p);
            tui.sel = word_at(session, r, c);
        }
    }

    // paint
    let (alt, highlight_on, scroll) = {
        let t = session.term.lock();
        (t.alt_screen(), t.highlight, t.scroll)
    };
    let mut t = session.term.lock();
    t.parser.screen_mut().set_scrollback(scroll);
    let t_cols = t.cols.min(cols);
    let t_rows = t.rows.min(rows);
    for r in 0..t_rows {
        let y = tui.origin.y + r as f32 * cell.y;
        // row text for highlighting
        let mut row_chars: Vec<char> = Vec::with_capacity(t_cols as usize);
        let mut spans: Vec<(usize, usize, Tag)> = Vec::new();
        if highlight_on && !alt {
            for c in 0..t_cols {
                let ch = t.parser.screen().cell(r, c).and_then(|x| x.contents().chars().next()).unwrap_or(' ');
                row_chars.push(ch);
            }
            let line: String = row_chars.iter().collect();
            spans = highlight::highlight_spans(&line);
        }
        let mut run = String::new();
        let mut run_start = 0u16;
        let mut run_style: Option<(Color32, bool)> = None; // (fg, underline)
        let mut flush = |run: &mut String, start: u16, style: Option<(Color32, bool)>, painter: &egui::Painter| {
            if let Some((fg, ul)) = style {
                if !run.trim().is_empty() || ul {
                    let x = tui.origin.x + start as f32 * cell.x;
                    if run.is_ascii() {
                        painter.text(Pos2::new(x, y), Align2::LEFT_TOP, run.as_str(), font(), fg);
                    } else {
                        for (k, ch) in run.chars().enumerate() {
                            if ch != ' ' {
                                painter.text(Pos2::new(x + k as f32 * cell.x, y), Align2::LEFT_TOP, ch.to_string(), font(), fg);
                            }
                        }
                    }
                    if ul {
                        let w = run.chars().count() as f32 * cell.x;
                        painter.line_segment([Pos2::new(x, y + cell.y - 2.0), Pos2::new(x + w, y + cell.y - 2.0)], Stroke::new(1.0, fg));
                    }
                }
            }
            run.clear();
        };
        let mut c = 0u16;
        while c < t_cols {
            let Some(cellp) = t.parser.screen().cell(r, c) else {
                c += 1;
                continue;
            };
            if cellp.is_wide_continuation() {
                c += 1;
                continue;
            }
            let bold = cellp.bold();
            let mut fg = resolve(cellp.fgcolor(), theme::TERM_FG, bold);
            let mut bg = resolve(cellp.bgcolor(), theme::TERM_BG, false);
            if cellp.inverse() {
                std::mem::swap(&mut fg, &mut bg);
                if cellp.bgcolor() == vt100::Color::Default && cellp.fgcolor() == vt100::Color::Default {
                    bg = theme::TERM_FG;
                    fg = theme::TERM_BG;
                }
            }
            if cellp.dim() {
                fg = fg.gamma_multiply(0.65);
            }
            let mut underline = cellp.underline();
            if cellp.fgcolor() == vt100::Color::Default && !cellp.inverse() && !spans.is_empty() {
                if let Some(sp) = spans.iter().find(|s| (c as usize) >= s.0 && (c as usize) < s.1) {
                    let (rgb, _b, ul) = highlight::format(sp.2);
                    fg = Color32::from_rgb(rgb[0], rgb[1], rgb[2]);
                    underline |= ul;
                }
            }
            let w = if cellp.is_wide() { 2.0 } else { 1.0 };
            if bg != theme::TERM_BG {
                let x = tui.origin.x + c as f32 * cell.x;
                painter.rect_filled(Rect::from_min_size(Pos2::new(x, y), Vec2::new(cell.x * w, cell.y)), 0.0, bg);
            }
            let text = if cellp.has_contents() { cellp.contents() } else { " " };
            let style = Some((fg, underline));
            if style != run_style {
                flush(&mut run, run_start, run_style, &painter);
                run_style = style;
                run_start = c;
            }
            run.push_str(text);
            if w > 1.0 {
                // wide glyphs break the monospace grid: draw them on their own
                let st = run_style;
                let start = run_start;
                flush(&mut run, start, st, &painter);
                run_start = c + 2;
            }
            c += w as u16;
        }
        flush(&mut run, run_start, run_style, &painter);
    }

    // selection overlay
    if let Some(sel) = tui.sel {
        let ((r1, c1), (r2, c2)) = norm(sel);
        for r in r1..=r2.min(t_rows.saturating_sub(1)) {
            let (a, b) = (if r == r1 { c1 } else { 0 }, if r == r2 { c2 } else { t_cols.saturating_sub(1) });
            let x = tui.origin.x + a as f32 * cell.x;
            let w = (b.saturating_sub(a) + 1) as f32 * cell.x;
            painter.rect_filled(Rect::from_min_size(Pos2::new(x, tui.origin.y + r as f32 * cell.y), Vec2::new(w, cell.y)), 0.0, Color32::from_rgba_unmultiplied(0x7c, 0x8c, 0xff, 80));
        }
    }

    // cursor
    if scroll == 0 && !t.parser.screen().hide_cursor() {
        let (cr, cc) = t.parser.screen().cursor_position();
        if cr < t_rows && cc < t_cols {
            let pos = Pos2::new(tui.origin.x + cc as f32 * cell.x, tui.origin.y + cr as f32 * cell.y);
            let r = Rect::from_min_size(pos, cell);
            if response.has_focus() {
                painter.rect_filled(r, 1.0, theme::TERM_ACCENT.gamma_multiply(0.85));
                if let Some(ch) = t.parser.screen().cell(cr, cc) {
                    if ch.has_contents() && ch.contents() != " " {
                        painter.text(pos, Align2::LEFT_TOP, ch.contents(), font(), theme::TERM_BG);
                    }
                }
            } else {
                painter.rect_stroke(r, 1.0, Stroke::new(1.0, theme::TERM_ACCENT), egui::StrokeKind::Inside);
            }
        }
    }
    t.parser.screen_mut().set_scrollback(0);
    drop(t);

    if scroll > 0 {
        let tag = format!("\u{2191} scrolled back {scroll} lines - press any key to return");
        let g = painter.layout_no_wrap(tag, FontId::proportional(12.0), theme::WARN);
        let r = Rect::from_min_size(Pos2::new(rect.right() - g.size().x - 20.0, rect.top() + 6.0), g.size() + Vec2::new(12.0, 6.0));
        painter.rect_filled(r, 6.0, theme::BG2);
        painter.galley(r.min + Vec2::new(6.0, 3.0), g, theme::WARN);
    }
    TermResponse { response, rect }
}

fn word_at(session: &Session, row: u16, col: u16) -> Option<((u16, u16), (u16, u16))> {
    let mut t = session.term.lock();
    let scroll = t.scroll;
    let cols = t.cols;
    t.parser.screen_mut().set_scrollback(scroll);
    let ch = |t: &crate::session::TermState, c: u16| -> char {
        t.parser.screen().cell(row, c).and_then(|x| x.contents().chars().next()).unwrap_or(' ')
    };
    let is_word = |c: char| !c.is_whitespace();
    let result = if col < cols && is_word(ch(&t, col)) {
        let (mut a, mut b) = (col, col);
        while a > 0 && is_word(ch(&t, a - 1)) {
            a -= 1;
        }
        while b + 1 < cols && is_word(ch(&t, b + 1)) {
            b += 1;
        }
        Some(((row, a), (row, b)))
    } else {
        None
    };
    t.parser.screen_mut().set_scrollback(0);
    result
}
