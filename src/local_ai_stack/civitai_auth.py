"""Civitai authentication for model downloads that require an account.

Tokens are never written as plaintext. On Windows the current user's DPAPI key
protects the persisted token; automation can instead provide ``CIVITAI_TOKEN``.
Authorization headers are returned only for requests to civitai.com over HTTPS.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from platformdirs import user_config_path

from .windows_credentials import protect, unprotect

CIVITAI_HOST = "civitai.com"
CIVITAI_TOKEN_ENV = "CIVITAI_TOKEN"
CIVITAI_ME_URL = "https://civitai.com/api/v1/me"


class CivitaiAuthError(RuntimeError):
    """Authentication could not be completed or the credential is invalid."""


@dataclass(frozen=True)
class CivitaiAuthStatus:
    authenticated: bool
    source: str | None = None
    username: str | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "authenticated": self.authenticated,
            "source": self.source,
            "username": self.username,
            "detail": self.detail,
        }


def token_path() -> Path:
    return Path(user_config_path("PepitosAIHub", appauthor=False)) / "civitai-token.dpapi"


def _environment_token() -> tuple[str | None, str | None]:
    token = os.environ.get(CIVITAI_TOKEN_ENV, "").strip()
    return (token or None), (CIVITAI_TOKEN_ENV if token else None)


def save_token(token: str) -> Path:
    token = token.strip()
    if not token:
        raise CivitaiAuthError("empty Civitai token")
    destination = token_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        temporary.write_bytes(protect(token.encode("utf-8"), "PepitosAIHub Civitai token"))
        os.replace(temporary, destination)
    except (OSError, RuntimeError) as exc:
        temporary.unlink(missing_ok=True)
        raise CivitaiAuthError(
            "could not protect the Civitai credential with Windows DPAPI"
        ) from exc
    return destination


def load_token() -> tuple[str | None, str | None]:
    token, source = _environment_token()
    if token:
        return token, source
    path = token_path()
    if not path.is_file():
        return None, None
    try:
        token = unprotect(path.read_bytes()).decode("utf-8").strip()
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise CivitaiAuthError("the stored Civitai credential is unreadable") from exc
    return (token or None), ("windows-dpapi" if token else None)


def clear_token() -> None:
    token_path().unlink(missing_ok=True)


def request_headers(url: str) -> dict[str, str]:
    """Return a bearer header only for Civitai's own HTTPS host."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != CIVITAI_HOST:
        return {}
    token, _source = load_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def authenticate_with_token(token: str, *, client: httpx.Client | None = None) -> CivitaiAuthStatus:
    status = check_status(token=token, source="provided", client=client)
    if not status.authenticated:
        raise CivitaiAuthError(status.detail)
    save_token(token)
    return status


def check_status(
    *,
    token: str | None = None,
    source: str | None = None,
    client: httpx.Client | None = None,
) -> CivitaiAuthStatus:
    if token is None:
        token, loaded_source = load_token()
        source = source or loaded_source
    if not token:
        return CivitaiAuthStatus(False, detail="no Civitai credential configured")
    owns_client = client is None
    session = client or httpx.Client(timeout=30.0, follow_redirects=False)
    try:
        identity = session.get(CIVITAI_ME_URL, headers={"Authorization": f"Bearer {token}"})
        if identity.status_code in {401, 403}:
            return CivitaiAuthStatus(False, source=source, detail="credential rejected")
        identity.raise_for_status()
        username = str(identity.json().get("username") or "") or None
        return CivitaiAuthStatus(True, source, username, "ready")
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        raise CivitaiAuthError(f"could not verify Civitai access: {exc}") from exc
    finally:
        if owns_client:
            session.close()
