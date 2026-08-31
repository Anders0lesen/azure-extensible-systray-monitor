# UI redesign plan

## Recommendation

Use **WinUI 3 for the Windows 11 shell** and retain Python for the tested Azure/rule engine. Microsoft identifies WinUI 3 as its recommended native framework for new Windows desktop applications. It provides the strongest fit for Windows windowing, accessibility, high-DPI rendering, notifications, startup integration, and the notification area.

Flutter remains the fallback if the spike reveals a material WinUI limitation or if cross-platform UI becomes an active requirement again. The framework choice is not allowed to change rule-pack compatibility, credential isolation, the 14-day deletion boundary, or tray-state semantics.

## Product structure

The redesigned application uses one main window with five destinations:

1. **Overview** — current Beacon state and checks needing attention.
2. **Checks** — searchable list, create, edit, rename, test, enable, disable, import, export, and delete.
3. **Activity** — recent in-memory check outcomes and timestamps; no secret values or retained Azure response bodies.
4. **Settings** — Windows startup, minimized startup, theme, interval, and opt-in update policy.
5. **About** — version, GitHub repository, licenses, and update status.

The tray icon remains an operational communication surface and continues to use only the established green, grey, connecting, red, and amber states. Product branding and Tabler navigation icons do not replace it.

## New-check flow

Creating a check is a deliberate three-stage workflow:

### 1. Signal source

- Open a full **Add a check** page.
- Show all available signal sources immediately as equally weighted cards.
- Preselect nothing.
- Keep **Continue** disabled until the user explicitly chooses one source.
- Label each source as **Guided** or **Advanced** and explain it in one sentence.
- Initial sources: Provisioning state, VM power state, Resource property, Resource Graph, Logs/Application Insights, and Azure Monitor metric.

### 2. Configure

- Show only fields relevant to the selected source.
- Use live Azure discovery instead of requiring pasted IDs wherever permissions allow it.
- Keep the rule name editable.
- Present the condition as a readable sentence before exposing advanced fields.
- Put KQL in a proper editor only for Resource Graph or Logs/Application Insights.
- Preserve an explicit route back to signal-source selection without losing the draft.

### 3. Test and enable

- Run the exact unsaved draft.
- Show **Healthy**, **Confirmed finding**, or **Could not determine** with the same green/red/grey semantics as the tray.
- Explain the observed value and the expected condition.
- Keep **Save and enable** disabled until a reachable live test completes.
- Never turn authentication, access, timeout, or missing-telemetry uncertainty red.

## Framework boundary

The migration must not begin as a rewrite of working Azure behavior.

```text
WinUI 3 shell
  -> narrow local application API
      -> existing Python credential, rule, discovery, and check services
          -> app-isolated Azure CLI profile
```

- The UI sends typed operations such as `list_rules`, `test_rule`, and `check_now`; it never sends executable command lines.
- The Python service owns configuration validation, Azure calls, redaction, credential expiry, and update verification.
- Local communication must use a current-user-only named pipe or equivalent authenticated local transport.
- Tokens, passwords, Azure CLI cache files, and raw credential objects never cross the UI boundary.
- Existing schema-6 configuration and schema-4 rule packs remain compatible.
- The Tk interface stays buildable until the WinUI replacement passes feature parity and updater rollback testing.

## Tabler icon policy

- Bundle the exact SVG files in `assets/icons/tabler`; never download icons at runtime.
- Pin the source release and keep the upstream MIT license beside the assets.
- Use outline icons on the 24 x 24 grid with a 2 px stroke and theme-driven foreground colour.
- Treat icons as supporting labels, not replacements for text.
- Keep operational tray-state graphics separate from Tabler.
- Add icons to the bundle only when a product surface actually uses them.

The initial curated set and product mapping are documented in `assets/icons/tabler/README.md`.

## Delivery slices

1. **Windows integration spike** — prove WinUI windowing, tray animation, single instance, notifications, startup, dark/light mode, SVG rendering, and updater handoff in an isolated prototype.
2. **Core extraction** — separate Tk concerns from the existing Python services without changing behaviour or configuration.
3. **Shell and onboarding** — implement navigation, first use, sign-in, credential testing, and credential deletion.
4. **Checks experience** — implement the no-preselection source chooser, per-source configuration, live testing, editing, renaming, and rule-pack lifecycle.
5. **Operational surfaces** — implement overview, failed-item details, activity, settings, update status, About, and GitHub link.
6. **Parity and migration** — run both UIs against the same fixtures, validate an upgrade from 0.6.0, exercise updater rollback, and remove Tk only after the new shell passes.

## Release gates

- No credential or token is added to UI state, logs, crash reports, rule packs, or IPC payloads.
- Existing rules load unchanged and are not silently enabled, disabled, renamed, or rewritten.
- Every create/edit path still requires a live test before saving.
- Keyboard navigation, focus order, screen-reader names, scaling, reduced motion, and light/dark contrast are verified.
- Tray state and update behaviour pass the existing Windows regression suite.
- The shipped package contains every icon and UI asset required for offline operation.
