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

The prototype does not install services, drivers, scheduled tasks, or startup entries. To remove it:

1. Exit from the tray menu.
2. Delete `AzureHealthBeacon.exe`.
3. Delete `%LOCALAPPDATA%\AzureHealthBeacon` if rules, logs, configuration, and the isolated Azure CLI profile should also be removed.

Deleting the executable alone leaves local data behind by design, allowing a later reinstall to retain rules. A future signed installer should offer explicit **keep data** and **remove all local data** choices.
