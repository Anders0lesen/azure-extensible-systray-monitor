use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

use reqwest::{
    StatusCode,
    blocking::{Client, Response},
    header::LOCATION,
};
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use url::Url;
use uuid::Uuid;

use crate::{config::app_data_dir, security::atomic_replace};

const LATEST_RELEASE_API: &str =
    "https://api.github.com/repos/Anders0lesen/azure-extensible-systray-monitor/releases/latest";
const RELEASE_PAGE_PREFIX: &str =
    "https://github.com/Anders0lesen/azure-extensible-systray-monitor/releases/tag/";
const DOWNLOAD_PATH_PREFIX: &str =
    "/Anders0lesen/azure-extensible-systray-monitor/releases/download/";
const MAX_RELEASE_BYTES: usize = 1_000_000;
const MAX_CHECKSUM_BYTES: usize = 4_096;
const MAX_INSTALLER_BYTES: u64 = 100_000_000;

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ReleaseInfo {
    pub update_available: bool,
    pub version: String,
    pub tag: String,
    pub title: String,
    pub notes: String,
    pub page_url: String,
    #[serde(skip_serializing)]
    installer_name: String,
    #[serde(skip_serializing)]
    installer_url: Url,
    #[serde(skip_serializing)]
    checksum_url: Url,
    #[serde(skip_serializing)]
    installer_digest: String,
}

pub fn check(current_version: &str, timeout_seconds: u64) -> Result<ReleaseInfo, String> {
    let client = client(timeout_seconds)?;
    let response = client
        .get(LATEST_RELEASE_API)
        .header("Accept", "application/vnd.github+json")
        .header("X-GitHub-Api-Version", "2022-11-28")
        .header("User-Agent", format!("AzureHealthBeacon/{current_version}"))
        .send()
        .map_err(|_| "GitHub could not be reached securely")?;
    if !response.status().is_success() {
        return Err(format!("GitHub returned HTTP {}", response.status()));
    }
    let bytes = limited_bytes(response, MAX_RELEASE_BYTES)?;
    let payload: Value =
        serde_json::from_slice(&bytes).map_err(|_| "GitHub returned invalid release metadata")?;
    parse_release(&payload, current_version)
}

pub fn download(release: &ReleaseInfo, timeout_seconds: u64) -> Result<PathBuf, String> {
    validate_release_url(
        &release.installer_url,
        &release.tag,
        &release.installer_name,
    )?;
    validate_release_url(
        &release.checksum_url,
        &release.tag,
        &format!("{}.sha256", release.installer_name),
    )?;
    let checksum = String::from_utf8(download_limited(
        release.checksum_url.clone(),
        timeout_seconds,
        MAX_CHECKSUM_BYTES as u64,
    )?)
    .map_err(|_| "The release checksum is not plain text")?;
    let expected = parse_checksum(&checksum, &release.installer_name)?;
    if expected != release.installer_digest {
        return Err("GitHub's installer digest and checksum asset do not agree".into());
    }

    let directory = app_data_dir().join("updates");
    fs::create_dir_all(&directory).map_err(|_| "The update directory could not be created")?;
    let temporary = directory.join(format!("update-{}.part", Uuid::new_v4()));
    let outcome = (|| {
        let client = client(timeout_seconds)?;
        let mut response = get_following_safe_redirects(&client, release.installer_url.clone())?;
        if response
            .content_length()
            .is_some_and(|value| value > MAX_INSTALLER_BYTES)
        {
            return Err("The update installer exceeds the 100 MB safety limit".into());
        }
        let mut file = fs::File::create(&temporary)
            .map_err(|_| "The temporary update installer could not be created")?;
        let mut hasher = Sha256::new();
        let mut total = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let count = response
                .read(&mut buffer)
                .map_err(|_| "The update download stopped unexpectedly")?;
            if count == 0 {
                break;
            }
            total = total.saturating_add(count as u64);
            if total > MAX_INSTALLER_BYTES {
                return Err("The update installer exceeds the 100 MB safety limit".into());
            }
            hasher.update(&buffer[..count]);
            file.write_all(&buffer[..count])
                .map_err(|_| "The update installer could not be written")?;
        }
        file.sync_all()
            .map_err(|_| "The update installer could not be flushed")?;
        let digest = hasher.finalize();
        if total == 0 || hex_digest(&digest) != expected {
            return Err("The downloaded installer failed SHA-256 verification".into());
        }
        let destination = directory.join(&release.installer_name);
        atomic_replace(&temporary, &destination)?;
        Ok(destination)
    })();
    let _ = fs::remove_file(&temporary);
    outcome
}

