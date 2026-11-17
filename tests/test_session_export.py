"""Synthetic-only session evidence: no service, GPU, source photos or model weights."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import struct
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_ai_stack import session_export
from local_ai_stack.layout import StackLayout
from local_ai_stack.session_export import SessionExportError, export_session, verify_session_archive

# A synthetic one-pixel PNG, never decoded or displayed by these tests.
IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
MASK = IMAGE + b"synthetic-mask-evidence"
PROMPT = "Paint a token-sized red patch; preserve the face. Não corte a imagem."
USER = "synthetic-user"
CHAT = "synthetic-chat"
USER_HASH = hashlib.sha256(USER.encode()).hexdigest()
CHAT_HASH = hashlib.sha256(CHAT.encode()).hexdigest()
NATIVE = f"data/open-webui/pepito-native/{USER_HASH}/{CHAT_HASH}.sqlite3"
CALIBRATION = "data/open-webui/pepito-calibration/calibration.sqlite3.json"


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _calibration_module():
    path = Path(__file__).resolve().parents[1] / "assets" / "open-webui" / "pepito_calibration.py"
    spec = importlib.util.spec_from_file_location("session_export_calibration_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def evidence(tmp_path):
    layout = StackLayout.from_root(tmp_path / "stack")
    layout.ensure()
    image = layout.open_webui_data / "uploads" / "original.png"
    _write(image, IMAGE)
    _write(layout.comfyui_data / "input" / "mask.png", MASK)
    _write(layout.comfyui_data / "output" / "result.png", IMAGE + b"result")
    _write(layout.comfyui_app / "temp" / "legacy-preview.png", IMAGE + b"preview")
    connection = sqlite3.connect(layout.open_webui_data / "webui.db")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        "CREATE TABLE chat(id TEXT PRIMARY KEY,user_id TEXT,title TEXT,chat JSON,meta JSON,share_id TEXT,auth_secret TEXT);"
        "CREATE TABLE chat_message(id TEXT,chat_id TEXT,user_id TEXT,role TEXT,content JSON,files JSON,model_id TEXT);"
        "CREATE TABLE file(id TEXT,user_id TEXT,filename TEXT,path TEXT,data JSON,meta JSON);"
        "CREATE TABLE chatidfile(id TEXT,chat_id TEXT,file_id TEXT,user_id TEXT);"
        "CREATE TABLE model(id TEXT,name TEXT,base_model_id TEXT,meta JSON,api_key TEXT);"
        "CREATE TABLE auth(id TEXT,password TEXT,api_key TEXT);"
        "CREATE TABLE user(id TEXT,name TEXT,oauth_sub TEXT);"
        "CREATE TABLE session(id TEXT,token TEXT);"
        "CREATE TABLE config(key TEXT PRIMARY KEY,value JSON,updated_at INTEGER);"
    )
    conversation = {
        "history": {
            "currentId": "m1",
            "messages": {
                "m1": {
                    "id": "m1",
                    "role": "user",
                    "content": PROMPT,
                    "files": [{"id": "f1", "url": "/api/v1/files/f1/content"}],
                }
            },
        },
        "models": ["capella-image"],
    }
    connection.execute(
        "INSERT INTO chat VALUES (?,?,?,?,?,?,?)",
        (
            CHAT,
            USER,
            "Synthetic edition",
            json.dumps(conversation),
            "{}",
            "private-share-sentinel",
            "column-secret-sentinel",
        ),
    )
    connection.execute(
        "INSERT INTO chat_message VALUES (?,?,?,?,?,?,?)",
        ("m1", CHAT, USER, "user", json.dumps(PROMPT), '[{"id":"f1"}]', "capella-image"),
    )
    connection.execute(
        "INSERT INTO file VALUES (?,?,?,?,?,?)",
        (
            "f1",
            USER,
            "original.png",
            str(image),
            '{"content":"Synthetic OCR"}',
            '{"content_type":"image/png"}',
        ),
    )
    connection.execute("INSERT INTO chatidfile VALUES (?,?,?,?)", ("link1", CHAT, "f1", USER))
    connection.execute(
        "INSERT INTO model VALUES (?,?,?,?,?)",
        (
            "capella-image",
            "Capella Image",
            "qwen",
            '{"apiKey":"model-meta-secret-sentinel","description":"edition"}',
            "model-column-secret-sentinel",
        ),
    )
    connection.execute(
        "INSERT INTO auth VALUES (?,?,?)",
        (USER, "$2b$12$hash-secret-sentinel", "auth-key-secret-sentinel"),
    )
    connection.execute(
        "INSERT INTO user VALUES (?,?,?)",
        (USER, "account-name-secret-sentinel", "oauth-secret-sentinel"),
    )
    connection.execute("INSERT INTO session VALUES (?,?)", ("session", "session-secret-sentinel"))
    connection.execute(
        "INSERT INTO config VALUES (?,?,?)",
        ("openai.api_configs", '{"APIkeys":["config-secret-sentinel"]}', 1),
    )
    connection.execute(
        "INSERT INTO config VALUES (?,?,?)",
        ("image_generation.model", '"current-edition-model"', 2),
    )
    # Real Open WebUI 0.12 data: SQLite stores JSON numbers in a JSON column as integers.
    connection.execute("INSERT INTO config VALUES (?,?,?)", ("image_generation.enable", 1, 3))
    connection.commit()

    native_path = layout.root / NATIVE
    native_path.parent.mkdir(parents=True)
    with sqlite3.connect(native_path) as native:
        native.executescript(
            "CREATE TABLE gallery(id TEXT PRIMARY KEY,message_id TEXT NOT NULL,entries TEXT NOT NULL);"
            "CREATE TABLE image(number INTEGER PRIMARY KEY,file_id TEXT NOT NULL,message_id TEXT NOT NULL,entry TEXT NOT NULL);"
            "CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
        )
        entry = {
            "number": 1,
            "url": "/api/v1/files/f1/content",
            "origin": "generated",
            "caption": PROMPT,
        }
        native.execute(
            "INSERT INTO gallery VALUES (?,?,?)", ("gallery-1", "m1", json.dumps([entry]))
        )
        native.execute("INSERT INTO image VALUES (?,?,?,?)", (1, "f1", "m1", json.dumps(entry)))
        draft = {
            "number": 1,
            "text_hash": hashlib.sha256(PROMPT.encode()).hexdigest(),
            "expires_at": 2000000000,
            "mask_png": "data:image/png;base64," + base64.b64encode(MASK).decode(),
        }
        native.executemany(
            "INSERT INTO state VALUES (?,?)",
            [("current", "1"), ("selection", "1"), ("draft", json.dumps(draft))],
        )
    _write(native_path.parent / "groups" / "gallery-1", CHAT.encode())

    store = _calibration_module()
    facts = {
        "family": "krea2",
        "workflow": "edit",
        "change": "strong",
        "strength": 1.0,
        "fidelity": "auto",
        "request_id": "r1",
        "source_numbers": [1],
        "measurement": 10.0,
        "requested_count": 1,
        "status": "completed",
        "delivered": True,
        "output_number": 2,
    }
    store.register_attempt(layout.open_webui_data, USER, CHAT, "attempt1", facts)
    store.record_feedback(layout.open_webui_data, USER, CHAT, "attempt1", "accepted")
    _write(
        layout.open_webui_data / "pepito-image-stacks.json",
        json.dumps(
            {
                "capella-image": {
                    "model": "qwen-image",
                    "workflow": {
                        "1": {"class_type": "LoadImage", "inputs": {"image": "original.png"}}
                    },
                }
            }
        ).encode(),
    )
    _write(
        layout.state / "config.yaml",
        b"schema_version: 1\nprofile:\n  id: capella\nstack:\n  id: capella\n",
    )
    try:
        yield SimpleNamespace(layout=layout, writer=connection, image=image)
    finally:
        connection.close()


def _load(archive: zipfile.ZipFile, name: str):
    return json.loads(archive.read(name))


def _manifest(archive: zipfile.ZipFile):
    manifest = _load(archive, "manifest.json")
    assert {item["path"] for item in manifest["members"]} | {"manifest.json"} == set(
        archive.namelist()
    )
    for item in manifest["members"]:
        data = archive.read(item["path"])
        assert item["size"] == len(data)
        assert item["sha256"] == hashlib.sha256(data).hexdigest()
    assert manifest["limits"]["truncation"] is False
    assert manifest["limits"]["byte_limit"] == session_export.MAX_TOTAL_UNCOMPRESSED_BYTES
    assert manifest["limits"]["manifest_byte_limit"] == session_export.MAX_MANIFEST_BYTES
    assert manifest["limits"]["member_count_limit"] == session_export.MAX_ARCHIVE_MEMBERS
    return manifest


def test_roundtrip_preserves_conversation_original_result_mask_and_native_calibration(
    evidence, tmp_path
):
    destination = tmp_path / "session.zip"
    assert export_session(evidence.layout, destination) == destination
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(destination) as archive:
        manifest = _manifest(archive)
        archive.extractall(extracted)
        webui = _load(archive, "data/open-webui/webui.db.json")["tables"]
        assert webui["chat"][0]["chat"]["history"]["messages"]["m1"]["content"] == PROMPT
        assert webui["chat_message"][0]["content"] == PROMPT
        assert webui["file"][0]["exported_asset"] == "data/open-webui/uploads/original.png"
        assert webui["chatidfile"][0]["file_id"] == "f1"
        assert webui["model"][0]["id"] == "capella-image"
        native = _load(archive, NATIVE + ".json")["tables"]
        state = {row["key"]: row["value"] for row in native["state"]}
        assert state["selection"] == 1
        assert native["gallery"][0]["entries"][0]["caption"] == PROMPT
        assert native["image"][0]["file_id"] == "f1"
        assert base64.b64decode(state["draft"]["mask_png"].split(",", 1)[1]) == MASK
        assert archive.read(state["draft"]["exported_mask"]) == MASK
        assert (
            archive.read(f"data/open-webui/pepito-native/{USER_HASH}/groups/gallery-1")
            == CHAT.encode()
        )
        calibration = _load(archive, CALIBRATION)["tables"]
        assert calibration["attempt"][0]["attempt_id"] == "attempt1"
        assert calibration["attempt"][0]["facts"]["workflow"] == "edit"
        assert calibration["feedback"][0]["verdict"] == "accepted"
        assert calibration["calibration"][0]["accepted"] == 1
        assert _load(archive, "state/config.yaml.json")["profile"]["id"] == "capella"
        assert (
            _load(archive, "data/open-webui/pepito-image-stacks.json")["capella-image"]["model"]
            == "qwen-image"
        )
        assert any(item["reason"] == "excluded-config-record" for item in manifest["omissions"])
        assert webui["config"] == [
            {"key": "image_generation.enable", "value": 1, "updated_at": 3},
            {"key": "image_generation.model", "value": "current-edition-model", "updated_at": 2},
        ]
        assert not any(
            name.endswith((".db", ".sqlite3", "-wal", "-shm")) for name in archive.namelist()
        )
    assert (extracted / "data/open-webui/uploads/original.png").read_bytes() == IMAGE
    assert (extracted / "data/comfyui/input/mask.png").read_bytes() == MASK
    assert (extracted / "data/comfyui/output/result.png").read_bytes() == IMAGE + b"result"
    assert (extracted / "apps/comfyui/temp/legacy-preview.png").read_bytes() == IMAGE + b"preview"


def test_live_wal_commits_are_visible_without_checkpoint(evidence, tmp_path):
    evidence.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    evidence.writer.execute(
        "INSERT INTO chat_message VALUES (?,?,?,?,?,?,?)",
        (
            "wal-only",
            CHAT,
            USER,
            "assistant",
            json.dumps("Live WAL response"),
            "[]",
            "capella-image",
        ),
    )
    evidence.writer.commit()
    assert Path(str(evidence.layout.open_webui_data / "webui.db") + "-wal").stat().st_size > 0
    destination = export_session(evidence.layout, tmp_path / "wal.zip")
    with zipfile.ZipFile(destination) as archive:
        messages = _load(archive, "data/open-webui/webui.db.json")["tables"]["chat_message"]
        assert any(
            row["id"] == "wal-only" and row["content"] == "Live WAL response" for row in messages
        )


def test_all_webui_tables_share_one_read_snapshot(evidence, tmp_path, monkeypatch):
    original = session_export._Collector.file_reference
    wrote = []

    def add_concurrent_commit(collector, row):
        if not wrote:
            evidence.writer.execute(
                "INSERT INTO chat_message VALUES (?,?,?,?,?,?,?)",
                (
                    "after-snapshot",
                    CHAT,
                    USER,
                    "assistant",
                    json.dumps("later"),
                    "[]",
                    "capella-image",
                ),
            )
            evidence.writer.execute(
                "INSERT INTO chatidfile VALUES (?,?,?,?)", ("after-link", CHAT, "f1", USER)
            )
            evidence.writer.commit()
            wrote.append(True)
        original(collector, row)

    monkeypatch.setattr(session_export._Collector, "file_reference", add_concurrent_commit)
    destination = export_session(evidence.layout, tmp_path / "snapshot.zip")
    assert wrote
    with zipfile.ZipFile(destination) as archive:
        tables = _load(archive, "data/open-webui/webui.db.json")["tables"]
        assert not any(row["id"] == "after-snapshot" for row in tables["chat_message"])
        assert not any(row["id"] == "after-link" for row in tables["chatidfile"])


def test_credentials_auth_hashes_signed_urls_and_environment_are_excluded(
    evidence, tmp_path, monkeypatch
):
    secret_values = [
        "basic-secret-sentinel",
        "bearer-secret-sentinel",
        "cookie-secret-sentinel",
        "log-secret-sentinel",
        "signed-secret-sentinel",
        "google-secret-sentinel",
        "azure-secret-sentinel",
        "flag-secret-sentinel",
        "nested-secret-sentinel",
        "private-key-secret-sentinel",
        "env-secret-sentinel",
        "runtime-secret-sentinel",
    ]
    _write(
        evidence.layout.logs / "open-webui.log",
        (
            "Authorization: Basic basic-secret-sentinel\nAuthorization: Bearer bearer-secret-sentinel\n"
            "Cookie: arbitrary=cookie-secret-sentinel; another=value\nAPI_KEY=log-secret-sentinel\n"
            "https://example.test/a.png?X-Amz-Signature=signed-secret-sentinel&X-Goog-Credential=google-secret-sentinel&sig=azure-secret-sentinel\n"
            '--token flag-secret-sentinel\n{"password": "nested-secret-sentinel"}\n'
            "-----BEGIN PRIVATE KEY-----\nprivate-key-secret-sentinel\n-----END PRIVATE KEY-----\n"
            + PROMPT
        ).encode(),
    )
    _write(
        evidence.layout.open_webui_data / "open-webui-workflow.json",
        json.dumps(
            {
                "apiKeys": ["config-array-secret-sentinel"],
                "Authorization": "config-auth-secret-sentinel",
                "nested": {"client_secret": "client-secret-sentinel"},
                "encoded": '{"APIkeys":["encoded-secret-sentinel"]}',
                "prompt": PROMPT,
            }
        ).encode(),
    )
    _write(evidence.layout.open_webui_data / "open-webui.env", b"API_KEY=env-secret-sentinel")
    _write(evidence.layout.open_webui_data / ".webui_secret_key", b"webui-secret-sentinel")
    _write(evidence.layout.runtime / "supervisor-token", b"runtime-secret-sentinel")
    _write(evidence.layout.open_webui_data / "uploads" / ".env", b"env-secret-sentinel")
    _write(
        evidence.layout.open_webui_data / "uploads" / "private.pem", b"private-key-secret-sentinel"
    )
    monkeypatch.setenv("SESSION_EXPORT_PRIVATE_TOKEN", "process-environment-secret-sentinel")
    destination = export_session(evidence.layout, tmp_path / "redacted.zip")
    with zipfile.ZipFile(destination) as archive:
        _manifest(archive)
        payload = b"\n".join(archive.read(name) for name in archive.namelist())
        secret_values += [
            "private-share-sentinel",
            "column-secret-sentinel",
            "model-meta-secret-sentinel",
            "model-column-secret-sentinel",
            "hash-secret-sentinel",
            "auth-key-secret-sentinel",
            "account-name-secret-sentinel",
            "oauth-secret-sentinel",
            "session-secret-sentinel",
            "config-secret-sentinel",
            "config-array-secret-sentinel",
            "config-auth-secret-sentinel",
            "client-secret-sentinel",
            "encoded-secret-sentinel",
            "webui-secret-sentinel",
            "process-environment-secret-sentinel",
        ]
        for secret in secret_values:
            assert secret.encode() not in payload
        assert PROMPT.encode() in payload


def test_only_known_media_roots_are_collected_and_models_caches_are_omitted(evidence, tmp_path):
    for path in (
        evidence.layout.models / "secret-model.safetensors",
        evidence.layout.open_webui_data / "cache" / "image.png",
        evidence.layout.open_webui_data / "uploads" / "nested" / "weights.gguf",
        evidence.layout.open_webui_data / "uploads" / "cache" / "cached.png",
        evidence.layout.apps / "foreign" / "private.png",
        evidence.layout.state / "unowned.json",
    ):
        _write(path, b"excluded-root-secret-sentinel")
    destination = export_session(evidence.layout, tmp_path / "ownership.zip")
    with zipfile.ZipFile(destination) as archive:
        manifest = _manifest(archive)
        assert not any(
            b"excluded-root-secret-sentinel" in archive.read(name) for name in archive.namelist()
        )
        assert any(
            item["reason"] == "excluded-cache-or-model-directory" for item in manifest["omissions"]
        )
        assert any(
            item["reason"] == "excluded-secret-database-or-model-file"
            for item in manifest["omissions"]
        )


def test_unowned_file_metadata_does_not_read_arbitrary_paths(evidence, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside-secret-sentinel")
    evidence.writer.execute("UPDATE file SET path=?", (str(outside),))
    evidence.writer.commit()
    destination = export_session(evidence.layout, tmp_path / "unowned.zip")
    with zipfile.ZipFile(destination) as archive:
        manifest = _manifest(archive)
        row = _load(archive, "data/open-webui/webui.db.json")["tables"]["file"][0]
        assert row["exported_asset"] is None
        assert row["path"] == "[unowned storage]"
        assert any(item["reason"] == "unowned-file-reference" for item in manifest["omissions"])
        assert not any(
            b"outside-secret-sentinel" in archive.read(name) for name in archive.namelist()
        )


@pytest.mark.parametrize("placement", ["media", "database", "ancestor"])
def test_symlink_escape_aborts_without_publishing(evidence, tmp_path, placement):
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside / "escape.png", b"escape-secret-sentinel")
    if placement == "media":
        link = evidence.layout.open_webui_data / "uploads" / "escape"
        target = outside
    elif placement == "database":
        # The native ledger has no live writer and can be moved without touching webui.db.
        link = evidence.layout.root / NATIVE
        target = outside / "ledger.sqlite3"
        try:
            link.replace(target)
        except PermissionError:
            pytest.skip("Windows keeps the synthetic ledger open; Linux covers this placement")
    else:
        link = evidence.layout.comfyui_data / "input"
        (link / "mask.png").unlink()
        link.rmdir()
        target = outside
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("This host does not permit synthetic symlinks")
    destination = tmp_path / "blocked.zip"
    with pytest.raises(SessionExportError, match="Symlinks|reparse"):
        export_session(evidence.layout, destination)
    assert not destination.exists()
    assert not tuple(tmp_path.glob(".session-export-*.partial"))


def test_windows_junction_attribute_is_rejected(evidence, tmp_path, monkeypatch):
    junction = evidence.layout.open_webui_data / "uploads" / "junction"
    junction.mkdir()
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == junction:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(SessionExportError, match="reparse"):
        export_session(evidence.layout, tmp_path / "junction.zip")
    assert not (tmp_path / "junction.zip").exists()


def test_hardlinked_media_is_not_treated_as_owned(evidence, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"hardlink-secret-sentinel")
    try:
        os.link(outside, evidence.layout.open_webui_data / "uploads" / "hardlink.png")
    except OSError:
        pytest.skip("This host does not support hardlinks")
    with pytest.raises(SessionExportError, match="hardlinked"):
        export_session(evidence.layout, tmp_path / "hardlink.zip")


@pytest.mark.parametrize(
    "failure",
    ["missing-webui", "broken-native", "broken-calibration", "missing-original", "traversal"],
)
def test_core_failure_preserves_previous_archive_and_removes_temporary(evidence, tmp_path, failure):
    if failure == "missing-webui":
        evidence.writer.close()
        (evidence.layout.open_webui_data / "webui.db").unlink()
    elif failure == "broken-native":
        (evidence.layout.root / NATIVE).write_bytes(b"not SQLite")
    elif failure == "broken-calibration":
        (
            evidence.layout.open_webui_data / "pepito-calibration" / "calibration.sqlite3"
        ).write_bytes(b"not SQLite")
    elif failure == "missing-original":
        evidence.image.unlink()
    else:
        evidence.writer.execute("UPDATE file SET path='uploads/../../outside.png'")
        evidence.writer.commit()
    destination = tmp_path / "previous.zip"
    destination.write_bytes(b"previous archive bytes")
    with pytest.raises(SessionExportError):
        export_session(evidence.layout, destination)
    assert destination.read_bytes() == b"previous archive bytes"
    assert not tuple(tmp_path.glob(".session-export-*.partial"))


def test_optional_unreadable_text_is_explicit_without_echoing_secret_parser_lines(
    evidence, tmp_path
):
    _write(evidence.layout.state / "config.yaml", b"password: [parser-secret-sentinel\n")
    _write(evidence.layout.logs / "comfyui.log", b"\xff\xfeinvalid UTF-8")
    destination = export_session(evidence.layout, tmp_path / "omissions.zip")
    with zipfile.ZipFile(destination) as archive:
        manifest = _manifest(archive)
        assert manifest["status"] == "complete-with-omissions"
        assert {row["source"] for row in manifest["errors"]} == {
            "state/config.yaml",
            "logs/comfyui.log",
        }
        assert b"parser-secret-sentinel" not in archive.read("manifest.json")
        assert any(row["reason"] == "not-present" for row in manifest["omissions"])


def test_the_filter_trace_and_its_rotation_are_exported_redacted_record_by_record(
    evidence, tmp_path
):
    directory = evidence.layout.open_webui_data / "pepito-trace"
    records = [
        {
            "ts": "2026-10-31T23:31:48+00:00",
            "chat_id": CHAT,
            "turn": "t1",
            "event": "turn_start",
            "user_text": PROMPT,
            "headers": {"Authorization": "Bearer trace-header-secret-sentinel"},
        },
        {
            "ts": "2026-10-31T23:32:03+00:00",
            "chat_id": CHAT,
            "turn": "t1",
            "event": "error",
            "message": "Authorization: Bearer trace-message-secret-sentinel",
            "api_key": "trace-key-secret-sentinel",
        },
    ]
    current = "".join(json.dumps(record) + "\n" for record in records)
    # The filter was appending when the export read the file: the last record is cut.
    current += '{"event": "router_result", "token": "trace-cut-secret-sentinel'
    _write(directory / "turns.jsonl", current.encode())
    _write(
        directory / "turns.jsonl.1",
        (json.dumps({"event": "turn_end", "turn": "t0", "total_seconds": 12.5}) + "\n").encode(),
    )
    destination = export_session(evidence.layout, tmp_path / "trace.zip")
    manifest = verify_session_archive(destination)
    kinds = {item["path"]: item["kind"] for item in manifest["members"]}
    assert kinds["data/open-webui/pepito-trace/turns.jsonl"] == "trace"
    assert kinds["data/open-webui/pepito-trace/turns.jsonl.1"] == "trace"
    with zipfile.ZipFile(destination) as archive:
        lines = archive.read("data/open-webui/pepito-trace/turns.jsonl").decode().splitlines()
        rotated = archive.read("data/open-webui/pepito-trace/turns.jsonl.1").decode()
    assert len(lines) == 3
    first, second = json.loads(lines[0]), json.loads(lines[1])
    assert first["event"] == "turn_start" and first["user_text"] == PROMPT
    assert first["headers"] == "[redacted]"
    assert second["api_key"] == "[redacted]"
    assert "trace-message-secret-sentinel" not in second["message"]
    assert "trace-cut-secret-sentinel" not in lines[2] and "router_result" in lines[2]
    assert json.loads(rotated) == {"event": "turn_end", "turn": "t0", "total_seconds": 12.5}


def test_a_missing_trace_is_an_omission_and_an_oversized_one_aborts_without_truncating(
    evidence, tmp_path, monkeypatch
):
    destination = tmp_path / "trace-budget.zip"
    manifest = verify_session_archive(export_session(evidence.layout, destination))
    assert {"source": "data/open-webui/pepito-trace/turns.jsonl", "reason": "not-present"} in (
        manifest["omissions"]
    )
    total = sum(item["size"] for item in manifest["members"])
    _write(
        evidence.layout.open_webui_data / "pepito-trace" / "turns.jsonl",
        b'{"event": "turn_start"}\n' * (total // 20 + 10),
    )
    monkeypatch.setattr(session_export, "MAX_TOTAL_UNCOMPRESSED_BYTES", total + 10)
    previous = destination.read_bytes()
    with pytest.raises(SessionExportError, match="Trace evidence exceeds"):
        export_session(evidence.layout, destination)
    assert destination.read_bytes() == previous


def test_export_inside_uploads_never_collects_itself_or_its_temporary(evidence):
    destination = evidence.layout.open_webui_data / "uploads" / "session.zip"
    destination.write_bytes(b"previous-export-secret-sentinel")
    export_session(evidence.layout, destination)
    with zipfile.ZipFile(destination) as archive:
        manifest = _manifest(archive)
        assert not any(
            name.endswith("session.zip") or ".partial" in name for name in archive.namelist()
        )
        assert any(row["reason"] == "export-output" for row in manifest["omissions"])
        assert not any(
            b"previous-export-secret-sentinel" in archive.read(name) for name in archive.namelist()
        )


def test_destination_and_layout_traversal_are_rejected(evidence, tmp_path):
    with pytest.raises(SessionExportError, match="traversal"):
        export_session(evidence.layout, tmp_path / ".." / "escape.zip")
    with pytest.raises(SessionExportError, match="traversal"):
        export_session(StackLayout(evidence.layout.root / ".." / "stack"), tmp_path / "escape.zip")


def test_legacy_config_blob_is_never_dumped(evidence, tmp_path):
    evidence.writer.execute("DROP TABLE config")
    evidence.writer.execute("CREATE TABLE config(id INTEGER,data JSON)")
    evidence.writer.execute(
        "INSERT INTO config VALUES (1,?)",
        ('{"api_keys":["legacy-secret-sentinel"],"model":"old"}',),
    )
    evidence.writer.commit()
    destination = export_session(evidence.layout, tmp_path / "legacy.zip")
    with zipfile.ZipFile(destination) as archive:
        manifest = _manifest(archive)
        assert any(
            item["reason"] == "legacy-config-blob-excluded" for item in manifest["omissions"]
        )
        assert not any(
            b"legacy-secret-sentinel" in archive.read(name) for name in archive.namelist()
        )


def test_secret_upload_metadata_does_not_reintroduce_excluded_file_contents(evidence, tmp_path):
    secret = evidence.layout.open_webui_data / "uploads" / ".env"
    secret.write_bytes(b"UNRECOGNIZED_VARIABLE=secret-upload-sentinel")
    evidence.writer.execute(
        "UPDATE file SET filename=?,path=?,data=?",
        (".env", str(secret), '{"content":"UNRECOGNIZED_VARIABLE=secret-upload-sentinel"}'),
    )
    evidence.writer.commit()
    destination = export_session(evidence.layout, tmp_path / "secret-upload.zip")
    with zipfile.ZipFile(destination) as archive:
        manifest = _manifest(archive)
        assert any(
            item["reason"] == "file-metadata-excluded-by-policy" for item in manifest["omissions"]
        )
        assert not any(
            b"secret-upload-sentinel" in archive.read(name) for name in archive.namelist()
        )


def test_publish_failure_leaves_previous_archive_intact(evidence, tmp_path, monkeypatch):
    destination = tmp_path / "previous.zip"
    destination.write_bytes(b"previous bytes")

    def refuse_replace(source, target):
        raise PermissionError("sensitive-path-or-token-must-not-be-echoed")

    monkeypatch.setattr(session_export.os, "replace", refuse_replace)
    with pytest.raises(SessionExportError) as error:
        export_session(evidence.layout, destination)
    assert "sensitive-path-or-token" not in str(error.value)
    assert destination.read_bytes() == b"previous bytes"
    assert not tuple(tmp_path.glob(".session-export-*.partial"))


def test_external_storage_is_explicit_and_signed_url_credentials_are_absent(evidence, tmp_path):
    evidence.writer.execute(
        "UPDATE file SET path=?",
        ("https://example.test/image.png?X-Amz-Signature=external-signature-sentinel",),
    )
    evidence.writer.commit()
    destination = export_session(evidence.layout, tmp_path / "remote.zip")
    with zipfile.ZipFile(destination) as archive:
        manifest = _manifest(archive)
        row = _load(archive, "data/open-webui/webui.db.json")["tables"]["file"][0]
        assert row["exported_asset"] is None
        assert row["path"] == "[external storage]"
        assert any(
            item["reason"] == "external-file-reference-not-fetched"
            for item in manifest["omissions"]
        )
        assert not any(
            b"external-signature-sentinel" in archive.read(name) for name in archive.namelist()
        )


def test_verify_archive_returns_manifest_without_extracting(evidence, tmp_path, monkeypatch):
    destination = export_session(evidence.layout, tmp_path / "verified.zip")
    with zipfile.ZipFile(destination) as archive:
        expected = _load(archive, "manifest.json")
    before = set(tmp_path.iterdir())

    def forbid_extraction(*args, **kwargs):
        raise AssertionError("Archive verification must not extract files")

    monkeypatch.setattr(zipfile.ZipFile, "extract", forbid_extraction)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", forbid_extraction)
    assert verify_session_archive(destination) == expected
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    "damage",
    [
        "missing-manifest",
        "invalid-json",
        "schema",
        "incomplete-status",
        "missing-declarations",
        "duplicate-json-key",
        "duplicate-declaration",
        "missing-payload",
        "unexpected-payload",
        "hash",
        "size",
        "bool-size",
        "negative-size",
        "invalid-hash",
        "unsafe-declaration",
        "nonfinite-json",
        "symlink-entry",
        "directory-entry",
        "traversal",
        "absolute",
        "backslash",
        "drive",
        "missing-errors",
        "invalid-member",
    ],
)
def test_verify_rejects_invalid_manifest_members_and_integrity(evidence, tmp_path, damage):
    original = export_session(evidence.layout, tmp_path / "original.zip")
    with zipfile.ZipFile(original) as archive:
        payloads = {info.filename: archive.read(info) for info in archive.infolist()}
    manifest = json.loads(payloads.pop("manifest.json"))
    first = manifest["members"][0]
    extra_info = None
    raw_manifest = None
    if damage == "invalid-json":
        raw_manifest = b'{"private":"manifest-secret-sentinel"'
    elif damage == "schema":
        manifest["schema_version"] = 999
    elif damage == "incomplete-status":
        manifest["status"] = "failed"
    elif damage == "missing-declarations":
        manifest.pop("members")
    elif damage == "duplicate-json-key":
        raw_manifest = b'{"schema_version":1,' + json.dumps(manifest).encode()[1:]
    elif damage == "duplicate-declaration":
        manifest["members"].append(dict(first))
    elif damage == "missing-payload":
        payloads.pop(first["path"])
    elif damage == "unexpected-payload":
        payloads["unexpected.txt"] = b"unexpected"
    elif damage == "hash":
        first["sha256"] = "0" * 64
    elif damage == "size":
        first["size"] += 1
    elif damage == "bool-size":
        first["size"] = True
    elif damage == "negative-size":
        first["size"] = -1
    elif damage == "invalid-hash":
        first["sha256"] = "not-a-sha256"
    elif damage == "unsafe-declaration":
        first["path"] = "../outside"
    elif damage == "nonfinite-json":
        manifest["untrusted"] = float("nan")
    elif damage == "missing-errors":
        manifest.pop("errors")
    elif damage == "invalid-member":
        manifest["members"][0] = "not-an-object"
    elif damage in {"symlink-entry", "directory-entry"}:
        extra_info = zipfile.ZipInfo("link" if damage == "symlink-entry" else "directory/")
        extra_info.create_system = 3
        extra_info.external_attr = (0o120777 if damage == "symlink-entry" else 0o040755) << 16
    elif damage in {"traversal", "absolute", "backslash", "drive"}:
        payloads[
            {
                "traversal": "../outside",
                "absolute": "/outside",
                "backslash": "data\\outside",
                "drive": "C:/outside",
            }[damage]
        ] = b"unsafe"
    damaged = tmp_path / "damaged.zip"
    with zipfile.ZipFile(damaged, "w") as archive:
        for name, data in payloads.items():
            archive.writestr(name, data)
        if damage != "missing-manifest":
            archive.writestr(
                "manifest.json",
                raw_manifest if raw_manifest is not None else json.dumps(manifest).encode(),
            )
        if extra_info is not None:
            archive.writestr(extra_info, b"outside")
    with pytest.raises(SessionExportError) as error:
        verify_session_archive(damaged)
    assert "manifest-secret-sentinel" not in str(error.value)


@pytest.mark.parametrize("duplicate", ["manifest.json", "data/open-webui/webui.db.json"])
def test_verify_rejects_duplicate_zip_entries(evidence, tmp_path, duplicate):
    destination = export_session(evidence.layout, tmp_path / "duplicate.zip")
    with zipfile.ZipFile(destination, "a") as archive:
        existing = archive.read(duplicate)
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr(duplicate, existing)
    with pytest.raises(SessionExportError, match="duplicate"):
        verify_session_archive(destination)


def test_verify_rejects_crc_corruption_and_non_zip_files(evidence, tmp_path):
    original = export_session(evidence.layout, tmp_path / "original.zip")
    stored = tmp_path / "corrupt.zip"
    with (
        zipfile.ZipFile(original) as source,
        zipfile.ZipFile(stored, "w", compression=zipfile.ZIP_STORED) as target,
    ):
        for info in source.infolist():
            target.writestr(info.filename, source.read(info))
    raw = bytearray(stored.read_bytes())
    position = raw.find(IMAGE)
    assert position >= 0
    raw[position] ^= 1
    stored.write_bytes(raw)
    with pytest.raises(SessionExportError):
        verify_session_archive(stored)
    invalid = tmp_path / "not-a-zip.zip"
    invalid.write_bytes(b"not a zip")
    with pytest.raises(SessionExportError):
        verify_session_archive(invalid)


def test_native_accepted_origin_and_editor_preferences_survive_with_nested_redaction(
    evidence, tmp_path
):
    preferences = {
        "auto_region": True,
        "editor_model_id": "capella-alternate-editor",
        "comparison_seed": 123456,
        "diagnostics": {"api_key": "preference-secret-sentinel", "note": PROMPT},
    }
    with sqlite3.connect(evidence.layout.root / NATIVE) as connection:
        connection.executemany(
            "INSERT INTO state VALUES (?,?)",
            [("accepted", "1"), ("preferences", json.dumps(preferences))],
        )
    destination = export_session(evidence.layout, tmp_path / "preferences.zip")
    manifest = verify_session_archive(destination)
    with zipfile.ZipFile(destination) as archive:
        native = _load(archive, NATIVE + ".json")["tables"]
        state = {row["key"]: row["value"] for row in native["state"]}
        accepted_image = next(row for row in native["image"] if row["number"] == state["accepted"])
        assert accepted_image["entry"]["origin"] == "generated"
        assert accepted_image["file_id"] == "f1"
        assert state["preferences"]["auto_region"] is True
        assert state["preferences"]["editor_model_id"] == "capella-alternate-editor"
        assert state["preferences"]["comparison_seed"] == 123456
        assert state["preferences"]["diagnostics"]["note"] == PROMPT
        assert state["preferences"]["diagnostics"]["api_key"] != "preference-secret-sentinel"
        assert not any(
            b"preference-secret-sentinel" in archive.read(item["path"])
            for item in manifest["members"]
        )


def test_consumed_mask_and_last_edit_recipe_are_recovered_from_filter_state(evidence, tmp_path):
    directory = evidence.layout.open_webui_data / "pepito-characters"
    stem = hashlib.sha256(CHAT.encode()).hexdigest()[:16]
    state_path = directory / f"{stem}.pictures.json"
    saved = {
        "pictures": [{"number": 1, "url": "/api/v1/files/f1/content", "description": PROMPT}],
        "last_edit": {
            "prompt": PROMPT,
            "sources": ["/api/v1/files/f1/content"],
            "outputs": ["/api/v1/files/f1/content"],
            "mask_png": base64.b64encode(MASK).decode(),
            "credentials": {"token": "filter-secret-sentinel"},
        },
    }
    _write(state_path, json.dumps(saved).encode())
    _write(directory / f"{stem}.json", b'{"derived":["/api/v1/files/f1/content"]}')
    with sqlite3.connect(evidence.layout.root / NATIVE) as connection:
        connection.execute("DELETE FROM state WHERE key='draft'")
    destination = export_session(evidence.layout, tmp_path / "consumed-mask.zip")
    verify_session_archive(destination)
    with zipfile.ZipFile(destination) as archive:
        snapshot = _load(archive, f"data/open-webui/pepito-characters/{stem}.pictures.json")
        assert snapshot["last_edit"]["prompt"] == PROMPT
        assert archive.read(snapshot["last_edit"]["exported_mask"]) == MASK
        assert base64.b64decode(snapshot["last_edit"]["mask_png"]) == MASK
        assert _load(archive, f"data/open-webui/pepito-characters/{stem}.json")["derived"] == [
            "/api/v1/files/f1/content"
        ]
        assert not any(
            b"filter-secret-sentinel" in archive.read(name) for name in archive.namelist()
        )


@pytest.mark.parametrize(
    "limit,value",
    [
        ("MAX_MANIFEST_BYTES", 128),
        ("MAX_ARCHIVE_MEMBERS", 1),
        ("MAX_TOTAL_UNCOMPRESSED_BYTES", 32),
        ("MAX_CENTRAL_DIRECTORY_BYTES", 1),
    ],
)
def test_export_rejects_excess_evidence_without_truncating_or_replacing(
    evidence, tmp_path, monkeypatch, limit, value
):
    monkeypatch.setattr(session_export, limit, value)
    destination = tmp_path / "budget.zip"
    destination.write_bytes(b"previous bytes")
    with pytest.raises(SessionExportError, match="budget"):
        export_session(evidence.layout, destination)
    assert destination.read_bytes() == b"previous bytes"
    assert not tuple(tmp_path.glob(".session-export-*.partial"))


def test_compressed_manifest_budget_is_checked_before_decompression(tmp_path, monkeypatch):
    path = tmp_path / "manifest-bomb.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", b" " * 8192)
    monkeypatch.setattr(session_export, "MAX_MANIFEST_BYTES", 128)

    def forbid_decompression(*args, **kwargs):
        raise AssertionError("Oversized manifest must be rejected before decompression")

    monkeypatch.setattr(zipfile.ZipFile, "open", forbid_decompression)
    with pytest.raises(SessionExportError, match="manifest-byte budget"):
        verify_session_archive(path)


def test_manifest_streaming_budget_is_enforced_even_if_reader_exceeds_advertised_size(
    evidence, tmp_path, monkeypatch
):
    path = export_session(evidence.layout, tmp_path / "manifest-stream.zip")
    with zipfile.ZipFile(path) as archive:
        limit = archive.getinfo("manifest.json").file_size + 1
    monkeypatch.setattr(session_export, "MAX_MANIFEST_BYTES", limit)
    original = zipfile.ZipFile.open

    def larger_manifest(archive, name, *args, **kwargs):
        if getattr(name, "filename", name) == "manifest.json":
            return io.BytesIO(b"x" * (limit + 2))
        return original(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", larger_manifest)
    with pytest.raises(SessionExportError, match="manifest-byte budget"):
        verify_session_archive(path)


def test_aggregate_compression_budget_is_checked_before_any_member_is_read(tmp_path, monkeypatch):
    path = tmp_path / "aggregate-bomb.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", b"{}")
        for number in range(3):
            archive.writestr(f"media-{number}.png", b"x" * 8192)
    assert path.stat().st_size < 8192
    monkeypatch.setattr(session_export, "MAX_TOTAL_UNCOMPRESSED_BYTES", 16384)

    def forbid_decompression(*args, **kwargs):
        raise AssertionError("Aggregate size must be rejected before decompression")

    monkeypatch.setattr(zipfile.ZipFile, "open", forbid_decompression)
    with pytest.raises(SessionExportError, match="uncompressed-byte budget"):
        verify_session_archive(path)


@pytest.mark.parametrize("forged_count", [False, True])
def test_member_count_budget_precedes_zipfile_directory_allocation(
    tmp_path, monkeypatch, forged_count
):
    path = tmp_path / "many-members.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for number in range(3):
            archive.writestr(f"entry-{number}", b"")
    if forged_count:
        raw = bytearray(path.read_bytes())
        position = raw.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", raw, position + 8, 1, 1)
        path.write_bytes(raw)
    monkeypatch.setattr(session_export, "MAX_ARCHIVE_MEMBERS", 2)

    def forbid_zipfile_allocation(*args, **kwargs):
        raise AssertionError("Oversized central directory must not be parsed by ZipFile")

    monkeypatch.setattr(zipfile, "ZipFile", forbid_zipfile_allocation)
    with pytest.raises(SessionExportError, match="member-count"):
        verify_session_archive(path)


def test_zip64_end_records_remain_verifiable(evidence, tmp_path, monkeypatch):
    monkeypatch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 1)
    path = export_session(evidence.layout, tmp_path / "zip64.zip")
    assert b"PK\x06\x06" in path.read_bytes()
    raw = bytearray(path.read_bytes())
    end = raw.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", raw, end + 8, 65535, 65535)
    path.write_bytes(raw)
    assert verify_session_archive(path)["schema_version"] == 1


def test_streaming_aggregate_budget_rejects_reader_exceeding_advertised_total(
    evidence, tmp_path, monkeypatch
):
    path = export_session(evidence.layout, tmp_path / "stream-budget.zip")
    with zipfile.ZipFile(path) as archive:
        limit = sum(info.file_size for info in archive.infolist()) + 1
        first = _load(archive, "manifest.json")["members"][0]["path"]
    monkeypatch.setattr(session_export, "MAX_TOTAL_UNCOMPRESSED_BYTES", limit)
    original = zipfile.ZipFile.open

    def inflated_member(archive, name, *args, **kwargs):
        if getattr(name, "filename", name) == first:
            return io.BytesIO(b"x" * (limit + 2))
        return original(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", inflated_member)
    with pytest.raises(SessionExportError, match="streamed uncompressed-byte budget"):
        verify_session_archive(path)


@pytest.mark.parametrize(
    "comment",
    # Python's zipfile cannot open an archive whose comment embeds an EOCD signature,
    # so verification rejects it regardless; only parseable comments are meaningful here.
    [b"", b"synthetic archive comment"],
)
def test_zip_preflight_reads_eocd_comment_length_from_real_zip_bytes(comment):
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("first.json", b'{"synthetic":true}')
        archive.writestr("second.png", IMAGE)
        archive.comment = comment
    assert session_export._zip_preflight(source) == 2
    assert source.tell() == 0
    with zipfile.ZipFile(source) as archive:
        assert archive.comment == comment
        assert archive.read("second.png") == IMAGE


def test_zip_preflight_rejects_eocd_comment_length_exceeding_available_bytes():
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("synthetic.txt", b"evidence")
    raw = bytearray(source.getvalue())
    end = raw.rfind(b"PK\x05\x06")
    struct.pack_into("<H", raw, end + 20, 1)
    with pytest.raises(SessionExportError):
        session_export._zip_preflight(io.BytesIO(raw))
