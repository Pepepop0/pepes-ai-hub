"""The Civitai credential is validated, protected, and scoped to its own host."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from local_ai_stack import civitai_auth
from local_ai_stack.stack_application import model_request_headers

TOKEN = "civitai_test_secret"


@pytest.fixture
def isolated_token(monkeypatch, tmp_path):
    path = tmp_path / "civitai-token.dpapi"
    monkeypatch.setattr(civitai_auth, "token_path", lambda: path)
    monkeypatch.delenv("CIVITAI_TOKEN", raising=False)
    return path


def test_the_civitai_token_goes_only_to_civitai_over_https(monkeypatch, isolated_token):
    monkeypatch.setenv("CIVITAI_TOKEN", f" {TOKEN} ")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    monkeypatch.setattr(
        "local_ai_stack.huggingface_auth.token_path", lambda: Path("Z:/nowhere/hf-token.dpapi")
    )
    url = "https://civitai.com/api/download/models/3390681?fileId=3280058"
    assert model_request_headers(url) == {"Authorization": f"Bearer {TOKEN}"}
    for other in (
        "http://civitai.com/api/download/models/1",
        "https://civitai.com.evil.example/x",
        "https://huggingface.co/Comfy-Org/Krea-2/resolve/abc/x.safetensors",
    ):
        assert model_request_headers(other) == {}
    monkeypatch.delenv("CIVITAI_TOKEN")
    assert model_request_headers(url) == {}


def test_environment_token_takes_precedence_over_saved_token(monkeypatch, isolated_token):
    isolated_token.write_bytes(b"stored")
    monkeypatch.setattr(civitai_auth, "unprotect", lambda value: b"saved-secret")
    monkeypatch.setenv("CIVITAI_TOKEN", "environment-secret")
    assert civitai_auth.load_token() == ("environment-secret", "CIVITAI_TOKEN")


def test_authenticate_validates_then_saves_encrypted_token(monkeypatch, isolated_token):
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers["Authorization"]))
        return httpx.Response(200, json={"id": 7, "username": "pedro", "tier": "free"})

    monkeypatch.setattr(civitai_auth, "protect", lambda _value, _description: b"ciphertext")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        status = civitai_auth.authenticate_with_token(TOKEN, client=client)

    assert status.as_dict() == {
        "authenticated": True,
        "source": "provided",
        "username": "pedro",
        "detail": "ready",
    }
    assert seen == [(civitai_auth.CIVITAI_ME_URL, f"Bearer {TOKEN}")]
    assert isolated_token.read_bytes() == b"ciphertext"
    assert TOKEN.encode() not in isolated_token.read_bytes()


def test_rejected_token_is_not_saved(isolated_token):
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(401)))
    with client, pytest.raises(civitai_auth.CivitaiAuthError, match="credential rejected"):
        civitai_auth.authenticate_with_token(TOKEN, client=client)
    assert not isolated_token.exists()


def test_saved_token_is_loaded_and_can_be_removed(monkeypatch, isolated_token):
    isolated_token.write_bytes(b"ciphertext")
    monkeypatch.setattr(civitai_auth, "unprotect", lambda value: TOKEN.encode())
    assert civitai_auth.load_token() == (TOKEN, "windows-dpapi")
    civitai_auth.clear_token()
    assert not isolated_token.exists()
