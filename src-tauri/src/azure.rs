use std::{collections::HashMap, sync::Arc, time::Duration};

use chrono::{SecondsFormat, Utc};
use reqwest::{Method, blocking::Client};
use secrecy::ExposeSecret;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use url::Url;

use crate::{
    identity::{ARM_SCOPE, IdentityManager, LOG_ANALYTICS_SCOPE},
    model::{CheckDefinition, CheckFinding, CheckResult, CheckState},
};

const ARM_ENDPOINT: &str = "https://management.azure.com";
const LOG_ENDPOINT: &str = "https://api.loganalytics.io";
const MAX_RESPONSE_BYTES: u64 = 20 * 1024 * 1024;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Subscription {
    pub id: String,
    pub name: String,
    pub tenant_id: String,
}

pub struct AzureClient {
    identity: Arc<IdentityManager>,
    timeout: Duration,
}

impl AzureClient {
    pub fn new(identity: Arc<IdentityManager>, timeout_seconds: u64) -> Self {
        Self {
            identity,
            timeout: Duration::from_secs(timeout_seconds.clamp(5, 300)),
        }
    }

    pub fn subscriptions(&self) -> Result<Vec<Subscription>, String> {
        let mut tenants = vec!["organizations".to_owned()];
        if let Ok(payload) = self.request_json(
            Method::GET,
            arm_url("/tenants")?,
            "organizations",
            ARM_SCOPE,
            Some(&[("api-version", "2020-01-01")]),
            None,
        ) {
            for row in payload
                .get("value")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                if let Some(tenant) = row.get("tenantId").and_then(Value::as_str) {
                    if uuid::Uuid::parse_str(tenant).is_ok() {
                        tenants.push(tenant.to_owned());
                    }
                }
            }
        }

