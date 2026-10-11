"""Builders for small, fully controlled test snapshots (fake data only)."""

import gzip
import json
from datetime import date
from pathlib import Path
from typing import Any

from flagbox.data.download import sha256_of, write_manifest
from flagbox.data.ingest import ingest

TODAY = date(2026, 10, 11)
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "indicators.yaml"

# (award_id, amount, currency, [(supplier_id, supplier_name), ...])
AwardSpec = tuple[str, float | None, str, list[tuple[str, str]]]


def make_release(
    ocid: str,
    awards: list[AwardSpec],
    buyer_id: str = "b1",
    buyer_name: str = "Buyer One",
    end: str | None = "2024-06-01T00:00:00+03:00",
    method: str | None = "open",
    category: str | None = "goods",
    title: str | None = None,
    number: int = 1000,
) -> dict[str, Any]:
    tender: dict[str, Any] = {
        "id": f"20240101120000-{number}-{ocid}",
        "title": title if title is not None else f"Tender {ocid}",
    }
    if end:
        tender["tenderPeriod"] = {"endDate": end}
    if method:
        tender["procurementMethod"] = method
    if category:
        tender["mainProcurementCategory"] = category
    return {
        "ocid": ocid,
        "buyer": {"id": buyer_id, "name": buyer_name},
        "tender": tender,
        "awards": [
            {
                "id": award_id,
                "value": {"amount": amount, "currency": currency},
                "suppliers": [{"id": sid, "name": name} for sid, name in suppliers],
            }
            for award_id, amount, currency, suppliers in awards
        ],
    }


def build_snapshot(tmp_path: Path, releases: list[dict[str, Any]]) -> Path:
    """Write releases as a gzipped JSONL file, record a manifest, ingest, return the data dir."""
    raw = tmp_path / "raw"
    raw.mkdir()
    target = raw / "2024.jsonl.gz"
    with gzip.open(target, "wt", encoding="utf-8") as out:
        for release in releases:
            out.write(json.dumps(release) + "\n")
    entry: dict[str, object] = {
        "name": "2024.jsonl.gz",
        "url": "https://example.test/2024.jsonl.gz",
        "sha256": sha256_of(target),
        "downloaded_at": "2026-10-11T00:00:00+00:00",
        "line_count": len(releases),
    }
    write_manifest(tmp_path / "MANIFEST.json", {"2024.jsonl.gz": entry})
    ingest(tmp_path, today=TODAY)
    return tmp_path


ALPHA = ("S1", "Alpha Ltd")
BETA = ("S2", "JOHN DOE")  # no incorporation marker, so the name is withheld
GAMMA = ("S3", "Gamma Ltd")
DELTA = ("S4", "Delta Ltd")


def scenario_releases() -> list[dict[str, Any]]:
    """Buyer b1, range 2024. Eligible awards: 10, total value 1000.

    Alpha: 4 awards in 3 processes, 600 (60%).
    Beta: 3 awards in 1 process, 150 (15%), name withheld.
    Gamma: 2 awards, 200 (20%), below the 3-win minimum.  Delta: 1 award, 50.
    Five more awards exist in range but are data-quality exclusions, and one award is out of range.
    """
    kes = "KES"
    return [
        make_release(
            "p1",
            [("a1", 150, kes, [ALPHA]), ("a2", 150, kes, [ALPHA])],
            end="2024-02-01T00:00:00+03:00",
            number=1001,
        ),
        make_release(
            "p2",
            [("a1", 150, kes, [ALPHA]), ("a2", 100, kes, [GAMMA])],
            end="2024-03-01T00:00:00+03:00",
            number=1002,
        ),
        make_release(
            "p3",
            [("a1", 150, kes, [ALPHA]), ("a2", 100, kes, [GAMMA])],
            end="2024-04-01T00:00:00+03:00",
            number=1003,
        ),
        make_release(
            "p4",
            [("a1", 50, kes, [BETA]), ("a2", 50, kes, [BETA]), ("a3", 50, kes, [BETA])],
            end="2024-05-01T00:00:00+03:00",
            number=1004,
        ),
        make_release(
            "p5",
            [
                ("a1", 50, kes, [DELTA]),
                ("a2", 0, kes, [DELTA]),  # zero value
                ("a3", 70, "USD", [DELTA]),  # other currency
                ("a4", 2e10, kes, [DELTA]),  # above the sanity cap
                ("a5", 80, kes, []),  # no supplier
            ],
            end="2024-06-01T00:00:00+03:00",
            number=1005,
        ),
        make_release(  # closed in 2023: outside the 2024 range
            "p6", [("a1", 999, kes, [ALPHA])], end="2023-05-01T00:00:00+03:00", number=1006
        ),
        make_release(  # a different buyer that also used Alpha
            "p7",
            [("a1", 500, kes, [ALPHA])],
            buyer_id="b2",
            buyer_name="Buyer Two",
            end="2024-03-01T00:00:00+03:00",
            number=1007,
        ),
    ]
