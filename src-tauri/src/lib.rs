mod azure;
mod commands;
mod config;
mod identity;
mod model;
mod security;
mod state;

use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let state = state::AppState::load().expect("Azure Health Beacon state could not be loaded");
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            Some(vec!["--minimized"]),
        ))
        .plugin(tauri_plugin_single_instance::init(
            |app, _arguments, _cwd| {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.show();
                    let _ = window.unminimize();
                    let _ = window.set_focus();
                }
            },
        ))
        .manage(state)
        .invoke_handler(tauri::generate_handler![
            commands::snapshot,
            commands::sign_in,
            commands::discover_subscriptions,
            commands::complete_setup,
            commands::delete_connection,
            commands::save_settings,
            commands::test_rule,
            commands::save_rule,
            commands::delete_rule,
            commands::check_now,
            commands::show_main,
        ])
        .setup(|app| {
            if let Some(window) = app.get_webview_window("main") {
                let minimized = std::env::args().any(|argument| argument == "--minimized");
                if !minimized {
                    window.show()?;
                    window.set_focus()?;
                }
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("Azure Health Beacon failed to start");
}
