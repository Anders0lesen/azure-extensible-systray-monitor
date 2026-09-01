# Credential security model

## Goal

Azure Health Beacon is self-contained. It does not execute Azure CLI, read `%USERPROFILE%\.azure`, inherit a developer login, or require Azure CLI to be installed.

Microsoft's browser handles the account, password, Windows Hello, Conditional Access, and MFA. The Beacon receives the resulting OAuth authorization in process memory and persists the reusable session only through Microsoft's MSAL Extensions encrypted persistence.

## Identity type

This is delegated user OAuth for a Windows desktop application, not Azure Managed Identity. Managed Identity is for workloads hosted on supported Azure resources and is not available to an ordinary Windows 11 tray process.

The app uses Microsoft Azure's public development client ID, the same default documented for Azure Identity interactive browser credentials. It is a public client and has no client secret, certificate, or private key to ship or hide. Authorization uses the browser-based authorization-code flow with PKCE.

## Data flow

```text
User
  -> Microsoft browser sign-in (password, Hello, CA, MFA remain there)
      -> MSAL authorization-code + PKCE
          -> DPAPI-encrypted app-owned token cache
              -> direct HTTPS to Azure Resource Manager / Resource Graph / Monitor
                  -> normalized status returned to the private engine
```

No token is returned through the WPF bridge or placed in a rule, configuration file, export, update request, or log.

## App-owned encrypted storage

The complete credential-bearing state lives under:

```text
%LOCALAPPDATA%\AzureHealthBeacon\identity\
  token-cache.bin
  account-state.bin
```

Both files are encrypted with Windows DPAPI for the current Windows user through [Microsoft Authentication Extensions for Python](https://github.com/AzureAD/microsoft-authentication-extensions-for-python). There is deliberately no plaintext fallback. If DPAPI encryption is unavailable, login fails closed.

`token-cache.bin` contains MSAL's encrypted token cache. `account-state.bin` contains the encrypted home-account identifier, username, and home tenant needed to select the correct cached account. The non-secret selected tenant/subscription binding and 14-day timestamp remain in `checks.json`.

DPAPI CurrentUser encryption protects data at rest from other Windows accounts and offline copying. It does not protect against malicious code already executing as the same Windows user; that process could call DPAPI or inspect this process. That is the honest Windows security boundary.

## Token handling

Access tokens must briefly exist in private engine memory to authorize HTTPS requests. They are:

- requested silently from the encrypted cache during monitoring;
- placed only in the HTTPS `Authorization` header;
- never printed, logged, exported, or sent through anonymous IPC;
- removed from temporary header/token variables immediately after each request;
- never accepted from rule fields or configuration.

Azure responses and exceptions are reduced and redacted before crossing into the WPF shell.

## Fourteen-day lease

The connection-establishment time is UTC metadata. Its maximum age is a source-code constant of exactly 14 days and has no setting.

At expiry:

1. monitoring is blocked;
2. connection metadata is cleared;
3. the complete app-owned `identity` directory is recursively deleted;
4. any legacy v0.7.0 `azure-cli` profile is deleted;
5. setup and fresh Microsoft sign-in are required.

Deletion is fail-closed. A purge-pending marker is saved before removal; failure leaves monitoring and login blocked until deletion succeeds. Rules are retained.

## Stored-data summary

| Data | Stored? | Protection/location |
|---|---:|---|
| Password, Windows Hello, MFA response | No | Microsoft sign-in only |
| Client secret, certificate, private key | No | Unsupported |
| OAuth access/refresh-token cache | Up to 14 days | DPAPI-encrypted `identity/token-cache.bin` |
| Account selector metadata | Up to 14 days | DPAPI-encrypted `identity/account-state.bin` |
| Tenant/subscription selection | Yes | Non-secret metadata in `checks.json` |
| Resource IDs, KQL, thresholds | Yes | Rule definitions and rule-pack exports |
| Successful Azure response bodies | No | Reduced to in-memory results |

Identifiers and KQL are not authentication secrets, but they can reveal internal infrastructure names and must still be shared carefully.

## Upgrade from v0.7.0

v0.7.0 used a Beacon-isolated Azure CLI profile. v0.7.1 does not import or reuse that authorization state. On first v0.7.1 start it deletes the legacy profile, retains rules and settings, and requires one fresh Microsoft sign-in to establish the new DPAPI-encrypted cache.

## References

- [MSAL Python token-cache serialization](https://learn.microsoft.com/entra/msal/python/advanced/msal-python-token-cache-serialization)
- [Microsoft Authentication Extensions encrypted persistence](https://github.com/AzureAD/microsoft-authentication-extensions-for-python)
- [Azure Identity token caching](https://learn.microsoft.com/azure/developer/python/sdk/authentication/additional-configurations#persist-the-token-cache)
