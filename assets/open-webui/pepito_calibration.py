"""Account-scoped image calibration; pure stdlib and no generation side effects.

Only a delivered attempt explicitly accepted by its owner can train a scope.
Negative feedback is evidence, not proof that a proposed correction worked.
Manual or workflow-fixed strengths never train automatic settings.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

# Starting strength per change level (Krea ``ref_boost``); learned bias moves the target.
BASE_STRENGTHS = {"subtle": 4.0, "moderate": 2.5, "strong": 1.5}
# Where a learned target may go: the filter's automatic range (``FIDELITY_RANGE``).
STRENGTH_RANGE = (1.0, 5.0)
# Every ref_boost a delivered attempt may carry and still be recorded (an attempt that cannot
# be recorded loses its feedback and approval): the filter's automatic, manual and correction
# values, and the node's loosening below 1.0 down to 0.5 (session 02 delivered 0.5).
ATTEMPT_STRENGTH_RANGE = (0.5, 5.0)
BIAS_LIMIT = 2.5
LEARNING_RATE = 0.35
TIME_HALF_LIFE = 90 * 86400
SAMPLE_HALF_LIFE = 128
MAX_ATTEMPTS = 10000
MAX_SCOPES = 256
MAX_LEGACY_BYTES = 1024 * 1024
MAX_LEGACY_ACCOUNTS = 4096
LEGACY_FILE = "pepito-fidelity.json"
VERDICTS = ("accepted", "no_change", "too_much", "wrong_source")
FIDELITIES = ("auto", "correction", "manual", "workflow")
TRAINING_FIDELITIES = ("auto", "correction")
MAX_INT = 2_147_483_647


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"Invalid {name}.")
    return value


def _account(user_id):
    return hashlib.sha256(_identifier(user_id, "user_id").encode()).hexdigest()


def _number(value, name, low, high, *, nullable=False):
    if nullable and value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"Invalid {name}.")
    return float(value)


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"Invalid {name}.")
    return value


def _scope(family, workflow, change, operation="edit"):
    return tuple(
        _identifier(value, name)
        for value, name in (
            (family, "family"),
            (workflow, "workflow"),
            (change, "change"),
            (operation, "operation"),
        )
    )


def _facts(facts):
    if not isinstance(facts, dict):
        raise TypeError("Invalid attempt facts.")
    if facts.get("status") != "completed" or facts.get("delivered") is not True:
        raise ValueError("Only completed, delivered attempts can be registered.")
    family, workflow, change, operation = _scope(
        facts.get("family"),
        facts.get("workflow"),
        facts.get("change"),
        facts.get("operation", "edit"),
    )
    if facts.get("fidelity") not in FIDELITIES:
        raise ValueError("Invalid fidelity.")
    sources = facts.get("source_numbers")
    if not isinstance(sources, list) or len(sources) > 16:
        raise ValueError("Invalid source_numbers.")
    sources = [_integer(value, "source number", 1, MAX_INT) for value in sources]
    if len(set(sources)) != len(sources):
        raise ValueError("Duplicate source numbers.")
    clean = {
        "family": family,
        "workflow": workflow,
        "change": change,
        "operation": operation,
        "strength": _number(
            facts.get("strength"), "strength", *ATTEMPT_STRENGTH_RANGE, nullable=True
        ),
        "fidelity": facts["fidelity"],
        "request_id": _identifier(facts.get("request_id"), "request_id"),
        "source_numbers": sources,
        "measurement": _number(facts.get("measurement"), "measurement", 0, 255, nullable=True),
        "requested_count": _integer(facts.get("requested_count"), "requested_count", 1, 256),
        "status": "completed",
        "delivered": True,
    }
    if facts.get("output_number") is not None:
        clean["output_number"] = _integer(facts["output_number"], "output_number", 1, MAX_INT)
    if facts.get("previous_attempt_id") is not None:
        clean["previous_attempt_id"] = _identifier(
            facts["previous_attempt_id"], "previous_attempt_id"
        )
    return clean


def _migrate_legacy(connection, data_dir, now):
    """Import ``pepito-fidelity.json`` exactly once.

    Its global ``bias`` came from picks between variations: a kept picture, so it stays
    usable (and fades). Its per-level offsets were moved by *rejections* before any retry
    was confirmed; they are retained only as unconfirmed evidence and never applied.
    """
    if connection.execute("SELECT 1 FROM metadata WHERE key='legacy-migrated'").fetchone():
        return
    path = Path(data_dir) / LEGACY_FILE
    saved = {}
    try:
        if path.is_file() and path.stat().st_size <= MAX_LEGACY_BYTES:
            saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if isinstance(saved, dict):
        for key, value in list(saved.items())[:MAX_LEGACY_ACCOUNTS]:
            if (
                not isinstance(key, str)
                or len(key) != 16
                or any(char not in "0123456789abcdef" for char in key)
                or not isinstance(value, dict)
            ):
                continue
            evidence = {"pick_bias": 0.0, "unconfirmed_levels": {}, "migrated_at": now}
            try:
                evidence["pick_bias"] = _number(
                    value.get("bias", 0.0), "legacy bias", -BIAS_LIMIT, BIAS_LIMIT
                )
            except ValueError:
                pass
            levels = value.get("levels")
            if isinstance(levels, dict):
                for level in BASE_STRENGTHS:
                    if level not in levels:
                        continue
                    try:
                        evidence["unconfirmed_levels"][level] = _number(
                            levels.get(level, 0.0), "legacy level", -BIAS_LIMIT, BIAS_LIMIT
                        )
                    except ValueError:
                        continue
            connection.execute(
                "INSERT OR IGNORE INTO legacy(account_prefix,evidence) VALUES (?,?)",
                (key, json.dumps(evidence, allow_nan=False)),
            )
    connection.execute("INSERT INTO metadata(key,value) VALUES ('legacy-migrated','1')")


SCHEMA = (
    "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS legacy(account_prefix TEXT PRIMARY KEY,evidence TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS account(user_id TEXT PRIMARY KEY,samples INTEGER NOT NULL DEFAULT 0)",
    (
        "CREATE TABLE IF NOT EXISTS attempt(user_id TEXT NOT NULL,attempt_id TEXT NOT NULL,"
        "chat_id TEXT NOT NULL,facts TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(user_id,attempt_id))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS feedback(user_id TEXT NOT NULL,attempt_id TEXT NOT NULL,"
        "verdict TEXT NOT NULL,created REAL NOT NULL,learned INTEGER NOT NULL DEFAULT 0,"
        "PRIMARY KEY(user_id,attempt_id,verdict))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS calibration(user_id TEXT NOT NULL,family TEXT NOT NULL,"
        "workflow TEXT NOT NULL,change TEXT NOT NULL,operation TEXT NOT NULL,bias REAL NOT NULL,"
        "accepted INTEGER NOT NULL,updated REAL NOT NULL,sample INTEGER NOT NULL,"
        "PRIMARY KEY(user_id,family,workflow,change,operation))"
    ),
)


@contextmanager
def _transaction(data_dir):
    root = Path(data_dir) / "pepito-calibration"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "calibration.sqlite3"
    connection = sqlite3.connect(path, timeout=15)
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        for statement in SCHEMA:
            connection.execute(statement)
        _migrate_legacy(connection, data_dir, time.time())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def register_attempt(data_dir, user_id, chat_id, attempt_id, facts: dict) -> None:
    """Store immutable, bounded facts of a completed delivered attempt.

    Repeating identical facts is safe; reusing an ID for different facts is an error.
    Registration alone never trains anything.
    """
    account = _account(user_id)
    chat_id = _identifier(chat_id, "chat_id")
    attempt_id = _identifier(attempt_id, "attempt_id")
    encoded = json.dumps(_facts(facts), sort_keys=True, separators=(",", ":"), allow_nan=False)
    with _transaction(data_dir) as connection:
        previous = connection.execute(
            "SELECT chat_id,facts FROM attempt WHERE user_id=? AND attempt_id=?",
            (account, attempt_id),
        ).fetchone()
        if previous:
            if tuple(previous) != (chat_id, encoded):
                raise ValueError("An attempt ID cannot be reassigned.")
            return
        connection.execute("INSERT OR IGNORE INTO account(user_id) VALUES (?)", (account,))
        connection.execute(
            "INSERT INTO attempt VALUES (?,?,?,?,?)",
            (account, attempt_id, chat_id, encoded, time.time()),
        )
        # Learned scopes are kept separately, so the oldest evidence can be forgotten.
        excess = (
            connection.execute(
                "SELECT COUNT(*) FROM attempt WHERE user_id=?", (account,)
            ).fetchone()[0]
            - MAX_ATTEMPTS
        )
        if excess > 0:
            connection.execute(
                "DELETE FROM attempt WHERE rowid IN (SELECT rowid FROM attempt WHERE user_id=? "
                "ORDER BY created,rowid LIMIT ?)",
                (account, excess),
            )
        connection.execute(
            "DELETE FROM feedback WHERE user_id=? AND attempt_id NOT IN "
            "(SELECT attempt_id FROM attempt WHERE user_id=?)",
            (account, account),
        )


def _attempt(connection, account, chat_id, attempt_id):
    row = connection.execute(
        "SELECT facts FROM attempt WHERE user_id=? AND chat_id=? AND attempt_id=?",
        (account, chat_id, attempt_id),
    ).fetchone()
    return json.loads(row[0]) if row else None


def attempt_state(data_dir, user_id, chat_id, attempt_id) -> dict | None:
    """The owner's registered facts plus recorded verdicts; None for any other account/chat."""
    account = _account(user_id)
    chat_id = _identifier(chat_id, "chat_id")
    attempt_id = _identifier(attempt_id, "attempt_id")
    with _transaction(data_dir) as connection:
        facts = _attempt(connection, account, chat_id, attempt_id)
        if facts is None:
            return None
        feedback = {
            verdict: bool(learned)
            for verdict, learned in connection.execute(
                "SELECT verdict,learned FROM feedback WHERE user_id=? AND attempt_id=?",
                (account, attempt_id),
            )
        }
        return {**facts, "attempt_id": attempt_id, "chat_id": chat_id, "feedback": feedback}


