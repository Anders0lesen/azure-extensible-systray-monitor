use std::{
    collections::HashSet,
    env, fs,
    io::Write,
    path::{Path, PathBuf},
};

use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

use crate::{model::CheckDefinition, security::atomic_replace};

pub const SCHEMA_VERSION: u32 = 7;
pub const RULE_PACK_FORMAT: &str = "azure-health-beacon-rule-pack";
pub const RULE_PACK_SCHEMA_VERSION: u32 = 4;
pub const MAX_RULE_PACK_BYTES: u64 = 1_000_000;
pub const MAX_RULES_PER_PACK: usize = 500;
pub const AUTHORIZATION_MAX_DAYS: i64 = 14;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct AppConfig {
    pub schema_version: u32,
    pub onboarding_completed: bool,
    pub azure_subscription_id: String,
    pub azure_subscription_name: String,
    pub azure_tenant_id: String,
    pub connection_established_utc: String,
    pub connection_purge_pending: bool,
    pub interval_minutes: u32,
    pub timeout_seconds: u64,
    pub retry_count: u32,
    pub update_mode: String,
    pub last_update_check_utc: String,
    pub start_with_windows: bool,
    pub start_minimized: bool,
    pub theme_mode: String,
    pub checks: Vec<CheckDefinition>,
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            onboarding_completed: false,
            azure_subscription_id: String::new(),
            azure_subscription_name: String::new(),
            azure_tenant_id: String::new(),
            connection_established_utc: String::new(),
            connection_purge_pending: false,
            interval_minutes: 5,
            timeout_seconds: 30,
            retry_count: 2,
            update_mode: "manual".to_owned(),
            last_update_check_utc: String::new(),
            start_with_windows: false,
            start_minimized: false,
            theme_mode: "dark".to_owned(),
            checks: Vec::new(),
        }
    }
}

impl AppConfig {
    pub fn validate(&self) -> Result<(), String> {
        if !(1..=1440).contains(&self.interval_minutes) {
            return Err("Check interval must be between 1 and 1440 minutes".into());
        }
        if !(5..=300).contains(&self.timeout_seconds) {
            return Err("Timeout must be between 5 and 300 seconds".into());
        }
        if self.retry_count > 5 {
            return Err("Retry count must be between 0 and 5".into());
        }
        if !matches!(self.update_mode.as_str(), "manual" | "notify" | "automatic") {
            return Err("Update mode must be manual, notify, or automatic".into());
        }
        if !matches!(self.theme_mode.as_str(), "dark" | "light") {
            return Err("Theme must be dark or light".into());
        }
        let mut ids = HashSet::new();
        for check in &self.checks {
            validate_rule(check)?;
            if !ids.insert(check.id.to_ascii_lowercase()) {
                return Err(format!("Duplicate rule ID: {}", check.id));
            }
        }
        reject_secret_like_json(
            &serde_json::to_value(self).map_err(|_| "Configuration could not be validated")?,
        )
    }

    pub fn connection_expired(&self, now: DateTime<Utc>) -> bool {
        if !self.onboarding_completed || self.connection_established_utc.is_empty() {
            return self.onboarding_completed;
        }
        DateTime::parse_from_rfc3339(&self.connection_established_utc)
            .map(|value| now >= value.with_timezone(&Utc) + Duration::days(AUTHORIZATION_MAX_DAYS))
            .unwrap_or(true)
    }

    pub fn clear_connection(&mut self) {
        self.onboarding_completed = false;
        self.azure_subscription_id.clear();
        self.azure_subscription_name.clear();
        self.azure_tenant_id.clear();
        self.connection_established_utc.clear();
    }
}

pub fn app_data_dir() -> PathBuf {
    env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join("AzureHealthBeacon")
}

pub fn config_path() -> PathBuf {
    app_data_dir().join("checks.json")
}

pub fn load_config_from(path: &Path) -> Result<AppConfig, String> {
    if !path.exists() {
        return Ok(AppConfig::default());
    }
    let bytes = fs::read(path).map_err(|_| "The Beacon configuration could not be read")?;
    let raw: Value =
        serde_json::from_slice(&bytes).map_err(|_| "The Beacon configuration is not valid JSON")?;
    reject_sensitive_keys(&raw, "config")?;
    let loaded_schema = raw
        .get("schema_version")
        .and_then(Value::as_u64)
        .unwrap_or_default();
    if !(1..=SCHEMA_VERSION as u64).contains(&loaded_schema) {
        return Err(format!("Unsupported configuration schema: {loaded_schema}"));
    }
    let mut config: AppConfig = serde_json::from_value(raw)
        .map_err(|error| format!("The Beacon configuration is incompatible: {error}"))?;
    config.schema_version = SCHEMA_VERSION;
    config.validate()?;
    Ok(config)
}

