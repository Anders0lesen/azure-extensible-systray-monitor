# Azure Extensible Systray Monitor

<img src="assets/AzureHealthBeacon-Brand.png" alt="Azure Health Beacon" width="640">

Current version: **0.3.0 public preview** — see the [changelog](CHANGELOG.md).

> **Prototype:** Windows 11 only. The initial implementation is called **Azure Health Beacon** and monitors Azure resource provisioning states.

Azure Health Beacon is a small system-tray monitor that answers one question at a glance: **is anything in Azure broken that I need to care about right now?**

It checks configured resources every five minutes, displays a persistent visual state, and opens a concise incident view when clicked.

## Lifecycle first

The application is designed around the complete operating lifecycle, not only the steady-state monitor:

| Phase | Required behavior |
|---|---|
| First-ever use | Start grey, open setup automatically, and block monitoring |
| Add Azure connection | Open Microsoft's interactive login in an app-isolated Azure CLI profile |
| Test connection | Select a subscription and complete a live, read-only Azure request |
| Add rule | Paste a Portal URL or resource ID into an unsaved form |
| Test rule | Resolve the resource and show its actual provisioning state without saving |
| Apply rule | Enabled only after the current rule contents have produced a reachable result |
| Rules exist | Check every five minutes; aggregate results into one tray state |
| Connection reaches 14 days | Stop monitoring, hard-delete the app-owned Azure CLI profile, and require setup again |
| Export rules | Produce a credential-free, data-only team rule pack |
| Import rules | Strictly validate and import disabled pending review and testing |
| Delete rule | Confirm and remove only the selected rule |
| Delete Azure connection | Delete the isolated Azure CLI profile and scope binding; retain rules |
| Update the app | Manual by default; notification or automatic installation requires explicit opt-in |
| Remove the app | Use Windows Installed apps; local rules and credentials are retained unless explicitly deleted |

See [Lifecycle](docs/lifecycle.md) and [First-run setup](docs/first-run.md).

## Credential security

Azure Health Beacon has no username, password, token, client-secret, certificate, or connection-string field. Microsoft's UI and Azure CLI/Windows Web Account Manager handle interactive authentication. The Beacon never calls `az account get-access-token` and stores only non-secret tenant/subscription identifiers plus a connection-establishment timestamp.

Every Azure CLI process receives an app-specific `AZURE_CONFIG_DIR` under `%LOCALAPPDATA%\AzureHealthBeacon\azure-cli`. The user's normal `%USERPROFILE%\.azure` profile is never read, changed, logged out, or deleted by the Beacon.

The authorization lease is exactly 14 days and is not configurable. At expiry, monitoring stops and the entire app-owned Azure CLI directory is deleted. WAM may still know the Windows work account for system SSO; the Beacon does not and must not remove a Windows/IBM account.

Read [Credential security](docs/credential-security.md) and [Security policy](SECURITY.md) before using the prototype with a work account. Microsoft documents WAM as Azure CLI's default authentication broker on current Windows versions: [Sign in with Azure CLI](https://learn.microsoft.com/cli/azure/authenticate-azure-cli-interactively).

## Install

Download the installer from the [latest GitHub release](https://github.com/Anders0lesen/azure-extensible-systray-monitor/releases/latest) and run `AzureHealthBeacon-Setup-v0.3.0.exe`. It installs for the current Windows user, adds a Start Menu entry, and does not request administrator access.

The public-preview installer is not Authenticode code-signed yet, so Windows may show **Unknown publisher**. The release includes a SHA-256 checksum and GitHub build-provenance attestation. Do not install a copy obtained from anywhere except this repository.

## First use

1. Start `AzureHealthBeacon.exe`.
2. Select **Sign in with Microsoft** in the automatically opened setup wizard.
3. Complete Microsoft's account, Conditional Access, and MFA prompts.
4. Select the intended subscription.
5. Select **Verify Azure access**.
6. Continue to checks only after validation succeeds.
7. Paste a Portal resource URL, select **Test without saving**, then **Apply tested rule**.

Until setup succeeds, the tray remains grey and monitoring/check-management commands are disabled.

## Tray states

| State | Icon | Meaning |
|---|---|---|
| Healthy | Solid green ball | Every enabled rule returned its expected value |
| Unconnectable | Grey crosshatched circle | Setup is incomplete, authorization expired, authentication failed, or Azure is unreachable |
| Connecting | Spinning circular arrow | Establishing or restoring the Azure connection |
| Failed | Solid red ball | Azure returned a value that violates a configured rule |
| Checking | Pulsing amber ball | A scheduled/manual check is running unless a confirmed red condition already exists |

Red is reserved for a confirmed Azure problem. Authentication and network failures are grey.

The application artwork is used for Windows branding only. The tray deliberately continues to use the five operational state icons above; it is a monitoring surface, not a branding surface.

## Current rule type

Version `0.3.0` supports one strict, data-only rule:

- Fetch an Azure resource's `properties.provisioningState`.
- Treat `Succeeded` as healthy by default.
- Treat any returned value outside the rule's configured healthy values as red.
- Treat login, authorization, connectivity, and timeout errors as grey.

The Azure CLI query returns only provisioning-state text, not the complete resource document.

## Rule packs

Exports contain resource metadata and expected states but no authentication material. Imports reject unknown fields, scripts, commands, non-Azure links, excessive size/count, duplicate IDs, credential-like fields, and common secret formats. Imported rules are disabled until reviewed, tested, and applied.

Resource names, subscription IDs, and tenant IDs can still be sensitive internal metadata. Use an approved team channel.

## Updates

Open **Updates** from the status window or **Update settings** from the tray menu.

| Mode | Network behavior | Installation behavior |
|---|---|---|
| Manual only | No background update requests | You select **Check now** and approve installation |
| Notify me | Checks GitHub once per day | Notification only; you approve installation |
| Install automatically | Checks GitHub once per day | Downloads, verifies, installs silently, and restarts the Beacon |

Manual is the default for new and upgraded configurations. Enabling automatic installation requires a separate confirmation and can be disabled at any time.

Updates are pinned to this exact repository and exact asset name. The app requires GitHub's release-asset SHA-256 digest and the separately published checksum to match the downloaded installer before it runs. Update requests contain the app version and standard HTTP metadata only; Azure credentials, tenant/subscription bindings, and rules are not sent.

## Development build

Requirements:

- Windows 11
- Python 3.12
- Machine-wide [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli-windows)

```powershell
./build.ps1
```

The script creates `.venv`, installs pinned dependencies, runs tests, and builds `dist/AzureHealthBeacon.exe` without a console window.

With Inno Setup 7 installed, build the per-user installer as well:

```powershell
./build.ps1 -Installer
```

## Prototype limitations

- The executable and installer are not Authenticode code-signed yet; GitHub provenance and checksums are provided, but Windows publisher verification requires a future signing certificate.
- Windows startup is not configured automatically.
- First-use validation lists only the number of resource groups and therefore requires that Azure permission.
- The application can delete everything it owns, but it cannot securely erase an account retained by Windows WAM without modifying system-wide account state.
- A compromised Windows user session can act with that user's permissions; no desktop app can make that scenario impossible.

See [Architecture](docs/architecture.md) for extension boundaries and roadmap.