        let mut found = HashMap::new();
        let mut first_error = None;
        for tenant in tenants {
            let payload = match self.request_json(
                Method::GET,
                arm_url("/subscriptions")?,
                &tenant,
                ARM_SCOPE,
                Some(&[("api-version", "2020-01-01")]),
                None,
            ) {
                Ok(value) => value,
                Err(error) => {
                    first_error.get_or_insert(error);
                    continue;
                }
            };
            for row in payload
                .get("value")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                if !row
                    .get("state")
                    .and_then(Value::as_str)
                    .unwrap_or("Enabled")
                    .eq_ignore_ascii_case("Enabled")
                {
                    continue;
                }
                let id = row
                    .get("subscriptionId")
                    .or_else(|| row.get("id"))
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .trim_matches('/')
                    .to_owned();
                if uuid::Uuid::parse_str(&id).is_err() {
                    continue;
                }
                let tenant_id = row
                    .get("tenantId")
                    .and_then(Value::as_str)
                    .filter(|value| uuid::Uuid::parse_str(value).is_ok())
                    .unwrap_or(&tenant)
                    .to_owned();
                found.insert(
                    id.to_ascii_lowercase(),
                    Subscription {
                        id,
                        name: row
                            .get("displayName")
                            .or_else(|| row.get("name"))
                            .and_then(Value::as_str)
                            .unwrap_or("Azure subscription")
                            .to_owned(),
                        tenant_id,
                    },
                );
            }
        }
        let mut subscriptions = found.into_values().collect::<Vec<_>>();
        subscriptions.sort_by_key(|item| item.name.to_lowercase());
        if subscriptions.is_empty() {
            Err(first_error.unwrap_or_else(|| {
                "The Microsoft account has no accessible enabled Azure subscriptions".to_owned()
            }))
        } else {
            Ok(subscriptions)
        }
    }

    pub fn evaluate(
        &self,
        check: &CheckDefinition,
        default_tenant: &str,
        subscriptions: &[Subscription],
    ) -> CheckResult {
        let outcome = match check.kind.as_str() {
            "azure_resource_provisioning" => self.evaluate_property(
                check,
                default_tenant,
                "properties.provisioningState",
                "equals_any",
            ),
            "azure_resource_property" => self.evaluate_property(
                check,
                default_tenant,
                &check.property_path,
                &check.property_operator,
            ),
            "azure_vm_power_state" => self.evaluate_vm(check, default_tenant),
            "azure_resource_graph" => self.evaluate_graph(check, subscriptions),
            "azure_log_analytics" => self.evaluate_logs(check, default_tenant),
            "azure_monitor_metric" => self.evaluate_metric(check, default_tenant),
            _ => Err("This rule source is not supported by v0.8".to_owned()),
        };
        outcome.unwrap_or_else(|error| CheckResult::unreachable(check, safe_text(&error)))
    }

    fn evaluate_property(
        &self,
        check: &CheckDefinition,
        default_tenant: &str,
        path: &str,
        operator: &str,
    ) -> Result<CheckResult, String> {
        let tenant = tenant_for(check, default_tenant)?;
        let document = self.resource_document(&check.resource_id, tenant)?;
        let observed = property(&document, path);
        let healthy = compare_property(observed, operator, &check.expected_values)?;
        Ok(result(
            check,
            healthy,
            format!(
                "{path} is {}.",
                observed
                    .map(display_value)
                    .unwrap_or_else(|| "missing".to_owned())
            ),
            observed
                .map(display_value)
                .unwrap_or_else(|| "missing".to_owned()),
            Vec::new(),
        ))
    }

    fn evaluate_vm(
        &self,
        check: &CheckDefinition,
        default_tenant: &str,
    ) -> Result<CheckResult, String> {
        let tenant = tenant_for(check, default_tenant)?;
        let api_version = self.resource_api_version(&check.resource_id, tenant)?;
        let mut url = resource_url(&format!("{}/instanceView", check.resource_id))?;
        url.query_pairs_mut()
            .append_pair("api-version", &api_version);
        let payload = self.request_json(Method::GET, url, tenant, ARM_SCOPE, None, None)?;
        let state = payload
            .get("statuses")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|item| item.get("code").and_then(Value::as_str))
            .find(|value| value.to_ascii_lowercase().starts_with("powerstate/"))
            .ok_or("Azure returned no VM power state")?;
        let healthy = check
            .expected_values
            .iter()
            .any(|expected| expected.eq_ignore_ascii_case(state));
        Ok(result(
            check,
            healthy,
            format!("VM power state is {state}."),
            state.to_owned(),
            Vec::new(),
        ))
    }

    fn evaluate_graph(
        &self,
        check: &CheckDefinition,
        subscriptions: &[Subscription],
    ) -> Result<CheckResult, String> {
        if subscriptions.is_empty() {
            return Err("No accessible subscriptions are available for Resource Graph".into());
        }
        let mut rows = Vec::new();
        let mut by_tenant: HashMap<&str, Vec<&str>> = HashMap::new();
        for subscription in subscriptions {
            by_tenant
                .entry(&subscription.tenant_id)
                .or_default()
                .push(&subscription.id);
        }
        for (tenant, subscription_ids) in by_tenant {
            let body = json!({
                "subscriptions": subscription_ids,
                "query": check.query,
                "options": { "$top": 1000, "resultFormat": "objectArray" }
            });
            let payload = self.request_json(
                Method::POST,
                arm_url("/providers/Microsoft.ResourceGraph/resources")?,
                tenant,
                ARM_SCOPE,
                Some(&[("api-version", "2022-10-01")]),
                Some(&body),
            )?;
            rows.extend(
                payload
                    .get("data")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default(),
            );
        }
        Ok(rows_result(check, rows, "Resource Graph"))
    }

    fn evaluate_logs(
        &self,
        check: &CheckDefinition,
        default_tenant: &str,
    ) -> Result<CheckResult, String> {
        let tenant = tenant_for(check, default_tenant)?;
        let path = format!("/v1/workspaces/{}/query", check.workspace_id);
        let body = json!({
            "query": check.query,
            "timespan": format!("PT{}M", check.lookback_minutes)
        });
        let payload = self.request_json(
            Method::POST,
            log_url(&path)?,
            tenant,
            LOG_ANALYTICS_SCOPE,
            None,
            Some(&body),
        )?;
        let mut rows = Vec::new();
        for table in payload
            .get("tables")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            rows.extend(
                table
                    .get("rows")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default(),
            );
        }
        Ok(rows_result(check, rows, "Logs / Application Insights"))
    }

    fn evaluate_metric(
        &self,
        check: &CheckDefinition,
        default_tenant: &str,
    ) -> Result<CheckResult, String> {
        let tenant = tenant_for(check, default_tenant)?;
        let start = Utc::now() - chrono::Duration::minutes(check.lookback_minutes.into());
        let end = Utc::now();
        let mut url = resource_url(&format!(
            "{}/providers/microsoft.insights/metrics",
            check.resource_id
        ))?;
        {
            let mut query = url.query_pairs_mut();
            query
                .append_pair("api-version", "2018-01-01")
                .append_pair("metricnames", &check.metric_name)
                .append_pair("aggregation", &check.metric_aggregation)
                .append_pair(
                    "timespan",
                    &format!(
                        "{}/{}",
                        start.to_rfc3339_opts(SecondsFormat::Secs, true),
                        end.to_rfc3339_opts(SecondsFormat::Secs, true)
                    ),
                )
                .append_pair("interval", "PT1M");
            if !check.metric_namespace.is_empty() {
                query.append_pair("metricnamespace", &check.metric_namespace);
            }
            if !check.metric_filter.is_empty() {
                query.append_pair("$filter", &check.metric_filter);
            }
        }
        let payload = self.request_json(Method::GET, url, tenant, ARM_SCOPE, None, None)?;
        let aggregation = check.metric_aggregation.to_ascii_lowercase();
        let mut values = Vec::new();
        for metric in payload
            .get("value")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            for series in metric
                .get("timeseries")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                for point in series
                    .get("data")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    if let Some(value) = point.get(&aggregation).and_then(Value::as_f64) {
                        values.push(value);
                    }
                }
            }
        }
        let observed = reduce(&values, &check.metric_reducer)
            .ok_or("Azure Monitor returned no numeric metric samples")?;
        let failed = compare_number(observed, check.metric_threshold, &check.metric_operator)?;
        Ok(result(
            check,
            !failed,
            format!(
                "{} {} is {:.3}; alert threshold is {:.3}.",
                check.metric_name, check.metric_reducer, observed, check.metric_threshold
            ),
            observed.to_string(),
            Vec::new(),
        ))
    }

    fn resource_document(&self, resource_id: &str, tenant: &str) -> Result<Value, String> {
        let version = self.resource_api_version(resource_id, tenant)?;
        self.request_json(
            Method::GET,
            resource_url(resource_id)?,
            tenant,
            ARM_SCOPE,
            Some(&[("api-version", version.as_str())]),
            None,
        )
    }

    fn resource_api_version(&self, resource_id: &str, tenant: &str) -> Result<String, String> {
        let (subscription, namespace, resource_type) = resource_shape(resource_id)?;
        let path = format!("/subscriptions/{subscription}/providers/{namespace}");
        let payload = self.request_json(
            Method::GET,
            arm_url(&path)?,
            tenant,
            ARM_SCOPE,
            Some(&[("api-version", "2021-04-01")]),
            None,
        )?;
        let versions = payload
            .get("resourceTypes")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .find(|item| {
                item.get("resourceType")
                    .and_then(Value::as_str)
                    .is_some_and(|value| value.eq_ignore_ascii_case(&resource_type))
            })
            .and_then(|item| item.get("apiVersions"))
            .and_then(Value::as_array)
            .ok_or("Azure advertised no API versions for this resource type")?;
        let mut candidates = versions
            .iter()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>();
        candidates.sort_unstable_by(|left, right| right.cmp(left));
        candidates
            .iter()
            .find(|value| !value.to_ascii_lowercase().contains("preview"))
            .or_else(|| candidates.first())
            .map(|value| (*value).to_owned())
            .ok_or_else(|| "Azure advertised no API version for this resource type".to_owned())
    }

    fn request_json(
        &self,
        method: Method,
        mut url: Url,
        tenant: &str,
        scope: &str,
        query: Option<&[(&str, &str)]>,
        body: Option<&Value>,
    ) -> Result<Value, String> {
        validate_endpoint(&url, scope)?;
        if let Some(values) = query {
            url.query_pairs_mut().extend_pairs(values.iter().copied());
        }
        let token = self.identity.access_token(tenant, scope)?;
        let client = Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(self.timeout)
            .build()
            .map_err(|_| "A secure Azure HTTPS client could not be created")?;
        let mut request = client
            .request(method, url)
            .bearer_auth(token.expose_secret())
            .header("Accept", "application/json");
        if let Some(value) = body {
            request = request.json(value);
        }
        let response = request
            .send()
            .map_err(|_| "Azure could not be reached securely")?;
        if response.status().is_redirection() || !response.status().is_success() {
            let status = response.status();
            let text = response.text().unwrap_or_default();
            return Err(format!(
                "Azure returned HTTP {status}: {}",
                safe_text(&text)
            ));
        }
        if response
            .content_length()
            .is_some_and(|size| size > MAX_RESPONSE_BYTES)
        {
            return Err("Azure returned an unexpectedly large response".into());
        }
        let bytes = response
            .bytes()
            .map_err(|_| "Azure returned an unreadable response")?;
        if bytes.len() as u64 > MAX_RESPONSE_BYTES {
            return Err("Azure returned an unexpectedly large response".into());
        }
        if bytes.is_empty() {
            Ok(json!({}))
        } else {
            serde_json::from_slice(&bytes).map_err(|_| "Azure returned invalid JSON".into())
        }
    }
}

