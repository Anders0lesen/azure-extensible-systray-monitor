# Changelog

This project uses [Semantic Versioning](https://semver.org/). Changes are grouped by release so each GitHub tag has a short, human-readable scope.

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
