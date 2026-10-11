"""Read-only access to the SQLite snapshot built by `flagbox ingest`.

Every value that comes from a tool argument is passed as a bound `?` parameter. The few SQL
fragments that are joined into queries (FLAG_SQL) are constants written in this file.
"""

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

# A process belongs to a date range by its tender closing date (SPEC D22).
RANGE_SQL = "substr(p.period_end, 1, 10) BETWEEN ? AND ?"
PLAUSIBLE_CLOSE_SQL = "substr(p.period_end, 1, 4) BETWEEN '2015' AND '2026'"

# "Usable" tests per field, as SQL conditions on awards `a` and processes `p`.
# Value: positive, in the data's currency, and not above the sanity cap (two bound params).
FLAG_SQL: dict[str, str] = {
    "value": "(a.amount > 0 AND a.currency = ? AND a.amount <= ?)",
    "supplier_id": (
        "EXISTS (SELECT 1 FROM award_suppliers s WHERE s.ocid = a.ocid "
        "AND s.award_id = a.award_id AND s.raw_id IS NOT NULL)"
    ),
    "category": "p.category IS NOT NULL",
    "method": "p.method IS NOT NULL",
}


class ToolInputError(ValueError):
    """A bad tool argument. Returned to the model as a structured error, not a crash."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"No snapshot at {db_path}. Run `flagbox ingest` first.")
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def parse_range(date_from: str, date_to: str) -> tuple[str, str]:
    try:
        start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except (TypeError, ValueError):
        raise ToolInputError("bad_date", "Dates must look like 2023-01-01 (YYYY-MM-DD).") from None
    if start > end:
        raise ToolInputError("bad_range", "date_from must not be after date_to.")
    return start.isoformat(), end.isoformat()


def require_buyer(conn: sqlite3.Connection, buyer_key: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT buyer_key, name FROM buyers WHERE buyer_key = ?", (buyer_key,)
    ).fetchone()
    if row is None:
        raise ToolInputError(
            "unknown_buyer", "No buyer has that buyer_key. Use find_buyer to look one up."
        )
    return row


def source_files(conn: sqlite3.Connection) -> dict[int, dict[str, str]]:
    rows = conn.execute("SELECT file_id, name, url, sha256 FROM source_files").fetchall()
    return {
        r["file_id"]: {"file": r["name"], "dataset_url": r["url"], "sha256": r["sha256"]}
        for r in rows
    }


def source_ref(row: sqlite3.Row, files: dict[int, dict[str, str]]) -> dict[str, Any]:
    """Compact reference to the exact source line.

    The full reference adds the file's dataset_url and sha256 (see sources_block).
    """
    return {
        "file": files[row["file_id"]]["file"],
        "line_no": row["line_no"],
        "ocid": row["ocid"],
        "portal_url": row["portal_url"],
    }


def sources_block(files: dict[int, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Per-file dataset URL and hash, given once per tool result instead of once per item."""
    return {
        f["file"]: {"dataset_url": f["dataset_url"], "sha256": f["sha256"]} for f in files.values()
    }


def count_processes(conn: sqlite3.Connection, buyer_key: str, d_from: str, d_to: str) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) FROM processes p WHERE p.buyer_key = ? AND {RANGE_SQL}",
        (buyer_key, d_from, d_to),
    ).fetchone()
    return int(row[0])


def count_awards(conn: sqlite3.Connection, buyer_key: str, d_from: str, d_to: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM processes p JOIN awards a ON a.ocid = p.ocid "
        f"WHERE p.buyer_key = ? AND {RANGE_SQL}",
        (buyer_key, d_from, d_to),
    ).fetchone()
    return int(row[0])


AWARD_COLUMNS = (
    "a.ocid, a.award_id, a.amount, a.currency, p.title, p.method, p.category, "
    "substr(p.period_end, 1, 10) AS closed, p.file_id, p.line_no, p.portal_url"
)


def awards_in_range(
    conn: sqlite3.Connection,
    buyer_key: str,
    d_from: str,
    d_to: str,
    limit: int | None = None,
    offset: int = 0,
) -> list[sqlite3.Row]:
    sql = (
        f"SELECT {AWARD_COLUMNS} FROM processes p JOIN awards a ON a.ocid = p.ocid "
        f"WHERE p.buyer_key = ? AND {RANGE_SQL} ORDER BY p.period_end, a.ocid, a.award_id"
    )
    params: list[Any] = [buyer_key, d_from, d_to]
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
    return conn.execute(sql, params).fetchall()


def suppliers_in_range(
    conn: sqlite3.Connection, buyer_key: str, d_from: str, d_to: str
) -> dict[tuple[str, str], list[sqlite3.Row]]:
    rows = conn.execute(
        "SELECT s.ocid, s.award_id, s.supplier_key, s.display_name, s.name_withheld "
        "FROM award_suppliers s JOIN processes p ON p.ocid = s.ocid "
        f"WHERE p.buyer_key = ? AND {RANGE_SQL} ORDER BY s.ocid, s.award_id, s.supplier_key",
        (buyer_key, d_from, d_to),
    ).fetchall()
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault((row["ocid"], row["award_id"]), []).append(row)
    return grouped


def supplier_label(display_name: str, supplier_key: str, withheld: bool) -> str:
    """How a supplier is named in claims. Withheld names already carry the key."""
    return display_name if withheld else f"{display_name} (ID {supplier_key})"


def coverage_counts(
    conn: sqlite3.Connection,
    buyer_key: str,
    d_from: str,
    d_to: str,
    needs: dict[str, tuple[str, ...]],
    currency: str,
    cap: float,
) -> dict[str, int]:
    """Counts of awards in range: total, each usable field, each indicator's joint coverage,
    and the data-quality exclusions. One query, all conditions bound."""

    def flag(name: str) -> tuple[str, list[Any]]:
        return FLAG_SQL[name], ([currency, cap] if name == "value" else [])

    selects: list[str] = ["COUNT(*) AS awards"]
    params: list[Any] = []

    def add(alias: str, condition: str, bound: list[Any]) -> None:
        selects.append(f"COALESCE(SUM(CASE WHEN {condition} THEN 1 ELSE 0 END), 0) AS {alias}")
        params.extend(bound)

    for name in FLAG_SQL:
        sql, bound = flag(name)
        add(f"f_{name}", sql, bound)
    for indicator, fields in needs.items():
        parts = [flag(f) for f in fields]
        add(f"j_{indicator}", " AND ".join(p[0] for p in parts), [b for p in parts for b in p[1]])
    add("dq_no_value", "a.amount IS NULL OR a.amount <= 0", [])
    add("dq_other_currency", "a.amount > 0 AND a.currency IS NOT ?", [currency])
    add("dq_above_cap", "a.amount > ? AND a.currency = ?", [cap, currency])

    row = conn.execute(
        f"SELECT {', '.join(selects)} FROM processes p JOIN awards a ON a.ocid = p.ocid "
        f"WHERE p.buyer_key = ? AND {RANGE_SQL}",
        [*params, buyer_key, d_from, d_to],
    ).fetchone()
    return {key: int(row[key]) for key in row.keys()}
