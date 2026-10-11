"""Run state: the hits and findings a run produces (SPEC 4.1).

This lives in its own SQLite file (data/run_state.sqlite), separate from the read-only snapshot,
so re-ingesting the data never deletes findings and the snapshot stays frozen.

Differences from SPEC 4.1, logged as decision D29: `hits` is keyed by (run_id, hit_id) because
hit IDs are deterministic and the same hit can appear in several runs; rejected attempts are
kept as `findings` rows with status "rejected" (no rationale text stored) so evals can count
them.
"""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flagbox.agent.verify import LoggedHit, Violation

RUN_STATE_NAME = "run_state.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    buyer_key      TEXT,
    buyer_name     TEXT,
    date_from      TEXT NOT NULL,
    date_to        TEXT NOT NULL,
    model          TEXT NOT NULL,
    num_ctx        INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    status         TEXT NOT NULL,
    tool_calls     INTEGER,
    llm_calls      INTEGER,
    tokens_in      INTEGER,
    tokens_out     INTEGER
);

CREATE TABLE IF NOT EXISTS hits (
    run_id        TEXT NOT NULL,
    hit_id        TEXT NOT NULL,
    tool_call_id  TEXT NOT NULL,
    indicator     TEXT NOT NULL,
    claim_text    TEXT NOT NULL,
    ocids         TEXT NOT NULL,
    source_refs   TEXT NOT NULL,
    source_fields TEXT NOT NULL,
    sources       TEXT NOT NULL,
    PRIMARY KEY (run_id, hit_id)
);

CREATE TABLE IF NOT EXISTS findings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id TEXT UNIQUE,
    run_id     TEXT NOT NULL,
    hit_id     TEXT NOT NULL,
    severity   TEXT,
    rationale  TEXT,
    reason_code TEXT,
    status     TEXT NOT NULL CHECK (status IN ('kept', 'dismissed', 'rejected')),
    reason     TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_findings_run ON findings (run_id, status);
"""


def connect(data_dir: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(data_dir / RUN_STATE_NAME)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(findings)")}
    if "reason_code" not in columns:  # a database made before decision D31
        connection.execute("ALTER TABLE findings ADD COLUMN reason_code TEXT")
        connection.commit()
    return connection


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def finding_id_for(run_id: str, hit_id: str) -> str:
    return "F-" + hashlib.sha256(f"{run_id}|{hit_id}".encode()).hexdigest()[:8]


def has_decision(conn: sqlite3.Connection, run_id: str, hit_id: str) -> bool:
    """True if this hit already has a kept or dismissed decision in this run."""
    row = conn.execute(
        "SELECT 1 FROM findings WHERE run_id = ? AND hit_id = ? "
        "AND status IN ('kept', 'dismissed')",
        (run_id, hit_id),
    ).fetchone()
    return row is not None


def store_kept_finding(
    conn: sqlite3.Connection,
    run_id: str,
    logged: LoggedHit,
    severity: str,
    reason_code: str,
    rationale: str,
) -> str:
    """Save a verified finding. Everything about the hit is copied from the logged tool output."""
    hit = logged.hit
    refs: list[dict[str, Any]] = list(hit.get("source_refs") or [])
    finding_id = finding_id_for(run_id, hit["hit_id"])
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO hits VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                hit["hit_id"],
                logged.tool_call_id,
                str(hit.get("indicator", logged.tool)),
                str(hit["claim_text"]),
                json.dumps([r.get("ocid") for r in refs]),
                json.dumps(refs),
                json.dumps(hit.get("source_fields") or []),
                json.dumps(logged.sources),
            ),
        )
        conn.execute(
            "INSERT INTO findings (finding_id, run_id, hit_id, severity, rationale, "
            "reason_code, status, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'kept', NULL, ?)",
            (finding_id, run_id, hit["hit_id"], severity, rationale, reason_code, _now()),
        )
    return finding_id


def store_rejection(
    conn: sqlite3.Connection, run_id: str, hit_id: str, violations: list[Violation]
) -> None:
    """Record that an attempt was refused and why. The refused text itself is not stored."""
    with conn:
        conn.execute(
            "INSERT INTO findings (finding_id, run_id, hit_id, severity, rationale, status, "
            "reason, created_at) VALUES (NULL, ?, ?, NULL, NULL, 'rejected', ?, ?)",
            (run_id, hit_id[:80], ",".join(v.rule for v in violations), _now()),
        )


def start_run(
    conn: sqlite3.Connection,
    run_id: str,
    date_from: str,
    date_to: str,
    model: str,
    num_ctx: int,
    prompt_version: str,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, date_from, date_to, model, num_ctx, prompt_version, "
            "started_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'running')",
            (run_id, date_from, date_to, model, num_ctx, prompt_version, _now()),
        )


def set_run_buyer(conn: sqlite3.Connection, run_id: str, buyer_key: str, buyer_name: str) -> None:
    with conn:
        conn.execute(
            "UPDATE runs SET buyer_key = ?, buyer_name = ? WHERE run_id = ?",
            (buyer_key, buyer_name, run_id),
        )


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    tool_calls: int,
    llm_calls: int,
    tokens_in: int,
    tokens_out: int,
) -> None:
    with conn:
        conn.execute(
            "UPDATE runs SET ended_at = ?, status = ?, tool_calls = ?, llm_calls = ?, "
            "tokens_in = ?, tokens_out = ? WHERE run_id = ?",
            (_now(), status, tool_calls, llm_calls, tokens_in, tokens_out, run_id),
        )


def kept_findings(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT f.id, f.finding_id, f.hit_id, f.severity, f.rationale, f.reason_code, "
        "h.claim_text, "
        "h.source_refs, h.sources, h.tool_call_id "
        "FROM findings f JOIN hits h ON h.run_id = f.run_id AND h.hit_id = f.hit_id "
        "WHERE f.run_id = ? AND f.status = 'kept' ORDER BY f.id",
        (run_id,),
    ).fetchall()


def reject_kept_finding(conn: sqlite3.Connection, row_id: int, reason: str) -> None:
    """Used by the verify step if a stored finding no longer passes the rules."""
    with conn:
        conn.execute(
            "UPDATE findings SET status = 'rejected', reason = ?, rationale = NULL, "
            "finding_id = NULL WHERE id = ?",
            (reason, row_id),
        )
