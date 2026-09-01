from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

import msal
from msal_extensions import PersistedTokenCache, build_encrypted_persistence

from .config import app_data_dir

# Microsoft Azure's public development client is the documented default used
# by Azure Identity's InteractiveBrowserCredential. It is a public client: no
# client secret exists in the application or repository.
AZURE_DEVELOPMENT_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
ARM_SCOPE = "https://management.azure.com/.default"
LOG_ANALYTICS_SCOPE = "https://api.loganalytics.io/.default"
AUTHORITY_HOST = "https://login.microsoftonline.com"

_TOKEN_CACHE_NAME = "token-cache.bin"
_ACCOUNT_STATE_NAME = "account-state.bin"
_AUTH_LOCK = threading.RLock()


class IdentityUnavailableError(RuntimeError):
    """Raised when the app-owned authorization can no longer acquire a token."""


def identity_dir() -> Path:
    return app_data_dir() / "identity"


def token_cache_path() -> Path:
    return identity_dir() / _TOKEN_CACHE_NAME


def account_state_path() -> Path:
    return identity_dir() / _ACCOUNT_STATE_NAME


def _encrypted_persistence(path: Path):
    if os.name != "nt":
        raise IdentityUnavailableError(
            "Azure Health Beacon credential storage requires Windows 11."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    persistence = build_encrypted_persistence(str(path))
    if not persistence.is_encrypted:
        raise IdentityUnavailableError(
            "Windows credential encryption is unavailable; sign-in was refused."
        )
    return persistence


def _load_account_state() -> dict[str, str]:
    path = account_state_path()
    if not path.exists():
        raise IdentityUnavailableError("The app-owned Azure connection is missing.")
    try:
        raw = _encrypted_persistence(path).load()
        payload = json.loads(raw)
    except Exception as error:
        raise IdentityUnavailableError(
            "The encrypted Azure connection could not be opened. Sign in again."
        ) from error
    required = ("home_account_id", "username", "home_tenant_id")
    if not isinstance(payload, dict) or any(not payload.get(key) for key in required):
        raise IdentityUnavailableError(
            "The encrypted Azure connection is incomplete. Sign in again."
        )
    return {key: str(payload[key]) for key in required}


def identity_state_available() -> bool:
    return token_cache_path().is_file() and account_state_path().is_file()


def verify_encrypted_storage() -> str:
    """Exercise DPAPI and the persisted-cache lock without retaining test data."""
    directory = identity_dir()
    probe = directory / f".dpapi-probe-{os.getpid()}-{threading.get_ident()}.bin"
    lock = Path(f"{probe}.lockfile")
    try:
        persistence = _encrypted_persistence(probe)
        cache = PersistedTokenCache(persistence)
        list(cache.search("AccessToken"))
        persistence.save("azure-health-beacon-dpapi-probe")
        if persistence.load() != "azure-health-beacon-dpapi-probe":
            raise IdentityUnavailableError("Windows credential encryption self-test failed.")
        return "windows-dpapi-current-user"
    finally:
        probe.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass


def _application(authority_tenant: str, cache: PersistedTokenCache):
    tenant = authority_tenant.strip() or "organizations"
    return msal.PublicClientApplication(
        AZURE_DEVELOPMENT_CLIENT_ID,
        authority=f"{AUTHORITY_HOST}/{tenant}",
        token_cache=cache,
    )


def interactive_sign_in(
    tenant_hint: str = "", timeout_seconds: int = 300
) -> tuple[bool, str]:
    """Run OAuth authorization code + PKCE and persist only DPAPI ciphertext."""
    with _AUTH_LOCK:
        try:
            cache = PersistedTokenCache(_encrypted_persistence(token_cache_path()))
            application = _application(tenant_hint, cache)
            result = application.acquire_token_interactive(
                scopes=[ARM_SCOPE],
                prompt="select_account",
                timeout=timeout_seconds,
            )
            if not isinstance(result, dict) or "access_token" not in result:
                message = "Microsoft sign-in was not completed."
                if isinstance(result, dict):
                    message = str(
                        result.get("error_description")
                        or result.get("error")
                        or message
                    )
                return False, message
            account = result.get("account") or {}
            claims = result.get("id_token_claims") or {}
            state = {
                "home_account_id": str(account.get("home_account_id", "")),
                "username": str(account.get("username", "")),
                "home_tenant_id": str(claims.get("tid", "")),
            }
            if any(not value for value in state.values()):
                return False, "Microsoft sign-in did not return a complete account record."
            _encrypted_persistence(account_state_path()).save(json.dumps(state))
        except IdentityUnavailableError as error:
            return False, str(error)
        except Exception:  # noqa: BLE001 - security boundary returns no library detail
            return False, "Microsoft sign-in could not be completed securely."
        else:
            return True, "Microsoft sign-in completed using app-owned encrypted storage."
        finally:
            # Access tokens are necessarily present briefly in process memory,
            # but are never returned through the bridge, logged, or written as
            # plaintext by Azure Health Beacon.
            if "result" in locals():
                result = None


def get_access_token(tenant_id: str, scope: str = ARM_SCOPE) -> str:
    """Acquire silently from the app-owned encrypted cache; never open UI."""
    with _AUTH_LOCK:
        state = _load_account_state()
        tenant = tenant_id.strip() or state["home_tenant_id"]
        try:
            cache = PersistedTokenCache(_encrypted_persistence(token_cache_path()))
            application = _application(tenant, cache)
            accounts = application.get_accounts(username=state["username"])
            account = next(
                (
                    item
                    for item in accounts
                    if str(item.get("home_account_id", ""))
                    == state["home_account_id"]
                ),
                None,
            )
            if account is None:
                raise IdentityUnavailableError(
                    "The encrypted Azure session no longer contains the selected account."
                )
            result = application.acquire_token_silent(
                scopes=[scope],
                account=account,
                authority=f"{AUTHORITY_HOST}/{tenant}",
            )
            if not isinstance(result, dict) or "access_token" not in result:
                raise IdentityUnavailableError(
                    "The Azure session needs renewal. Open the Beacon and sign in again."
                )
            return str(result["access_token"])
        except IdentityUnavailableError:
            raise
        except Exception as error:
            raise IdentityUnavailableError(
                "The encrypted Azure session could not acquire authorization."
            ) from error


def home_tenant_id() -> str:
    return _load_account_state()["home_tenant_id"]


def delete_identity_state() -> None:
    """Hard-delete only Beacon-owned encrypted identity data and legacy state."""
    parent = app_data_dir().resolve()
    for name in ("identity", "azure-cli"):
        target = (parent / name).resolve()
        if target.parent != parent or target.name != name:
            raise RuntimeError("Refusing to delete an unexpected identity path")
        if target.exists():
            shutil.rmtree(target)
