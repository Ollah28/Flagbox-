# Spike results

Measured facts from the M0 and M1 spikes. Each section says how to reproduce it.

## S3: model tool calling (M0.2)

Not recorded yet. Run by the developer in PowerShell; results to be pasted here.

## S4: Filesystem MCP server on Windows (M0.3)

**Run:** `uv run python spikes/s4_filesystem.py` (Windows 11, Node v22.14.0, 2026-10-11).

**Server:** `@modelcontextprotocol/server-filesystem@2026.8.31`, pinned exactly in `package.json`
and `package-lock.json`, installed with `npm ci`. Launched as
`node node_modules/@modelcontextprotocol/server-filesystem/dist/index.js ./filed` from the repo root
(no `npx`). Starts cleanly over stdio from the Python MCP client (`mcp==1.30.0`).

| Check | Result |
|---|---|
| Tools exposed | 14: `read_file`, `read_text_file`, `read_media_file`, `read_multiple_files`, `write_file`, `edit_file`, `create_directory`, `list_directory`, `list_directory_with_sizes`, `directory_tree`, `move_file`, `search_files`, `get_file_info`, `list_allowed_directories` |
| Allowed directory | Only the absolute path of `filed/` (the client does not advertise MCP Roots, so the server falls back to its command-line argument) |
| Write inside `filed/` | Works |
| Write the same file again | **`write_file` silently overwrites.** The server gives no protection against it |
| Write via `filed/../x` | Refused: `Access denied - path outside allowed directories` |
| Write to an absolute path outside `filed/` | Refused with the same error |
| `list_directory` | Returns lines like `[FILE] name.txt` |

**Consequences for the design:**
- Overwrite protection has to be ours: the `file` node must call `list_directory` first and refuse a
  duplicate filename (PLAN M3.2).
- The server exposes 14 tools, including `move_file` and `edit_file`. The model must be given none
  of them. Only our code calls `write_file` and `list_directory` (CLAUDE.md hard rule 4).
- `dist-tags.latest` on npm is `1.0.0` while `2026.8.31` is a different, newer-dated release line.
  That is why the exact version is pinned and never installed as `latest`.

## S1: data shape (early notes from M1.1; the full report is `docs/data_coverage.md`)

**Run:** `uv run flagbox setup-data`, then a one-off scan of the three files (2026-10-11).

| File | Lines (processes) | With awards | `tender.status` populated | `award.status` populated | Latest `tenderPeriod.endDate` |
|---|---|---|---|---|---|
| 2023.jsonl.gz | 56,548 | 23,448 | 0 | 0 | 2030-02-05 |
| 2024.jsonl.gz | 58,971 | 24,572 | 0 | 0 | 2029-02-12 |
| 2025.jsonl.gz | 33,258 | 9,562 | 0 | 0 | 2027-12-31 |

- **`tender.status` and `award.status` are never populated.** SPEC §4's live-tender filter relies on
  them. See the SPEC §13 proposal in PROGRESS.md.
- `tenderPeriod.endDate` is often wrong. The first 2025 record has `startDate` 2025 and `endDate`
  2021. Some end dates are in 2027 to 2030.
- `contactPoint` is present in 11,367 of the 33,258 records in 2025 (it must be dropped on ingest).
- `buyer.id` is present (for example `"5284"`), though the registry says no ID scheme is given.
- One JSON line is one compiled release per contracting process; `ocid` looks like `ocds-5whusi-062929`.

## S2: per-record portal URL (M1.4)

**Result: a working URL pattern exists.** `https://tenders.go.ke/tenders/<number>`, where `<number>` is the
middle part of the raw `tender.id`, whose shape is `<14-digit timestamp>-<number>-<tender reference>`.
Checked on 2026-10-11 in the browser, about 15 minutes of the 30-minute timebox.

How it was found:
- `tenders.go.ke/ocds` is only a list of yearly dataset downloads, with no per-record pages.
- The portal's tender list opens each tender in a pop-up without changing the address bar, but the page
  `tenders.go.ke/tenders/<number>` (the number shown in that pop-up's OCID) opens the same detail view directly.
  `/tender/<n>` and `/tenders?ocid=...` do not work (404, and the list page with no pop-up).

Why the number comes from `tender.id` and not the OCID:
- Our OCIDs are mostly built from the tender reference (`ocds-5whusi-SNSC/196/2022/2023`). Only 12 of 57,576
  kept processes have an OCID made from the number.
- All 57,576 kept processes have a `tender.id` in the expected shape, and no number is used twice.

Verification (4 of 4 matched on both OCID and title), historical closed tenders from the ingested data:

| Number | Page showed OCID | Matches our record |
|---|---|---|
| 132743 | `ocds-5whusi-SNSC/196/2022/2023` | yes |
| 147032 | `ocds-5whusi-KeNHA/R9/LVP/134/2023` | yes |
| 191939 | `ocds-5whusi-1494106-2023/2024` | yes |
| 198775 | `ocds-5whusi-NTZDC/QT/300/2023/2024` | yes |

Limits to state honestly:
- Only 4 links were checked, not all 57,576. The README should say links are built from a verified pattern.
- The portal is a JavaScript app, so the link needs a browser, not a plain download.
- The portal can change its routes at any time. `portal_url` is an extra pointer; the dataset reference
  (`file`, `sha256`, `line_no`, `ocid`) stays the authoritative source.
- The portal pages were not checked for personal data. We link to them but never copy their content.
