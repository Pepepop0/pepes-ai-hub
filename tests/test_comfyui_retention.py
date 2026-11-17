"""SPEC-208: ComfyUI's input and output folders are cleaned of old files the Hub wrote.

The cleanup itself on a temporary folder (new, old, other owner, subfolder, busy queue),
the prefixes against the shipped workflows and the filter's upload names, the profile key,
and the composition: a start and a stop clean, report in ``hub-events.log``, and a profile
without the key deletes nothing.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml
from first_run_support import CAPELLA, capella_yaml, controlled_stack

from local_ai_stack.comfyui_retention import (
    clean_comfyui_folders,
    comfyui_queue_busy,
    managed_output_prefixes,
)
from local_ai_stack.installation import detect_repo_root
from local_ai_stack.logs import HUB_EVENTS_LOG
from local_ai_stack.operation_contracts import OperationState
from local_ai_stack.profiles import ProfileError, validate_profile_data

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
HOUR = 3600
UUID_NAME = "3f2b8c1e-5d4a-4e7b-9c60-1a2b3c4d5e6f.png"
SWAP_NAME = "pepito_swap_" + "ab" * 16 + ".png"
OUTPUT_NAME = "Qwen_image_2.1_edit_00007_.png"


def age(path: Path, hours: float) -> Path:
    stamp = NOW.timestamp() - hours * HOUR
    os.utime(path, (stamp, stamp))
    return path


def put(folder: Path, name: str, hours: float, size: int = 10) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"x" * size)
    return age(path, hours)


def clean(app: Path, hours: float | None = 24, *, busy: bool = False):
    return clean_comfyui_folders(app, hours, now=NOW, busy=lambda: busy)


@pytest.fixture
def app(tmp_path: Path) -> Path:
    return tmp_path / "comfyui"


def test_old_files_with_our_names_go_and_new_ones_stay(app):
    old_up = put(app / "input", UUID_NAME, 30, 100)
    old_swap = put(app / "input", SWAP_NAME, 25, 50)
    old_out = put(app / "output", OUTPUT_NAME, 48, 200)
    new_up = put(app / "input", "pepito_swap_" + "cd" * 16 + ".png", 2)
    new_out = put(app / "output", "Flux2-Klein_00001_.png", 23)

    report = clean(app)

    assert (report.removed, report.freed_bytes) == (3, 350)
    assert not any(p.exists() for p in (old_up, old_swap, old_out))
    assert new_up.exists() and new_out.exists()


def test_other_owners_and_subfolders_stay(app):
    others = [
        put(app / "input", "example.png", 500),
        put(app / "input", "my_reference.png", 500),
        put(app / "output", "ComfyUI_00001_.png", 500),
        put(app / "output", "Qwen_image_2.1_edit_notes.txt", 500),
        put(app / "output" / "PepitoSwap", "notes.txt", 500),
        put(app / "output" / "PepitoSwap" / "deeper", "depth_00001_.png", 500),
        put(app / "output" / "Other", "depth_00001_.png", 500),
        put(app / "input" / "sub", UUID_NAME, 500),
        put(app / "output" / "sub", OUTPUT_NAME, 500),
    ]

    report = clean(app)

    assert report.removed == 0
    assert all(path.exists() for path in others)


def test_the_swap_mask_folder_is_cleaned_by_the_same_rule(app):
    swap = app / "output" / "PepitoSwap"
    old = [put(swap, "depth_00003_.png", 30, 40), put(swap, "people_00002_.png", 30, 60)]
    new = put(swap, "depth_00004_.png", 1)

    report = clean(app)

    assert (report.removed, report.freed_bytes) == (2, 100)
    assert not any(p.exists() for p in old)
    assert new.exists() and swap.is_dir()

    busy_old = put(swap, "people_00009_.png", 30)
    assert clean(app, busy=True).deferred
    assert busy_old.exists()


def test_a_busy_comfyui_defers_everything(app):
    old = put(app / "output", OUTPUT_NAME, 100)

    report = clean(app, busy=True)

    assert old.exists()
    assert report.removed == 0
    assert report.deferred
    assert "deferred" in report.describe(24)


def test_the_queue_is_not_asked_when_nothing_is_old(app):
    put(app / "output", OUTPUT_NAME, 1)

    def never() -> bool:
        raise AssertionError("no need to ask")

    assert clean_comfyui_folders(app, 24, now=NOW, busy=never).removed == 0


def test_without_the_hours_nothing_is_removed(app):
    old = put(app / "output", OUTPUT_NAME, 1000)

    assert clean(app, None).removed == 0
    assert old.exists()


def test_a_missing_folder_is_not_an_error(app):
    assert clean(app).removed == 0


def test_the_queue_answers(monkeypatch):
    def answer(body, status=200):
        def get(url, timeout):
            assert url == "http://comfy/queue"
            return httpx.Response(status, json=body, request=httpx.Request("GET", url))

        return get

    monkeypatch.setattr(httpx, "get", answer({"queue_running": [], "queue_pending": []}))
    assert comfyui_queue_busy("http://comfy") is False
    monkeypatch.setattr(httpx, "get", answer({"queue_running": [[1]], "queue_pending": []}))
    assert comfyui_queue_busy("http://comfy/") is True
    monkeypatch.setattr(httpx, "get", answer({"queue_running": [], "queue_pending": [[2]]}))
    assert comfyui_queue_busy("http://comfy") is True
    monkeypatch.setattr(httpx, "get", answer({}, status=500))
    assert comfyui_queue_busy("http://comfy") is True

    def refused(url, timeout):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", refused)
    assert comfyui_queue_busy("http://comfy") is False


def test_every_workflow_prefix_is_cleaned():
    root = detect_repo_root()
    flat, nested = managed_output_prefixes(root)
    seen, subfolders = set(), set()
    for path in (root / "workflows").glob("*.json"):
        for node in json.loads(path.read_text(encoding="utf-8")).values():
            prefix = (
                (node.get("inputs") or {}).get("filename_prefix")
                if isinstance(node, dict)
                else None
            )
            if not isinstance(prefix, str):
                continue
            folder, _, name = prefix.rpartition("/")
            (subfolders.add((folder, name)) if folder else seen.add(prefix))
    assert seen and subfolders, "the workflows name prefixes"
    assert seen <= set(flat)
    for folder, name in subfolders:
        assert name in nested.get(folder, ()), f"{folder}/{name} is never cleaned"


def test_the_prefixes_come_from_the_loaded_workflows_including_extras(tmp_path):
    """A pack's workflow (``manifests/workflows.d``) is covered without being named in code."""
    root = tmp_path / "repo"
    (root / "manifests" / "workflows.d").mkdir(parents=True)
    (root / "workflows").mkdir()
    base = yaml.safe_load((detect_repo_root() / "manifests" / "workflows.yaml").read_text("utf-8"))
    entry = next(iter(base["workflows"].values()))
    for name, prefix, directory in (
        ("one", "Mine_one", "workflows.yaml"),
        ("two", "Pack_two", "workflows.d/pack.yaml"),
    ):
        graph = {
            "1": {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix}},
            "2": {"class_type": "PreviewImage", "inputs": {"filename_prefix": "Not_owned"}},
        }
        (root / "workflows" / f"{name}.json").write_text(json.dumps(graph), encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "workflows": {name: {**entry, "file": f"workflows/{name}.json"}},
        }
        (root / "manifests" / directory).write_text(yaml.safe_dump(manifest), encoding="utf-8")

    flat, nested = managed_output_prefixes(root)

    assert flat == ("Mine_one", "Pack_two")
    assert nested == {}