pub fn launch(installer: &Path) -> Result<(), String> {
    let parent = installer
        .parent()
        .ok_or("The update installer path is incomplete")?;
    Command::new(installer)
        .current_dir(parent)
        .args([
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/CLOSEAPPLICATIONS",
            "/RESTARTAPPLICATIONS",
        ])
        .spawn()
        .map_err(|_| "The verified update installer could not be started")?;
    Ok(())
}

fn parse_release(payload: &Value, current: &str) -> Result<ReleaseInfo, String> {
    if payload
        .get("draft")
        .and_then(Value::as_bool)
        .unwrap_or(true)
        || payload
            .get("prerelease")
            .and_then(Value::as_bool)
            .unwrap_or(true)
    {
        return Err("The latest GitHub release is not stable".into());
    }
    let tag = payload
        .get("tag_name")
        .and_then(Value::as_str)
        .ok_or("GitHub returned no release tag")?;
    let version = tag.strip_prefix('v').unwrap_or(tag);
    let current_parts = version_parts(current)?;
    let candidate_parts = version_parts(version)?;
    let page_url = payload
        .get("html_url")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if page_url != format!("{RELEASE_PAGE_PREFIX}{tag}") {
        return Err("GitHub returned an unexpected release page".into());
    }
    let installer_name = format!("AzureHealthBeacon-Setup-{tag}.exe");
    let checksum_name = format!("{installer_name}.sha256");
    let assets = payload
        .get("assets")
        .and_then(Value::as_array)
        .ok_or("The release has no assets")?;
    let asset = |name: &str| {
        assets
            .iter()
            .find(|item| item.get("name").and_then(Value::as_str) == Some(name))
    };
    let installer = asset(&installer_name).ok_or("The release is missing its Windows installer")?;
    let checksum = asset(&checksum_name).ok_or("The release is missing its checksum")?;
    let digest = installer
        .get("digest")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .strip_prefix("sha256:")
        .unwrap_or_default()
        .to_ascii_lowercase();
    if digest.len() != 64 || !digest.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("The GitHub installer has no valid SHA-256 digest".into());
    }
    let installer_url = Url::parse(
        installer
            .get("browser_download_url")
            .and_then(Value::as_str)
            .unwrap_or_default(),
    )
    .map_err(|_| "GitHub returned an invalid installer URL")?;
    let checksum_url = Url::parse(
        checksum
            .get("browser_download_url")
            .and_then(Value::as_str)
            .unwrap_or_default(),
    )
    .map_err(|_| "GitHub returned an invalid checksum URL")?;
    validate_release_url(&installer_url, tag, &installer_name)?;
    validate_release_url(&checksum_url, tag, &checksum_name)?;
    Ok(ReleaseInfo {
        update_available: candidate_parts > current_parts,
        version: version.to_owned(),
        tag: tag.to_owned(),
        title: payload
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or(tag)
            .chars()
            .take(200)
            .collect(),
        notes: payload
            .get("body")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .chars()
            .take(20_000)
            .collect(),
        page_url: page_url.to_owned(),
        installer_name,
        installer_url,
        checksum_url,
        installer_digest: digest,
    })
}

fn validate_release_url(url: &Url, tag: &str, name: &str) -> Result<(), String> {
    let expected = format!("{DOWNLOAD_PATH_PREFIX}{tag}/{name}");
    if url.scheme() != "https"
        || url.host_str() != Some("github.com")
        || url.path() != expected
        || url.query().is_some()
        || url.fragment().is_some()
        || !url.username().is_empty()
        || url.password().is_some()
    {
        Err("GitHub returned an unexpected update asset URL".into())
    } else {
        Ok(())
    }
}