fn result(
    check: &CheckDefinition,
    healthy: bool,
    summary: String,
    observed_value: String,
    findings: Vec<CheckFinding>,
) -> CheckResult {
    CheckResult {
        check_id: check.id.clone(),
        name: check.name.clone(),
        state: if healthy {
            CheckState::Healthy
        } else {
            CheckState::Failed
        },
        summary,
        observed_value,
        checked_at: Utc::now(),
        first_detected_at: if healthy { None } else { Some(Utc::now()) },
        portal_url: check.portal_url.clone(),
        findings,
    }
}

fn rows_result(check: &CheckDefinition, rows: Vec<Value>, source: &str) -> CheckResult {
    let count = rows.len();
    let findings = rows
        .into_iter()
        .take(20)
        .enumerate()
        .map(|(index, row)| CheckFinding {
            title: format!("Finding {}", index + 1),
            summary: display_value(&row),
            portal_url: row
                .get("portalUrl")
                .and_then(Value::as_str)
                .filter(|value| value.starts_with("https://portal.azure.com/"))
                .unwrap_or_default()
                .to_owned(),
        })
        .collect();
    result(
        check,
        count == 0,
        if count == 0 {
            format!("{source} returned no findings.")
        } else {
            format!("{source} returned {count} finding(s).")
        },
        count.to_string(),
        findings,
    )
}

