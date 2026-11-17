"""The Hugging Face credential reaches Hugging Face, and nothing else."""

from __future__ import annotations

import httpx
import pytest

from local_ai_stack import huggingface_auth
from local_ai_stack.download import Downloader, DownloadSpec
from local_ai_stack.huggingface_auth import request_headers

TOKEN = "hf_test_secret"


@pytest.fixture
def env_token(monkeypatch, tmp_path):
    monkeypatch.setattr(huggingface_auth, "token_path", lambda: tmp_path / "absent.dpapi")
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    monkeypatch.setenv("HF_TOKEN", TOKEN)


@pytest.mark.parametrize(
    "url",
    [
        "http://huggingface.co/x/resolve/main/f",  # plain http
        "https://huggingface.co.evil.example/x",  # look-alike host
        "https://cdn-lfs.huggingface.co/blob",  # redirect target, not the API host
        "https://github.com/ollama/ollama/releases/download/v1/ollama.zip",
    ],
)
def test_the_token_is_never_offered_to_another_host(env_token, url):
    assert request_headers(url) == {}


def test_the_token_is_sent_to_hugging_face(env_token):
    assert request_headers("https://huggingface.co/org/repo/resolve/abc/f") == {
        "Authorization": f"Bearer {TOKEN}"
    }


def test_no_credential_means_no_header(monkeypatch, tmp_path):
    monkeypatch.setattr(huggingface_auth, "token_path", lambda: tmp_path / "absent.dpapi")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    assert request_headers("https://huggingface.co/org/repo/resolve/abc/f") == {}


def test_a_gated_download_carries_the_token_and_others_do_not(env_token, tmp_path):
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("Authorization")
        return httpx.Response(200, content=b"weights")

    downloader = Downloader(
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        request_headers=request_headers,
    )
    downloader.fetch(DownloadSpec("https://huggingface.co/o/r/resolve/c/a.bin", tmp_path / "a"))
    downloader.fetch(DownloadSpec("https://example.com/b.bin", tmp_path / "b"))
    assert seen == {"huggingface.co": f"Bearer {TOKEN}", "example.com": None}
