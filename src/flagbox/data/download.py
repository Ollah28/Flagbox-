"""M1.1: download the Kenya PPRA OCDS yearly files and record a manifest.

The files come from the Open Contracting Partnership data registry (publication 147).
They are never committed to git: this module fetches them into data/raw/ and writes
data/MANIFEST.json with each file's url, sha256, download time and line count, so a
later run can prove which exact bytes it analysed.

Running it twice is cheap: a file is skipped when it exists on disk and its SHA-256
matches the one recorded in the manifest.
"""

import gzip
import hashlib
import json
import shutil
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PUBLICATION_URL = "https://data.open-contracting.org/en/publication/147"
YEARS = (2023, 2024, 2025)
CHUNK_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 120

# A fetcher takes (url, destination path) and writes the bytes there. Tests swap in a fake.
Fetcher = Callable[[str, Path], None]


@dataclass(frozen=True)
class FileResult:
    name: str
    status: str  # "downloaded" or "skipped"
    sha256: str
    line_count: int


def file_url(year: int) -> str:
    return f"{PUBLICATION_URL}/download?name={year}.jsonl.gz"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def count_lines(path: Path) -> int:
    """Count JSONL lines by decompressing the whole file, which also proves the gzip is intact."""
    count = 0
    with gzip.open(path, "rb") as handle:
        for _ in handle:
            count += 1
    return count


def http_fetch(url: str, destination: Path) -> None:
    headers = {"User-Agent": "flagbox/0.1 (open-data research)"}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        with destination.open("wb") as out:
            shutil.copyfileobj(response, out, CHUNK_BYTES)


def read_manifest(path: Path) -> dict[str, dict[str, object]]:
    """Return the manifest's files keyed by file name, or {} if there is no usable manifest."""
    if not path.exists():
        return {}
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["files"]
        return {entry["name"]: entry for entry in entries}
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


def write_manifest(path: Path, entries: dict[str, dict[str, object]]) -> None:
    body = {
        "publication_url": PUBLICATION_URL,
        "files": [entries[name] for name in sorted(entries)],
    }
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def setup_data(
    data_dir: Path,
    years: tuple[int, ...] = YEARS,
    fetch: Fetcher = http_fetch,
) -> list[FileResult]:
    raw_dir = data_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = data_dir / "MANIFEST.json"
    entries = read_manifest(manifest_path)
    results: list[FileResult] = []

    for year in years:
        name = f"{year}.jsonl.gz"
        target = raw_dir / name
        known = entries.get(name)

        if target.exists() and known is not None:
            actual = sha256_of(target)
            if actual == known["sha256"]:
                line_count = known["line_count"]
                assert isinstance(line_count, int)
                results.append(FileResult(name, "skipped", actual, line_count))
                continue

        # Download to a .part file first so an interrupted run never leaves a half file
        # that looks complete.
        partial = raw_dir / f"{name}.part"
        url = file_url(year)
        fetch(url, partial)
        line_count = count_lines(partial)  # raises if the gzip is truncated or corrupt
        digest = sha256_of(partial)
        partial.replace(target)

        entries[name] = {
            "name": name,
            "url": url,
            "sha256": digest,
            "downloaded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "line_count": line_count,
        }
        write_manifest(manifest_path, entries)  # after each file, so progress survives a crash
        results.append(FileResult(name, "downloaded", digest, line_count))

    write_manifest(manifest_path, entries)
    return results
