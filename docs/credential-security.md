# Credential security model

## Goal

The safest application-owned credential database is no credential database. Azure Health Beacon delegates interactive OAuth and token maintenance to Microsoft Azure CLI/WAM and never stores a username, password, MFA response, client secret, certificate, access token, or refresh token in its own configuration.

## Data flow

```text
User
  -> Microsoft sign-in / Windows Web Account Manager
      -> app-isolated Azure CLI profile
          -> read-only Azure Resource Manager command
              -> non-secret status returned to Azure Health Beacon
```

The password and MFA response do not pass through Beacon fields, arguments, configuration, rule packs, or logs.

## Isolation

Every Azure CLI child process receives:

```text
AZURE_CONFIG_DIR=%LOCALAPPDATA%\AzureHealthBeacon\azure-cli
```

The normal `%USERPROFILE%\.azure` profile remains outside the Beacon's scope. Deleting or expiring the Beacon connection removes only the isolated directory.

## Fourteen-day lease

The connection-establishment time is stored as UTC metadata. The maximum age is a source-code constant of exactly 14 days. It is not represented as a user/configuration option.

Expiry deletes the isolated directory and all connection metadata before any further check can run. Reauthentication creates a new 14-day lease.

The purge is fail-closed. A purge-pending flag is persisted before deletion; failure leaves monitoring and new login blocked until the old isolated directory is successfully removed.

## What is stored

| Data | Stored? | Location/reason |
|---|---:|---|
| Username/password | No | Microsoft sign-in only |
| MFA/Windows Hello response | No | Microsoft sign-in only |
| Client secret/private key | No | Unsupported |
| Azure CLI authorization cache | Temporarily | Isolated profile, hard-deleted after at most 14 days |
| Tenant/subscription ID and name | Yes | Non-secret scope binding in `checks.json` |
| Resource IDs and names | Yes | Rule definitions |
| Connection timestamp | Yes | Enforces the hard lease |

Identifiers are not authentication secrets, but they can expose internal structure.

## Why not Windows Credential Manager or a custom DPAPI vault?

The Beacon does not need to invent a second credential format or token-handling implementation. Azure CLI on Windows already integrates with Microsoft's broker and encrypted Windows token-cache mechanisms. An additional app-owned secret vault would add parsing, logging, backup, migration, and deletion risks.

## Honest WAM limit

Windows WAM controls its own account knowledge and SSO behavior. Deleting the Beacon profile guarantees that the Beacon retains no local Azure CLI profile and refuses further work until setup. It does not guarantee that Windows forgets the corporate account or asks for a password instead of Windows Hello/SSO next time.

Removing the Windows work account is deliberately out of scope because it affects the OS and other managed applications.

## Process protections

- Machine-wide Azure CLI under `Program Files` is preferred.
- MSI Azure CLI is invoked through its Python module, not its `.cmd` wrapper.
- Arguments are a list, never a concatenated shell string.
- Rules cannot select commands, executables, or query expressions.
- Portal links must use exactly `https://portal.azure.com`.
