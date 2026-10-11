import gzip
import json
from pathlib import Path

import pytest

from flagbox.data.download import file_url, setup_data


class FakeFetcher:
    """Stands in for the network: writes a tiny gzipped JSONL file and counts calls."""

    def __init__(self, lines: int = 3) -> None:
        self.lines = lines
        self.calls: list[str] = []

    def __call__(self, url: str, destination: Path) -> None:
        self.calls.append(url)
        body = "".join(f'{{"ocid": "x{i}"}}\n' for i in range(self.lines)).encode()
        destination.write_bytes(gzip.compress(body))


def test_file_url_points_at_registry_download():
    assert file_url(2024) == (
        "https://data.open-contracting.org/en/publication/147/download?name=2024.jsonl.gz"
    )


def test_first_run_downloads_and_writes_manifest(tmp_path: Path):
    fetcher = FakeFetcher(lines=3)
    results = setup_data(tmp_path, years=(2023, 2024), fetch=fetcher)

    assert [r.status for r in results] == ["downloaded", "downloaded"]
    assert all(r.line_count == 3 for r in results)
    assert (tmp_path / "raw" / "2023.jsonl.gz").exists()
    assert not list((tmp_path / "raw").glob("*.part"))

    manifest = json.loads((tmp_path / "MANIFEST.json").read_text(encoding="utf-8"))
    assert [f["name"] for f in manifest["files"]] == ["2023.jsonl.gz", "2024.jsonl.gz"]
    first = manifest["files"][0]
    assert first["url"] == file_url(2023)
    assert len(first["sha256"]) == 64
    assert first["line_count"] == 3
    assert first["downloaded_at"]


def test_second_run_skips_files_whose_hash_matches(tmp_path: Path):
    fetcher = FakeFetcher()
    setup_data(tmp_path, years=(2023,), fetch=fetcher)
    results = setup_data(tmp_path, years=(2023,), fetch=fetcher)

    assert [r.status for r in results] == ["skipped"]
    assert len(fetcher.calls) == 1  # only the first run touched the network


def test_modified_file_is_downloaded_again(tmp_path: Path):
    fetcher = FakeFetcher()
    setup_data(tmp_path, years=(2023,), fetch=fetcher)
    (tmp_path / "raw" / "2023.jsonl.gz").write_bytes(b"corrupted")

    results = setup_data(tmp_path, years=(2023,), fetch=fetcher)

    assert [r.status for r in results] == ["downloaded"]
    assert len(fetcher.calls) == 2


def test_file_without_manifest_entry_is_not_trusted(tmp_path: Path):
    fetcher = FakeFetcher()
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "2023.jsonl.gz").write_bytes(gzip.compress(b'{"ocid": "old"}\n'))

    results = setup_data(tmp_path, years=(2023,), fetch=fetcher)

    assert [r.status for r in results] == ["downloaded"]


def test_truncated_download_raises_and_leaves_no_final_file(tmp_path: Path):
    def broken_fetch(url: str, destination: Path) -> None:
        destination.write_bytes(gzip.compress(b'{"ocid": "x"}\n' * 1000)[:20])

    with pytest.raises((EOFError, OSError)):
        setup_data(tmp_path, years=(2023,), fetch=broken_fetch)

    assert not (tmp_path / "raw" / "2023.jsonl.gz").exists()