fn compare_property(
    value: Option<&Value>,
    operator: &str,
    expected: &[String],
) -> Result<bool, String> {
    let text = value.map(display_value).unwrap_or_default();
    let equals_any = expected.iter().any(|item| item.eq_ignore_ascii_case(&text));
    match operator {
        "equals_any" => Ok(equals_any),
        "not_equals_any" => Ok(!equals_any),
        "contains" => Ok(expected
            .iter()
            .any(|item| text.to_lowercase().contains(&item.to_lowercase()))),
        "not_contains" => Ok(expected
            .iter()
            .all(|item| !text.to_lowercase().contains(&item.to_lowercase()))),
        "greater_than" => Ok(text
            .parse::<f64>()
            .map_err(|_| "Azure property is not numeric")?
            > expected
                .first()
                .ok_or("A numeric threshold is required")?
                .parse::<f64>()
                .map_err(|_| "The property threshold is not numeric")?),
        "less_than" => Ok(text
            .parse::<f64>()
            .map_err(|_| "Azure property is not numeric")?
            < expected
                .first()
                .ok_or("A numeric threshold is required")?
                .parse::<f64>()
                .map_err(|_| "The property threshold is not numeric")?),
        "exists" => Ok(value.is_some()),
        "missing" => Ok(value.is_none()),
        _ => Err("Unsupported property comparison".into()),
    }
}

fn compare_number(value: f64, threshold: f64, operator: &str) -> Result<bool, String> {
    match operator {
        "gt" => Ok(value > threshold),
        "gte" => Ok(value >= threshold),
        "lt" => Ok(value < threshold),
        "lte" => Ok(value <= threshold),
        "eq" => Ok((value - threshold).abs() < f64::EPSILON),
        "ne" => Ok((value - threshold).abs() >= f64::EPSILON),
        _ => Err("Unsupported metric comparison".into()),
    }
}

fn reduce(values: &[f64], reducer: &str) -> Option<f64> {
    match reducer {
        "latest" => values.last().copied(),
        "maximum" => values.iter().copied().reduce(f64::max),
        "minimum" => values.iter().copied().reduce(f64::min),
        "average" => (!values.is_empty()).then(|| values.iter().sum::<f64>() / values.len() as f64),
        "total" => (!values.is_empty()).then(|| values.iter().sum()),
        _ => None,
    }
}

