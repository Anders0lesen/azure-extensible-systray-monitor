# First-run setup

Azure Health Beacon blocks monitoring until authentication and Azure access have been validated.

## 1. Setup required

On a new profile, the wizard opens automatically and the tray icon is grey. Closing the wizard leaves the Beacon uninitialized; reopen it from **Azure connection setup** in the tray menu. No scheduled checks run.

## 2. Sign in with Microsoft

Select **Sign in with Microsoft**. The Beacon starts Azure CLI's interactive login in an app-isolated profile without passing a username or password. Microsoft handles the account, password, Windows Hello, Conditional Access, and MFA.

An optional tenant ID can be supplied for multi-tenant accounts. Login uses `--output none`; the Beacon does not request token output.

## 3. Choose a subscription

After login, the Beacon reads enabled subscription names, IDs, and tenant IDs from its isolated Azure CLI profile. It does not run `az account set`.

## 4. Verify live access

Select **Test credentials and finish setup**. The Beacon performs a read-only request scoped to the selected subscription and returns only its resource-group count:

```text
az group list --subscription <subscription-id> --query length(@) --output tsv
```

If the identity has narrower resource-level access but cannot list resource groups, setup currently fails. This is a known prototype limitation.

## 5. Discover, test, and apply the first rule

Select **Add a check**. The source page shows all six signal surfaces with nothing preselected. Choose one, use its resource/workspace/metric discovery controls where relevant, define what should return a finding, choose **Test without saving**, inspect the live result, and then select **Save and enable**. Editing or renaming any field invalidates the test and requires another test before saving.

Discovery reads metadata only and does not save it. KQL result rows remain in memory for the current evaluation and never enter the configuration or a rule-pack export.

## Renewal

After 14 days, the isolated Azure CLI profile and scope binding are deleted automatically. Rules remain, the tray turns grey, and the full sign-in/validation sequence is required again.