pub fn load_config() -> Result<AppConfig, String> {
    load_config_from(&config_path())
}

pub fn save_config_to(path: &Path, config: &AppConfig) -> Result<(), String> {
    config.validate()?;
    let mut normalized = config.clone();
    normalized.schema_version = SCHEMA_VERSION;
    let bytes = serde_json::to_vec_pretty(&normalized)
        .map_err(|_| "Configuration could not be serialized")?;
    let parent = path
        .parent()
        .ok_or("Configuration path has no parent directory")?;
    fs::create_dir_all(parent).map_err(|_| "Configuration directory could not be created")?;
    let temporary = parent.join(format!("checks-{}.tmp", Uuid::new_v4()));
    let result = (|| {
        let mut file = fs::File::create(&temporary)
            .map_err(|_| "Temporary configuration could not be created")?;
        file.write_all(&bytes)
            .map_err(|_| "Temporary configuration could not be written")?;
        file.write_all(b"\n")
            .map_err(|_| "Temporary configuration could not be finalized")?;
        file.sync_all()
            .map_err(|_| "Temporary configuration could not be flushed")?;
        if path.exists() {
            fs::copy(path, path.with_extension("json.bak"))
                .map_err(|_| "Configuration backup could not be created")?;
        }
        atomic_replace(&temporary, path)
            .map_err(|_| "Configuration could not be replaced atomically")
    })();
    let _ = fs::remove_file(&temporary);
    result
}

