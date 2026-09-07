use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CheckState {
    Healthy,
    Failed,
    #[default]
    Unconnectable,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BeaconState {
    Healthy,
    #[default]
    Unconnectable,
    Connecting,
    Failed,
    Checking,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct CheckDefinition {
    pub id: String,
    pub name: String,
    pub resource_id: String,
    pub portal_url: String,
    pub tenant_id: String,
    pub expected_values: Vec<String>,
    pub enabled: bool,
    pub kind: String,
    pub query: String,
    pub scope: String,
    pub workspace_id: String,
    pub lookback_minutes: u32,
    pub metric_name: String,
    pub metric_namespace: String,
    pub metric_aggregation: String,
    pub metric_reducer: String,
    pub metric_operator: String,
    pub metric_threshold: f64,
    pub metric_filter: String,
    pub property_path: String,
    pub property_operator: String,
}

impl Default for CheckDefinition {
    fn default() -> Self {
        Self {
            id: Uuid::new_v4().to_string(),
            name: String::new(),
            resource_id: String::new(),
            portal_url: String::new(),
            tenant_id: String::new(),
            expected_values: vec!["Succeeded".to_owned()],
            enabled: true,
            kind: "azure_resource_provisioning".to_owned(),
            query: String::new(),
            scope: "resource".to_owned(),
            workspace_id: String::new(),
            lookback_minutes: 5,
            metric_name: String::new(),
            metric_namespace: String::new(),
            metric_aggregation: "Average".to_owned(),
            metric_reducer: "latest".to_owned(),
            metric_operator: "gt".to_owned(),
            metric_threshold: 0.0,
            metric_filter: String::new(),
            property_path: String::new(),
            property_operator: "equals_any".to_owned(),
        }
    }
}

impl CheckDefinition {
    pub fn subscription_id(&self) -> Option<&str> {
        let parts = self
            .resource_id
            .split('/')
            .filter(|part| !part.is_empty())
            .collect::<Vec<_>>();
        parts
            .windows(2)
            .find(|pair| pair[0].eq_ignore_ascii_case("subscriptions"))
            .map(|pair| pair[1])
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CheckFinding {
    pub title: String,
    #[serde(default)]
    pub summary: String,
    #[serde(default)]
    pub portal_url: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CheckResult {
    pub check_id: String,
    pub name: String,
    pub state: CheckState,
    pub summary: String,
    #[serde(default)]
    pub observed_value: String,
    pub checked_at: DateTime<Utc>,
    pub first_detected_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub portal_url: String,
    #[serde(default)]
    pub findings: Vec<CheckFinding>,
}

impl CheckResult {
    pub fn unreachable(check: &CheckDefinition, message: impl Into<String>) -> Self {
        Self {
            check_id: check.id.clone(),
            name: check.name.clone(),
            state: CheckState::Unconnectable,
            summary: message.into(),
            observed_value: String::new(),
            checked_at: Utc::now(),
            first_detected_at: None,
            portal_url: check.portal_url.clone(),
            findings: Vec::new(),
        }
    }
}

pub fn aggregate_state(results: &[CheckResult], checking: bool, connecting: bool) -> BeaconState {
    if results
        .iter()
        .any(|result| result.state == CheckState::Failed)
    {
        BeaconState::Failed
    } else if checking {
        BeaconState::Checking
    } else if connecting {
        BeaconState::Connecting
    } else if results.is_empty()
        || results
            .iter()
            .any(|result| result.state == CheckState::Unconnectable)
    {
        BeaconState::Unconnectable
    } else {
        BeaconState::Healthy
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn result(state: CheckState) -> CheckResult {
        CheckResult {
            check_id: "id".into(),
            name: "name".into(),
            state,
            summary: String::new(),
            observed_value: String::new(),
            checked_at: Utc::now(),
            first_detected_at: None,
            portal_url: String::new(),
            findings: Vec::new(),
        }
    }

    #[test]
    fn confirmed_failure_wins_during_recheck() {
        assert_eq!(
            aggregate_state(&[result(CheckState::Failed)], true, false),
            BeaconState::Failed
        );
    }

    #[test]
    fn connection_uncertainty_is_never_red() {
        assert_eq!(
            aggregate_state(&[result(CheckState::Unconnectable)], false, false),
            BeaconState::Unconnectable
        );
    }
}