fn get_following_safe_redirects(client: &Client, mut url: Url) -> Result<Response, String> {
    for _ in 0..6 {
        let response = client
            .get(url.clone())
            .header("User-Agent", "AzureHealthBeacon/0.8")
            .send()
            .map_err(|_| "The update download could not be reached securely")?;
        if response.status().is_redirection() {
            let location = response
                .headers()
                .get(LOCATION)
                .and_then(|value| value.to_str().ok())
                .ok_or("The update redirect was incomplete")?;
            url = url
                .join(location)
                .map_err(|_| "The update redirect URL was invalid")?;
            validate_download_host(&url)?;
            continue;
        }
        if response.status() != StatusCode::OK {
            return Err(format!(
                "The update download returned HTTP {}",
                response.status()
            ));
        }
        validate_download_host(response.url())?;
        return Ok(response);
    }
    Err("The update download used too many redirects".into())
}

fn validate_download_host(url: &Url) -> Result<(), String> {
    const HOSTS: &[&str] = &[
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    ];
    if url.scheme() == "https"
        && url.host_str().is_some_and(|host| HOSTS.contains(&host))
        && url.port().is_none_or(|port| port == 443)
        && url.username().is_empty()
        && url.password().is_none()
    {
        Ok(())
    } else {
        Err("The update download redirected outside approved GitHub hosts".into())
    }
}

fn download_limited(url: Url, timeout: u64, limit: u64) -> Result<Vec<u8>, String> {
    let client = client(timeout)?;
    let response = get_following_safe_redirects(&client, url)?;
    limited_bytes(response, limit as usize)
}

fn limited_bytes(mut response: Response, limit: usize) -> Result<Vec<u8>, String> {
    if response
        .content_length()
        .is_some_and(|value| value > limit as u64)
    {
        return Err("The update response exceeds its safety limit".into());
    }
    let mut bytes = Vec::new();
    response
        .take(limit as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "The update response could not be read")?;
    if bytes.len() > limit {
        Err("The update response exceeds its safety limit".into())
    } else {
        Ok(bytes)
    }
}

fn parse_checksum(text: &str, installer_name: &str) -> Result<String, String> {
    let mut parts = text.trim().split_whitespace();
    let digest = parts.next().unwrap_or_default().to_ascii_lowercase();
    let name = parts.next().unwrap_or_default().trim_start_matches('*');
    if parts.next().is_some()
        || digest.len() != 64
        || !digest.bytes().all(|byte| byte.is_ascii_hexdigit())
        || name != installer_name
    {
        Err("The release checksum file is invalid".into())
    } else {
        Ok(digest)
    }
}

fn version_parts(value: &str) -> Result<(u64, u64, u64), String> {
    let values = value
        .strip_prefix('v')
        .unwrap_or(value)
        .split('.')
        .map(str::parse::<u64>)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| "The release version is invalid")?;
    if values.len() != 3 {
        return Err("The release version is invalid".into());
    }
    Ok((values[0], values[1], values[2]))
}

fn hex_digest(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn client(timeout_seconds: u64) -> Result<Client, String> {
    Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(timeout_seconds.clamp(5, 300)))
        .build()
        .map_err(|_| "A secure update client could not be created".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_lookalike_download_hosts() {
        let url = Url::parse("https://github.com.example.test/file").unwrap();
        assert!(validate_download_host(&url).is_err());
    }

    #[test]
    fn checksum_requires_exact_asset_name() {
        let digest = "a".repeat(64);
        assert!(parse_checksum(&format!("{digest} *expected.exe"), "expected.exe").is_ok());
        assert!(parse_checksum(&format!("{digest} *other.exe"), "expected.exe").is_err());
    }

    #[test]
    fn semantic_versions_are_compared_numerically() {
        assert!(version_parts("0.10.0").unwrap() > version_parts("0.8.9").unwrap());
    }
}
