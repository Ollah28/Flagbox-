import gzip
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from flagbox.data.download import sha256_of, write_manifest
from flagbox.data.ingest import DB_NAME, ingest, portal_url

FIXTURE = Path(__file__).parent / "fixtures" / "ppra_sample.jsonl"
TODAY = date(2026, 10, 11)
# Fake personal data planted in the fixture; none of it may reach the database.
SECRETS = ["FAKE PERSON", "fake.person@example.test", "+254700000000", "FAKESON", "MARY NOBODY"]


def build_data_dir(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    raw.mkdir()
    target = raw / "2024.jsonl.gz"
    with gzip.open(target, "wb") as out:
        out.write(FIXTURE.read_bytes())
    entry: dict[str, object] = {
        "name": "2024.jsonl.gz",
        "url": "https://example.test/2024.jsonl.gz",
        "sha256": sha256_of(target),
        "downloaded_at": "2026-10-11T00:00:00+00:00",
        "line_count": 5,
    }
    write_manifest(tmp_path / "MANIFEST.json", {"2024.jsonl.gz": entry})
    return tmp_path


@pytest.fixture
def db(tmp_path: Path):
    data_dir = build_data_dir(tmp_path)
    report = ingest(data_dir, today=TODAY)
    connection = sqlite3.connect(data_dir / DB_NAME)
    yield connection, report
    connection.close()


def test_report_counts_kept_and_excluded(db):
    _, report = db
    assert report.lines_read == 5
    assert report.kept == 3
    assert dict(report.excluded) == {"no_award": 1, "end_date_not_past": 1}
    assert report.suppliers_skipped == 0


def test_only_kept_processes_are_stored_with_their_source_line(db):
    connection, _ = db
    rows = connection.execute(
        "SELECT ocid, file_id, line_no FROM processes ORDER BY line_no"
    ).fetchall()
    assert rows == [("ocds-test-001", 1, 1), ("ocds-test-002", 1, 2), ("ocds-test-005", 1, 5)]
    assert connection.execute("SELECT sha256 FROM source_files").fetchone()[0]


def test_process_fields_are_mapped(db):
    connection, _ = db
    row = connection.execute(
        "SELECT buyer_key, title, method, category, n_tenderers, period_end "
        "FROM processes WHERE ocid = ?",
        ("ocds-test-002",),
    ).fetchone()
    assert row == ("100", "Repair of road", "direct", "works", 1, "2024-05-01T00:00:00+03:00")


def test_buyer_without_id_falls_back_to_normalised_name(db):
    connection, _ = db
    key = connection.execute(
        "SELECT buyer_key FROM processes WHERE ocid = ?", ("ocds-test-005",)
    ).fetchone()[0]
    assert key == "name:nameonly buyer authority"


def test_suppliers_are_redacted(db):
    connection, _ = db
    rows = connection.execute(
        "SELECT ocid, display_name, name_withheld FROM award_suppliers ORDER BY ocid, display_name"
    ).fetchall()
    assert ("ocds-test-001", "Acme Furniture Ltd", 0) in rows
    assert ("ocds-test-002", "Supplier 901 (name withheld)", 1) in rows
    assert ("ocds-test-005", "Zeta Supplies Limited", 0) in rows
    nameless_id = [r for r in rows if r[0] == "ocds-test-005" and r[2] == 1]
    assert len(nameless_id) == 1 and nameless_id[0][1].startswith("Supplier name:")


def test_awards_and_contracts_are_stored(db):
    connection, _ = db
    assert connection.execute(
        "SELECT amount, currency FROM awards WHERE ocid = ?", ("ocds-test-001",)
    ).fetchone() == (250000.0, "KES")
    assert connection.execute(
        "SELECT award_id, date_signed, amount FROM contracts WHERE ocid = ?", ("ocds-test-001",)
    ).fetchone() == ("a1", "2024-03-01T00:00:00+03:00", 250000.0)


def test_no_personal_data_anywhere_in_the_database(db):
    connection, _ = db
    dump = "\n".join(connection.iterdump())
    for secret in SECRETS:
        assert secret not in dump, secret
    assert "contactPoint" not in dump


def test_ingest_refuses_a_file_that_no_longer_matches_the_manifest(tmp_path: Path):
    data_dir = build_data_dir(tmp_path)
    with gzip.open(data_dir / "raw" / "2024.jsonl.gz", "wb") as out:
        out.write(b'{"ocid": "tampered"}\n')
    with pytest.raises(ValueError, match="does not match MANIFEST"):
        ingest(data_dir, today=TODAY)


def test_ingest_without_manifest_explains_what_to_do(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="setup-data"):
        ingest(tmp_path, today=TODAY)


def test_ingest_is_repeatable(tmp_path: Path):
    data_dir = build_data_dir(tmp_path)
    first = ingest(data_dir, today=TODAY)
    second = ingest(data_dir, today=TODAY)
    assert first.rows == second.rows


def test_portal_url_is_built_from_the_number_inside_tender_id(db):
    connection, _ = db
    urls = dict(connection.execute("SELECT ocid, portal_url FROM processes").fetchall())
    assert urls["ocds-test-001"] == "https://tenders.go.ke/tenders/4242"
    assert urls["ocds-test-002"] is None  # tender id "t2" has no number we can use


@pytest.mark.parametrize(
    ("tender_id", "expected"),
    [
        ("20230718154019-132743-SNSC/196/2022/2023", "https://tenders.go.ke/tenders/132743"),
        ("20240711125015-191939-1494106-2023/2024", "https://tenders.go.ke/tenders/191939"),
        ("20250205150148-62929-KNL/HQ/T009/2020-2021", "https://tenders.go.ke/tenders/62929"),
        ("t1", None),
        ("", None),
        (None, None),
        ("2024-04-11-1234-x", None),
    ],
)
def test_portal_url_helper(tender_id, expected):
    assert portal_url(tender_id) == expected
