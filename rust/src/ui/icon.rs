//! The application symbol: the original `assets/icon.svg`, rendered with resvg.

use resvg::{tiny_skia, usvg};

const SVG: &str = include_str!("../../assets/icon.svg");

/// Straight (non-premultiplied) RGBA pixels of the icon at `size` x `size`.
pub fn rgba(size: u32) -> Option<Vec<u8>> {
    let tree = usvg::Tree::from_str(SVG, &usvg::Options::default()).ok()?;
    let mut pixmap = tiny_skia::Pixmap::new(size, size)?;
    let scale = size as f32 / tree.size().width().max(1.0);
    resvg::render(&tree, tiny_skia::Transform::from_scale(scale, scale), &mut pixmap.as_mut());
    let mut out = Vec::with_capacity((size * size * 4) as usize);
    for p in pixmap.pixels() {
        let c = p.demultiply();
        out.extend_from_slice(&[c.red(), c.green(), c.blue(), c.alpha()]);
    }
    Some(out)
}

pub fn window_icon() -> Option<eframe::egui::IconData> {
    Some(eframe::egui::IconData { rgba: rgba(256)?, width: 256, height: 256 })
}

pub fn texture(ctx: &eframe::egui::Context, size: u32) -> Option<eframe::egui::TextureHandle> {
    let img = eframe::egui::ColorImage::from_rgba_unmultiplied([size as usize, size as usize], &rgba(size)?);
    Some(ctx.load_texture(format!("app_icon_{size}"), img, Default::default()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_the_real_symbol() {
        let px = rgba(64).expect("icon renders");
        assert_eq!(px.len(), 64 * 64 * 4);
        // corners are transparent (rounded tile), the centre of the tile is opaque and dark
        assert_eq!(px[3], 0);
        let mid = (32 * 64 + 20) * 4;
        assert_eq!(px[mid + 3], 255);
        // the three window-control dots: red, amber, green are present
        let has = |r: u8, g: u8, b: u8| px.chunks(4).any(|c| (c[0] as i32 - r as i32).abs() < 12 && (c[1] as i32 - g as i32).abs() < 12 && (c[2] as i32 - b as i32).abs() < 12 && c[3] > 250);
        assert!(has(0xef, 0x6a, 0x73) && has(0xe0, 0xa9, 0x3b) && has(0x4c, 0xc3, 0x8a));
    }
}
