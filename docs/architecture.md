# Architecture

Azure Health Beacon separates six concerns:

1. **Connection lifecycle** — isolated Azure CLI profile, onboarding gate, 14-day lease, renewal, and deletion.
2. **Check engine** — provisioning and Resource Graph implementations returning healthy, failed, or unconnectable.
3. **Rule lifecycle** — draft, test, apply, active, export/import, and delete.
4. **Aggregation state machine** — maps rule results and active work to tray states.
5. **Windows UI** — setup wizard, animated tray icon, notifications, status panel, and rule manager.
6. **Release/update boundary** — per-user installer, pinned GitHub metadata, explicit update policy, dual SHA-256 verification, and installer handoff.

Rule definitions are data, not executable extensions. Resource Graph rules may contain KQL, but it is sent only to Azure's read-only Resource Graph endpoint and is never executed by a local shell or interpreter. New checker types must be implemented and reviewed in source with an explicit schema and tests.

## State precedence

```text
confirmed failure
  > active check
  > connecting
  > unconnectable/no result
  > all healthy
```

A confirmed failure remains red during a recheck so a known incident cannot disappear behind amber animation.

## Roadmap

- Validate onboarding against a selected resource for narrow RBAC identities.
- Add Azure Monitor metric and Log Analytics rule families for signals Resource Graph cannot provide.
- Add Authenticode code signing; the release pipeline and provenance attestation are in place, but the public-preview binaries remain unsigned.
- Add an explicit remove-all-local-data option to uninstall.
- Evaluate a PowerToys Command Palette Dock view using the same engine.
