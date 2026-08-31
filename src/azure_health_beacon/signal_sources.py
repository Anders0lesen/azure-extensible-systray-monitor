from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SignalSource:
    key: str
    label: str
    description: str
    query_language: str = ""


SIGNAL_SOURCES = (
    SignalSource(
        "azure_resource_provisioning",
        "Provisioning state",
        "Confirm that one Azure resource finished provisioning successfully.",
    ),
    SignalSource(
        "azure_vm_power_state",
        "VM power state",
        "Confirm that one Azure virtual machine is in an expected live power state.",
    ),
    SignalSource(
        "azure_resource_property",
        "Resource property (advanced)",
        "Read and compare a chosen property from one Azure Resource Manager document.",
    ),
    SignalSource(
        "azure_resource_graph",
        "Resource Graph",
        "Find inventory, health, policy, and control-plane records across subscriptions.",
        "Resource Graph KQL",
    ),
    SignalSource(
        "azure_log_analytics",
        "Logs / Application Insights",
        "Run full KQL against a Log Analytics workspace. Any returned row is a finding.",
        "Azure Monitor KQL",
    ),
    SignalSource(
        "azure_monitor_metric",
        "Azure Monitor metric",
        "Evaluate any metric exposed by a selected Azure resource against a threshold.",
    ),
)

SOURCE_BY_KEY = {source.key: source for source in SIGNAL_SOURCES}
SOURCE_KEY_BY_LABEL = {source.label: source.key for source in SIGNAL_SOURCES}


LOG_TEMPLATES = {
    "Application Insights — failed requests": """AppRequests
| where TimeGenerated > ago(5m)
| where Success == false
| project TimeGenerated, Name, ResultCode, OperationId, _ResourceId""",
    "Application Insights — three errors in one session": """AppExceptions
| where TimeGenerated > ago(5m)
| where isnotempty(SessionId)
| summarize ErrorCount=count(), FirstSeen=min(TimeGenerated), LastSeen=max(TimeGenerated), Example=any(OuterMessage) by SessionId, UserId, _ResourceId
| where ErrorCount >= 3
| project SessionId, UserId, ErrorCount, FirstSeen, LastSeen, Example, _ResourceId""",
    "Azure Virtual Desktop — failed new connections": """WVDConnections
| where TimeGenerated > ago(5m)
| where State =~ 'Failed'
| join kind=leftouter (
    WVDErrors
    | where TimeGenerated > ago(5m)
    | summarize Error=any(Message), ErrorCode=any(CodeSymbolic) by CorrelationId
) on CorrelationId
| project TimeGenerated, UserName, SessionHostName, CorrelationId, ErrorCode, Error, _ResourceId""",
    "Azure Functions — failed invocations": """AppRequests
| where TimeGenerated > ago(5m)
| where Success == false
| where OperationName startswith 'Functions.'
| project TimeGenerated, OperationName, ResultCode, DurationMs, OperationId, _ResourceId""",
    "Container restarts": """KubePodInventory
| where TimeGenerated > ago(5m)
| summarize Restarts=max(ContainerRestartCount) by ClusterName, Namespace, Name, ContainerName, _ResourceId
| where Restarts > 0""",
}

CUSTOM_LOG_TEMPLATE = """// Return one row per condition that should turn the Beacon red.
// Zero rows means healthy. Missing access or data is shown as unknown/grey.
AzureActivity
| where TimeGenerated > ago(5m)
| take 10"""


METRIC_OPERATORS = {
    "gt": ">  greater than",
    "gte": "≥  at least",
    "lt": "<  less than",
    "lte": "≤  at most",
    "eq": "=  equal to",
    "ne": "≠  not equal to",
}

METRIC_REDUCERS = {
    "latest": "Latest value",
    "maximum": "Maximum value",
    "minimum": "Minimum value",
    "average": "Average value",
    "total": "Sum of values",
}

PROPERTY_OPERATORS = {
    "equals_any": "Equals any healthy value",
    "not_equals_any": "Does not equal any value",
    "contains": "Contains text",
    "not_contains": "Does not contain text",
    "greater_than": "Is greater than",
    "less_than": "Is less than",
    "exists": "Exists",
    "missing": "Is missing",
}

VM_POWER_STATES = (
    "PowerState/running",
    "PowerState/stopped",
    "PowerState/deallocated",
    "PowerState/starting",
    "PowerState/stopping",
    "PowerState/deallocating",
)

METRIC_OPERATOR_BY_LABEL = {label: key for key, label in METRIC_OPERATORS.items()}
METRIC_REDUCER_BY_LABEL = {label: key for key, label in METRIC_REDUCERS.items()}
PROPERTY_OPERATOR_BY_LABEL = {label: key for key, label in PROPERTY_OPERATORS.items()}
