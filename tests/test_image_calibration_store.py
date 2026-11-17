"""The pure, account-scoped image calibration ledger; no models and no Open WebUI."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

ASSET = Path(__file__).resolve().parents[1] / "assets" / "open-webui" / "pepito_calibration.py"
spec = importlib.util.spec_from_file_location("pepito_calibration_under_test", ASSET)
store = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = store
spec.loader.exec_module(store)


def facts(**overrides):
    value = {
        "family": "krea2",
        "workflow": "edit",
        "change": "strong",
        "strength": 1.0,
        "fidelity": "auto",
        "request_id": "request-1",
        "source_numbers": [1],
        "measurement": 10.0,
        "requested_count": 1,
        "status": "completed",
        "delivered": True,
        "output_number": 2,
    }
    value.update(overrides)
    return value


def register(tmp_path, attempt="a1", user="alice", chat="chat", **overrides):
    store.register_attempt(tmp_path, user, chat, attempt, facts(**overrides))


def accept(tmp_path, attempt="a1", user="alice", chat="chat", verdict="accepted"):
    return store.record_feedback(tmp_path, user, chat, attempt, verdict)


def bias(tmp_path, user="alice", family="krea2", workflow="edit", change="strong", **kwargs):
    return store.calibration_bias(tmp_path, user, family, workflow, change, **kwargs)


@pytest.fixture
def clock(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(store, "time", SimpleNamespace(time=lambda: now[0]))
    return now


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "failed"},
        {"status": "cancelled"},
        {"status": None},
        {"delivered": False},
        {"delivered": None},
        {"strength": math.nan},
        {"strength": math.inf},
        {"strength": 0.49},
        {"strength": 5.5},
        {"strength": True},
        {"measurement": -1},
        {"measurement": math.nan},
        {"measurement": 256},
        {"requested_count": 0},
        {"requested_count": 1.0},
        {"fidelity": "guess"},
        {"family": ""},
        {"workflow": None},
        {"change": 3},
        {"source_numbers": "1"},
        {"source_numbers": [1, 1]},
        {"source_numbers": [0]},
        {"request_id": ""},
        {"output_number": 0},
    ],
)
def test_only_completed_delivered_valid_attempts_register(tmp_path, overrides):
    with pytest.raises(ValueError):
        register(tmp_path, **overrides)
    state = store.calibration_state(tmp_path, "alice")
    assert state["attempts"] == 0 and state["scopes"] == []
    assert store.attempt_state(tmp_path, "alice", "chat", "a1") is None


@pytest.mark.parametrize("strength", [0.5, 1.0, 1.5, 2.5, 4.0, 5.0])
def test_every_strength_an_edit_can_use_is_recorded_with_its_feedback(tmp_path, strength):
    """02/11: three edits delivered at 0.5 were refused by the ledger, so they had no attempt,
    their "nada mudou" was never recorded and they could not be approved."""
    register(tmp_path, strength=strength, fidelity="correction")
    assert store.attempt_state(tmp_path, "alice", "chat", "a1")["strength"] == strength
    assert accept(tmp_path, verdict="no_change")["recorded"] is True
    assert store.attempt_state(tmp_path, "alice", "chat", "a1")["feedback"] == {"no_change": False}


def test_an_accepted_strength_below_the_automatic_range_keeps_the_target_inside_it(tmp_path):
    register(tmp_path, strength=0.5, fidelity="correction")
    assert accept(tmp_path)["learned"] is True
    target = store.BASE_STRENGTHS["strong"] + bias(tmp_path)
    assert store.STRENGTH_RANGE[0] - 1e-9 <= target <= store.STRENGTH_RANGE[1] + 1e-9


def test_unsafe_identifiers_are_rejected(tmp_path):
    for user, chat, attempt in (("", "c", "a"), ("u", "", "a"), ("u", "c", ""), (None, "c", "a")):
        with pytest.raises(ValueError):
            store.register_attempt(tmp_path, user, chat, attempt, facts())
    with pytest.raises(ValueError):
        store.register_attempt(tmp_path, "u", "c", "x" * 257, facts())


def test_registration_is_idempotent_and_ids_cannot_be_reassigned(tmp_path):
    register(tmp_path)
    register(tmp_path)
    assert store.calibration_state(tmp_path, "alice")["attempts"] == 1
    with pytest.raises(ValueError, match="reassigned"):
        register(tmp_path, strength=2.0)
    with pytest.raises(ValueError, match="reassigned"):
        register(tmp_path, chat="other-chat")
    # Registration alone never trains.
    assert bias(tmp_path) == 0
    state = store.attempt_state(tmp_path, "alice", "chat", "a1")
    assert state["operation"] == "edit" and state["source_numbers"] == [1]
    assert state["output_number"] == 2 and state["feedback"] == {}


def test_attempt_state_is_bound_to_account_and_chat(tmp_path):
    register(tmp_path)
    assert store.attempt_state(tmp_path, "bob", "chat", "a1") is None
    assert store.attempt_state(tmp_path, "alice", "other", "a1") is None
    with pytest.raises(ValueError, match="not found"):
        accept(tmp_path, user="bob")
    with pytest.raises(ValueError, match="not found"):
        accept(tmp_path, chat="other")
    assert bias(tmp_path, user="bob") == 0


def test_accepting_moves_target_once_within_gradual_learning_rate(tmp_path):
    register(tmp_path)
    first = accept(tmp_path)
    again = accept(tmp_path)
    assert first == {"attempt_id": "a1", "verdict": "accepted", "recorded": True, "learned": True}
    assert again["recorded"] is False and again["learned"] is True
    assert bias(tmp_path) == pytest.approx(-0.175, abs=1e-3)  # 0.35 * (1.0 - 1.5)
    state = store.calibration_state(tmp_path, "alice")
    assert state["accepted_samples"] == 1
    assert state["scopes"][0]["accepted"] == 1
    assert state["scopes"][0]["target"] == pytest.approx(1.5 - 0.175, abs=1e-3)
    assert state["feedback"] == {"accepted": 1}


def test_learning_reaches_stronger_freedom_than_the_old_floor_but_stays_bounded(tmp_path):
    for index in range(40):
        register(tmp_path, f"free-{index}", strength=1.0)
        accept(tmp_path, f"free-{index}")
    state = store.calibration_state(tmp_path, "alice")
    assert state["scopes"][0]["target"] == pytest.approx(1.0, abs=0.01)
    assert state["scopes"][0]["target"] >= store.STRENGTH_RANGE[0] - 1e-9
    for index in range(40):
        register(tmp_path, f"faithful-{index}", change="subtle", strength=5.0)
        accept(tmp_path, f"faithful-{index}")
    for index in range(40):
        register(tmp_path, f"moderate-{index}", change="moderate", strength=5.0)
        accept(tmp_path, f"moderate-{index}")
    for change in store.BASE_STRENGTHS:
        value = bias(tmp_path, change=change)
        assert abs(value) <= store.BIAS_LIMIT
        assert (
            store.STRENGTH_RANGE[0] - 1e-9
            <= store.BASE_STRENGTHS[change] + value
            <= store.STRENGTH_RANGE[1] + 1e-9
        )
    # Learning fades with later accepted samples elsewhere, so read-backs are approximate.
    assert 0.5 < bias(tmp_path, change="subtle") <= 1.0 + 1e-9
    assert bias(tmp_path, change="moderate") == pytest.approx(store.BIAS_LIMIT, abs=0.05)


@pytest.mark.parametrize("fidelity", ["manual", "workflow"])
def test_manual_or_workflow_fixed_attempts_never_train_automatic_settings(tmp_path, fidelity):
    register(tmp_path, fidelity=fidelity, strength=1.0)
    assert accept(tmp_path)["learned"] is False
    assert bias(tmp_path) == 0
    assert store.calibration_state(tmp_path, "alice")["accepted_samples"] == 0


def test_attempt_without_a_strength_cannot_train(tmp_path):
    register(tmp_path, strength=None)
    assert accept(tmp_path)["learned"] is False
    assert store.calibration_state(tmp_path, "alice")["scopes"] == []


def test_corrected_attempt_trains_only_after_its_own_acceptance(tmp_path):
    register(tmp_path, "first", strength=2.5)
    register(tmp_path, "retry", strength=1.5, fidelity="correction", previous_attempt_id="first")
    rejected = accept(tmp_path, "first", verdict="no_change")
    assert rejected == {
        "attempt_id": "first",
        "verdict": "no_change",
        "recorded": True,
        "learned": False,
    }
    # The rejection is evidence, not a lesson, and the retry has not been accepted yet.
    assert bias(tmp_path) == 0
    assert store.calibration_state(tmp_path, "alice")["feedback"] == {"no_change": 1}
    assert accept(tmp_path, "retry")["learned"] is True
    assert bias(tmp_path) == pytest.approx(0.0, abs=1e-9)  # strength == baseline 1.5
    register(tmp_path, "freer", strength=1.0, fidelity="correction", previous_attempt_id="retry")
    assert accept(tmp_path, "freer")["learned"] is True
    assert bias(tmp_path) < 0


@pytest.mark.parametrize("verdict", ["no_change", "too_much", "wrong_source"])
def test_negative_feedback_is_idempotent_and_blocks_later_acceptance_from_learning(
    tmp_path, verdict
):
    register(tmp_path)
    assert accept(tmp_path, verdict=verdict)["recorded"] is True
    assert accept(tmp_path, verdict=verdict)["recorded"] is False
    late = accept(tmp_path)
    assert late["recorded"] is True and late["learned"] is False
    assert bias(tmp_path) == 0
    assert store.calibration_state(tmp_path, "alice")["accepted_samples"] == 0


def test_unknown_verdict_is_rejected_without_recording(tmp_path):
    register(tmp_path)
    with pytest.raises(ValueError, match="verdict"):
        accept(tmp_path, verdict="great")
    assert store.calibration_state(tmp_path, "alice")["feedback"] == {}


def test_accounts_families_workflows_operations_and_changes_are_isolated(tmp_path):
    register(tmp_path, "a1")
    register(tmp_path, "b1", user="bob", chat="bob-chat")
    accept(tmp_path, "a1")
    learned = bias(tmp_path)
    assert learned < 0
    assert bias(tmp_path, user="bob") == 0
    assert bias(tmp_path, family="qwen21") == 0
    assert bias(tmp_path, workflow="restage") == 0
    assert bias(tmp_path, change="moderate") == 0
    assert bias(tmp_path, operation="pose") == 0
    register(tmp_path, "pose", operation="pose", strength=1.2)
    accept(tmp_path, "pose")
    assert bias(tmp_path, operation="pose") < 0
    assert bias(tmp_path) == pytest.approx(learned, rel=0.01)
    assert bias(tmp_path, user="bob") == 0
    scopes = store.calibration_state(tmp_path, "alice")["scopes"]
    assert {(s["family"], s["workflow"], s["change"], s["operation"]) for s in scopes} == {
        ("krea2", "edit", "strong", "edit"),
        ("krea2", "edit", "strong", "pose"),
    }


def test_concurrent_acceptance_of_one_attempt_learns_once(tmp_path):
    register(tmp_path)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: accept(tmp_path), range(8)))
    assert sum(item["recorded"] for item in results) == 1
    assert store.calibration_state(tmp_path, "alice")["accepted_samples"] == 1
    assert bias(tmp_path) == pytest.approx(-0.175, abs=1e-3)


def test_learning_fades_with_elapsed_time(tmp_path, clock):
    register(tmp_path)
    accept(tmp_path)
    fresh = bias(tmp_path)
    clock[0] += store.TIME_HALF_LIFE
    assert bias(tmp_path) == pytest.approx(fresh / 2, rel=1e-6)
    clock[0] += store.TIME_HALF_LIFE * 40
    assert abs(bias(tmp_path)) < abs(fresh) / 1e6


def test_learning_fades_with_later_accepted_samples_elsewhere(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(store, "SAMPLE_HALF_LIFE", 2)
    register(tmp_path, "a1")
    accept(tmp_path, "a1")
    fresh = bias(tmp_path)
    for index in range(2):
        register(tmp_path, f"other-{index}", change="moderate", strength=2.0)
        accept(tmp_path, f"other-{index}")
    assert bias(tmp_path) == pytest.approx(fresh / 2, rel=1e-6)


def test_repeated_samples_move_gradually_not_in_one_jump(tmp_path):
    values = []
    for index in range(4):
        register(tmp_path, f"a{index}", strength=1.0)
        accept(tmp_path, f"a{index}")
        values.append(bias(tmp_path))
    assert values == sorted(values, reverse=True)
    assert values[0] == pytest.approx(-0.175, abs=1e-3)
    assert all(-0.5 <= value < 0 for value in values)
    assert values[0] > values[1] > values[2] > values[3] > -0.5


def test_old_json_migrates_once_without_turning_rejections_into_successes(tmp_path, clock):
    key = hashlib.sha256(b"alice").hexdigest()[:16]
    legacy = tmp_path / "pepito-fidelity.json"
    legacy.write_text(
        json.dumps(
            {
                key: {"bias": 0.8, "levels": {"strong": -1.2, "subtle": 0.4, "bogus": 3}},
                "short": {"bias": 9},
                "ffffffffffffffff": "not an object",
                hashlib.sha256(b"bob").hexdigest()[:16]: {"bias": -0.6},
            }
        ),
        encoding="utf-8",
    )
    # The account keeps what its kept pictures taught; level offsets, learned from
    # rejections before any retry succeeded, are evidence only and are never applied.
    assert bias(tmp_path) == pytest.approx(0.8)
    assert bias(tmp_path, change="subtle") == pytest.approx(0.8)
    assert bias(tmp_path, user="bob") == pytest.approx(-0.6)
    assert bias(tmp_path, user="carol") == 0
    state = store.calibration_state(tmp_path, "alice")
    assert state["scopes"] == [] and state["accepted_samples"] == 0
    assert state["legacy"]["unconfirmed_levels"] == {"strong": -1.2, "subtle": 0.4}
    # Imported once: edits to the old file afterwards are ignored.
    legacy.write_text(json.dumps({key: {"bias": -2.0}}), encoding="utf-8")
    assert bias(tmp_path) == pytest.approx(0.8)
    # The legacy lesson fades and is superseded by the scope's own accepted learning.
    clock[0] += store.TIME_HALF_LIFE
    assert bias(tmp_path) == pytest.approx(0.4)
    register(tmp_path, strength=1.5)
    accept(tmp_path)
    assert bias(tmp_path) == pytest.approx(0.4 * 0.65, abs=0.005)
    assert bias(tmp_path, change="subtle") == pytest.approx(0.4, abs=0.005)


def test_malformed_or_oversized_legacy_file_is_ignored(tmp_path, monkeypatch):
    (tmp_path / "pepito-fidelity.json").write_text("{not json", encoding="utf-8")
    assert bias(tmp_path) == 0
    other = tmp_path / "other"
    other.mkdir()
    (other / "pepito-fidelity.json").write_text(
        json.dumps({hashlib.sha256(b"alice").hexdigest()[:16]: {"bias": 1}}), encoding="utf-8"
    )
    monkeypatch.setattr(store, "MAX_LEGACY_BYTES", 4)
    assert bias(other) == 0


def test_legacy_values_outside_bounds_are_not_trusted(tmp_path):
    key = hashlib.sha256(b"alice").hexdigest()[:16]
    (tmp_path / "pepito-fidelity.json").write_text(
        json.dumps({key: {"bias": 99, "levels": {"strong": "x"}}}), encoding="utf-8"
    )
    assert bias(tmp_path) == 0
    assert store.calibration_state(tmp_path, "alice")["legacy"]["unconfirmed_levels"] == {}


def test_reset_removes_only_this_account_and_does_not_reimport_legacy(tmp_path):
    key = hashlib.sha256(b"alice").hexdigest()[:16]
    (tmp_path / "pepito-fidelity.json").write_text(
        json.dumps({key: {"bias": 0.8}}), encoding="utf-8"
    )
    register(tmp_path, "a1")
    register(tmp_path, "b1", user="bob", chat="bob-chat")
    accept(tmp_path, "a1")
    accept(tmp_path, "b1", user="bob", chat="bob-chat")
    bob = bias(tmp_path, user="bob")
    assert bob < 0
    store.reset_calibration(tmp_path, "alice")
    state = store.calibration_state(tmp_path, "alice")
    assert state == {
        "attempts": 0,
        "accepted_samples": 0,
        "feedback": {},
        "scopes": [],
        "legacy": None,
    }
    assert bias(tmp_path) == 0
    assert store.attempt_state(tmp_path, "alice", "chat", "a1") is None
    assert bias(tmp_path, user="bob") == pytest.approx(bob)
    # Idempotent, and the same ID can be registered and learned again afterwards.
    store.reset_calibration(tmp_path, "alice")
    register(tmp_path, "a1")
    assert accept(tmp_path, "a1")["learned"] is True


def test_attempt_and_scope_storage_is_bounded_but_keeps_learning(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MAX_ATTEMPTS", 3)
    monkeypatch.setattr(store, "MAX_SCOPES", 2)
    for index in range(5):
        register(tmp_path, f"a{index}", workflow=f"w{index}")
    state = store.calibration_state(tmp_path, "alice")
    assert state["attempts"] == 3
    assert store.attempt_state(tmp_path, "alice", "chat", "a0") is None
    assert store.attempt_state(tmp_path, "alice", "chat", "a4") is not None
    for index in (2, 3, 4):
        accept(tmp_path, f"a{index}")
    scopes = store.calibration_state(tmp_path, "alice")["scopes"]
    assert len(scopes) == 2
    assert {scope["workflow"] for scope in scopes} == {"w3", "w4"}
    assert store.calibration_state(tmp_path, "alice")["accepted_samples"] == 3


def test_database_is_one_transactional_sqlite_file_with_hashed_accounts(tmp_path):
    register(tmp_path, user="alice@example.com")
    path = tmp_path / "pepito-calibration" / "calibration.sqlite3"
    assert path.is_file()
    with sqlite3.connect(path) as connection:
        accounts = [row[0] for row in connection.execute("SELECT user_id FROM attempt")]
    assert accounts == [hashlib.sha256(b"alice@example.com").hexdigest()]
    assert b"alice@example.com" not in path.read_bytes()
