use std::{
    io::{BufRead, BufReader, Read, Write},
    net::TcpListener,
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};

use chrono::{DateTime, Duration as ChronoDuration, Utc};
use oauth2::{
    AuthType, AuthUrl, AuthorizationCode, ClientId, CsrfToken, PkceCodeChallenge, RedirectUrl,
    RefreshToken, Scope, TokenResponse, TokenUrl, basic::BasicClient, reqwest,
};
use secrecy::SecretString;
use serde::{Deserialize, Serialize};
use tauri::AppHandle;
use tauri_plugin_opener::OpenerExt;
use url::Url;
use zeroize::Zeroize;

use crate::{
    config::app_data_dir,
    security::{DpapiStore, remove_legacy_identity_after_migration},
};

pub const AZURE_DEVELOPMENT_CLIENT_ID: &str = "04b07795-8ddb-461a-bbee-02f9e1bf7b46";
pub const ARM_SCOPE: &str = "https://management.azure.com/.default";
pub const LOG_ANALYTICS_SCOPE: &str = "https://api.loganalytics.io/.default";
const AUTHORITY_HOST: &str = "https://login.microsoftonline.com";
const IDENTITY_VERSION: u32 = 1;

#[derive(Deserialize, Serialize)]
struct PersistedIdentity {
    version: u32,
    refresh_token: String,
    established_utc: String,
}

impl Drop for PersistedIdentity {
    fn drop(&mut self) {
        self.refresh_token.zeroize();
    }
}

pub struct IdentityManager {
    gate: Mutex<()>,
    store: DpapiStore,
}

impl Default for IdentityManager {
    fn default() -> Self {
        Self {
            gate: Mutex::new(()),
            store: DpapiStore::new(app_data_dir().join("identity-v2").join("oauth.bin")),
        }
    }
}

impl IdentityManager {
    pub fn available(&self) -> bool {
        self.store.exists()
    }

    pub fn sign_in(&self, app: &AppHandle, tenant_hint: &str) -> Result<(), String> {
        let _guard = self
            .gate
            .lock()
            .map_err(|_| "The identity lock is unavailable")?;
        let listener = TcpListener::bind("127.0.0.1:0")
            .map_err(|_| "A secure loopback sign-in listener could not be created")?;
        listener
            .set_nonblocking(true)
            .map_err(|_| "The sign-in listener could not be secured")?;
        let port = listener
            .local_addr()
            .map_err(|_| "The sign-in listener has no address")?
            .port();
        let redirect = format!("http://localhost:{port}");
        let tenant = validated_tenant(tenant_hint).unwrap_or("organizations");
        let client = oauth_client(tenant, Some(&redirect))?;
        let (challenge, verifier) = PkceCodeChallenge::new_random_sha256();
        let (authorization_url, expected_state) = client
            .authorize_url(CsrfToken::new_random)
            .add_scope(Scope::new(ARM_SCOPE.to_owned()))
            .add_scope(Scope::new("offline_access".to_owned()))
            .add_scope(Scope::new("openid".to_owned()))
            .add_scope(Scope::new("profile".to_owned()))
            .add_extra_param("prompt", "select_account")
            .set_pkce_challenge(challenge)
            .url();

        app.opener()
            .open_url(authorization_url.as_str(), None::<&str>)
            .map_err(|_| "Microsoft sign-in could not be opened in the system browser")?;
        let (code, returned_state) = receive_callback(&listener, Duration::from_secs(300))?;
        if returned_state.secret() != expected_state.secret() {
            return Err("Microsoft sign-in returned an invalid state value".into());
        }
        let client = oauth_client(tenant, Some(&redirect))?;
        let http = token_client()?;
        let token = client
            .exchange_code(code)
            .set_pkce_verifier(verifier)
            .request(&http)
            .map_err(
                |_| "Microsoft sign-in completed, but the authorization could not be exchanged",
            )?;
        let refresh = token
            .refresh_token()
            .ok_or("Microsoft sign-in did not return a renewable authorization")?
            .secret()
            .to_owned();
        let mut record = PersistedIdentity {
            version: IDENTITY_VERSION,
            refresh_token: refresh,
            established_utc: Utc::now().to_rfc3339(),
        };
        let mut serialized = serde_json::to_vec(&record)
            .map_err(|_| "The Azure authorization could not be prepared for encryption")?;
        self.store.save(&mut serialized)?;
        record.refresh_token.zeroize();
        remove_legacy_identity_after_migration(&app_data_dir())?;
        Ok(())
    }

    pub fn access_token(&self, tenant_id: &str, scope: &str) -> Result<SecretString, String> {
        let _guard = self
            .gate
            .lock()
            .map_err(|_| "The identity lock is unavailable")?;
        let tenant = validated_tenant(tenant_id).ok_or("Azure returned an invalid tenant ID")?;
        if !matches!(scope, ARM_SCOPE | LOG_ANALYTICS_SCOPE) {
            return Err("The requested Azure token scope is not allowed".into());
        }
        let mut plaintext = self.store.load()?;
        let mut record: PersistedIdentity = serde_json::from_slice(&plaintext)
            .map_err(|_| "The encrypted Azure connection is incomplete")?;
        plaintext.zeroize();
        if record.version != IDENTITY_VERSION {
            return Err("The encrypted Azure connection requires a fresh sign-in".into());
        }
        let established = DateTime::parse_from_rfc3339(&record.established_utc)
            .map(|value| value.with_timezone(&Utc))
            .map_err(|_| "The encrypted Azure connection has no valid creation time")?;
        if Utc::now() >= established + ChronoDuration::days(14) {
            self.store.delete_parent()?;
            return Err("The Azure authorization reached its 14-day limit and was deleted".into());
        }
        let client = oauth_client(tenant, None)?;
        let http = token_client()?;
        let token = client
            .exchange_refresh_token(&RefreshToken::new(record.refresh_token.clone()))
            .add_scope(Scope::new(scope.to_owned()))
            .request(&http)
            .map_err(|_| "The Azure authorization could not be renewed; sign in again")?;
        if let Some(rotated) = token.refresh_token() {
            record.refresh_token.zeroize();
            record.refresh_token = rotated.secret().to_owned();
            let mut serialized = serde_json::to_vec(&record)
                .map_err(|_| "The renewed authorization could not be encrypted")?;
            self.store.save(&mut serialized)?;
        }
        Ok(SecretString::from(token.access_token().secret().to_owned()))
    }

