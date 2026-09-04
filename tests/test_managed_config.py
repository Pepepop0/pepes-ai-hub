from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_ai_stack.managed_config import (
    DIGEST_KEY,
    MANAGED_BY,
    MARKER_KEY,
    ConfigAction,
    ConfigDocument,
    ConfigFormat,
    ManagedConfigWriter,
    Ownership,
    describe_plan,
    env_document,
    json_document,
    read_managed,
    read_marker,
    yaml_document,
)


@pytest.fixture
def writer() -> ManagedConfigWriter:
    return ManagedConfigWriter()


def env(tmp_path, **values):
    return env_document(tmp_path / "service.env", values or {"A": "1"})


# --- ownership -------------------------------------------------------------


def test_an_absent_file_is_free_to_write(tmp_path, writer):
    plan = writer.plan([env(tmp_path)])
    assert plan.changes[0].action is ConfigAction.CREATE
    assert plan.changes[0].ownership is Ownership.ABSENT
    assert not plan.blocked


def test_a_file_we_wrote_is_ours_to_rewrite(tmp_path, writer):
    writer.apply([env(tmp_path, A="1")])
    ownership, digest = read_marker(tmp_path / "service.env")
    assert ownership is Ownership.MANAGED
    assert digest is not None

    plan = writer.plan([env(tmp_path, A="2")])
    assert plan.changes[0].action is ConfigAction.UPDATE


def test_a_file_we_did_not_write_is_never_touched(tmp_path, writer):
    path = tmp_path / "service.env"
    path.write_text("A=handwritten\n", encoding="utf-8")

    plan = writer.plan([env(tmp_path, A="generated")])
    assert plan.changes[0].action is ConfigAction.CONFLICT
    assert plan.blocked

    report = writer.apply([env(tmp_path, A="generated")])
    assert not report.ok
    assert report.refused == (path,)
    assert path.read_text(encoding="utf-8") == "A=handwritten\n", "left exactly as it was"


def test_a_json_file_without_our_marker_is_external(tmp_path, writer):
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps({"prompt": {"node_id": "1"}}), encoding="utf-8")
    assert read_marker(path)[0] is Ownership.EXTERNAL
    assert writer.plan([json_document(path, {"prompt": {}})]).blocked