def test_the_names_the_filter_uploads_are_cleaned(app):
    """``ComfyClient.upload`` names a file ``pepito_swap_<uuid4 hex>.png``."""
    old = put(app / "input", SWAP_NAME, 100)
    assert clean(app).removed == 1
    assert not old.exists()


def test_the_profile_key(tmp_path):
    base = yaml.safe_load(capella_yaml())

    def load(value):
        data = json.loads(json.dumps(base))
        if value is None:
            data["runtime"].pop("comfyui_retention_hours", None)
        else:
            data["runtime"]["comfyui_retention_hours"] = value
        return validate_profile_data(data, "capella.yaml")

    assert load(24).comfyui_retention_hours == 24
    assert load(0.5).comfyui_retention_hours == 0.5
    assert load(None).comfyui_retention_hours is None
    assert "comfyui_retention_hours" not in load(24).runtime
    for bad in (0, -1, "24", True):
        with pytest.raises(ProfileError):
            load(bad)


def test_the_schema_knows_the_key():
    schema = json.loads(
        (detect_repo_root() / "schemas" / "profile.schema.json").read_text(encoding="utf-8")
    )
    key = schema["properties"]["runtime"]["properties"]["comfyui_retention_hours"]
    assert key["type"] == "number" and key["exclusiveMinimum"] == 0


