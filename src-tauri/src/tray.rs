use tauri::{
    App, AppHandle, Emitter, Manager,
    image::Image,
    menu::{Menu, MenuItem},
    tray::{MouseButton, TrayIconBuilder, TrayIconEvent},
};

use crate::model::BeaconState;

pub const TRAY_ID: &str = "azure-health-beacon-status";

pub fn install(app: &App) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "Open Azure Health Beacon", true, None::<&str>)?;
    let check = MenuItem::with_id(app, "check", "Check now", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &check, &quit])?;
    TrayIconBuilder::with_id(TRAY_ID)
        .menu(&menu)
        .show_menu_on_left_click(false)
        .icon(status_icon(BeaconState::Unconnectable))
        .tooltip("Azure Health Beacon — status not confirmed")
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => show_window(app),
            "check" => {
                show_window(app);
                let _ = app.emit("tray-check-now", ());
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if matches!(
                event,
                TrayIconEvent::Click {
                    button: MouseButton::Left,
                    ..
                } | TrayIconEvent::DoubleClick {
                    button: MouseButton::Left,
                    ..
                }
            ) {
                show_window(tray.app_handle());
            }
        })
        .build(app)?;
    Ok(())
}

pub fn update(app: &AppHandle, state: BeaconState) {
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        let _ = tray.set_icon(Some(status_icon(state)));
        let tooltip = match state {
            BeaconState::Healthy => "Azure Health Beacon — healthy",
            BeaconState::Failed => "Azure Health Beacon — Azure needs attention",
            BeaconState::Checking => "Azure Health Beacon — checking Azure",
            BeaconState::Connecting => "Azure Health Beacon — connecting",
            BeaconState::Unconnectable => "Azure Health Beacon — status not confirmed",
        };
        let _ = tray.set_tooltip(Some(tooltip));
    }
}

fn show_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn status_icon(state: BeaconState) -> Image<'static> {
    const SIZE: u32 = 32;
    let mut rgba = vec![0_u8; (SIZE * SIZE * 4) as usize];
    let color = match state {
        BeaconState::Healthy => [44, 214, 119, 255],
        BeaconState::Failed => [255, 68, 79, 255],
        BeaconState::Checking => [255, 174, 48, 255],
        BeaconState::Connecting => [238, 247, 255, 255],
        BeaconState::Unconnectable => [126, 143, 160, 255],
    };
    for y in 0..SIZE {
        for x in 0..SIZE {
            let dx = x as f32 - 15.5;
            let dy = y as f32 - 15.5;
            let distance = (dx * dx + dy * dy).sqrt();
            let draw = match state {
                BeaconState::Connecting => (10.0..=14.0).contains(&distance) && !(x > 20 && y < 12),
                BeaconState::Unconnectable => {
                    distance <= 14.0 && ((x + y) % 7 <= 2 || (x + SIZE - y) % 7 <= 2)
                }
                _ => distance <= 13.5,
            };
            if draw {
                let offset = ((y * SIZE + x) * 4) as usize;
                rgba[offset..offset + 4].copy_from_slice(&color);
            }
        }
    }
    if state == BeaconState::Connecting {
        for y in 6..13 {
            for x in (20 + (y - 6) / 2)..27 {
                let offset = ((y * SIZE + x) * 4) as usize;
                rgba[offset..offset + 4].copy_from_slice(&color);
            }
        }
    }
    Image::new_owned(rgba, SIZE, SIZE)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_icons_are_generated_without_branding_assets() {
        let _ = status_icon(BeaconState::Healthy);
        let _ = status_icon(BeaconState::Unconnectable);
        let _ = status_icon(BeaconState::Connecting);
        let _ = status_icon(BeaconState::Failed);
        let _ = status_icon(BeaconState::Checking);
    }
}
