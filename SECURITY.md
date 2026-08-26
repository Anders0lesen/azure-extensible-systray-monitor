# Security policy

Security is a primary design constraint because Azure Health Beacon initiates authenticated Azure operations from a long-running desktop process.

## Prototype status

No version should be treated as production-approved or enterprise-supported until it has been reviewed, scanned, code-signed, and released through an approved pipeline.

## Reporting a vulnerability

Do not open a public issue containing credentials, tokens, tenant details, subscription IDs, resource IDs, internal hostnames, or other confidential material.

Use GitHub private vulnerability reporting/security advisories. If unavailable, contact the repository owner through an established private channel and provide only the minimum necessary evidence.

## Non-negotiable rules

- Never add a username/password form to the Beacon.
- Never request, print, persist, export, or log access or refresh tokens.
- Never support client secrets, storage keys, SAS tokens, PATs, connection strings, or private keys in rules.
- Never run imported commands, scripts, query expressions, or arbitrary URLs.
- Never run `az account set` or alter the user's global Azure CLI context.
- Never read, clear, log out, or delete `%USERPROFILE%\.azure`.
- Never make the 14-day authorization lease configurable.
- Never enable background update checks or automatic installation by default.
- Never accept update metadata or binaries from a repository, host, tag, or asset name outside the pinned release format.
- Never publish real configuration, logs, rule packs, or build artifacts without inspection.

## Authentication boundary

The Beacon launches Microsoft Azure CLI's interactive login using an isolated `AZURE_CONFIG_DIR` under its own local app-data directory. Microsoft sign-in/WAM handles passwords, Windows Hello, Conditional Access, and MFA. The Beacon receives success/failure and non-secret subscription metadata, not the password or MFA response.

For an MSI Azure CLI installation, the Beacon invokes the Azure CLI Python module directly from `Program Files`. Imported rule values never pass through `cmd.exe`.

## Fourteen-day hard boundary

The Beacon records when its Azure connection was established. At exactly 14 days or later:

1. Monitoring is blocked.
2. In-memory results are cleared.
3. The complete app-owned `azure-cli` directory is recursively deleted.
4. Tenant/subscription bindings and the timestamp are cleared.
5. The tray becomes grey and setup is required again.

Deletion is fail-closed: the connection is marked purge-pending and monitoring is blocked before deletion starts. If Windows temporarily prevents removal because a file is busy, setup remains blocked and retries deletion before allowing another login.

Rules are retained. The duration is a constant in source and has no configuration field.

WAM is a Windows authentication broker and can independently retain the Windows account for SSO. Removing that account would affect Windows and other managed applications, so the Beacon intentionally does not attempt it. “Delete Azure connection” means delete all Beacon-owned authorization state, not remove a corporate identity from Windows.

## Stored local data

`%LOCALAPPDATA%\AzureHealthBeacon\checks.json` contains non-secret scope metadata, the connection timestamp, rules, and scheduling settings. The isolated Azure CLI profile lives below the same parent directory. Writes are validated and atomic, with one configuration backup.

Logs rotate and do not contain successful Azure response bodies. Azure CLI errors are truncated and scrubbed for bearer-token, JWT, SAS-signature, and access-token patterns.

## Least-data operations

- Setup validation runs a read-only resource-group list scoped to the selected subscription and returns only the count.
- Provisioning checks request only `properties.provisioningState` as text.
- Every lookup includes its subscription ID.
- Optional tenant pins are verified before lookup.

## Update boundary

Update mode defaults to `manual`. Manual mode performs no background update requests. `notify` and `automatic` modes are persisted only after the user selects them; entering automatic mode requires an additional confirmation.

The updater reads only the stable `latest` release from `Anders0lesen/azure-extensible-systray-monitor`. It rejects drafts, prereleases, non-semantic tags, unexpected release URLs, missing assets, redirects outside GitHub's release-asset hosts, oversized responses, and unexpected filenames.

Before execution, the downloaded installer must match both GitHub's API-provided SHA-256 asset digest and the release's checksum file. Release builds also receive a GitHub artifact-provenance attestation. These controls do not replace Authenticode publisher signing; the current public preview remains unsigned and should not be enterprise-deployed until signing is added.

No Azure credential, rule, tenant ID, subscription ID, or isolated Azure CLI data is included in update requests.

## Rule-pack boundary

Rule packs are strict data-only JSON. Imports reject unknown fields, arbitrary commands, scripts, non-Azure links, credential-like keys, common secret formats, oversized files, excessive rule counts, and duplicate IDs. Imported rules are disabled until reviewed, tested, and explicitly applied.

For details, see [Credential security](docs/credential-security.md).
