mod ai;
mod config;
mod conn;
#[cfg(test)]
mod e2e_tests;
mod editlogic;
mod highlight;
mod logclean;
mod macrorec;
mod plugins;
mod samples;
mod script;
mod session;
mod sftp;
mod suite;
mod ui;

fn main() -> eframe::Result {
    let mut viewport = eframe::egui::ViewportBuilder::default()
        .with_title(config::APP_NAME)
        .with_inner_size([1340.0, 780.0])
        .with_min_inner_size([1020.0, 620.0]);
    if let Some(icon) = ui::icon::window_icon() {
        viewport = viewport.with_icon(std::sync::Arc::new(icon));
    }
    let options = eframe::NativeOptions { viewport, ..Default::default() };
    eframe::run_native(config::APP_NAME, options, Box::new(|cc| Ok(Box::new(ui::app::App::new(cc)))))
}
