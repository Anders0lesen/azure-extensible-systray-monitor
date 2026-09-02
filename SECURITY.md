# Security policy

Security is a primary design constraint because Azure Health Beacon performs authenticated Azure operations from a long-running Windows process.

## Prototype status

No release is production-approved or enterprise-supported until it has been independently reviewed, scanned, Authenticode-signed, and distributed through an approved pipeline.

## Reporting a vulnerability

Do not open a public issue containing credentials, tokens, tenant details, subscription IDs, resource IDs, internal hostnames, or confidential query results. Use GitHub private vulnerability reporting/security advisories or an established private channel.

## Non-negotiable rules

- Never add a username, password, passkey, security-key, Authenticator, or MFA field.
- Never print, log, export, or return access or refresh tokens.
- Never permit plaintext credential-cache fallback.
- Never support client secrets, storage keys, SAS tokens, PATs, connection strings, or private keys in rules.
- Never execute Azure CLI, PowerShell, imported commands, scripts, or arbitrary URLs.
- Never read or mutate `%USERPROFILE%\.azure`.
- Never make the 14-day authorization lease configurable.
- Never enable background update checks or automatic updates by default.
- Never publish real configuration, logs, rule packs, or local identity files.

## Authentication boundary

Interactive authentication uses MSAL's authorization-code flow with PKCE and Microsoft's browser UI. The app is a public client and contains no client secret. Passkeys, FIDO2 security keys, Authenticator, Windows Hello, passwords, Conditional Access, and MFA stay on Microsoft's surface. After completion, the app selects the account through MSAL's encrypted cache instead of depending on authentication-method-specific result fields.

MSAL's reusable token cache and the account selector metadata are stored only under `%LOCALAPPDATA%\AzureHealthBeacon\identity`. Both are encrypted with Windows DPAPI CurrentUser through Microsoft Authentication Extensions. Construction fails if encrypted persistence is unavailable; unencrypted fallback is forbidden.

The Windows shell starts one private monitoring-engine child through redirected anonymous stdin/stdout pipes. The bridge has a fixed data-only JSON command set, no listener, no executable-command field, and no token-return operation. Tokens remain inside the engine and only briefly enter direct HTTPS `Authorization` headers.

## Fourteen-day hard boundary

At exactly 14 days or later:

1. monitoring is blocked;
2. connection metadata is cleared;
3. the app-owned encrypted `identity` directory is hard-deleted;
4. any legacy v0.7.0 `azure-cli` directory is hard-deleted;
5. the tray becomes grey and setup is required again.

Deletion is fail-closed: a purge-pending flag is stored before removal. Rules remain intact. Removing a Windows work account is out of scope because it would affect Windows and other managed applications.

## Stored local data

- `checks.json`: non-secret scope metadata, connection timestamp, rules, scheduling, theme, and update choices.
- `identity/token-cache.bin`: DPAPI-encrypted MSAL token cache.
- `identity/account-state.bin`: DPAPI-encrypted account selector metadata.
- `beacon.log`: rotating stage/category diagnostics without identity values, authentication material, successful Azure response bodies, or tokens.
- `updates`: verified installer downloads.

DPAPI protects at rest against other accounts and offline copying. It cannot protect against malicious code already executing as the same Windows user.

## Least-data operations

- Setup validates the selected subscription with a read-only resource-group request.
- Provisioning and generic property checks fetch the resource document directly from ARM, retain only the selected value, and do not persist the document.
- VM checks retain only the live `PowerState/...` value.
- Resource Graph requests enumerate explicit accessible subscription IDs and retain at most 25 compact findings in memory.
- Log Analytics/Application Insights KQL goes directly to `api.loganalytics.io`; the app adds a 26-row cap and persists no rows.
- Metric checks retain only samples needed for the configured reducer and threshold. No data is grey, not healthy.
- Discovery data remains in memory for the open window and is capped at 1,000 resources.

All network calls use fixed Microsoft HTTPS endpoints. Provider-specific resource API versions are discovered from ARM metadata; rules cannot choose an endpoint or API version.

## Update boundary

Update mode defaults to manual. Notify-only and automatic modes require explicit opt-in; automatic installation requires an additional confirmation.

Updates are pinned to the stable release in `Anders0lesen/azure-extensible-systray-monitor`. The installer must match both GitHub's asset SHA-256 digest and the separately published checksum. Release builds receive GitHub artifact-provenance attestations. These controls do not replace Authenticode publisher signing.

No Azure authorization, identity cache, tenant/subscription binding, rules, or query text is included in update requests.

## Rule-pack boundary

Rule packs are strict data-only JSON. Imports reject unknown fields, commands, scripts, non-Azure links, credential-like keys, common secret formats, excessive sizes/counts, and duplicate IDs. Imported rules are disabled until reviewed, tested live, and explicitly applied.

See [Credential security](docs/credential-security.md).
