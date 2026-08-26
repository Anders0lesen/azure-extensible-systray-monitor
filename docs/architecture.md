# Architecture

Azure Health Beacon separates five concerns:

1. **Connection lifecycle** — isolated Azure CLI profile, onboarding gate, 14-day lease, renewal, and deletion.
2. **Check engine** — fixed read-only implementations returning healthy, failed, or unconnectable.
3. **Rule lifecycle** — draft, test, apply, active, export/import, and delete.
4. **Aggregation state machine** — maps rule results and active work to tray states.
5. **Windows UI** — setup wizard, animated tray icon, notifications, status panel, and rule manager.

Rule definitions are data, not executable extensions. New checker types must be implemented and reviewed in source with an explicit schema and tests. This prevents shared rule packs from becoming a remote-code-execution mechanism.

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
- Add a signed installer with explicit retain/remove-data uninstall choices.
- Add fixed, reviewed Azure checker types.
- Add code signing and a controlled release pipeline.
- Evaluate a PowerToys Command Palette Dock view using the same engine.
