# Application lifecycle

## State model

| Lifecycle state | Allowed actions | Exit condition |
|---|---|---|
| Never initialized | Sign in, exit | Microsoft login completes |
| Signed in, unvalidated | Select subscription, validate, delete connection | Live read-only validation succeeds |
| Connected, no rules | Add/import rule, export, delete connection | A tested rule is applied |
| Connected, rules active | Scheduled/manual checks, add/edit/delete/import/export rules | Connection deleted/expires or app exits |
| Connection expired | Sign in again, manage retained rules after validation, exit | New validation succeeds |
| Unconnectable | Retry, renew connection, inspect error | A read succeeds or connection is deleted |

## Update lifecycle

```text
manual by default
  -> user may opt into notify-only or automatic installation
      -> read stable release metadata from the pinned GitHub repository
          -> require exact installer/checksum assets
              -> download to app-owned local update storage
                  -> verify GitHub digest and release checksum
                      -> launch per-user installer silently after approval
                          -> close and restart Beacon
```

Automatic mode is never inferred from a previous install or Azure setup choice. Existing configurations migrate to manual mode. Failed metadata, download, redirect, size, or checksum validation leaves the current version running.

## Rule lifecycle

```text
unsaved form
  -> test current values
      -> reachable result (healthy or failed)
          -> apply tested rule
              -> first background evaluation
                  -> active rule
```

An unconnectable test cannot be applied. Any edit after a test invalidates that test. Imported rules enter disabled and must be reviewed and tested.

## Deletion behavior

### Delete rule

Removes only the selected definition after confirmation. It does not change authentication or other rules.

### Delete Azure connection

Deletes `%LOCALAPPDATA%\AzureHealthBeacon\azure-cli`, clears saved scope/timestamp, stops monitoring, and retains rules. It never changes `%USERPROFILE%\.azure` or the Windows account.

The connection is blocked before deletion begins. If a file lock prevents immediate deletion, the app records a purge-pending state and refuses login/monitoring until deletion succeeds.

### Remove application

The installer does not add services, drivers, or scheduled tasks. The app can add one current-user Windows startup value only after explicit opt-in. To remove it:

1. Exit from the tray menu.
2. Open **Settings → Apps → Installed apps**.
3. Uninstall **Azure Health Beacon**.
4. Delete `%LOCALAPPDATA%\AzureHealthBeacon` only if rules, logs, configuration, downloaded updates, and the isolated Azure CLI profile should also be removed.

Uninstall removes the app's Windows startup value but leaves local data behind by design, allowing a later reinstall to retain rules. Use **Delete Azure connection** before uninstall when the app-owned Azure CLI profile must be purged without removing retained rules.