def _decay(value, then, sample, now, samples):
    """Fade by elapsed time and by how many accepted samples followed."""
    elapsed = max(0.0, now - then)
    newer = max(0, samples - sample)
    return float(value) * math.exp2(-elapsed / TIME_HALF_LIFE - newer / SAMPLE_HALF_LIFE)


def _samples(connection, account):
    row = connection.execute("SELECT samples FROM account WHERE user_id=?", (account,)).fetchone()
    return row[0] if row else 0


def _legacy(connection, account, now, samples):
    row = connection.execute(
        "SELECT evidence FROM legacy WHERE account_prefix=?", (account[:16],)
    ).fetchone()
    if row is None:
        return None, 0.0
    evidence = json.loads(row[0])
    return evidence, _decay(evidence["pick_bias"], evidence["migrated_at"], 0, now, samples)


def record_feedback(data_dir, user_id, chat_id, attempt_id, verdict) -> dict:
    """Record one verdict per attempt; only a first-time ``accepted`` of an eligible,
    not previously rejected automatic attempt moves its scope toward the strength used."""
    account = _account(user_id)
    chat_id = _identifier(chat_id, "chat_id")
    attempt_id = _identifier(attempt_id, "attempt_id")
    if verdict not in VERDICTS:
        raise ValueError("Invalid feedback verdict.")
    with _transaction(data_dir) as connection:
        facts = _attempt(connection, account, chat_id, attempt_id)
        if facts is None:
            raise ValueError("Attempt not found in this account and chat.")
        previous = connection.execute(
            "SELECT learned FROM feedback WHERE user_id=? AND attempt_id=? AND verdict=?",
            (account, attempt_id, verdict),
        ).fetchone()
        if previous is not None:
            return {
                "attempt_id": attempt_id,
                "verdict": verdict,
                "recorded": False,
                "learned": bool(previous[0]),
            }
        now = time.time()
        rejected = connection.execute(
            "SELECT 1 FROM feedback WHERE user_id=? AND attempt_id=? AND verdict!='accepted'",
            (account, attempt_id),
        ).fetchone()
        learned = (
            verdict == "accepted"
            and rejected is None
            and facts["fidelity"] in TRAINING_FIDELITIES
            and facts["strength"] is not None
            and facts["change"] in BASE_STRENGTHS
        )
        if learned:
            scope = _scope(facts["family"], facts["workflow"], facts["change"], facts["operation"])
            row = connection.execute(
                "SELECT bias,accepted,updated,sample FROM calibration WHERE user_id=? AND "
                "family=? AND workflow=? AND change=? AND operation=?",
                (account, *scope),
            ).fetchone()
            samples = _samples(connection, account) + 1
            baseline = BASE_STRENGTHS[facts["change"]]
            if row:
                current = _decay(row[0], row[2], row[3], now, samples)
            else:
                current = _legacy(connection, account, now, samples)[1]
            target = max(-BIAS_LIMIT, min(BIAS_LIMIT, facts["strength"] - baseline))
            bias = current + LEARNING_RATE * (target - current)
            # Keep the learned target inside what any attempt could have used.
            bias = max(
                STRENGTH_RANGE[0] - baseline,
                -BIAS_LIMIT,
                min(STRENGTH_RANGE[1] - baseline, BIAS_LIMIT, bias),
            )
            if row is None:
                excess = (
                    connection.execute(
                        "SELECT COUNT(*) FROM calibration WHERE user_id=?", (account,)
                    ).fetchone()[0]
                    - MAX_SCOPES
                    + 1
                )
                if excess > 0:
                    connection.execute(
                        "DELETE FROM calibration WHERE rowid IN (SELECT rowid FROM calibration "
                        "WHERE user_id=? ORDER BY updated,rowid LIMIT ?)",
                        (account, excess),
                    )
            connection.execute("UPDATE account SET samples=? WHERE user_id=?", (samples, account))
            connection.execute(
                "INSERT INTO calibration VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,family,"
                "workflow,change,operation) DO UPDATE SET bias=excluded.bias,"
                "accepted=excluded.accepted,updated=excluded.updated,sample=excluded.sample",
                (account, *scope, bias, (row[1] if row else 0) + 1, now, samples),
            )
        connection.execute(
            "INSERT INTO feedback VALUES (?,?,?,?,?)",
            (account, attempt_id, verdict, now, int(learned)),
        )
        return {"attempt_id": attempt_id, "verdict": verdict, "recorded": True, "learned": learned}


