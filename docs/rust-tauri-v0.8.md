# Rust/Tauri v0.8 preview

Version 0.8 is a side-by-side implementation test. The Python/WPF application remains in the repository until the Rust application reaches feature and security parity.

## Non-negotiable contracts

- Red means a confirmed Azure finding; authentication, authorization, network, timeout, and incomplete-scope results remain grey.
- Rules remain strict, data-only JSON. No rule may execute a local command, script, dynamic library, user-selected endpoint, or downloaded plug-in.
- The frontend never receives an OAuth access token, refresh token, authorization code, PKCE verifier, or DPAPI plaintext.
- Interactive sign-in uses the system browser, authorization code flow, PKCE S256, a random loopback port, a random state value, and fixed Microsoft HTTPS endpoints.
- The refresh authorization is stored only as Windows DPAPI CurrentUser ciphertext. There is no plaintext fallback.
- The complete v0.8 identity directory is hard-deleted after 14 days. The duration is not configurable.
- Existing rule packs remain importable and exports remain credential-free.
- Settings, About, version, diagnostics, and update recovery remain available without Azure credentials.
- Start with Windows, start minimized, notification updates, and automatic updates remain explicitly opt-in.
- A release is blocked if Microsoft Defender detects the final installer on the Windows build runner.

## Boundary

```text
Tauri webview (presentation only)
        |
        | typed commands and redacted results
        v
Rust application core
  |-- DPAPI-backed OAuth/PKCE identity
  |-- fixed-host Azure HTTPS adapters
  |-- rule validation and evaluation
  |-- scheduler and tray state machine
  `-- signed-update verification
```

The WebView cannot make arbitrary network requests. Azure and GitHub traffic originates only from reviewed Rust commands using fixed HTTPS hosts.

## Migration

The v0.8 preview reads the existing `checks.json` configuration and rule schema. It does not attempt to parse or migrate the Python MSAL token cache. A fresh Microsoft sign-in creates a separate v0.8 DPAPI cache; the legacy cache is removed only after the new connection succeeds.
