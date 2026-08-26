# Changelog

This project uses [Semantic Versioning](https://semver.org/). Changes are grouped by release so each GitHub tag has a short, human-readable scope.

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