def calibration_bias(data_dir, user_id, family, workflow, change, *, operation="edit") -> float:
    """The bounded offset for one scope: its own accepted learning, otherwise the account's
    legacy pick bias. Both fade with elapsed time and with later accepted samples."""
    account = _account(user_id)
    scope = _scope(family, workflow, change, operation)
    with _transaction(data_dir) as connection:
        now = time.time()
        samples = _samples(connection, account)
        row = connection.execute(
            "SELECT bias,updated,sample FROM calibration WHERE user_id=? AND family=? AND "
            "workflow=? AND change=? AND operation=?",
            (account, *scope),
        ).fetchone()
        value = _decay(*row, now, samples) if row else _legacy(connection, account, now, samples)[1]
        return max(-BIAS_LIMIT, min(BIAS_LIMIT, value))


def calibration_state(data_dir, user_id) -> dict:
    """Bounded, account-only summary: learned scopes, counts and legacy evidence."""
    account = _account(user_id)
    with _transaction(data_dir) as connection:
        now = time.time()
        samples = _samples(connection, account)
        scopes = []
        for (
            family,
            workflow,
            change,
            operation,
            bias,
            accepted,
            updated,
            sample,
        ) in connection.execute(
            "SELECT family,workflow,change,operation,bias,accepted,updated,sample FROM "
            "calibration WHERE user_id=? ORDER BY family,workflow,change,operation",
            (account,),
        ).fetchall():
            value = _decay(bias, updated, sample, now, samples)
            scopes.append(
                {
                    "family": family,
                    "workflow": workflow,
                    "change": change,
                    "operation": operation,
                    "bias": value,
                    "target": BASE_STRENGTHS[change] + value,
                    "accepted": accepted,
                    "updated_at": updated,
                }
            )
        legacy, legacy_bias = _legacy(connection, account, now, samples)
        return {
            "attempts": connection.execute(
                "SELECT COUNT(*) FROM attempt WHERE user_id=?", (account,)
            ).fetchone()[0],
            "accepted_samples": samples,
            "feedback": dict(
                connection.execute(
                    "SELECT verdict,COUNT(*) FROM feedback WHERE user_id=? GROUP BY verdict",
                    (account,),
                ).fetchall()
            ),
            "scopes": scopes,
            "legacy": None
            if legacy is None
            else {
                "bias": legacy_bias,
                "unconfirmed_levels": legacy["unconfirmed_levels"],
                "migrated_at": legacy["migrated_at"],
            },
        }


def reset_calibration(data_dir, user_id) -> None:
    """Delete this account's attempts, feedback, learning and legacy evidence for good:
    the legacy file is never imported a second time."""
    account = _account(user_id)
    with _transaction(data_dir) as connection:
        for table in ("feedback", "calibration", "attempt", "account"):
            connection.execute(f"DELETE FROM {table} WHERE user_id=?", (account,))
        connection.execute("DELETE FROM legacy WHERE account_prefix=?", (account[:16],))
