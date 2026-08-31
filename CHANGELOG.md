# Changelog

This project uses [Semantic Versioning](https://semver.org/). Changes are grouped by release so each GitHub tag has a short, human-readable scope.

## [0.7.0] - 2026-08-31

### Added

- A modern, self-contained Windows 11 shell with dark-first navigation, light mode, Overview, Checks, Activity, Settings, and About pages.
- A deliberate three-stage check workflow: choose one of six unselected signal sources, configure only relevant fields, then live-test before saving.
- Source-specific Azure resource, workspace, and metric discovery directly inside rule configuration.
- A proper KQL editing surface for Resource Graph and Logs/Application Insights checks.
- Accessible navigation names and a Windows UI regression that verifies first use, all six sources, no preselection, and the test-before-save controls.

### Changed

- The tested Python Azure/rule engine now runs as a private child process behind a fixed data-only command boundary; no credentials or tokens cross into UI state.
- The installer ships the modern shell and private engine separately while preserving schema-6 configuration, schema-4 rule packs, the install location, and in-place updates.
- Tabler SVG paths are compiled into the shell and require no runtime web access. Operational tray icons remain status-only.

### Fixed

- Update checks in notify/automatic mode are limited to once per day and the successful manual state reads “✅ Full up-to-date - No new updates available”.
- Tray animation now releases native icon handles instead of leaking them during long-running sessions.

## [0.6.0] - 2026-08-31

### Added

- Guided VM power-state rules using the VM's live Azure instance view.
- Generic Azure Resource Manager property rules with explicit property paths, comparisons, and healthy values.
- Direct resource selection from Signal Explorer for provisioning, VM, and generic property rules.

### Changed

- Renamed the old misleading **Resource property** source to **Provisioning state**.
- Rule-pack schema 4 carries the new credential-free native rule fields; older packs remain importable and imports remain disabled pending review.
- Generic property checks ask Azure for only the selected value, parse it locally, and never execute rule text in a shell.

### Fixed

- In-place updates now reset inherited PyInstaller one-file state before restart, preventing the temporary `python312.dll` startup failure.
- The release workflow now reproduces the stale `_MEI` update environment and fails the release if the restarted app does not survive.

## [0.5.0] - 2026-08-27

### Added

- Full Azure Monitor Logs and workspace-based Application Insights KQL rules.
- Azure Monitor metric rules with live metric-definition discovery, aggregation, reducer, dimension filter, comparison, threshold, and lookback controls.
- Signal Explorer for permission-scoped workspace/table and resource/metric discovery.
- Transparent starter queries for Application Insights session errors, failed requests, AVD connection failures, Function failures, and container restarts.
- Dark-first Rule Studio, Windows dark title bars, persistent light-mode toggle, and GitHub-linked About window.

### Changed

- Rule names and every condition are explicitly editable and require a fresh live test before saving.
- Rule-pack schema 3 carries credential-free log and metric rules; schemas 1 and 2 remain importable.
- Log result collection is server-capped and never persisted or exported.
- Missing access, query errors, and absent metric samples remain grey; only confirmed findings are red.

## [0.4.0] - 2026-08-27

### Added

- Resource Graph/KQL findings rules across every enabled subscription visible to the signed-in account.
- Reviewed templates for fired Azure Monitor alerts, Resource Health problems, active Service Health incidents, and Azure Policy non-compliance.
- Native KQL editor with live test-before-apply behaviour and compact finding previews.
- Explicit opt-in settings for starting with Windows and starting minimized.

### Changed

- Manual, user-approved updates now install silently in place and restart the Beacon.
- A successful update check is reported inline as “Fully up to date” instead of opening another dialog.
- Rule-pack schema 2 carries credential-free KQL rules; schema 1 packs remain importable and all imports remain disabled pending review.
- Red remains reserved for confirmed Azure findings; query, access, tenant, timeout, and partial-scope uncertainty stays grey.

## [0.3.0] - 2026-08-26

### Added

- Per-user Windows 11 installer with Start Menu and Installed apps registration.
- In-app manual update checks plus notify-only and automatic modes.
- Explicit confirmation before automatic installation can be enabled.
- Pinned GitHub release metadata, strict asset URLs, dual SHA-256 verification, size limits, and atomic downloads.
- Automated installer release workflow and GitHub build-provenance attestation.
- Customer-facing brand artwork across the executable, app windows, installer, Windows shortcuts, Installed apps, and README while preserving status-only tray icons.

### Changed

- Existing and new configurations use manual-only updates unless the user opts in.
- Release tags no longer duplicate the normal `main` test workflow.

## [0.2.0] - 2026-08-26

First public preview.

### Added

- Windows 11 tray beacon with healthy, unconnectable, connecting, failed, and actively-checking states.
- First-run Microsoft interactive sign-in, subscription selection, and read-only access validation.
- App-isolated Azure CLI profile with a fixed 14-day authorization lifetime and fail-closed deletion.
- Test-before-apply Azure resource provisioning rules.
- Strict data-only rule-pack import and export.
- Security, credential, lifecycle, first-run, and architecture documentation.
- Automated linting, unit tests, and dependency auditing.

[0.2.0]: https://github.com/Anders0lesen/azure-extensible-systray-monitor/releases/tag/v0.2.0
[0.3.0]: https://github.com/Anders0lesen/azure-extensible-systray-monitor/releases/tag/v0.3.0
[0.4.0]: https://github.com/Anders0lesen/azure-extensible-systray-monitor/releases/tag/v0.4.0
[0.5.0]: https://github.com/Anders0lesen/azure-extensible-systray-monitor/releases/tag/v0.5.0
[0.6.0]: https://github.com/Anders0lesen/azure-extensible-systray-monitor/releases/tag/v0.6.0
[0.7.0]: https://github.com/Anders0lesen/azure-extensible-systray-monitor/releases/tag/v0.7.0
