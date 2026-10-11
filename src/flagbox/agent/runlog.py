"""The run log: one tamper-evident JSONL file per run, logs/<run_id>.jsonl.

Each line is a JSON record with a sequence number, a timestamp, an event name, the hash of the
previous line (`prev_hash`) and its own `hash`. The hash covers every other field of the line, so
editing, deleting, inserting or reordering a line breaks the chain and `verify_log` says where.

What this does and does not prove: it shows that a log was not edited by accident or by someone
who does not recompute the whole chain. It has no secret key, so a person who rewrites every line
and every hash cannot be caught from the file alone. Keep the head hash that `flagbox verify-log`
prints (the approval record stores it) to close that gap.
"""

import hashlib
import json
import os
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64
REQUIRED_KEYS = ("seq", "ts", "run_id", "event", "prev_hash", "hash")
RESERVED_KEYS = frozenset(REQUIRED_KEYS)


class LogError(Exception):
    """The log cannot be used (for example, it was tampered with)."""


def parse_run_id(value: str) -> str:
    """Accept only a canonical lower-case UUID. This is what keeps run IDs out of path tricks."""
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(
            "run_id must be a UUID such as 123e4567-e89b-12d3-a456-426614174000"
        ) from None
    if value != canonical:
        raise ValueError("run_id must be written as a lower-case hyphenated UUID")
    return canonical


def log_path(logs_dir: Path, run_id: str) -> Path:
    return logs_dir / f"{parse_run_id(run_id)}.jsonl"


def canonical_json(record: dict[str, Any]) -> str:
    """One fixed way to write a record, so the same content always gives the same hash."""
    return json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def _hash_of(body: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    run_id: str
    records: int = 0
    tool_calls: int = 0
    complete: bool = False  # a run_end record is present
    head_hash: str = GENESIS_HASH
    line: int | None = None  # 1-based line number of the first problem
    error: str | None = None


def _fail(run_id: str, line: int, error: str, **counts: Any) -> VerifyResult:
    return VerifyResult(False, run_id, line=line, error=error, **counts)


def verify_log(path: Path) -> VerifyResult:
    """Check every line of a log. Returns the first problem found, or ok with the head hash."""
    run_id = path.stem
    if not path.exists():
        return _fail(run_id, 0, f"log file not found: {path}")

    previous = GENESIS_HASH
    records = tool_calls = 0
    ended = False
    seen_ids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for number, raw in enumerate(handle, start=1):
                line = raw.rstrip("\r\n")
                counts = {"records": records, "tool_calls": tool_calls}
                if not line.strip():
                    return _fail(run_id, number, "blank line", **counts)
                if ended:
                    return _fail(run_id, number, "record after run_end", **counts)
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return _fail(run_id, number, "not valid JSON", **counts)
                if not isinstance(record, dict) or any(k not in record for k in REQUIRED_KEYS):
                    return _fail(run_id, number, "missing required fields", **counts)
                if record["seq"] != number - 1:
                    return _fail(
                        run_id,
                        number,
                        "sequence number out of order (line moved or removed)",
                        **counts,
                    )
                if record["prev_hash"] != previous:
                    return _fail(
                        run_id,
                        number,
                        "does not follow the previous line (line removed, inserted or moved)",
                        **counts,
                    )
                body = {k: v for k, v in record.items() if k != "hash"}
                if _hash_of(body) != record["hash"]:
                    return _fail(run_id, number, "hash mismatch (line was edited)", **counts)
                if record["run_id"] != run_id:
                    return _fail(run_id, number, "record belongs to a different run", **counts)
                if number == 1 and record["event"] != "run_start":
                    return _fail(run_id, number, "first record is not run_start", **counts)
                if record["event"] == "tool_call":
                    call_id = record.get("tool_call_id")
                    if not isinstance(call_id, str) or call_id in seen_ids:
                        return _fail(run_id, number, "missing or repeated tool_call_id", **counts)
                    seen_ids.add(call_id)
                    tool_calls += 1
                ended = record["event"] == "run_end"
                previous = record["hash"]
                records += 1
    except UnicodeDecodeError:
        return _fail(run_id, records + 1, "not valid UTF-8", records=records, tool_calls=tool_calls)

    if records == 0:
        return _fail(run_id, 0, "log is empty")
    return VerifyResult(
        True, run_id, records=records, tool_calls=tool_calls, complete=ended, head_hash=previous
    )


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the records of a log that has already passed verify_log."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        for raw in handle:
            yield json.loads(raw)


class RunLog:
    """Appends records to one run's log, keeping the hash chain. Safe to share between sessions."""

    def __init__(self, path: Path, run_id: str, seq: int, prev_hash: str, tool_calls: int) -> None:
        self.path = path
        self.run_id = run_id
        self._seq = seq
        self._prev = prev_hash
        self._tool_calls = tool_calls
        self._lock = threading.Lock()

    @classmethod
    def create(cls, logs_dir: Path, run_id: str | None = None, **start_fields: Any) -> "RunLog":
        run_id = parse_run_id(run_id) if run_id else str(uuid.uuid4())
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = log_path(logs_dir, run_id)
        with path.open("x", encoding="utf-8"):  # "x": refuse to touch a log that already exists
            pass
        log = cls(path, run_id, seq=0, prev_hash=GENESIS_HASH, tool_calls=0)
        log.append("run_start", **start_fields)
        return log

    @classmethod
    def resume(cls, logs_dir: Path, run_id: str) -> "RunLog":
        """Continue an unfinished run's chain (the approval step resumes after a pause)."""
        path = log_path(logs_dir, run_id)
        result = verify_log(path)
        if not result.ok:
            raise LogError(f"cannot resume: line {result.line}: {result.error}")
        if result.complete:
            raise LogError("cannot resume: this run already has a run_end record")
        return cls(path, result.run_id, result.records, result.head_hash, result.tool_calls)

    def next_tool_call_id(self) -> str:
        with self._lock:
            self._tool_calls += 1
            return f"tc-{self._tool_calls:04d}"

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        clash = RESERVED_KEYS & set(fields)
        if clash:
            raise ValueError(f"fields may not use reserved names: {sorted(clash)}")
        with self._lock:
            body: dict[str, Any] = {
                "seq": self._seq,
                "ts": _now(),
                "run_id": self.run_id,
                "event": event,
                "prev_hash": self._prev,
                **fields,
            }
            record = {**body, "hash": _hash_of(body)}
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(canonical_json(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._seq += 1
            self._prev = record["hash"]
            return record

    def finish(self, status: str, **fields: Any) -> dict[str, Any]:
        return self.append("run_end", status=status, **fields)

    @property
    def head_hash(self) -> str:
        return self._prev
