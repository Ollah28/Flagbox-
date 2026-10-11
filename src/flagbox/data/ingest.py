"""M1.2: turn the downloaded PPRA files into a redacted, live-tender-filtered SQLite snapshot.

For every line of every file listed in data/MANIFEST.json:
  1. check the file still matches its recorded SHA-256 (provenance);
  2. drop contactPoint blocks, then apply the live-tender filter;
  3. store only the fields in schema.sql, with supplier names redacted (see redact.py).

The database is rebuilt from scratch each time, so there is nothing to migrate.
"""

import gzip
import json
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from flagbox.data import live_filter, redact
from flagbox.data.download import read_manifest, sha256_of

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DB_NAME = "flagbox.sqlite"

# The portal's page for one tender is https://tenders.go.ke/tenders/<number> (spike S2, M1.4).
# <number> is the middle part of the raw tender.id, "<14-digit timestamp>-<number>-<tender ref>".
# It cannot be taken from the OCID: only 12 of 57,576 OCIDs contain it.
PORTAL_BASE_URL = "https://tenders.go.ke/tenders/"
_TENDER_ID = re.compile(r"^\d{14}-(\d+)-")


def portal_url(tender_id: str | None) -> str | None:
    match = _TENDER_ID.match(tender_id) if tender_id else None
    return PORTAL_BASE_URL + match.group(1) if match else None


@dataclass
class IngestReport:
    today: date
    seconds: float = 0.0
    lines_read: int = 0
    kept: int = 0
    excluded: Counter[str] = field(default_factory=Counter)
    rows: Counter[str] = field(default_factory=Counter)  # rows written per table
    suppliers_skipped: int = 0  # award suppliers with neither an ID nor a name


# ---- tiny helpers for reading untrusted JSON without crashing on odd shapes -------------------


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


# ---- one release -> rows ---------------------------------------------------------------------


def _buyer_row(release: dict[str, Any]) -> tuple[str, str | None, str, str]:
    buyer = _dict(release.get("buyer"))
    raw_id = _text(buyer.get("id"))
    name = _text(buyer.get("name")) or "(unknown)"
    name_norm = redact.normalise_name(name)
    key = raw_id if raw_id else f"name:{name_norm}"
    return key, raw_id, name, name_norm


def _party_names(release: dict[str, Any]) -> dict[str, str]:
    """Fallback names for suppliers whose award entry has no name (parties may hold it)."""
    names: dict[str, str] = {}
    for party in map(_dict, _list(release.get("parties"))):
        party_id = _text(party.get("id"))
        name = _text(party.get("name")) or _text(_dict(party.get("identifier")).get("legalName"))
        if party_id and name:
            names[party_id] = name
    return names


def _process_rows(
    release: dict[str, Any], file_id: int, line_no: int, report: IngestReport
) -> dict[str, list[tuple[Any, ...]]]:
    ocid = _text(release.get("ocid")) or ""
    tender = _dict(release.get("tender"))
    period = _dict(tender.get("tenderPeriod"))
    tender_value = _dict(tender.get("value"))
    buyer_key, buyer_raw_id, buyer_name, buyer_norm = _buyer_row(release)
    tenderers = tender.get("numberOfTenderers")

    rows: dict[str, list[tuple[Any, ...]]] = {
        "buyers": [(buyer_key, buyer_raw_id, buyer_name, buyer_norm)],
        "processes": [
            (
                ocid,
                file_id,
                line_no,
                buyer_key,
                _text(tender.get("title")),
                _text(tender.get("procurementMethod")),
                _text(tender.get("procurementMethodDetails")),
                _text(tender.get("mainProcurementCategory")),
                tenderers if isinstance(tenderers, int) and not isinstance(tenderers, bool) else None,
                _text(period.get("startDate")),
                _text(period.get("endDate")),
                _text(tender.get("status")),
                _number(tender_value.get("amount")),
                _text(tender_value.get("currency")),
                portal_url(_text(tender.get("id"))),
            )
        ],
        "awards": [],
        "award_suppliers": [],
        "contracts": [],
    }

    party_names = _party_names(release)
    for index, award in enumerate(map(_dict, _list(release.get("awards")))):
        award_id = _text(award.get("id")) or f"award-{index}"
        value = _dict(award.get("value"))
        rows["awards"].append(
            (
                ocid,
                award_id,
                _text(award.get("date")),
                _number(value.get("amount")),
                _text(value.get("currency")),
                _text(award.get("status")),
            )
        )
        for supplier in map(_dict, _list(award.get("suppliers"))):
            raw_id = _text(supplier.get("id"))
            name = _text(supplier.get("name")) or (party_names.get(raw_id) if raw_id else None)
            key = redact.supplier_key(raw_id, name)
            if key is None:
                report.suppliers_skipped += 1
                continue
            shown, withheld = redact.display_name(name, key)
            rows["award_suppliers"].append((ocid, award_id, key, raw_id, shown, int(withheld)))

    for index, contract in enumerate(map(_dict, _list(release.get("contracts")))):
        value = _dict(contract.get("value"))
        rows["contracts"].append(
            (
                _text(contract.get("id")) or f"contract-{index}",
                ocid,
                _text(contract.get("awardID")),
                _text(contract.get("dateSigned")),
                _number(value.get("amount")),
            )
        )
    return rows


