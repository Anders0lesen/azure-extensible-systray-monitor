from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class CheckState(StrEnum):
    HEALTHY = "healthy"
    FAILED = "failed"
    UNCONNECTABLE = "unconnectable"


class BeaconState(StrEnum):
    HEALTHY = "healthy"
    UNCONNECTABLE = "unconnectable"
    CONNECTING = "connecting"
    FAILED = "failed"
    CHECKING = "checking"


@dataclass(slots=True)
class CheckDefinition:
    id: str
    name: str
    resource_id: str
    portal_url: str = ""
    tenant_id: str = ""
    expected_values: list[str] = field(default_factory=lambda: ["Succeeded"])
    enabled: bool = True
    kind: str = "azure_resource_provisioning"
    query: str = ""
    scope: str = "resource"

    @property
    def subscription_id(self) -> str:
        segments = [segment for segment in self.resource_id.split("/") if segment]
        for index, segment in enumerate(segments[:-1]):
            if segment.casefold() == "subscriptions":
                return segments[index + 1]
        return ""


@dataclass(slots=True)
class CheckResult:
    check_id: str
    name: str
    state: CheckState
    summary: str
    observed_value: str = ""
    checked_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    first_detected_at: datetime | None = None
    portal_url: str = ""
    findings: list[CheckFinding] = field(default_factory=list)


@dataclass(slots=True)
class CheckFinding:
    title: str
    summary: str = ""
    portal_url: str = ""


def aggregate_state(
    results: list[CheckResult], *, checking: bool = False, connecting: bool = False
) -> BeaconState:
    # A confirmed Azure failure always remains visible, including during rechecks.
    if any(result.state is CheckState.FAILED for result in results):
        return BeaconState.FAILED
    if checking:
        return BeaconState.CHECKING
    if connecting:
        return BeaconState.CONNECTING
    if not results or any(
        result.state is CheckState.UNCONNECTABLE for result in results
    ):
        return BeaconState.UNCONNECTABLE
    return BeaconState.HEALTHY