pub fn save_config(config: &AppConfig) -> Result<(), String> {
    save_config_to(&config_path(), config)
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RulePack {
    format: String,
    schema_version: u32,
    created_utc: String,
    checks: Vec<CheckDefinition>,
}

pub fn export_rule_pack(path: &Path, checks: &[CheckDefinition]) -> Result<(), String> {
    if checks.is_empty() {
        return Err("There are no rules to export".into());
    }
    if checks.len() > MAX_RULES_PER_PACK {
        return Err("Too many rules to export in one pack".into());
    }
    for check in checks {
        validate_rule(check)?;
    }
    let pack = RulePack {
        format: RULE_PACK_FORMAT.to_owned(),
        schema_version: RULE_PACK_SCHEMA_VERSION,
        created_utc: Utc::now().to_rfc3339(),
        checks: checks.to_vec(),
    };
    let value = serde_json::to_value(&pack).map_err(|_| "Rule pack could not be prepared")?;
    reject_sensitive_keys(&value, "rule_pack")?;
    reject_secret_like_json(&value)?;
    let mut bytes =
        serde_json::to_vec_pretty(&pack).map_err(|_| "Rule pack could not be serialized")?;
    bytes.push(b'\n');
    if bytes.len() as u64 > MAX_RULE_PACK_BYTES {
        return Err("Rule pack exceeds the 1 MB safety limit".into());
    }
    let parent = path
        .parent()
        .ok_or("Rule pack path has no parent directory")?;
    fs::create_dir_all(parent).map_err(|_| "Rule pack directory could not be created")?;
    let temporary = parent.join(format!("rules-{}.tmp", Uuid::new_v4()));
    let result = (|| {
        let mut file =
            fs::File::create(&temporary).map_err(|_| "Temporary rule pack could not be created")?;
        file.write_all(&bytes)
            .map_err(|_| "Temporary rule pack could not be written")?;
        file.sync_all()
            .map_err(|_| "Temporary rule pack could not be flushed")?;
        atomic_replace(&temporary, path).map_err(|_| "Rule pack could not be replaced atomically")
    })();
    let _ = fs::remove_file(temporary);
    result
}

pub fn import_rule_pack(path: &Path) -> Result<Vec<CheckDefinition>, String> {
    let metadata = fs::metadata(path).map_err(|_| "Rule pack could not be opened")?;
    if metadata.len() > MAX_RULE_PACK_BYTES {
        return Err("Rule pack exceeds the 1 MB safety limit".into());
    }
    let bytes = fs::read(path).map_err(|_| "Rule pack could not be read")?;
    let raw: Value = serde_json::from_slice(&bytes).map_err(|_| "Rule pack is not valid JSON")?;
    reject_sensitive_keys(&raw, "rule_pack")?;
    reject_secret_like_json(&raw)?;
    let mut pack: RulePack = serde_json::from_value(raw)
        .map_err(|error| format!("Rule pack is incompatible: {error}"))?;
    if pack.format != RULE_PACK_FORMAT
        || !(1..=RULE_PACK_SCHEMA_VERSION).contains(&pack.schema_version)
    {
        return Err("This is not a supported Azure Health Beacon rule pack".into());
    }
    if pack.checks.is_empty() || pack.checks.len() > MAX_RULES_PER_PACK {
        return Err("Rule pack contains no rules or exceeds the 500-rule limit".into());
    }
    let mut ids = HashSet::new();
    for check in &mut pack.checks {
        validate_rule(check)?;
        if !ids.insert(check.id.to_ascii_lowercase()) {
            return Err("Rule pack contains duplicate rule IDs".into());
        }
        check.enabled = false;
    }
    Ok(pack.checks)
}

pub fn validate_rule(rule: &CheckDefinition) -> Result<(), String> {
    const KINDS: &[&str] = &[
        "azure_resource_provisioning",
        "azure_vm_power_state",
        "azure_resource_property",
        "azure_resource_graph",
        "azure_log_analytics",
        "azure_monitor_metric",
    ];
    if !KINDS.contains(&rule.kind.as_str()) {
        return Err(format!("Unsupported check kind: {}", rule.kind));
    }
    if Uuid::parse_str(&rule.id).is_err() || rule.name.trim().is_empty() || rule.name.len() > 200 {
        return Err("Rule ID and a name of at most 200 characters are required".into());
    }
    if rule.query.len() > 20_000
        || rule
            .query
            .chars()
            .any(|character| character.is_control() && !matches!(character, '\r' | '\n' | '\t'))
    {
        return Err("KQL query is too long or contains control characters".into());
    }
    if rule.expected_values.len() > 20 || rule.expected_values.iter().any(|value| value.len() > 500)
    {
        return Err("Too many or overly long expected values".into());
    }
    if !rule.metric_threshold.is_finite() || !(1..=10_080).contains(&rule.lookback_minutes) {
        return Err(
            "Lookback and metric threshold must be finite and within supported limits".into(),
        );
    }
    if rule.kind == "azure_resource_graph" {
        if rule.scope != "all_accessible" || rule.query.trim().is_empty() {
            return Err(
                "Resource Graph requires a KQL query across all accessible subscriptions".into(),
            );
        }
    } else if rule.kind == "azure_log_analytics" {
        if rule.scope != "workspace"
            || rule.query.trim().is_empty()
            || Uuid::parse_str(&rule.workspace_id).is_err()
        {
            return Err("Logs requires a KQL query and a valid workspace ID".into());
        }
    } else {
        validate_resource_id(&rule.resource_id)?;
    }
    if rule.kind == "azure_resource_property" {
        validate_property_path(&rule.property_path)?;
        const OPERATORS: &[&str] = &[
            "equals_any",
            "not_equals_any",
            "contains",
            "not_contains",
            "greater_than",
            "less_than",
            "exists",
            "missing",
        ];
        if !OPERATORS.contains(&rule.property_operator.as_str()) {
            return Err("Unsupported property comparison".into());
        }
    }
    if rule.kind == "azure_monitor_metric" {
        const AGGREGATIONS: &[&str] = &["Average", "Count", "Maximum", "Minimum", "Total"];
        const REDUCERS: &[&str] = &["latest", "maximum", "minimum", "average", "total"];
        const OPERATORS: &[&str] = &["gt", "gte", "lt", "lte", "eq", "ne"];
        if rule.metric_name.is_empty()
            || !AGGREGATIONS.contains(&rule.metric_aggregation.as_str())
            || !REDUCERS.contains(&rule.metric_reducer.as_str())
            || !OPERATORS.contains(&rule.metric_operator.as_str())
            || rule.metric_filter.len() > 2_000
        {
            return Err("Metric definition is incomplete or unsupported".into());
        }
    }
    if !rule.portal_url.is_empty() {
        let parsed = url::Url::parse(&rule.portal_url).map_err(|_| "Portal URL is invalid")?;
        if parsed.scheme() != "https" || parsed.host_str() != Some("portal.azure.com") {
            return Err("Portal links must use https://portal.azure.com".into());
        }
    }
    reject_secret_like_json(&serde_json::to_value(rule).map_err(|_| "Rule could not be validated")?)
}

fn validate_resource_id(resource_id: &str) -> Result<(), String> {
    let value = resource_id.trim();
    let lower = value.to_ascii_lowercase();
    if !value.starts_with('/')
        || !lower.contains("/subscriptions/")
        || !lower.contains("/resourcegroups/")
        || !lower.contains("/providers/")
        || value.contains("..")
        || value.contains(['?', '#', '\\'])
    {
        return Err("Select a complete Azure resource ID".into());
    }
    Ok(())
}

fn validate_property_path(path: &str) -> Result<(), String> {
    if path.is_empty()
        || path.len() > 512
        || path.chars().any(|character| {
            !(character.is_ascii_alphanumeric() || matches!(character, '_' | '-' | '.' | '[' | ']'))
        })
    {
        return Err(
            "Enter a constrained property path such as properties.provisioningState".into(),
        );
    }
    if path.split('.').any(|segment| {
        segment.is_empty() || segment.starts_with(|character: char| character.is_ascii_digit())
    }) {
        return Err("Property path contains an unsupported segment".into());
    }
    Ok(())
}

fn reject_sensitive_keys(value: &Value, path: &str) -> Result<(), String> {
    match value {
        Value::Object(map) => {
            for (key, child) in map {
                let lower = key.to_ascii_lowercase();
                if ["password", "secret", "token", "credential", "access_key"]
                    .iter()
                    .any(|part| lower.contains(part))
                {
                    return Err(format!(
                        "Secrets are not allowed in check configuration ({path}.{key})"
                    ));
                }
                reject_sensitive_keys(child, &format!("{path}.{key}"))?;
            }
        }
        Value::Array(values) => {
            for (index, child) in values.iter().enumerate() {
                reject_sensitive_keys(child, &format!("{path}[{index}]"))?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn reject_secret_like_json(value: &Value) -> Result<(), String> {
    let text = serde_json::to_string(value).map_err(|_| "Configuration could not be inspected")?;
    let lower = text.to_ascii_lowercase();
    if lower.contains("accountkey=")
        || lower.contains("sharedaccesssignature=")
        || lower.contains("client_secret=")
        || lower.contains("client-secret=")
        || lower.contains("?sig=")
        || lower.contains("&sig=")
        || text
            .split('.')
            .any(|part| part.starts_with("eyJ") && part.len() > 30)
    {
        return Err("Possible secret detected; it will not be stored or exported".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn existing_v6_shape_migrates_without_credentials() {
        let directory = std::env::temp_dir().join(format!("beacon-config-{}", Uuid::new_v4()));
        fs::create_dir_all(&directory).unwrap();
        let path = directory.join("checks.json");
        fs::write(&path, r#"{"schema_version":6,"interval_minutes":5,"timeout_seconds":30,"retry_count":2,"update_mode":"manual","theme_mode":"dark","checks":[]}"#).unwrap();
        let loaded = load_config_from(&path).unwrap();
        assert_eq!(loaded.schema_version, SCHEMA_VERSION);
        let _ = fs::remove_dir_all(directory);
    }

    #[test]
    fn secret_like_rule_is_rejected() {
        let mut rule = CheckDefinition::default();
        rule.name = "unsafe".into();
        rule.resource_id = "/subscriptions/1/resourceGroups/a/providers/X/y/z".into();
        rule.portal_url = "https://portal.azure.com/#view/test?sig=abc".into();
        assert!(validate_rule(&rule).is_err());
    }

    #[test]
    fn expiry_is_exactly_fourteen_days() {
        let now = Utc::now();
        let mut config = AppConfig::default();
        config.onboarding_completed = true;
        config.connection_established_utc = (now - Duration::days(14)).to_rfc3339();
        assert!(config.connection_expired(now));
    }

    #[test]
    fn imported_rules_are_always_inert() {
        let directory = std::env::temp_dir().join(format!("beacon-pack-{}", Uuid::new_v4()));
        fs::create_dir_all(&directory).unwrap();
        let path = directory.join("rules.json");
        let mut rule = CheckDefinition::default();
        rule.name = "resource".into();
        rule.resource_id = "/subscriptions/1/resourceGroups/a/providers/X/y/z".into();
        export_rule_pack(&path, &[rule]).unwrap();
        let imported = import_rule_pack(&path).unwrap();
        assert!(!imported[0].enabled);
        let _ = fs::remove_dir_all(directory);
    }
}