fn property<'a>(value: &'a Value, path: &str) -> Option<&'a Value> {
    let mut current = value;
    for segment in path.split('.') {
        let (key, index) = if let Some(open) = segment.find('[') {
            let close = segment.find(']')?;
            (
                &segment[..open],
                Some(segment[open + 1..close].parse::<usize>().ok()?),
            )
        } else {
            (segment, None)
        };
        current = current.get(key)?;
        if let Some(position) = index {
            current = current.get(position)?;
        }
    }
    Some(current)
}

fn display_value(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => serde_json::to_string(other).unwrap_or_else(|_| "unreadable".to_owned()),
    }
    .chars()
    .take(500)
    .collect()
}

fn tenant_for<'a>(check: &'a CheckDefinition, default_tenant: &'a str) -> Result<&'a str, String> {
    let value = if check.tenant_id.is_empty() {
        default_tenant
    } else {
        &check.tenant_id
    };
    uuid::Uuid::parse_str(value)
        .map(|_| value)
        .map_err(|_| "The rule has no valid Azure tenant binding".to_owned())
}

fn resource_shape(resource_id: &str) -> Result<(String, String, String), String> {
    let parts = resource_id.trim_matches('/').split('/').collect::<Vec<_>>();
    let provider = parts
        .iter()
        .position(|part| part.eq_ignore_ascii_case("providers"))
        .ok_or("The resource ID has no provider")?;
    let subscription = parts
        .windows(2)
        .find(|pair| pair[0].eq_ignore_ascii_case("subscriptions"))
        .map(|pair| pair[1])
        .ok_or("The resource ID has no subscription")?;
    let namespace = parts
        .get(provider + 1)
        .ok_or("The resource ID has no provider namespace")?;
    let resource_type = parts[provider + 2..]
        .iter()
        .step_by(2)
        .copied()
        .collect::<Vec<_>>()
        .join("/");
    if resource_type.is_empty() {
        return Err("The resource ID has no resource type".into());
    }
    Ok((
        subscription.to_owned(),
        (*namespace).to_owned(),
        resource_type,
    ))
}

fn validate_endpoint(url: &Url, scope: &str) -> Result<(), String> {
    let expected = if scope == LOG_ANALYTICS_SCOPE {
        "api.loganalytics.io"
    } else {
        "management.azure.com"
    };
    if url.scheme() != "https"
        || url.host_str() != Some(expected)
        || url.port().is_some_and(|port| port != 443)
        || !url.username().is_empty()
        || url.password().is_some()
    {
        Err("Refusing to send Azure authorization to an unexpected endpoint".into())
    } else {
        Ok(())
    }
}

fn arm_url(path: &str) -> Result<Url, String> {
    endpoint_url(ARM_ENDPOINT, path)
}
fn log_url(path: &str) -> Result<Url, String> {
    endpoint_url(LOG_ENDPOINT, path)
}
fn resource_url(path: &str) -> Result<Url, String> {
    endpoint_url(ARM_ENDPOINT, path)
}

fn endpoint_url(base: &str, path: &str) -> Result<Url, String> {
    let mut url = Url::parse(base).map_err(|_| "The fixed Azure endpoint is invalid")?;
    url.set_path(path);
    Ok(url)
}

fn safe_text(value: &str) -> String {
    let mut safe = value.split_whitespace().collect::<Vec<_>>().join(" ");
    if let Some(index) = safe.to_ascii_lowercase().find("bearer ") {
        safe.truncate(index);
        safe.push_str("Bearer [REDACTED]");
    }
    if safe.len() > 500 {
        safe.truncate(500);
    }
    if safe.is_empty() {
        "Azure did not provide a safe error description".to_owned()
    } else {
        safe
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn endpoint_guard_rejects_lookalike_hosts() {
        let bad = Url::parse("https://management.azure.com.example.test/subscriptions").unwrap();
        assert!(validate_endpoint(&bad, ARM_SCOPE).is_err());
    }

    #[test]
    fn resource_shape_supports_nested_resources() {
        let shape = resource_shape("/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet/virtualNetworkPeerings/peer").unwrap();
        assert_eq!(shape.2, "virtualNetworks/virtualNetworkPeerings");
    }

    #[test]
    fn any_query_row_is_a_confirmed_failure() {
        let check = CheckDefinition {
            name: "errors".into(),
            ..Default::default()
        };
        assert_eq!(
            rows_result(&check, vec![json!({"error": "one"})], "Logs").state,
            CheckState::Failed
        );
    }
}
