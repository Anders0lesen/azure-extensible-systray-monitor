from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import __version__
from .config import app_data_dir

REPOSITORY = "Anders0lesen/azure-extensible-systray-monitor"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_PAGE_PREFIX = f"https://github.com/{REPOSITORY}/releases/tag/"
DOWNLOAD_PREFIX = f"/{REPOSITORY}/releases/download/"
MAX_RELEASE_JSON_BYTES = 1_000_000
MAX_CHECKSUM_BYTES = 4096
MAX_INSTALLER_BYTES = 100_000_000
ALLOWED_FINAL_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    version: str
    tag: str
    title: str
    notes: str
    page_url: str
    installer_name: str
    installer_url: str
    installer_digest: str
    checksum_name: str
    checksum_url: str


def parse_version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    if not match:
        raise ValueError(f"Unsupported version: {value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def is_newer_version(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def _read_limited(response: BinaryIO, limit: int) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared and int(declared) > limit:
        raise ValueError("The update response exceeds its safety limit")
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("The update response exceeds its safety limit")
    return data


def _request(url: str, timeout_seconds: int) -> Any:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"AzureHealthBeacon/{__version__}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    return urlopen(request, timeout=timeout_seconds)  # noqa: S310 - URLs are pinned and validated.


def _validate_release_asset_url(url: str, tag: str, asset_name: str) -> None:
    parsed = urlparse(url)
    expected_path = f"{DOWNLOAD_PREFIX}{tag}/{asset_name}"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("GitHub returned an unexpected update download URL")


def parse_release_payload(payload: dict[str, Any]) -> ReleaseInfo:
    if payload.get("draft") or payload.get("prerelease"):
        raise ValueError("The latest GitHub release is not a stable release")
    tag = str(payload.get("tag_name", ""))
    version = tag.removeprefix("v")
    parse_version(version)
    page_url = str(payload.get("html_url", ""))
    if page_url != f"{RELEASE_PAGE_PREFIX}{tag}":
        raise ValueError("GitHub returned an unexpected release page")

    installer_name = f"AzureHealthBeacon-Setup-{tag}.exe"
    checksum_name = f"{installer_name}.sha256"
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise TypeError("The GitHub release contains no downloadable assets")
    by_name = {
        str(asset.get("name", "")): asset for asset in assets if isinstance(asset, dict)
    }
    if installer_name not in by_name or checksum_name not in by_name:
        raise ValueError("The release is missing its installer or checksum")
    installer_url = str(by_name[installer_name].get("browser_download_url", ""))
    checksum_url = str(by_name[checksum_name].get("browser_download_url", ""))
    installer_digest = str(by_name[installer_name].get("digest", ""))
    if not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", installer_digest):
        raise ValueError("The release installer has no GitHub SHA-256 digest")
    _validate_release_asset_url(installer_url, tag, installer_name)
    _validate_release_asset_url(checksum_url, tag, checksum_name)
    return ReleaseInfo(
        version=version,
        tag=tag,
        title=str(payload.get("name") or tag)[:200],
        notes=str(payload.get("body") or "")[:20_000],
        page_url=page_url,
        installer_name=installer_name,
        installer_url=installer_url,
        installer_digest=installer_digest.removeprefix("sha256:").casefold(),
        checksum_name=checksum_name,
        checksum_url=checksum_url,
    )


def fetch_latest_release(timeout_seconds: int = 15) -> ReleaseInfo:
    with _request(LATEST_RELEASE_API, timeout_seconds) as response:
        final = urlparse(response.geturl())
        if final.scheme != "https" or final.hostname != "api.github.com":
            raise ValueError("The update service redirected outside GitHub")
        raw = _read_limited(response, MAX_RELEASE_JSON_BYTES)
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("GitHub returned an invalid release document")
    return parse_release_payload(payload)


def _expected_checksum(text: str, installer_name: str) -> str:
    line = text.strip()
    match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?([^\r\n]+)", line)
    if not match or match.group(2) != installer_name:
        raise ValueError("The release checksum file is invalid")
    return match.group(1).casefold()


def _validate_final_download(response: Any) -> None:
    parsed = urlparse(response.geturl())
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_FINAL_DOWNLOAD_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError("The update download redirected outside GitHub")


def download_verified_installer(
    release: ReleaseInfo,
    target_directory: Path | None = None,
    timeout_seconds: int = 60,
) -> Path:
    target = target_directory or app_data_dir() / "updates"
    target.mkdir(parents=True, exist_ok=True)

    with _request(release.checksum_url, timeout_seconds) as response:
        _validate_final_download(response)
        checksum_text = _read_limited(response, MAX_CHECKSUM_BYTES).decode("ascii")
    expected = _expected_checksum(checksum_text, release.installer_name)
    if expected != release.installer_digest:
        raise ValueError("The published checksums for the installer do not agree")

    handle, temporary_name = tempfile.mkstemp(
        prefix="update-", suffix=".part", dir=target
    )
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(handle, "wb") as stream:
            with _request(release.installer_url, timeout_seconds) as response:
                _validate_final_download(response)
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > MAX_INSTALLER_BYTES:
                    raise ValueError("The update installer exceeds its safety limit")
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_INSTALLER_BYTES:
                        raise ValueError(
                            "The update installer exceeds its safety limit"
                        )
                    digest.update(chunk)
                    stream.write(chunk)
        if total == 0 or digest.hexdigest().casefold() != expected:
            raise ValueError("The downloaded installer failed SHA-256 verification")
        destination = target / release.installer_name
        os.replace(temporary_name, destination)
        return destination
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def launch_installer(installer: Path, *, automatic: bool) -> None:
    # Both an explicitly approved update and opt-in automatic updating are
    # in-place operations. The surrounding UI owns consent and error reporting.
    arguments = [
        str(installer),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/CLOSEAPPLICATIONS",
        "/RESTARTAPPLICATIONS",
    ]
    subprocess.Popen(arguments, cwd=installer.parent, close_fds=True)  # noqa: S603
