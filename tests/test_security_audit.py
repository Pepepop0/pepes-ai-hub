"""SEC-04 audit: properties of the public snapshot that must keep holding.

Each test pins something the audit checked by reading the code, so that a later change
cannot quietly undo it: no secret-looking value is tracked, every shipped download is
pinned and hashed, archive extraction refuses to leave its folder, credentials only go to
their own host, the session export only copies named columns, and the guest link is
documented as open.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml
from test_open_webui_image_filter import load_module

from local_ai_stack import civitai_auth, huggingface_auth, linux_share, ollama, python_runtime
from local_ai_stack import session_export as export

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = ROOT / "manifests"

# --- secrets -----------------------------------------------------------------------------------

_SECRET_PATTERNS = {
    "huggingface token": re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"),
    "api key": re.compile(r"\bsk-[A-Za-z0-9]{30,}"),
    "aws key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----\s*[A-Za-z0-9+/=]{40,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}"),
    "credentials in a url": re.compile(
        r"\b[a-z][a-z0-9+.-]*://(?!user:|pedro:)[^/\s:@\"'`]+:(?![*]{3}|pass\b|secret\b)"
        r"[^/\s@\"'`]{6,}@(?![A-Za-z0-9.-]*\.(?:invalid|example)\b)[A-Za-z0-9.-]+"
    ),
}
_SECRET_FILES = re.compile(r"(^|/)(\.env(\..*)?|.*\.pem|.*\.key|.*\.p12|.*\.pfx|id_rsa.*)$")
_BINARY = re.compile(
    r"\.(png|jpe?g|gif|webp|ico|woff2?|ttf|zip|gz|zst|exe|db|sqlite3?)$", re.IGNORECASE
)


def _tracked() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [name for name in out.decode("utf-8").split("\0") if name]


def test_no_secret_file_is_tracked():
    assert [name for name in _tracked() if _SECRET_FILES.search(name)] == []


def test_no_secret_shaped_value_is_tracked():
    found = []
    for name in _tracked():
        path = ROOT / name
        if _BINARY.search(name) or not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in _SECRET_PATTERNS.items():
            for match in pattern.finditer(text):
                if "private key" in label and "sentinel" in match.group(0):
                    continue
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{name}:{line} ({label})")  # never the value
    assert found == []


# --- pinned downloads ----------------------------------------------------------------------------

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _urls(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "url" and isinstance(value, str):
                yield value
            else:
                yield from _urls(value)
    elif isinstance(node, list):
        for item in node:
            yield from _urls(item)


def test_every_manifest_url_is_https():
    for manifest in MANIFESTS.glob("*.yaml"):
        for url in _urls(yaml.safe_load(manifest.read_text(encoding="utf-8"))):
            assert url.startswith("https://"), (manifest.name, url)


def test_every_shipped_artifact_and_model_file_carries_a_sha256_and_a_bare_filename():
    artifacts = yaml.safe_load((MANIFESTS / "artifacts.yaml").read_text(encoding="utf-8"))
    records = artifacts["artifacts"]
    models = yaml.safe_load((MANIFESTS / "models.yaml").read_text(encoding="utf-8"))
    records = [*records, *(_files(models))]
    assert records
    for record in records:
        assert _SHA256.match(str(record.get("sha256", ""))), record.get("filename")
        assert not set("/\\:") & set(record["filename"]), record["filename"]
        assert str(record["url"]).startswith("https://")


def _files(node):
    if isinstance(node, dict):
        if "sha256" in node and "url" in node:
            yield node
        for value in node.values():
            yield from _files(value)
    elif isinstance(node, list):
        for item in node:
            yield from _files(item)


def test_model_urls_on_huggingface_are_pinned_to_a_commit_not_a_branch():
    models = yaml.safe_load((MANIFESTS / "models.yaml").read_text(encoding="utf-8"))
    for record in _files(models):
        url = record["url"]
        if "huggingface.co" in url:
            assert re.search(r"/resolve/[0-9a-f]{40}/", url), url


def test_every_git_dependency_is_pinned_to_a_full_commit():
    repositories = yaml.safe_load((MANIFESTS / "repositories.yaml").read_text(encoding="utf-8"))

    def walk(node):
        if isinstance(node, dict):
            if "url" in node and str(node["url"]).endswith(".git"):
                yield node
            for value in node.values():
                yield from walk(value)

    entries = list(walk(repositories))
    assert entries
    for entry in entries:
        assert _COMMIT.match(str(entry.get("commit", ""))), entry["url"]


def test_python_packages_are_pinned_to_an_exact_version():
    packages = yaml.safe_load((MANIFESTS / "packages.yaml").read_text(encoding="utf-8"))
    for record in packages["packages"].values():
        assert re.fullmatch(r"\d+(\.\d+)*", str(record["version"])), record


# --- credentials go to their own host only -----------------------------------------------------


@pytest.fixture
def tokens(monkeypatch):
    monkeypatch.setattr(civitai_auth, "load_token", lambda: ("civ-token", "test"))
    monkeypatch.setattr(huggingface_auth, "load_token", lambda: ("hf-token", "test"))


@pytest.mark.parametrize(
    "url",
    [
        "http://huggingface.co/file",
        "https://huggingface.co.attacker.invalid/file",
        "https://huggingface.co@attacker.invalid/file",
        "https://attacker.invalid/huggingface.co",
        "https://civitai.com.attacker.invalid/file",
        "https://user@civitai.com.attacker.invalid/file",
    ],
)
def test_a_credential_is_never_sent_to_another_host(tokens, url):
    assert huggingface_auth.request_headers(url) == {}
    assert civitai_auth.request_headers(url) == {}


def test_a_credential_is_sent_to_its_own_https_host(tokens):
    assert huggingface_auth.request_headers("https://huggingface.co/x") == {
        "Authorization": "Bearer hf-token"
    }
    assert civitai_auth.request_headers("https://civitai.com/x") == {
        "Authorization": "Bearer civ-token"
    }


# --- archive extraction ------------------------------------------------------------------------


def test_the_ollama_zip_guard_refuses_a_member_that_leaves_the_folder(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escaped.txt", "x")
    with pytest.raises(ValueError, match="outside"):
        ollama._extract_zip(archive, tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()


def test_the_python_runtime_tar_guard_refuses_traversal_and_links(tmp_path):
    traversal = tmp_path / "t.tar"
    with tarfile.open(traversal, "w") as bundle:
        info = tarfile.TarInfo("../escaped.txt")
        info.size = 1
        bundle.addfile(info, fileobj=__import__("io").BytesIO(b"x"))
    with pytest.raises(ValueError, match="outside"):
        python_runtime._extract_tar(traversal, tmp_path / "out")
    link = tmp_path / "l.tar"
    with tarfile.open(link, "w") as bundle:
        info = tarfile.TarInfo("python/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        bundle.addfile(info)
    with pytest.raises(ValueError, match="link"):
        python_runtime._extract_tar(link, tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()


def test_the_pod_session_extraction_refuses_traversal_and_links(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "pod_session_audit", ROOT / "scripts" / "pod_session.py"
    )
    pod_session = importlib.util.module_from_spec(spec)
    sys.modules["pod_session_audit"] = pod_session
    spec.loader.exec_module(pod_session)
    for name, kind in (("../escaped.txt", tarfile.REGTYPE), ("data/link", tarfile.SYMTYPE)):
        archive = tmp_path / "s.tar"
        with tarfile.open(archive, "w") as bundle:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
            bundle.addfile(info)
        with pytest.raises(pod_session.PodError):
            pod_session.extract(archive, tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()


# --- session export ----------------------------------------------------------------------------

_SECRET_COLUMN = re.compile(r"pass|hash(ed)?_|api_?key|secret|token|oauth|session|cookie|auth")


def test_the_session_export_copies_only_named_columns_and_no_credential_ones():
    for schema in (export._WEBUI, export._NATIVE, export._CALIBRATION):
        for table, columns in schema.items():
            assert table not in {"user", "auth", "api_key", "oauth_session", "token"}
            for column in columns:
                assert not _SECRET_COLUMN.search(column), (table, column)
    assert set(export._WEBUI["config"]) == {"key", "value", "updated_at"}


def test_the_session_export_redacts_a_token_a_hash_and_a_private_key():
    text = export._safe_text(
        'Authorization: Bearer abc.def.ghi password="hunter2hunter2" '
        "$2b$12$abcdefghijklmnopqrstuuABCDEFGHIJKLMNOPQRSTUVWXYZ0123 "
        "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"
    )
    for leaked in ("abc.def.ghi", "hunter2hunter2", "$2b$12$", "MIIEvQIBADAN"):
        assert leaked not in text


# --- the guest link ----------------------------------------------------------------------------


def test_the_guest_link_is_documented_as_open_to_anyone():
    assert "Anyone with the link can create an account" in linux_share.__doc__
    runbook = (ROOT / "docs" / "testing" / "runbook-linux-1.17.md").read_text(encoding="utf-8")
    assert "qualquer pessoa com ele pode criar conta" in runbook


# --- reference search: addresses that smuggle a private IPv4 -----------------------------------


@pytest.fixture
def ns(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return load_module()


@pytest.mark.parametrize(
    "address",
    ["64:ff9b::a00:1", "64:ff9b::7f00:1", "2002:7f00:1::", "::127.0.0.1", "fec0::1"],
)
def test_an_address_that_embeds_a_private_ipv4_is_refused(ns, address):
    ns["_resolve"] = lambda _host: [address]
    with pytest.raises(ns["FetchError"]):
        ns["public_addresses"]("tricky.example")


def test_an_address_that_embeds_a_public_ipv4_is_still_accepted(ns):
    ns["_resolve"] = lambda _host: ["64:ff9b::808:808"]
    assert ns["public_addresses"]("ok.example") == ["8.8.8.8"]
