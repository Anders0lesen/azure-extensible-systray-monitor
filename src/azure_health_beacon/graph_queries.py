from __future__ import annotations

GRAPH_TEMPLATES = {
    "Active Azure Monitor alerts": """AlertsManagementResources
| where type =~ 'microsoft.alertsmanagement/alerts'
| extend monitorCondition=tostring(properties.essentials.monitorCondition), severity=tostring(properties.essentials.severity), targetResourceName=tostring(properties.essentials.targetResourceName), startDateTime=todatetime(properties.essentials.startDateTime)
| where monitorCondition =~ 'Fired'
| project subscriptionId, severity, targetResourceName, startDateTime, id
| order by severity asc, startDateTime desc""",
    "Resource Health problems": """HealthResources
| where type =~ 'microsoft.resourcehealth/availabilitystatuses'
| extend availabilityState=tostring(properties.availabilityState), targetResourceId=tostring(properties.targetResourceId), occurredTime=todatetime(properties.occurredTime)
| where availabilityState !~ 'Available'
| project subscriptionId, availabilityState, targetResourceId, occurredTime, id=targetResourceId""",
    "Active Azure Service Health issues": """ServiceHealthResources
| where type =~ 'microsoft.resourcehealth/events'
| extend eventType=tostring(properties.EventType), status=tostring(properties.Status), title=tostring(properties.Title), summary=tostring(properties.Summary), impactStartTime=todatetime(properties.ImpactStartTime)
| where eventType == 'ServiceIssue' and status == 'Active'
| project subscriptionId, title, summary, impactStartTime, id""",
    "Non-compliant Azure Policy resources": """PolicyResources
| where type =~ 'microsoft.policyinsights/policystates'
| where tostring(properties.complianceState) =~ 'NonCompliant'
| extend resourceId=tostring(properties.resourceId), policyAssignmentName=tostring(properties.policyAssignmentName)
| project subscriptionId, policyAssignmentName, resourceId, id=resourceId""",
}

CUSTOM_TEMPLATE = """Resources
| where type =~ 'microsoft.example/resourceType'
| where tostring(properties.someState) !~ 'Healthy'
| project subscriptionId, name, resourceGroup, type, id"""

