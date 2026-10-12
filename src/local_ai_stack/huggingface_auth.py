"""Hugging Face authentication for gated model downloads.

Tokens are never written as plaintext. On Windows the current user's DPAPI key
protects the persisted token; automation can instead provide ``HF_TOKEN``.
Authorization headers are returned only for requests to huggingface.co.
"""

from __future__ import annotations

import ctypes
import getpass
import json
import os
import sys
import time
import webbrowser
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from platformdirs import user_config_path

HF_HOST = "huggingface.co"
HF_CLIENT_ID_ENV = "PEPITOS_HF_CLIENT_ID"
HF_TOKEN_ENVIRONMENTS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")
HF_SCOPES = "openid profile gated-repos"
HF_DEVICE_URL = "https://huggingface.co/oauth/device"
HF_TOKEN_URL = "https://huggingface.co/oauth/token"
HF_WHOAMI_URL = "https://huggingface.co/api/whoami-v2"
CAPELLA_MODEL_URL = (
    "https://huggingface.co/black-forest-labs/FLUX.2-klein-9b-fp8/resolve/"
    "902d9d510b51533e07729f19211414a3648b77d2/flux-2-klein-9b-fp8.safetensors"
)
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class HuggingFaceAuthError(RuntimeError):
    """Authentication could not be completed or the credential is invalid."""


@dataclass(frozen=True)
class HuggingFaceAuthStatus:
    authenticated: bool
    source: str | None = None
    username: str | None = None
    capella_model_access: bool | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "authenticated": self.authenticated,
            "source": self.source,
            "username": self.username,
            "capella_model_access": self.capella_model_access,
            "detail": self.detail,
        }


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def token_path() -> Path:
    return Path(user_config_path("PepitosAIHub", appauthor=False)) / "hf-token.dpapi"


def _environment_token() -> tuple[str | None, str | None]:
    for name in HF_TOKEN_ENVIRONMENTS:
        token = os.environ.get(name, "").strip()
        if token:
            return token, name
    return None, None


def _crypt_protect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise HuggingFaceAuthError("persistent token storage requires Windows DPAPI")
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "PepitosAIHub Hugging Face token",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(target),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def _crypt_unprotect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise HuggingFaceAuthError("persistent token storage requires Windows DPAPI")
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(target),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def save_token(token: str) -> Path:
    token = token.strip()
    if not token:
        raise HuggingFaceAuthError("empty Hugging Face token")
    destination = token_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(_crypt_protect(token.encode("utf-8")))
    os.replace(temporary, destination)
    return destination


def load_token() -> tuple[str | None, str | None]:
    token, source = _environment_token()
    if token:
        return token, source
    path = token_path()
    if not path.is_file():
        return None, None
    try:
        token = _crypt_unprotect(path.read_bytes()).decode("utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise HuggingFaceAuthError("the stored Hugging Face credential is unreadable") from exc
    return (token or None), ("windows-dpapi" if token else None)


def clear_token() -> None:
    token_path().unlink(missing_ok=True)


def request_headers(url: str) -> dict[str, str]:
    """Return a bearer header only for Hugging Face's own HTTPS host."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != HF_HOST:
        return {}
    token, _source = load_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def authenticate_with_token(
    token: str, *, client: httpx.Client | None = None
) -> HuggingFaceAuthStatus:
    status = check_status(token=token, source="provided", client=client)
    if not status.authenticated:
        raise HuggingFaceAuthError(status.detail)
    save_token(token)
    return status


def device_login(
    client_id: str,
    *,
    client: httpx.Client | None = None,
    open_browser: bool = True,
) -> HuggingFaceAuthStatus:
    """Complete the public-app OAuth device flow and persist its access token."""
    client_id = client_id.strip()
    if not client_id:
        raise HuggingFaceAuthError(f"set {HF_CLIENT_ID_ENV} to the public OAuth client id")
    owns_client = client is None
    session = client or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        response = session.post(
            HF_DEVICE_URL,
            data={"client_id": client_id, "scope": HF_SCOPES},
        )
        response.raise_for_status()
        payload = response.json()
        device_code = str(payload["device_code"])
        user_code = str(payload["user_code"])
        verification_uri = str(payload["verification_uri"])
        interval = max(1, int(payload.get("interval", 5)))
        expires_at = time.monotonic() + int(payload.get("expires_in", 900))
        print(f"Abra {verification_uri} e informe o código {user_code}")
        if open_browser:
            webbrowser.open(verification_uri)
        while time.monotonic() < expires_at:
            time.sleep(interval)
            token_response = session.post(
                HF_TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "client_id": client_id,
                },
            )
            token_payload: dict[str, Any] = token_response.json()
            if token_response.is_success and token_payload.get("access_token"):
                token = str(token_payload["access_token"])
                return authenticate_with_token(token, client=session)
            error = token_payload.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            raise HuggingFaceAuthError(str(token_payload.get("error_description") or error))
        raise HuggingFaceAuthError("Hugging Face authorization expired before completion")
    except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise HuggingFaceAuthError(f"Hugging Face OAuth failed: {exc}") from exc
    finally:
        if owns_client:
            session.close()


def interactive_login() -> HuggingFaceAuthStatus:
    client_id = os.environ.get(HF_CLIENT_ID_ENV, "").strip()
    if client_id:
        return device_login(client_id)
    print(
        "OAuth precisa do client id público do Pepito's AI Hub. "
        "Enquanto ele não está registrado, crie um token read em "
        "https://huggingface.co/settings/tokens. O valor não será exibido nem salvo em texto puro."
    )
    token = getpass.getpass("Hugging Face token: ")
    return authenticate_with_token(token)


def check_status(
    *,
    token: str | None = None,
    source: str | None = None,
    client: httpx.Client | None = None,
) -> HuggingFaceAuthStatus:
    if token is None:
        token, loaded_source = load_token()
        source = source or loaded_source
    if not token:
        return HuggingFaceAuthStatus(False, detail="no Hugging Face credential configured")
    owns_client = client is None
    session = client or httpx.Client(timeout=30.0, follow_redirects=False)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        identity = session.get(HF_WHOAMI_URL, headers=headers)
        if identity.status_code in {401, 403}:
            return HuggingFaceAuthStatus(False, source=source, detail="credential rejected")
        identity.raise_for_status()
        username = str(identity.json().get("name") or "") or None
        model = session.head(CAPELLA_MODEL_URL, headers=headers)
        access = model.status_code in {200, 302, 303, 307, 308}
        detail = "ready" if access else "accept the FLUX.2 Klein 9B license in the browser"
        return HuggingFaceAuthStatus(True, source, username, access, detail)
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        raise HuggingFaceAuthError(f"could not verify Hugging Face access: {exc}") from exc
    finally:
        if owns_client:
            session.close()