    pub fn delete(&self) -> Result<(), String> {
        let _guard = self
            .gate
            .lock()
            .map_err(|_| "The identity lock is unavailable")?;
        self.store.delete_parent()
    }
}

fn validated_tenant(value: &str) -> Option<&str> {
    let trimmed = value.trim();
    if trimmed.eq_ignore_ascii_case("organizations") || uuid::Uuid::parse_str(trimmed).is_ok() {
        Some(trimmed)
    } else {
        None
    }
}

fn oauth_client(
    tenant: &str,
    redirect: Option<&str>,
) -> Result<
    BasicClient<
        oauth2::EndpointSet,
        oauth2::EndpointNotSet,
        oauth2::EndpointNotSet,
        oauth2::EndpointNotSet,
        oauth2::EndpointSet,
    >,
    String,
> {
    let authority = format!("{AUTHORITY_HOST}/{tenant}/oauth2/v2.0");
    let mut client = BasicClient::new(ClientId::new(AZURE_DEVELOPMENT_CLIENT_ID.to_owned()))
        .set_auth_uri(
            AuthUrl::new(format!("{authority}/authorize"))
                .map_err(|_| "Invalid Microsoft authorization endpoint")?,
        )
        .set_token_uri(
            TokenUrl::new(format!("{authority}/token"))
                .map_err(|_| "Invalid Microsoft token endpoint")?,
        )
        .set_auth_type(AuthType::RequestBody);
    if let Some(value) = redirect {
        client = client.set_redirect_uri(
            RedirectUrl::new(value.to_owned()).map_err(|_| "Invalid loopback redirect address")?,
        );
    }
    Ok(client)
}

fn token_client() -> Result<reqwest::blocking::Client, String> {
    reqwest::blocking::ClientBuilder::new()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|_| "A secure Microsoft token client could not be created".into())
}

fn receive_callback(
    listener: &TcpListener,
    timeout: Duration,
) -> Result<(AuthorizationCode, CsrfToken), String> {
    let deadline = Instant::now() + timeout;
    loop {
        match listener.accept() {
            Ok((mut stream, _address)) => {
                stream.set_read_timeout(Some(Duration::from_secs(5))).ok();
                let mut request_line = String::new();
                BufReader::new(&stream)
                    .take(8192)
                    .read_line(&mut request_line)
                    .map_err(|_| "The Microsoft sign-in callback could not be read")?;
                let target = request_line
                    .split_whitespace()
                    .nth(1)
                    .ok_or("The Microsoft sign-in callback was malformed")?;
                let callback = Url::parse(&format!("http://localhost{target}"))
                    .map_err(|_| "The Microsoft sign-in callback URL was invalid")?;
                let response = "Authentication complete. You can return to Azure Health Beacon and close this tab. Do not share this page or its address.";
                let headers = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Security-Policy: default-src 'none'\r\nCache-Control: no-store\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response}",
                    response.len()
                );
                let _ = stream.write_all(headers.as_bytes());
                if let Some(error) = callback.query_pairs().find(|(key, _)| key == "error") {
                    let safe = error
                        .1
                        .chars()
                        .filter(|character| {
                            character.is_ascii_alphanumeric() || matches!(character, '_' | '-')
                        })
                        .take(80)
                        .collect::<String>();
                    return Err(format!("Microsoft sign-in was not completed ({safe})"));
                }
                let code = callback
                    .query_pairs()
                    .find(|(key, _)| key == "code")
                    .map(|(_, value)| AuthorizationCode::new(value.into_owned()))
                    .ok_or("Microsoft sign-in returned no authorization code")?;
                let state = callback
                    .query_pairs()
                    .find(|(key, _)| key == "state")
                    .map(|(_, value)| CsrfToken::new(value.into_owned()))
                    .ok_or("Microsoft sign-in returned no state value")?;
                return Ok((code, state));
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                if Instant::now() >= deadline {
                    return Err("Microsoft sign-in timed out after five minutes".into());
                }
                thread::sleep(Duration::from_millis(100));
            }
            Err(_) => return Err("The secure sign-in listener stopped unexpectedly".into()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_guid_or_organizations_tenants_are_allowed() {
        assert_eq!(validated_tenant("organizations"), Some("organizations"));
        assert!(validated_tenant("11111111-1111-1111-1111-111111111111").is_some());
        assert!(validated_tenant("common/path").is_none());
    }

    #[test]
    fn token_scopes_are_fixed_in_source() {
        assert!(matches!(ARM_SCOPE, "https://management.azure.com/.default"));
        assert!(matches!(
            LOG_ANALYTICS_SCOPE,
            "https://api.loganalytics.io/.default"
        ));
    }
}
