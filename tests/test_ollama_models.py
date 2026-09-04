"""The Ollama model-store client.

Every request is served by an httpx MockTransport: the real client code runs,
no socket is opened, and the Ollama installed on this machine is never touched.
"""

from __future__ import annotations

import json

import httpx
import pytest

from local_ai_stack.ollama_models import (
    HttpOllamaClient,
    OllamaClientError,
    OllamaModelClient,
    OllamaModelEntry,
)

TAGS = {
    "models": [
        {"name": "hermes3:8b", "digest": "4f6b83f30b62aaaabbbb", "size": 4700000000},
        {"name": "qwen3:8b", "digest": "sha256:500a1f067a9fcccc", "size": 5200000000},
    ]
}


def client_for(handler) -> HttpOllamaClient:
    transport = httpx.MockTransport(handler)
    return HttpOllamaClient(client_factory=lambda: httpx.Client(transport=transport))


def answering(payload, status: int = 200, record: list | None = None) -> HttpOllamaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        return httpx.Response(status, json=payload)

    return client_for(handler)


# --- contrato --------------------------------------------------------------


def test_the_client_satisfies_the_protocol():
    assert isinstance(answering(TAGS), OllamaModelClient)


def test_listing_uses_the_documented_json_endpoint():
    seen: list[httpx.Request] = []
    answering(TAGS, record=seen).list_models()
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/tags"


def test_listing_parses_names_and_digests():
    entries = answering(TAGS).list_models()
    assert [entry.name for entry in entries] == ["hermes3:8b", "qwen3:8b"]
    assert entries[0].size_bytes == 4700000000


def test_an_entry_without_a_name_is_ignored():
    entries = answering({"models": [{"digest": "abc"}, {"name": "ok:1b"}]}).list_models()
    assert [entry.name for entry in entries] == ["ok:1b"]


def test_the_model_key_is_accepted_as_a_name():
    entries = answering({"models": [{"model": "alt:1b", "digest": "abc"}]}).list_models()
    assert entries[0].name == "alt:1b"


def test_an_empty_store_is_not_an_error():
    assert answering({"models": []}).list_models() == ()


# --- digests ---------------------------------------------------------------


@pytest.mark.parametrize(
    "reported, pinned, expected",
    [
        ("4f6b83f30b62aaaa", "4f6b83f30b62", True),
        ("sha256:4f6b83f30b62aaaa", "4f6b83f30b62", True),
        ("SHA256:4F6B83F30B62AAAA", "4f6b83f30b62", True),
        ("deadbeefcafe", "4f6b83f30b62", False),
        ("anything", None, True),
        ("", None, True),
    ],
)
def test_digest_matching_compares_the_published_prefix(reported, pinned, expected):
    entry = OllamaModelEntry(name="demo:8b", digest=reported)
    assert entry.matches_digest(pinned) is expected


# --- falhas ----------------------------------------------------------------


def test_an_unreachable_store_raises_a_typed_error():
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(OllamaClientError, match="could not read"):
        client_for(explode).list_models()


def test_a_server_error_raises_a_typed_error():
    with pytest.raises(OllamaClientError):
        answering({}, status=500).list_models()


def test_a_malformed_payload_is_rejected():
    with pytest.raises(OllamaClientError, match="unexpected payload"):
        answering({"nope": 1}).list_models()


def test_invalid_json_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    with pytest.raises(OllamaClientError):
        client_for(handler).list_models()


# --- pull ------------------------------------------------------------------


def test_pull_posts_the_reference_without_streaming():
    seen: list[httpx.Request] = []
    answering({"status": "success"}, record=seen).pull("hermes3:8b")
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/api/pull"
    body = json.loads(request.content)
    assert body == {"model": "hermes3:8b", "stream": False}


def test_a_reported_error_fails_the_pull():
    with pytest.raises(OllamaClientError, match="could not pull"):
        answering({"error": "model not found"}).pull("nope:1b")


def test_an_unfinished_status_fails_the_pull():
    with pytest.raises(OllamaClientError, match="ended as"):
        answering({"status": "pulling manifest"}).pull("hermes3:8b")


def test_a_transport_failure_fails_the_pull():
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    with pytest.raises(OllamaClientError, match="could not pull"):
        client_for(explode).pull("hermes3:8b")


def test_the_client_never_deletes_anything():
    """There is no delete surface: the store is only read from and added to."""
    assert not hasattr(HttpOllamaClient, "delete")
    assert not hasattr(HttpOllamaClient, "remove")
    public = {name for name in vars(HttpOllamaClient) if not name.startswith("_")}
    assert public == {"list_models", "pull"}
