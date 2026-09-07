use chrono::Utc;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager, State};
use tauri_plugin_autostart::ManagerExt;

use crate::{
    azure::{AzureClient, Subscription},
    config::{AppConfig, save_config, validate_rule},
    model::{BeaconState, CheckDefinition, CheckResult, aggregate_state},
    state::AppState,
};

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AppSnapshot {
    pub version: String,
    pub connected: bool,
    pub connection_expires_utc: String,
    pub config: AppConfig,
    pub results: Vec<CheckResult>,
    pub beacon_state: BeaconState,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SettingsPatch {
    pub interval_minutes: u32,
    pub timeout_seconds: u64,
    pub retry_count: u32,
    pub update_mode: String,
    pub start_with_windows: bool,
    pub start_minimized: bool,
    pub theme_mode: String,
}

#[tauri::command]
pub fn snapshot(state: State<'_, AppState>) -> Result<AppSnapshot, String> {
    snapshot_inner(&state)
}

#[tauri::command]
pub async fn sign_in(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<Vec<Subscription>, String> {
    let identity = state.identity.clone();
    let sign_in_app = app.clone();
    tauri::async_runtime::spawn_blocking(move || identity.sign_in(&sign_in_app, "organizations"))
        .await
        .map_err(|_| "The secure sign-in task stopped unexpectedly")??;
    discover_subscriptions(state).await
}

#[tauri::command]
pub async fn discover_subscriptions(
    state: State<'_, AppState>,
) -> Result<Vec<Subscription>, String> {
    let identity = state.identity.clone();
    let timeout = state
        .config
        .lock()
        .map_err(|_| "The settings lock is unavailable")?
        .timeout_seconds;
    tauri::async_runtime::spawn_blocking(move || {
        AzureClient::new(identity, timeout).subscriptions()
    })
    .await
    .map_err(|_| "Azure subscription discovery stopped unexpectedly")?
}

#[tauri::command]
pub async fn complete_setup(
    subscription_id: String,
    state: State<'_, AppState>,
) -> Result<AppSnapshot, String> {
    let subscriptions = discover_subscriptions(state.clone()).await?;
    let selected = subscriptions
        .into_iter()
        .find(|item| item.id.eq_ignore_ascii_case(subscription_id.trim()))
        .ok_or("The selected Azure subscription is no longer available")?;
    {
        let mut config = state
            .config
            .lock()
            .map_err(|_| "The settings lock is unavailable")?;
        config.onboarding_completed = true;
        config.azure_subscription_id = selected.id;
        config.azure_subscription_name = selected.name;
        config.azure_tenant_id = selected.tenant_id;
        config.connection_established_utc = Utc::now().to_rfc3339();
        config.connection_purge_pending = false;
        save_config(&config)?;
    }
    snapshot_inner(&state)
}

#[tauri::command]
pub fn delete_connection(state: State<'_, AppState>) -> Result<AppSnapshot, String> {
    state.identity.delete()?;
    {
        let mut config = state
            .config
            .lock()
            .map_err(|_| "The settings lock is unavailable")?;
        config.clear_connection();
        save_config(&config)?;
    }
    state
        .results
        .lock()
        .map_err(|_| "The result lock is unavailable")?
        .clear();
    snapshot_inner(&state)
}

#[tauri::command]
pub fn save_settings(
    app: AppHandle,
    patch: SettingsPatch,
    state: State<'_, AppState>,
) -> Result<AppSnapshot, String> {
    let autostart = app.autolaunch();
    if patch.start_with_windows {
        autostart
            .enable()
            .map_err(|_| "Windows startup could not be enabled")?;
    } else {
        autostart
            .disable()
            .map_err(|_| "Windows startup could not be disabled")?;
    }
    {
        let mut config = state
            .config
            .lock()
            .map_err(|_| "The settings lock is unavailable")?;
        config.interval_minutes = patch.interval_minutes;
        config.timeout_seconds = patch.timeout_seconds;
        config.retry_count = patch.retry_count;
        config.update_mode = patch.update_mode;
        config.start_with_windows = patch.start_with_windows;
        config.start_minimized = patch.start_minimized;
        config.theme_mode = patch.theme_mode;
        save_config(&config)?;
    }
    snapshot_inner(&state)
}

#[tauri::command]
pub async fn test_rule(
    rule: CheckDefinition,
    state: State<'_, AppState>,
) -> Result<CheckResult, String> {
    validate_rule(&rule)?;
    let config = state
        .config
        .lock()
        .map_err(|_| "The settings lock is unavailable")?
        .clone();
    if !config.onboarding_completed {
        return Err("Connect Azure before testing rules".into());
    }
    let identity = state.identity.clone();
    let tested = tauri::async_runtime::spawn_blocking(move || {
        let client = AzureClient::new(identity, config.timeout_seconds);
        let subscriptions = if rule.kind == "azure_resource_graph" {
            client.subscriptions()?
        } else {
            Vec::new()
        };
        let result = client.evaluate(&rule, &config.azure_tenant_id, &subscriptions);
        Ok::<_, String>((rule, result))
    })
    .await
    .map_err(|_| "The rule test stopped unexpectedly")??;
    if tested.1.state != crate::model::CheckState::Unconnectable {
        state
            .tested_rules
            .lock()
            .map_err(|_| "The tested-rule lock is unavailable")?
            .insert(rule_fingerprint(&tested.0)?);
    }
    Ok(tested.1)
}

#[tauri::command]
pub fn save_rule(rule: CheckDefinition, state: State<'_, AppState>) -> Result<AppSnapshot, String> {
    validate_rule(&rule)?;
    let fingerprint = rule_fingerprint(&rule)?;
    if !state
        .tested_rules
        .lock()
        .map_err(|_| "The tested-rule lock is unavailable")?
        .remove(&fingerprint)
    {
        return Err("Test this exact rule successfully before applying it".into());
    }
    {
        let mut config = state
            .config
            .lock()
            .map_err(|_| "The settings lock is unavailable")?;
        if let Some(existing) = config.checks.iter_mut().find(|item| item.id == rule.id) {
            *existing = rule;
        } else {
            config.checks.push(rule);
        }
        save_config(&config)?;
    }
    snapshot_inner(&state)
}

fn rule_fingerprint(rule: &CheckDefinition) -> Result<String, String> {
    let serialized = serde_json::to_vec(rule).map_err(|_| "The rule could not be fingerprinted")?;
    Ok(format!("{:x}", Sha256::digest(serialized)))
}

#[tauri::command]
pub fn delete_rule(rule_id: String, state: State<'_, AppState>) -> Result<AppSnapshot, String> {
    {
        let mut config = state
            .config
            .lock()
            .map_err(|_| "The settings lock is unavailable")?;
        config.checks.retain(|item| item.id != rule_id);
        save_config(&config)?;
    }
    state
        .results
        .lock()
        .map_err(|_| "The result lock is unavailable")?
        .retain(|item| item.check_id != rule_id);
    snapshot_inner(&state)
}

#[tauri::command]
pub async fn check_now(state: State<'_, AppState>) -> Result<AppSnapshot, String> {
    let config = state
        .config
        .lock()
        .map_err(|_| "The settings lock is unavailable")?
        .clone();
    if !config.onboarding_completed {
        return Err("Connect Azure before running checks".into());
    }
    let identity = state.identity.clone();
    let results = tauri::async_runtime::spawn_blocking(move || {
        let client = AzureClient::new(identity, config.timeout_seconds);
        let graph_needed = config
            .checks
            .iter()
            .any(|rule| rule.enabled && rule.kind == "azure_resource_graph");
        let subscriptions = if graph_needed {
            client.subscriptions().unwrap_or_default()
        } else {
            Vec::new()
        };
        config
            .checks
            .iter()
            .filter(|rule| rule.enabled)
            .map(|rule| client.evaluate(rule, &config.azure_tenant_id, &subscriptions))
            .collect::<Vec<_>>()
    })
    .await
    .map_err(|_| "The Azure check task stopped unexpectedly")?;
    *state
        .results
        .lock()
        .map_err(|_| "The result lock is unavailable")? = results;
    snapshot_inner(&state)
}

#[tauri::command]
pub fn show_main(app: AppHandle) -> Result<(), String> {
    let window = app
        .get_webview_window("main")
        .ok_or("The main window is unavailable")?;
    window
        .show()
        .map_err(|_| "The main window could not be shown")?;
    window
        .unminimize()
        .map_err(|_| "The main window could not be restored")?;
    window
        .set_focus()
        .map_err(|_| "The main window could not be focused")
}

fn snapshot_inner(state: &State<'_, AppState>) -> Result<AppSnapshot, String> {
    let config = state
        .config
        .lock()
        .map_err(|_| "The settings lock is unavailable")?
        .clone();
    let results = state
        .results
        .lock()
        .map_err(|_| "The result lock is unavailable")?
        .clone();
    let expires = chrono::DateTime::parse_from_rfc3339(&config.connection_established_utc)
        .ok()
        .map(|value| value + chrono::Duration::days(14))
        .map(|value| value.to_rfc3339())
        .unwrap_or_default();
    Ok(AppSnapshot {
        version: env!("CARGO_PKG_VERSION").to_owned(),
        connected: config.onboarding_completed && state.identity.available(),
        connection_expires_utc: expires,
        beacon_state: aggregate_state(&results, false, false),
        config,
        results,
    })
}