def test_unreadable_content_is_treated_as_somebody_elses(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{ not json", encoding="utf-8")
    assert read_marker(path)[0] is Ownership.EXTERNAL


# --- idempotence and dry run ------------------------------------------------


def test_applying_the_same_configuration_twice_writes_once(tmp_path, writer):
    first = writer.apply([env(tmp_path, A="1")])
    second = writer.apply([env(tmp_path, A="1")])
    assert first.written and not first.unchanged
    assert not second.written
    assert second.unchanged == (tmp_path / "service.env",)
    assert writer.plan([env(tmp_path, A="1")]).up_to_date


def test_generation_is_deterministic_regardless_of_key_order(tmp_path):
    one = env_document(tmp_path / "a.env", {"B": "2", "A": "1"})
    two = env_document(tmp_path / "a.env", {"A": "1", "B": "2"})
    assert one.body == two.body
    assert one.digest == two.digest


def test_a_dry_run_plans_everything_and_writes_nothing(tmp_path, writer):
    report = writer.apply([env(tmp_path, A="1")], dry_run=True)
    assert report.dry_run
    assert report.written == (tmp_path / "service.env",)
    assert not (tmp_path / "service.env").exists(), "nothing was created"


def test_a_dry_run_still_refuses_a_conflict(tmp_path, writer):
    path = tmp_path / "service.env"
    path.write_text("mine\n", encoding="utf-8")
    report = writer.apply([env(tmp_path)], dry_run=True)
    assert report.refused == (path,)
    assert path.read_text(encoding="utf-8") == "mine\n"


def test_planning_never_creates_a_directory(tmp_path, writer):
    nested = tmp_path / "deep" / "service.env"
    writer.plan([env_document(nested, {"A": "1"})])
    assert not nested.parent.exists()


# --- rendering --------------------------------------------------------------


def test_an_env_file_carries_a_comment_marker(tmp_path, writer):
    writer.apply([env(tmp_path, HOST="127.0.0.1")])
    text = (tmp_path / "service.env").read_text(encoding="utf-8")
    assert text.startswith(f"# {MARKER_KEY}: {MANAGED_BY}")
    assert "HOST=127.0.0.1" in text


def test_a_json_file_carries_the_marker_as_keys(tmp_path, writer):
    document = json_document(tmp_path / "nodes.json", {"prompt": {"node_id": "76"}})
    writer.apply([document])
    payload = json.loads((tmp_path / "nodes.json").read_text(encoding="utf-8"))
    assert payload[MARKER_KEY] == MANAGED_BY
    assert payload[DIGEST_KEY] == document.digest
    assert payload["prompt"] == {"node_id": "76"}


def test_a_json_document_must_be_an_object(tmp_path):
    """The marker lives inside the object, so a list has nowhere to carry it."""
    document = ConfigDocument(tmp_path / "x.json", "[]", ConfigFormat.JSON)
    with pytest.raises(ValueError, match="must be an object"):
        document.rendered()


def test_a_relative_path_is_refused():
    with pytest.raises(ValueError, match="absolute path"):
        ConfigDocument(Path("relative.env"), "A=1")


# --- verify -----------------------------------------------------------------


def test_verify_reports_what_matches_what_drifted_and_what_is_missing(tmp_path, writer):
    good = env_document(tmp_path / "good.env", {"A": "1"})
    drifted = env_document(tmp_path / "drift.env", {"A": "1"})
    missing = env_document(tmp_path / "missing.env", {"A": "1"})
    foreign = env_document(tmp_path / "foreign.env", {"A": "1"})
    writer.apply([good, drifted])
    (tmp_path / "foreign.env").write_text("theirs\n", encoding="utf-8")

    changed = env_document(tmp_path / "drift.env", {"A": "2"})
    report = writer.verify([good, changed, missing, foreign])

    assert report.matching == (good.path,)
    assert report.drifted == (changed.path,)
    assert report.missing == (missing.path,)
    assert report.foreign == (foreign.path,)
    assert not report.ok
    assert len(report.problems) == 3


def test_verify_is_happy_right_after_apply(tmp_path, writer):
    documents = [
        env_document(tmp_path / "a.env", {"A": "1"}),
        yaml_document(tmp_path / "b.yaml", "k: v"),
    ]
    writer.apply(documents)
    assert writer.verify(documents).ok


def test_a_plan_can_be_described_for_a_reader(tmp_path, writer):
    lines = describe_plan(writer.plan([env(tmp_path)]))
    assert lines and lines[0].startswith("create: service.env")


# --- atomicity --------------------------------------------------------------


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path, writer, monkeypatch):
    document = env_document(tmp_path / "service.env", {"A": "1"})
    writer.apply([document])
    original = document.path.read_text(encoding="utf-8")

    from local_ai_stack import managed_config

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(managed_config.os, "replace", explode)
    with pytest.raises(OSError):
        writer.apply([env_document(tmp_path / "service.env", {"A": "2"})])

    assert document.path.read_text(encoding="utf-8") == original
    leftovers = [p for p in tmp_path.iterdir() if p != document.path]
    assert leftovers == [], "no temporary file was left behind"


# --- the marker is a claim, not evidence ------------------------------------


def test_editing_a_generated_file_is_detected_even_with_the_header_intact(tmp_path, writer):
    """The blocker: trusting the header would let an edited body pass."""
    document = env_document(tmp_path / "service.env", {"A": "1", "B": "2"})
    writer.apply([document])
    assert writer.verify([document]).ok

    text = document.path.read_text(encoding="utf-8")
    document.path.write_text(text.replace("A=1", "A=tampered"), encoding="utf-8")

    managed = read_managed(document.path)
    assert managed.ownership is Ownership.MANAGED, "it is still our file"
    assert managed.claimed_digest is not None
    assert managed.actual_digest != managed.claimed_digest
    assert not managed.intact

    report = writer.verify([document])
    assert not report.ok
    assert report.drifted == (document.path,)
    assert "edited after it was generated" in report.problems[0]


def test_an_edited_generated_file_is_regenerated_rather_than_left_alone(tmp_path, writer):
    document = env_document(tmp_path / "service.env", {"A": "1"})
    writer.apply([document])
    text = document.path.read_text(encoding="utf-8")
    document.path.write_text(text.replace("A=1", "A=edited"), encoding="utf-8")

    plan = writer.plan([document])
    assert plan.changes[0].action is ConfigAction.UPDATE
    assert "edited by hand" in plan.changes[0].detail

    writer.apply([document])
    assert writer.verify([document]).ok
    assert "A=1" in document.path.read_text(encoding="utf-8")


def test_a_tampered_json_body_is_detected_too(tmp_path, writer):
    document = json_document(tmp_path / "nodes.json", {"nodes": [{"type": "prompt"}]})
    writer.apply([document])

    payload = json.loads(document.path.read_text(encoding="utf-8"))
    payload["nodes"] = [{"type": "somebody-elses"}]
    document.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    managed = read_managed(document.path)
    assert managed.ownership is Ownership.MANAGED
    assert not managed.intact
    assert not writer.verify([document]).ok


def test_an_external_file_is_still_external_not_tampered(tmp_path, writer):
    """Ownership detection is unchanged by the integrity check."""
    path = tmp_path / "service.env"
    path.write_text("A=theirs\n", encoding="utf-8")
    managed = read_managed(path)
    assert managed.ownership is Ownership.EXTERNAL
    assert managed.intact is False
    report = writer.verify([env_document(path, {"A": "1"})])
    assert report.foreign == (path,)
    assert report.drifted == ()
