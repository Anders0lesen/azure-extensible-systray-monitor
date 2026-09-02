from __future__ import annotations

import json
import logging
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
LOGGER = logging.getLogger(__name__)


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


def _account_state_from_result(
    application: msal.PublicClientApplication, result: dict[str, object]
) -> tuple[dict[str, str] | None, int]:
    """Resolve the interactive identity from MSAL's cache, not result shape.

    MSAL documents ``get_accounts()`` as the account-selection surface. An
    interactive token result is not guaranteed to contain an ``account`` key,
    even though the corresponding account has been written to the token cache.
    """
    claims_value = result.get("id_token_claims")
    claims = claims_value if isinstance(claims_value, dict) else {}
    direct_value = result.get("account")
    direct = direct_value if isinstance(direct_value, dict) else {}
    username = str(
        direct.get("username")
        or claims.get("preferred_username")
        or claims.get("upn")
        or claims.get("email")
        or ""
    ).strip()
    tenant_id = str(claims.get("tid") or direct.get("realm") or "").strip()
    object_ids = {
        str(value).casefold()
        for value in (claims.get("home_oid"), claims.get("oid"))
        if value
    }
    direct_home_id = str(direct.get("home_account_id") or "").strip()

    accounts = application.get_accounts()
    candidates = [item for item in accounts if isinstance(item, dict)]
    selected: dict[str, object] | None = None
    if direct_home_id:
        selected = next(
            (
                item
                for item in candidates
                if str(item.get("home_account_id", "")).casefold()
                == direct_home_id.casefold()
            ),
            direct,
        )
    elif candidates:
        def score(item: dict[str, object]) -> int:
            item_username = str(item.get("username", "")).casefold()
            item_realm = str(item.get("realm", "")).casefold()
            local_id = str(item.get("local_account_id", "")).casefold()
            home_id = str(item.get("home_account_id", "")).casefold()
            return (
                (8 if username and item_username == username.casefold() else 0)
                + (4 if tenant_id and item_realm == tenant_id.casefold() else 0)
                + (2 if local_id and local_id in object_ids else 0)
                + (2 if any(home_id.startswith(f"{oid}.") for oid in object_ids) else 0)
            )

        scored = [(score(item), item) for item in candidates]
        best_score = max(value for value, _item in scored)
        best = [item for value, item in scored if value == best_score]
        if len(candidates) == 1 or (best_score > 0 and len(best) == 1):
            selected = best[0]

    if selected is None:
        return None, len(candidates)
    state = {
        "home_account_id": str(selected.get("home_account_id") or direct_home_id).strip(),
        "username": str(selected.get("username") or username).strip(),
        "home_tenant_id": tenant_id or str(selected.get("realm") or "").strip(),
    }
    if any(not value for value in state.values()):
        return None, len(candidates)
    return state, len(candidates)


def interactive_sign_in(
    tenant_hint: str = "", timeout_seconds: int = 300
) -> tuple[bool, str]:
    """Run OAuth authorization code + PKCE and persist only DPAPI ciphertext."""
    with _AUTH_LOCK:
        stage = "initialize"
        LOGGER.info("Authentication stage=%s status=started", stage)
        try:
            cache = PersistedTokenCache(_encrypted_persistence(token_cache_path()))
            application = _application(tenant_hint, cache)
            stage = "interactive-browser"
            result = application.acquire_token_interactive(
                scopes=[ARM_SCOPE],
                prompt="select_account",
                timeout=timeout_seconds,
            )
            if not isinstance(result, dict) or "access_token" not in result:
                error_code = str(result.get("error", "unknown")) if isinstance(result, dict) else "invalid-result"
                safe_error_code = (
                    error_code
                    if 0 < len(error_code) <= 80
                    and all(
                        character.isascii()
                        and (character.isalnum() or character in "_.-")
                        for character in error_code
                    )
                    else "provider-error"
                )
                LOGGER.warning(
                    "Authentication stage=%s status=not-completed error_code=%s",
                    stage,
                    safe_error_code,
                )
                message = "Microsoft sign-in was not completed."
                if isinstance(result, dict):
                    message = str(
                        result.get("error_description")
                        or result.get("error")
                        or message
                    )
                return False, message
            stage = "account-selection"
            state, account_count = _account_state_from_result(application, result)
            LOGGER.info(
                "Authentication stage=%s cached_account_count=%d result_account_present=%s claims_present=%s",
                stage,
                account_count,
                isinstance(result.get("account"), dict),
                isinstance(result.get("id_token_claims"), dict),
            )
            if state is None:
                LOGGER.warning(
                    "Authentication stage=%s status=incomplete cached_account_count=%d",
                    stage,
                    account_count,
                )
                return (
                    False,
                    "Microsoft sign-in completed, but its cached account could not be selected. Try signing in again.",
                )
            stage = "encrypted-persistence"
            _encrypted_persistence(account_state_path()).save(json.dumps(state))
        except IdentityUnavailableError as error:
            LOGGER.warning("Authentication stage=%s status=unavailable", stage)
            return False, str(error)
        except Exception as error:  # noqa: BLE001 - security boundary returns no library detail
            LOGGER.error(  # noqa: TRY400 - traceback could contain identity data
                "Authentication stage=%s status=failed exception_type=%s",
                stage,
                type(error).__name__,
            )
            return False, "Microsoft sign-in could not be completed securely."
        else:
            LOGGER.info("Authentication stage=complete status=succeeded")
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
