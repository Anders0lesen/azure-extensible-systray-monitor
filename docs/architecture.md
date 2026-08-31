# Architecture

Azure Health Beacon separates six concerns:

1. **Connection lifecycle** — isolated Azure CLI profile, onboarding gate, 14-day lease, renewal, and deletion.
2. **Signal adapters** — provisioning state, VM instance view, constrained ARM property, Resource Graph, Log Analytics/Application Insights, and Azure Monitor metric implementations returning healthy, failed, or unconnectable.
3. **Rule lifecycle** — draft, test, apply, active, export/import, and delete.
4. **Aggregation state machine** — maps rule results and active work to tray states.
5. **Windows UI** — dark-first setup, animated tray icon, notifications, status panel, Rule Studio, signal discovery, light-mode toggle, and About window.
6. **Release/update boundary** — per-user installer, pinned GitHub metadata, explicit update policy, dual SHA-256 verification, and installer handoff.

Rule definitions are data, not executable extensions. The source registry provides one stable contract while reviewed adapters own discovery, validation, execution, and result normalization. KQL is passed as one argument to Azure's read-only Resource Graph or Azure Monitor query surface and is never executed by a local shell or interpreter. New adapters must be implemented and reviewed in source with an explicit schema and tests.

```text
Rule Studio -> source registry -> reviewed Azure adapter -> normalized findings
                 | provisioning  | zero findings = healthy
                 | VM power      | confirmed finding = red
                 | ARM property  | missing access/data = grey
                 | graph KQL     | credentials never enter the rule
                 | logs KQL
                 ` metric
```

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

- Replace the current Tk interface with a modern WinUI 3 shell while preserving the tested rule, credential, and update boundaries behind it. Complete the Windows integration spike in the [UI redesign plan](ui-redesign-plan.md) before migrating product screens.
- Validate onboarding against a selected resource for narrow RBAC identities.
- Add compound named-signal rules with temporal AND/OR correlation across adapters.
- Add Authenticode code signing; the release pipeline and provenance attestation are in place, but the public-preview binaries remain unsigned.
- Add an explicit remove-all-local-data option to uninstall.
- Evaluate a PowerToys Command Palette Dock view using the same engine.
