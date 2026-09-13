"""A verified model is recognised by its record, and a changed one is not.

The Hub asked "is this model available?" dozens of times per status pass and
answered each time by reading the whole file. On a root with 12 GB of models
that was three minutes of hashing before the Dashboard could draw. The fix
has two halves, and both are pinned here with counters, never with timing:

- a **verification record** beside every file that passed a full SHA-256
  check, bound to the model, the pin, the path and the file's identity. A
  later look is a ``stat``. Everything that could mean "this is not the file
  that was verified" -- a byte changed, a size changed, a different pin, a
  record for another model, a corrupt record, a moved file -- falls back to
  the full hash, and a hash that fails discards the record;
- a **detection snapshot** per read-only pass, so one status pass answers
  each model once however many directions ask.

Acquisition is untouched: a download still verifies every byte before the
file is promoted, and a mismatch still refuses.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from first_run_support import controlled_stack, restarted_hub
from test_model_acquisition import (
    WEIGHTS,
    WEIGHTS_SHA,
    FakeOllama,
    file_spec,
    installer_for,
    ollama_spec,
    serving,
)

from local_ai_stack import model_installer
from local_ai_stack.installation import detect_repo_root
from local_ai_stack.model_installer import (
    VERIFICATION_SUFFIX,
    ModelInstaller,
    ModelOutcome,
    read_verification,
    verification_path,
)
from local_ai_stack.models import ManagedModelPaths, ModelState
from local_ai_stack.ollama_models import OllamaModelEntry
from local_ai_stack.operation_contracts import OperationState

OTHER_WEIGHTS = b"different-bytes-same-length" * 512
OTHER_WEIGHTS = OTHER_WEIGHTS[: len(WEIGHTS)]
assert len(OTHER_WEIGHTS) == len(WEIGHTS)
OTHER_SHA = hashlib.sha256(OTHER_WEIGHTS).hexdigest()


@pytest.fixture
def hasher(monkeypatch):
    """Counts full-file hashes. The product's own function still runs."""
    calls: list[Path] = []
    real = model_installer.sha256_of

    def counting(path, *args, **kwargs):
        calls.append(Path(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(model_installer, "sha256_of", counting)
    return calls


def placed(installer: ModelInstaller, spec=None, body: bytes = WEIGHTS) -> Path:
    """A model file already in the managed tree, as a finished download leaves it."""
    spec = spec or file_spec()
    destination = installer.paths.destination_for(spec)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body)
    return destination


def verified(installer: ModelInstaller, hasher, spec=None) -> Path:
    """A file that has been through one full verification and holds its record."""
    spec = spec or file_spec()
    destination = placed(installer, spec)
    assert installer.detect(spec).state is ModelState.AVAILABLE
    assert hasher == [destination]
    assert read_verification(destination) is not None
    hasher.clear()
    return destination


# --- A. the fast path -----------------------------------------------------------


def test_the_first_look_hashes_and_records(tmp_path, hasher):
    installer = installer_for(tmp_path)
    destination = placed(installer)
    status = installer.detect(file_spec())
    assert status.state is ModelState.AVAILABLE
    assert hasher == [destination]
    record = read_verification(destination)
    assert record is not None
    assert record.sha256 == WEIGHTS_SHA and record.model_id == "comfyui/demo_diffusion"
    assert record.size_bytes == len(WEIGHTS)
    assert verification_path(destination).name.endswith(VERIFICATION_SUFFIX)


def test_a_recorded_unchanged_file_is_available_without_hashing(tmp_path, hasher):
    installer = installer_for(tmp_path)
    verified(installer, hasher)
    for _ in range(5):
        status = installer.detect(file_spec())
        assert status.state is ModelState.AVAILABLE
        assert "recorded" in status.detail
    assert hasher == []


def test_a_new_installer_over_the_same_tree_trusts_the_record(tmp_path, hasher):
    verified(installer_for(tmp_path), hasher)
    again = installer_for(tmp_path)  # a reopened Hub
    assert again.detect(file_spec()).state is ModelState.AVAILABLE
    assert hasher == []


def test_a_verified_file_is_a_noop_plan_without_hashing(tmp_path, hasher):
    installer = installer_for(tmp_path)
    verified(installer, hasher)
    assert installer.plan(file_spec()).is_noop
    assert installer.apply(file_spec(), dry_run=True).outcome is ModelOutcome.SKIPPED
    assert hasher == []


# --- B. a modified file --------------------------------------------------------


def test_modified_bytes_with_a_new_mtime_are_rehashed_and_refused(tmp_path, hasher):
    installer = installer_for(tmp_path)
    destination = verified(installer, hasher)
    destination.write_bytes(OTHER_WEIGHTS)  # same size, different bytes, later mtime

    status = installer.detect(file_spec())

    assert status.state is ModelState.INVALID
    assert hasher == [destination], "a changed file must be read again"
    assert read_verification(destination) is None, "a failed hash discards the record"


def test_a_truncated_file_is_invalid(tmp_path, hasher):
    installer = installer_for(tmp_path)
    destination = verified(installer, hasher)
    destination.write_bytes(WEIGHTS[:-10])
    assert installer.detect(file_spec()).state is ModelState.INVALID
    assert hasher == [destination]


def test_a_larger_file_is_invalid(tmp_path, hasher):
    installer = installer_for(tmp_path)
    destination = verified(installer, hasher)
    destination.write_bytes(WEIGHTS + b"tail")
    assert installer.detect(file_spec()).state is ModelState.INVALID
    assert hasher == [destination]


def test_a_file_replaced_by_identical_bytes_is_rehashed_once_then_recorded(tmp_path, hasher):
    # A re-copy of the same model: not the file that was verified (new
    # identity), so it is read again, agrees, and earns a fresh record.
    installer = installer_for(tmp_path)
    destination = verified(installer, hasher)
    replacement = tmp_path / "copy.safetensors"
    replacement.write_bytes(WEIGHTS)
    os.replace(replacement, destination)

    assert installer.detect(file_spec()).state is ModelState.AVAILABLE
    assert hasher == [destination]
    hasher.clear()
    assert installer.detect(file_spec()).state is ModelState.AVAILABLE
    assert hasher == []


def test_a_missing_file_is_not_installed_whatever_the_record_says(tmp_path, hasher):
    installer = installer_for(tmp_path)
    destination = verified(installer, hasher)
    destination.unlink()
    assert verification_path(destination).exists()
    assert installer.detect(file_spec()).state is ModelState.NOT_INSTALLED
    assert hasher == []


# --- C. the pin changed ---------------------------------------------------------


def test_a_record_for_the_old_checksum_is_not_accepted_for_the_new_pin(tmp_path, hasher):
    installer = installer_for(tmp_path)
    destination = verified(installer, hasher)

    status = installer.detect(file_spec(sha=OTHER_SHA))  # the manifest re-pinned

    assert status.state is ModelState.INVALID
    assert hasher == [destination]
    assert read_verification(destination) is None


def test_a_record_for_another_model_is_not_accepted(tmp_path, hasher):
    installer = installer_for(tmp_path)
    destination = verified(installer, hasher)
    record = json.loads(verification_path(destination).read_text(encoding="utf-8"))
    record["model_id"] = "comfyui/something_else"
    verification_path(destination).write_text(json.dumps(record), encoding="utf-8")

    assert installer.detect(file_spec()).state is ModelState.AVAILABLE
    assert hasher == [destination], "a record for another model proves nothing"


# --- D. the record itself -------------------------------------------------------


@pytest.mark.parametrize(
    "corruption",
    [
        "not json at all",
        "[]",
        json.dumps({"schema_version": 2}),
        json.dumps({"schema_version": 1, "model_id": "comfyui/demo_diffusion", "sha256": "x"}),
    ],
)
def test_a_corrupt_or_foreign_record_falls_back_to_the_full_hash(tmp_path, hasher, corruption):
    installer = installer_for(tmp_path)
    destination = verified(installer, hasher)
    verification_path(destination).write_text(corruption, encoding="utf-8")

    assert installer.detect(file_spec()).state is ModelState.AVAILABLE
    assert hasher == [destination]
    assert read_verification(destination) is not None, "a good hash rewrites the record"


def test_a_record_that_only_matches_size_and_mtime_is_not_enough(tmp_path, hasher):
    # Same size, same mtime, same everything the old-style record carried --
    # but a different pin. The pin is part of the proof.
    installer = installer_for(tmp_path)
    destination = verified(installer, hasher)
    record = json.loads(verification_path(destination).read_text(encoding="utf-8"))
    record["sha256"] = OTHER_SHA
    verification_path(destination).write_text(json.dumps(record), encoding="utf-8")
    assert installer.detect(file_spec()).state is ModelState.AVAILABLE
    assert hasher == [destination]


def test_a_record_moved_with_its_file_to_another_path_is_not_proof(tmp_path, hasher):
    source = installer_for(tmp_path / "one")
    destination = verified(source, hasher)
    elsewhere = installer_for(tmp_path / "two")
    target = elsewhere.paths.destination_for(file_spec())
    target.parent.mkdir(parents=True)
    shutil.copy2(destination, target)  # keeps size and mtime
    shutil.copy2(verification_path(destination), verification_path(target))

    assert elsewhere.detect(file_spec()).state is ModelState.AVAILABLE
    assert hasher == [target], "a copied record does not vouch for a copied file"


def test_a_file_without_a_pinned_checksum_is_never_recorded(tmp_path, hasher):
    installer = installer_for(tmp_path)
    spec = file_spec(sha=None)
    destination = placed(installer, spec)
    assert installer.detect(spec).state is ModelState.INVALID
    assert hasher == []
    assert not verification_path(destination).exists()


# --- acquisition is unchanged -------------------------------------------------


def test_a_download_still_verifies_every_byte_then_records(tmp_path, hasher):
    installer = installer_for(tmp_path, downloader=serving())
    result = installer.apply(file_spec())
    assert result.outcome is ModelOutcome.ACQUIRED
    destination = installer.paths.destination_for(file_spec())
    assert destination.read_bytes() == WEIGHTS
    assert read_verification(destination) is not None
    hasher.clear()
    assert installer.detect(file_spec()).state is ModelState.AVAILABLE
    assert hasher == []


def test_a_download_that_does_not_match_the_pin_leaves_no_file_and_no_record(tmp_path):
    installer = installer_for(tmp_path, downloader=serving(OTHER_WEIGHTS))
    result = installer.apply(file_spec())
    assert result.outcome is ModelOutcome.FAILED
    destination = installer.paths.destination_for(file_spec())
    assert not destination.exists()
    assert not verification_path(destination).exists()


# --- E. one answer per pass ------------------------------------------------------


def test_a_detection_snapshot_answers_each_model_once(tmp_path, hasher, monkeypatch):
    store = FakeOllama(installed=(OllamaModelEntry("demo:8b", "sha256:4f6b83f30b62aaaa", 1),))
    installer = installer_for(tmp_path, ollama=store)
    placed(installer)
    listings = []
    original = store.list_models
    monkeypatch.setattr(store, "list_models", lambda: listings.append(1) or original())

    with installer.detection_snapshot():
        for _ in range(6):
            assert installer.detect(file_spec()).state is ModelState.AVAILABLE
            assert installer.detect(ollama_spec()).state is ModelState.AVAILABLE

    assert len(hasher) == 1 and len(listings) == 1


def test_outside_a_snapshot_every_detection_reads_the_world(tmp_path, monkeypatch):
    store = FakeOllama(installed=(OllamaModelEntry("demo:8b", "sha256:4f6b83f30b62aaaa", 1),))
    installer = installer_for(tmp_path, ollama=store)
    listings = []
    original = store.list_models
    monkeypatch.setattr(store, "list_models", lambda: listings.append(1) or original())
    for _ in range(3):
        installer.detect(ollama_spec())
    assert len(listings) == 3


def test_an_acquisition_inside_a_snapshot_is_seen_not_remembered(tmp_path):
    store = FakeOllama()
    installer = installer_for(tmp_path, ollama=store)
    with installer.detection_snapshot():
        assert installer.detect(ollama_spec()).state is ModelState.NOT_INSTALLED
        assert installer.apply(ollama_spec()).outcome is ModelOutcome.ACQUIRED
        assert installer.detect(ollama_spec()).state is ModelState.AVAILABLE


# --- the application: one status pass, one detection per model -----------------


def shipped(profile_id: str) -> str:
    return (detect_repo_root() / "profiles" / f"{profile_id}.yaml").read_text(encoding="utf-8")


def test_one_status_pass_detects_each_file_model_once_and_hashes_nothing(monkeypatch):
    built = controlled_stack(profile_yaml=shipped("pepe"), profile_id="pepe", vram_mib=12227)
    try:
        first = built.app.first_run("op-1", "pepe")
        assert first.state is OperationState.SUCCEEDED, [d.message for d in first.diagnostics]
        # Every downloaded file has been verified once and carries its record.
        paths = ManagedModelPaths.from_layout(built.app.layout)
        hashes: list[Path] = []
        real = model_installer.sha256_of
        monkeypatch.setattr(
            model_installer,
            "sha256_of",
            lambda p, *a, **k: hashes.append(Path(p)) or real(p, *a, **k),
        )
        detections: list[str] = []
        real_detect_file = ModelInstaller._detect_file
        monkeypatch.setattr(
            ModelInstaller,
            "_detect_file",
            lambda self, spec, source: (
                detections.append(str(spec.id)) or real_detect_file(self, spec, source)
            ),
        )

        again = restarted_hub(built)
        status = again.status("pepe")

        assert status.installation_ready and status.stack_up
        assert hashes == [], "a status pass must not hash a verified model"
        assert sorted(detections) == sorted(set(detections)), "one detection per model per pass"
        assert len(detections) == 3
        for model_id in detections:
            spec = again.catalog().get(
                __import__("local_ai_stack.models", fromlist=["ModelId"]).ModelId(model_id)
            )
            assert read_verification(paths.destination_for(spec)) is not None
    finally:
        built.release()
