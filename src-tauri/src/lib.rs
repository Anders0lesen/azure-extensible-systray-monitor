mod azure;
mod commands;
mod config;
mod identity;
mod model;
mod security;
mod state;
mod tray;
mod updater;

use tauri::{Emitter, Manager};

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let state = state::AppState::load().expect("Azure Health Beacon state could not be loaded");
    tauri::Builder::default()
        .plugin(
            tauri_plugin_log::Builder::new()
                .level(log::LevelFilter::Info)
                .max_file_size(2_000_000)
                .build(),
        )
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
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
            commands::set_theme,
            commands::check_for_updates,
            commands::install_latest_update,
            commands::test_rule,
            commands::save_rule,
            commands::delete_rule,
            commands::export_rules,
            commands::import_rules,
            commands::check_now,
            commands::show_main,
        ])
        .setup(|app| {
            tray::install(app)?;
            let handle = app.handle().clone();
            let scheduled_state = handle.state::<state::AppState>().inner().clone();
            tauri::async_runtime::spawn(async move {
                loop {
                    let minutes = scheduled_state
                        .config
                        .lock()
                        .map(|config| config.interval_minutes)
                        .unwrap_or(5);
                    tokio::time::sleep(std::time::Duration::from_secs(
                        u64::from(minutes).saturating_mul(60),
                    ))
                    .await;
                    let connected = scheduled_state
                        .config
                        .lock()
                        .map(|config| config.onboarding_completed)
                        .unwrap_or(false);
                    if connected {
                        if let Err(error) =
                            commands::run_checks(handle.clone(), scheduled_state.clone()).await
                        {
                            log::warn!("Scheduled check did not complete: {error}");
                            let _ = handle.emit("snapshot-updated", ());
                        }
                    }
                }
            });
            let update_handle = app.handle().clone();
            let update_state = update_handle.state::<state::AppState>().inner().clone();
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(std::time::Duration::from_secs(15)).await;
                loop {
                    if commands::automatic_update_is_due(&update_state) {
                        let mode = update_state
                            .config
                            .lock()
                            .map(|config| config.update_mode.clone())
                            .unwrap_or_else(|_| "manual".to_owned());
                        match commands::check_for_updates_inner(&update_state).await {
                            Ok(release) if release.update_available && mode == "automatic" => {
                                if let Err(error) = commands::install_latest_update_inner(
                                    update_handle.clone(),
                                    &update_state,
                                )
                                .await
                                {
                                    log::warn!("Automatic update was not installed: {error}");
                                    let _ = update_handle.emit("update-error", error);
                                }
                            }
                            Ok(release) if release.update_available => {
                                let _ = update_handle.emit("update-available", release);
                            }
                            Ok(_) => {}
                            Err(error) => {
                                log::warn!("Background update check did not complete: {error}");
                            }
                        }
                    }
                    tokio::time::sleep(std::time::Duration::from_secs(60 * 60)).await;
                }
            });
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