def test_capella_keeps_a_day():
    data = yaml.safe_load(capella_yaml())
    assert data["runtime"]["comfyui_retention_hours"] == 24


# --- composition ---------------------------------------------------------------------


def events(built) -> str:
    path = built.app.layout.log_file("comfyui").parent / HUB_EVENTS_LOG
    return path.read_text(encoding="utf-8") if path.exists() else ""


def stack(*, keep_key: bool, busy: bool = False):
    text = capella_yaml()
    if not keep_key:
        text = re.sub(r"^ +comfyui_retention_hours:.*\n", "", text, flags=re.MULTILINE)
        assert "comfyui_retention_hours" not in text
    built = controlled_stack(profile_yaml=text, profile_id=CAPELLA)
    assert built.app.first_run("op-1", CAPELLA).state is OperationState.SUCCEEDED
    assert built.app.stop_stack("op-2", CAPELLA).state is OperationState.SUCCEEDED
    built.app.deps.clock = lambda: datetime.now(UTC)
    asked = []
    built.app.deps.comfyui_queue_busy = lambda endpoint: asked.append(endpoint) or busy
    built.asked = asked
    return built


def seed(built):
    folder = built.app.layout.comfyui_app
    real = datetime.now(UTC).timestamp()
    out = []
    for name, hours in ((UUID_NAME, 30), (OUTPUT_NAME, 30), ("kept_by_age_00001_.png", 1)):
        target = "input" if name == UUID_NAME else "output"
        path = folder / target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"y" * 100)
        os.utime(path, (real - hours * HOUR, real - hours * HOUR))
        out.append(path)
    return out


def test_start_and_stop_clean_and_report_to_the_events_log():
    built = stack(keep_key=True)
    try:
        old_in, old_out, _recent = seed(built)
        assert built.app.start_stack("op-3", CAPELLA).state is OperationState.SUCCEEDED
        assert not old_in.exists() and not old_out.exists()
        assert "comfyui cleanup removed=2 freed_bytes=200 older_than_h=24" in events(built)
        assert built.asked == ["http://127.0.0.1:8188"]

        old_in, old_out, _ = seed(built)
        assert built.app.stop_stack("op-4", CAPELLA).state is OperationState.SUCCEEDED
        assert not old_in.exists() and not old_out.exists()
        assert events(built).count("comfyui cleanup removed=2") == 2
    finally:
        built.release()


def test_a_busy_comfyui_postpones_the_start_cleanup():
    built = stack(keep_key=True, busy=True)
    try:
        old_in, old_out, _ = seed(built)
        assert built.app.start_stack("op-3", CAPELLA).state is OperationState.SUCCEEDED
        assert old_in.exists() and old_out.exists()
        assert "comfyui cleanup deferred" in events(built)
    finally:
        built.release()


def test_a_profile_without_the_key_deletes_nothing():
    built = stack(keep_key=False)
    try:
        files = seed(built)
        assert built.app.start_stack("op-3", CAPELLA).state is OperationState.SUCCEEDED
        assert built.app.stop_stack("op-4", CAPELLA).state is OperationState.SUCCEEDED
        assert all(path.exists() for path in files)
        assert "comfyui cleanup" not in events(built)
        assert built.asked == []
    finally:
        built.release()
