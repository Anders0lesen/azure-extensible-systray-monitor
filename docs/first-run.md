# First-run setup

Azure Health Beacon blocks monitoring until Microsoft sign-in and live Azure access have been validated.

## 1. Setup required

On a new profile, the wizard opens automatically and the tray is grey. Closing it leaves the Beacon uninitialized. No scheduled checks run.

When upgrading from v0.7.0, existing rules and settings are retained, but the legacy Azure CLI authorization is deliberately not imported. One fresh sign-in creates the v0.7.1 encrypted identity cache.

## 2. Sign in with Microsoft

Select **Sign in with Microsoft**. The Beacon starts Microsoft's browser-based OAuth authorization-code flow with PKCE. Microsoft's browser handles the account and whichever method Entra requires: passkey, security key, Authenticator, Windows Hello, password, Conditional Access, or another tenant-approved method.

The Beacon does not inspect the authentication method, execute Azure CLI, or offer username, password, passkey, client-secret, or token fields. An optional tenant ID can narrow the initial sign-in for multi-tenant accounts.

After the OAuth callback, the Beacon selects the signed-in account through MSAL's encrypted token cache. It does not assume the interactive result contains a separate account record.

After success, MSAL persists the reusable authorization only as Windows DPAPI CurrentUser ciphertext in the app-owned `identity` directory. If encrypted persistence is unavailable, setup fails rather than writing plaintext.

## 3. Choose a subscription

The Beacon uses direct Azure Resource Manager HTTPS requests to enumerate enabled subscriptions across tenants available to the signed-in account. It does not read or change a global developer-tool context.

## 4. Verify live access

Select **Test credentials and finish setup**. The Beacon performs a live, read-only resource-group request scoped to the selected subscription. Only the resulting count is shown.

If the identity has narrow resource-only RBAC and cannot list resource groups, setup currently fails. Supporting alternate validation targets remains a known prototype limitation.

## 5. Discover, test, and apply the first rule

Select **Add a check**. All six signal sources appear with nothing preselected. Choose a source, use the relevant Azure discovery picker, define the finding, select **Test without saving**, inspect the live result, and then select **Save and enable**.

Editing or renaming after a test invalidates the test receipt. Discovery metadata and compact query findings remain in memory and never enter the encrypted identity cache.

## Renewal

After 14 days, the complete app-owned encrypted identity cache and scope binding are deleted automatically. Rules remain, the tray becomes grey, and full sign-in/validation is required again.