_INSERTS = {
    "buyers": "INSERT OR IGNORE INTO buyers VALUES (?, ?, ?, ?)",
    "processes": "INSERT OR IGNORE INTO processes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    "awards": "INSERT OR IGNORE INTO awards VALUES (?, ?, ?, ?, ?, ?)",
    "award_suppliers": "INSERT OR IGNORE INTO award_suppliers VALUES (?, ?, ?, ?, ?, ?)",
    "contracts": "INSERT OR IGNORE INTO contracts VALUES (?, ?, ?, ?, ?)",
}


def _verified_manifest_files(data_dir: Path) -> list[dict[str, Any]]:
    entries = read_manifest(data_dir / "MANIFEST.json")
    if not entries:
        raise FileNotFoundError("No data/MANIFEST.json found. Run `flagbox setup-data` first.")
    files = [entries[name] for name in sorted(entries)]
    for entry in files:
        path = data_dir / "raw" / str(entry["name"])
        if not path.exists() or sha256_of(path) != entry["sha256"]:
            raise ValueError(
                f"{entry['name']} is missing or does not match MANIFEST.json. "
                "Run `flagbox setup-data` again."
            )
    return files


def ingest(data_dir: Path, today: date | None = None) -> IngestReport:
    """Rebuild data_dir/flagbox.sqlite from the files in data_dir/raw/."""
    report = IngestReport(today=today or date.today())
    started = time.perf_counter()
    files = _verified_manifest_files(data_dir)

    db_path = data_dir / DB_NAME
    db_path.unlink(missing_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for file_id, entry in enumerate(files, start=1):
            connection.execute(
                "INSERT INTO source_files VALUES (?, ?, ?, ?, ?, ?)",
                (
                    file_id,
                    entry["name"],
                    entry["url"],
                    entry["sha256"],
                    entry["downloaded_at"],
                    entry["line_count"],
                ),
            )
            batch: dict[str, list[tuple[Any, ...]]] = {table: [] for table in _INSERTS}
            raw_path = data_dir / "raw" / str(entry["name"])
            with gzip.open(raw_path, "rt", encoding="utf-8") as lines:
                for line_no, line in enumerate(lines, start=1):
                    report.lines_read += 1
                    release = redact.drop_contact_points(json.loads(line))
                    if not isinstance(release, dict):
                        continue
                    reason = live_filter.exclusion_reason(release, report.today)
                    if reason is not None:
                        report.excluded[reason] += 1
                        continue
                    report.kept += 1
                    for table, rows in _process_rows(release, file_id, line_no, report).items():
                        batch[table].extend(rows)
            for table, sql in _INSERTS.items():
                connection.executemany(sql, batch[table])
            connection.commit()

        for table in ("source_files", "buyers", "processes", "awards", "award_suppliers", "contracts"):
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # fixed names
            report.rows[table] = count
    finally:
        connection.close()

    report.seconds = time.perf_counter() - started
    return report
