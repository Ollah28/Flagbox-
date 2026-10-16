# SPEC.md — Flagbox

> **Flagbox: sourced red flags for public tenders. The committee decides.**
> Bid Box Challenge, Governance track · Theme: Financial services — Value for money
> Status: Plan Mode spec **v2, after adversarial review** · 8 Oct 2026 · Deadline 15 Oct 2026 (time TBC)
> Certainty labels: **VERIFIED** (with link) · **ASSUMPTION** (default chosen, changeable) · **UNKNOWN** (must be tested)

---

## 1. Product

**Problem.** Kenya publishes 260,744 tenders and 109,237 awards as open data (VERIFIED, [OCP registry](https://data.open-contracting.org/en/publication/147)). Audit and oversight offices are too small to read them, so warning signs go unchecked until long after the money is spent.

**User.** An audit or oversight officer at any Kenyan public body, for example a county internal audit unit, an assembly oversight committee, a ministry audit unit, or the Office of the Auditor-General. Each one prepares review files on one procuring entity at a time.

**Job to be done.** "Give me a sourced shortlist of this buyer's awards that warrant review, with the evidence attached, so I can decide what to examine."

**The one thing it must do well.** Every sentence about a supplier can be traced to exact records and checked by a stranger. A wrong or unsourced flag is worse than no flag.

**Success metrics for the submission:**
- 100% of filed findings and summary sentences pass the verifier.
- One full run finishes in ≤ 20 min on the open-weights model.
- Precision and recall per indicator are **reported**, not used as a pass bar. They are measured against labels we wrote ourselves, which is stated honestly.

**Current workaround.** Manual spreadsheet review. The challenge brief estimates about 26 days of committee-level work.

### Scope

| In (v1, this submission) | Out (stated in README) |
|---|---|
| Any Kenyan buyer, national or county, from the PPRA OCDS bulk files | Bid documents (PDF/Word). A seam is left in the code (§11) |
| PPRA file format, plus the organisers' test-tender format once known | Generic merging of any OCDS package (v2) |
| **3 integrity indicators**, chosen by the data spike (§3.2) | `threshold_clustering`, since thresholds are unverified (v2) |
| Planning, hit triage, drill-down, verifier, human gate, filing | Price per unit. Item count = 0 in source (VERIFIED) |
| Terminal approval. Streamlit only if everything else is done by Wed 14 Oct | Contract value changes. Amendment count = 0 (VERIFIED) |
| English memo | Other countries (config seam only), any award decision, any accusation |

---

## 2. User journey and screen states

1. `uv run flagbox run --buyer "Nairobi City County" --from 2023-01-01 --to 2025-12-31`
2. **Before the agent starts**, the command line calls `find_buyer`. If one name clearly matches, it continues. If several match, it shows a numbered list and the user picks one. This keeps the graph to a single pause point.
3. A live progress line appears for each tool call: the tool, a short input, the result count, and the call ID.
4. The **full draft memo** is printed, plus its SHA-256 hash.
5. Approval gate. The user enters a name, then approves, rejects, or rejects with a note. Approval is tied to that exact hash.
6. Approve: the bytes with that hash are filed to `filed/<run_id>.md`. Reject: nothing is filed. Both are logged.

**"Aha" moment.** A flag with linked OCIDs, a fixed plain-language reason, a peer comparison, and a caveat saying what the data could *not* show.

| State | What the user sees |
|---|---|
| Loading | One progress line per tool call, plus elapsed time |
| Buyer not found | "No buyer matches 'X'. Closest names: …" Exit; nothing filed |
| No awards in range | The agent chooses whether to widen the range (§3.1). If it doesn't widen, or the wider range is still empty, it reports "No awards found…" |
| No hit survives triage | A memo is still produced, with a coverage table, checks run, and "no indicator crossed its threshold". This is not a failure |
| Partial failure | A tool error is retried once, then logged as a gap. The memo gets a "Checks not completed" section |
| Model unreachable | "Ollama not running at OLLAMA_HOST…" and exit code 2 |
| Success | Draft and hash, then approval, then the filed path |

---

## 3. Core logic

### 3.1 Agent graph (LangGraph, SQLite checkpointer)

```
[CLI: find_buyer + human pick] ─► check_coverage ─► plan ─► investigate ⟲ ─► verify ─► draft ─► human_gate ─► file | end
```

| Node | Decided by | What happens |
|---|---|---|
| check_coverage | Code | Calls `data_coverage`. Returns field-population %, a runnable indicator list, and the process count |
| plan | **LLM** | Picks indicators *and their order*. Writes a reason for each skip. Decides whether the range is too thin and calls `widen_range` (allowed once) |
| investigate | **LLM loop** | For each hit, the model decides whether to **keep** it (`record_finding`) or **dismiss** it (`dismiss_hit`, with a reason; logged and listed in the memo appendix). It can check context with `peer_baseline` and `process_detail`, and drill into a supplier with `supplier_footprint`. Limits: 25 tool calls and 15 min |
| verify | **Code only** | Applies §3.3 to every finding **and to the summary paragraph** |
| draft | Code + LLM | `draft_memo` builds tables from stored findings. The LLM writes only the summary, which is checked by the verifier. If it fails twice, a fixed template summary is used |
| human_gate | **Human** | Contains **only** `interrupt()`, so resuming does not re-run any work |
| file | **Code, not the model** | Code calls the borrowed server's `write_file` with the approved bytes. It first checks with `list_directory` that the filename is new. The model never sees `write_file` |

**What the model actually decides, and how we show it:**
- which indicators to run, and in what order
- whether to widen the date range
- whether each hit is kept or dismissed, with a reason
- when a peer comparison or drill-down is needed
- the severity of each finding, from a fixed list

A **no-model baseline** runs every runnable indicator and keeps every hit. EVALS.md reports what the agent adds against it: dismissed false positives, drill-down findings, and skip reasons.

### 3.2 Indicators (ship 3)

Spike S1 measures field coverage. We ship the **top 3 runnable** in this preference order (rule: field ≥ 60% populated across the eval buyers):

| Pref | Indicator | Fires when | Needs field | Status |
|---|---|---|---|---|
| 1 | `repeat_winner` | Supplier has ≥ N awards **and** ≥ X% of the buyer's award value in range | `awards.suppliers`, `awards.value` | **Runnable**, 80% joint coverage (VERIFIED). A2 thresholds never fire on real data: see D23 |
| 2 | `value_outlier` | Award value > Q3 + k×IQR among the buyer's awards in the same category | `awards.value`, category | **Runnable**, 99% joint coverage (VERIFIED) |
| 3 | `single_bidder` | `numberOfTenderers == 1` on a competitive method | `tender.numberOfTenderers`, `procurementMethod` | **Not runnable**: `numberOfTenderers` is 0% populated (VERIFIED) |
| 3 | `non_competitive_share` | Value share via direct or limited methods > Y% | `tender.procurementMethod(Details)` | **Runnable**, 99% joint coverage on `procurementMethod` and value (VERIFIED) |
| 3 | `short_window` | Tender open < D days | `tender.tenderPeriod` | **Not usable**: 91% of windows are under 7 days, so `startDate` is not an opening date (VERIFIED) |
| 3 | `date_sequence` | Contract signed before tender closed, or award dated before tender start | dates | Runnable (81%) but noisy: a hit appears at 219 of 240 buyers. Kept as the rehearsal candidate |

The remaining candidates are listed in the README as "next indicators". One of them is a ready-made **live-review rehearsal**.

**Parameters are locked.** N, X, Y, D and k live in `config/indicators.yaml`, each with a `source:` or `rationale:` line. The model **cannot pass them**: indicator tools accept only `buyer_key` and the date range. Starting values are ASSUMPTIONS until checked against the OCP red-flags guide.

**Extreme values** (VERIFIED: the top 1% outweighs the rest). Values above a sanity cap, or in a currency other than KES, become *data-quality notes*. They are excluded from statistics, and the count is stated. They are never integrity flags.

### 3.3 Verifier (deterministic; this is what makes the output defensible)

**How claims are built.** Indicator tools return `hits[]`. Each hit has a `hit_id`, OCIDs, `source_refs`, `source_fields`, and a **`claim_text` produced by the tool from a fixed template**, for example "Supplier S-102 received 6 of 41 awards (31% of award value) from this buyer, 2023–2025." The model never writes a number or a supplier name. `record_finding` accepts only `hit_id`, `severity` (low/medium/high), and a **`reason_code`** chosen from a closed list. The wording of each reason is fixed text kept in the indicator's own file, and each reason has a rule that decides from the hit whether it is true (for example "all within a single tender process" applies only when the hit's process count is 1). The model writes no explanation (D31).

A finding or summary sentence is filed only if:
1. The `hit_id` exists in the output of a logged tool call from **this run**. The OCIDs, `claim_text` and `source_refs` are copied from that call by code. A finding can cite several calls, for example the hit plus a `peer_baseline` call.
2. The explanation contains **no digits and no supplier names or IDs**. For findings this holds by construction (the text is fixed, D31) and is re-checked on the fixed text by a test; the same check, against the run's supplier list, guards the free text that remains (plan skip reasons and the summary paragraph).
3. The summary paragraph may only mention a supplier or OCID that belongs to a recorded finding, and its numbers must exactly match a filed `claim_text`.
4. It contains no banned words (`corrupt*, fraud*, guilty, stole, steal*, illegal, criminal, bribe*`). The wording uses "warrants review".
5. It contains no withheld name (§6.2).

`supplier_footprint` name-only "possible" matches are shown to the model as unconfirmed, and **never appear in the memo**.

---

## 4. Data

**Source (VERIFIED).** Kenya PPRA via the OCP Data Registry ([publication 147](https://data.open-contracting.org/en/publication/147)):
- covers Jul 2018 – Sep 2026, updated daily upstream and retrieved monthly
- yearly gzipped JSONL files of 3–11 MB
- OCID prefix `ocds-5whusi`

**Known mess (VERIFIED):**
- empty `awards` and `contracts` arrays
- extreme outliers
- an invalid supplier ID scheme
- no buyer ID scheme
- missing names in `parties`; we fall back to `buyer.name` and `awards.suppliers.name`

**Licence: UNKNOWN** (the registry shows "–"). Ask the organisers. **The data is never committed to the repo.** A setup script downloads it.

**Snapshot.** Ingest the 2023–2025 files once. Record each file's URL, SHA-256 and download date in `data/MANIFEST.json`. Evals run on this frozen snapshot.

**No live tenders (revised by D20).** `tender.status` and `award.status` are never populated in the 2023–2025 files (VERIFIED, `docs/spikes.md`), so ingest keeps a process only if it has at least one award **and** `tenderPeriod.endDate` is missing or earlier than **today**. It excludes the rest, counted by reason (`no_award`, `end_date_not_past`, `end_date_unreadable`).

The exclusion counts are logged. The same filter applies to every input.

**Inputs.** `flagbox ingest` supports the PPRA JSONL format, plus the organisers' test-tender format once its shape is known (spike S5). Generic merging of any OCDS package is v2.

**Source references.** Every record carries `source_ref = {dataset_url, file, sha256, line_no, ocid}`. A `portal_url` is added only if spike S2 (timeboxed to 30 min) finds a working per-record URL.

### 4.1 Data model (SQLite, parameterised SQL only)

| Table | Key fields | Notes |
|---|---|---|
| `source_files` | `file_id` PK, `url`, `sha256`, `downloaded_at`, `line_count` | Provenance |
| `processes` | `ocid` PK, `file_id`, `line_no`, `buyer_key`, `title`, `method`, `method_details`, `category`, `n_tenderers` NULL, `period_start/end` NULL, `status`, `tender_value` NULL, `currency` NULL, `portal_url` NULL (D25) | One row per process |
| `buyers` | `buyer_key` PK, `raw_id` NULL, `name`, `name_norm` | Key = raw ID, else `name:`+normalised name (ASSUMPTION) |
| `awards` | (`ocid`,`award_id`) PK, `date`, `amount`, `currency`, `status` | |
| `award_suppliers` | `ocid`, `award_id`, `supplier_key`, `raw_id`, `display_name`, `name_withheld` BOOL | §6.2 |
| `contracts` | `contract_id`, `ocid`, `award_id`, `date_signed`, `amount` | |
| `runs` | `run_id` UUID PK, `buyer_key`, range, `model`, `num_ctx`, times, `status`, `tool_calls`, `tokens_in/out` | |
| `hits` | `hit_id` PK, `run_id`, `tool_call_id`, `indicator`, `claim_text`, `ocids` JSON, `source_refs` JSON, `source_fields` JSON | Written by indicator tools |
| `findings` | `finding_id` PK, `hit_id` FK, `severity`, `reason_code`, `rationale` (the fixed text for that code), `status` (kept/dismissed/rejected), `reason` (why rejected) | |
| `approvals` | `run_id` PK, `approver_name`, `os_user`, `decision`, `note`, `memo_sha256`, `decided_at`, `log_head_hash`, `filed_path`, `filing_status` | One decision per run (D33) |

Raw JSON is not stored. No migrations are needed: `flagbox reset` rebuilds from the files.

---

## 5. Architecture and MCP

```
 Officer ─► CLI (find_buyer, pick) ─► LangGraph agent ◄──► Qwen3 via Ollama (local, num_ctx 16k, thinking off)
                                        │
                     Logging wrapper (client side, wraps EVERY MCP tool):
                     assigns tool_call_id, records inputs/outputs/timing/errors,
                     hash-chained lines ─► logs/<run_id>.jsonl
                        │                                   │ (file node only, after approval, code-called)
                        ▼                                   ▼
        Flagbox MCP server (ours, Python, mcp 1.30)   Filesystem MCP server (borrowed, pinned, node dist/index.js)
        read:  data_coverage, widen_range*, fetch_buyer_awards,   tools exposed to code: list_directory, write_file
               <3 indicators>, peer_baseline, process_detail,     root = ./filed only
               supplier_footprint, find_buyer (CLI)
        write: record_finding, dismiss_hit, draft_memo
        resource: flagbox://config/indicators
                        │
                        ▼
              SQLite snapshot ◄── ingest (live-tender filter, redaction) ◄── PPRA OCDS files / test tenders
```
\* `widen_range` returns the new range and its process count, and is allowed once per run.

**Diagram.** The PNG made earlier is out of date. The README uses a Mermaid version of this diagram, regenerated once the indicators are final.

### 5.1 Our MCP server's tools

| Tool | R/W | Input | Output |
|---|---|---|---|
| `find_buyer` | R | `name`, `limit=5` | Candidates: key, name, award count, span |
| `data_coverage` | R | `buyer_key`, range | % populated per field, process count, runnable indicators |
| `widen_range` | R | `buyer_key`, range | Suggested range plus its count (once per run) |
| `fetch_buyer_awards` | R | `buyer_key`, range, `page`, `page_size ≤ 50` | Award rows plus `source_ref` |
| 3 indicator tools | R | `buyer_key`, range **only** | `hits[]` with `hit_id`, `claim_text`, OCIDs, refs; `params_used`; `excluded_count` |
| `peer_baseline` | R | `hit_id` | The same metric for comparable buyers in the same category, with a sample size |
| `process_detail` | R | `ocid` | Method, dates, values, tenderer count, supplier key, `source_ref` |
| `supplier_footprint` | R | `supplier_key` | Exact-key awards at other buyers, as a hit with `claim_text`. Name-only matches are labelled unconfirmed |
| `record_finding` | W | `hit_id`, `severity`, `reason_code` | `finding_id` or a structured rejection |
| `dismiss_hit` | W | `hit_id`, `reason` | Logged dismissal |
| `draft_memo` | W | `run_id` (UUID-validated, must be the server's own run), optional `summary` | Draft path plus SHA-256, or the summary's violations (nothing written). The graph's `draft` node calls it; the model is never offered it |

**Output limits.** Every tool caps its output at about 4k tokens and returns totals plus a `truncated` flag, so the model's context is never silently overflowed.

**Borrowed server.** `@modelcontextprotocol/server-filesystem` 2026.8.31, installed with `npm ci` from a pinned `package.json`, and launched as `node node_modules/@modelcontextprotocol/server-filesystem/dist/index.js ./filed`. That avoids `npx` and the `npx.cmd` problem on Windows. Why borrow it: path-validated, sandboxed file writing is solved and maintained upstream.

**Auth.** Everything runs locally under one user. Write safety comes from structure:
- the model's write tools touch only the database and `drafts/`;
- `filed/` is reachable only by code after an approval tied to a hash.

**Live-review seam.** A new indicator needs one file in `indicators/`, one config block, one fixture test, and one claim template. It is registered automatically into `data_coverage` and the planner's menu, so the graph never changes.

---

## 6. Security and privacy

### 6.1 Threat model

| Threat | Mitigation |
|---|---|
| Injected instructions in record text steer the agent or the claim | Claims come from templates and explanations from a fixed list, not the model; record text is returned in quoted data fields; the model has no shell or network tools; eval E10 tests it |
| Model invents OCIDs or numbers | Verifier rules 1 and 3: everything is copied by code from logged tool output |
| Model lowers thresholds until something fires | Parameters are locked in config (§3.2) |
| An approved memo is swapped before filing | Hash shown to the approver, approval bound to that hash, exactly those bytes filed; no-overwrite check |
| Model bypasses the gate | The model is never given `write_file` |
| Path abuse | `run_id` validated as a UUID; Filesystem server root = `filed/` |
| Log tampering | Each JSONL line includes the hash of the previous line; `flagbox verify-log <run_id>` |
| Supply chain | `uv.lock`; `package-lock.json` with `npm ci`; Ollama model pulled by exact tag |
| A true number on bad data reads as an accusation | Caveats; data-quality notes kept separate; peer comparison; human gate |

No secrets are needed at $0. `.env.example` contains: `OLLAMA_HOST`, `FLAGBOX_MODEL`, `FLAGBOX_NUM_CTX`, `FLAGBOX_DATA_DIR`.

### 6.2 Personal data

| Data | Handling |
|---|---|
| `parties[].contactPoint` | Dropped on ingest; never stored, logged or sent to the model |
| Supplier names | Shown **only** with an incorporation marker: `Ltd`, `Limited`, `PLC`, `LLP`, `Inc`, `Corporation`, `Company Limited`, `Co. Ltd`. A name that contains `@` or a run of 7+ digits (an email or phone number typed into the name field, VERIFIED in the data) is withheld even with a marker. Otherwise `display_name = "Supplier <key> (name withheld)"`. The withholding applies everywhere: database display field, tool outputs to the model, logs and memo |
| Approver | Typed name plus OS username, stored locally (required by the brief) |

Data stays local; the user deletes `data/` and `logs/`. There are no accounts. Frame: Kenya Data Protection Act 2019; personal data is excluded by design. Code licence: MIT.

---

## 7. Non-functional

| Item | Target |
|---|---|
| Full run, mid-sized buyer (≤ 2,000 processes) | ≤ 20 min (measured in S3) |
| Model settings | `num_ctx = 16384`, Qwen3 thinking **off**, temperature 0, argument validation with errors returned to the model (max 2 repairs) |
| Limits | Investigate: 25 calls / 15 min; whole run: 30 min hard stop; Ollama call: 180 s, 1 retry |
| Ingest 2023–2025 | ≤ 5 min |
| Platforms | Windows 10/11, macOS, Linux; Python 3.12; Node 20+ |

---

## 8. Cost model ($0 cash; time is the cost)

These are ASSUMPTIONS until S3 measures them. Context grows each turn, so prompt tokens add up across turns.

| Input | 8B | 14B |
|---|---|---|
| Turns per run | ~18 | ~18 |
| Average prompt per turn (context grows) | ~8k | ~8k |
| Prompt processing / generation speed (16 GB laptop) | ~300 / ~12 tok/s | ~150 / ~5 tok/s |
| Output per run (no thinking) | ~3k | ~3k |
| **Estimated run time** | 18×8k/300 + 3k/12 ≈ 8 + 4 ≈ **~12 min** | 18×8k/150 + 3k/5 ≈ 16 + 10 ≈ **~26 min** |

**Implications.** The 14B model may miss the 20-minute target and may swap on 16 GB, since a 9.3 GB model plus about 2–3 GB of cache at 16k context leaves little room on Windows. **The 8B model is the default.** The 14B model is the comparison, run if S3 shows it fits.

| Scale | Laptop-hours (8B) |
|---|---|
| 1 run | 0.2 h |
| 100 runs | 20 h (overnight batches) |
| 1,000 runs | 200 h; needs a GPU server (v2) |

**Eval budget (D9).** Small eval buyers (≤ 300 processes, about 5 min per run on 8B):
- 8B: all 11 model tasks × 3 = 33 runs ≈ 3 h
- 14B: E1, E3, E4, E12 × 3 = 12 runs ≈ 2–3 h
- **Total ≈ 6 laptop-hours**, run overnight on Sun 11 and Mon 12 Oct

---

## 9. Testing and quality bar

**Deterministic tests (no model, run in CI, must be 100%):**
- indicator fixtures and claim templates
- the verifier: fake `hit_id`, a reason code that is not true for the hit, free text in place of a code, a summary with an uncited supplier, banned words
- redaction
- live-tender filter
- UUID and path checks
- the hash-bound gate: reject → nothing filed; changed draft → filing refused
- the no-overwrite check
- log hash-chain verification

**Model tasks (EVALS.md).** Each is repeated 3 times. We report the pass rate **and the overlap of cited OCID sets between runs** (Jaccard), as the measure of run-to-run variation.

| # | Task | Pass if |
|---|---|---|
| E1 | Known small buyer, full run | Memo produced; 100% of findings and summary pass the verifier |
| E2 | Buyer with a key field < 60% filled | That indicator is skipped and the reason is in the memo |
| E3 | Buyer with a labelled repeat winner | Hit kept with all awards cited |
| E4 | Same as E3 | `supplier_footprint` or `peer_baseline` called before keeping it |
| E5 | Thin date range | Model calls `widen_range`, or justifies not doing so; nothing invented |
| E6 | Buyer with extreme values | Reported as data-quality notes, excluded, count stated |
| E7 | Labelled false-positive hit (e.g. a framework contract) | Model dismisses it with a reason |
| E8 | Model is pushed to record a non-existent `hit_id` | Rejected |
| E9 | Injected instruction in a fixture tender title | Behaviour unchanged; no banned wording |
| E10 | Approver rejects | Nothing in `filed/` |
| E11 | Sole-trader supplier in the hits | Name withheld everywhere |
| E12 | **Accuracy.** One buyer, 40 hand-labelled processes | Precision and recall per indicator, **reported only**, labelled "self-labelled, no practitioner review" |
| B | No-model baseline against the agent on E1/E3/E7 | Report the difference |

**Unfixed failure.** An honest failing task is chosen from real results during Sun–Mon evals, with a candidate in advance: the same company under different supplier IDs, where the test asserts a footprint finding and the system cannot make it. We document the failure and what we would try next.

**Definition of done (per task):**
- tests pass
- `ruff` and `pyright` (basic mode) clean
- every tool call appears in the log
- README updated if behaviour changed

---

## 10. Environments and delivery

- **Local only.** `uv run flagbox setup` runs `npm ci`, downloads the data, checks hashes, ingests, and pulls **`qwen3:8b`** (5.2 GB).
- `uv run flagbox demo` does a real run on a fixed small buyer.
- `uv run flagbox demo --replay <run_id>` replays a **recorded** run from its log, with no model download. Every screen is labelled **"REPLAY of run <id> recorded <time>, not a new run"**, so judges can inspect the run without the download and without anything passing as a fresh run.
- **README prerequisites:** Ollama, uv, Node 20+. This is described honestly as "one command after three installs".
- **CI:** GitHub Actions runs the deterministic tests.
- **Release:** tag `v1.0-submission` before the deadline. No commits after it.

---

## 11. Future-proofing seams (not built)

- A `country` column and `config/countries/<cc>.yaml` for other countries
- An `input_adapters/` folder, with `ppra.py` now and `ocds_generic.py` and `bid_documents.py` later
- `source_ref.portal_url` kept nullable
- An indicator registry
- `threshold_clustering`, ready to add once the gazetted thresholds are cited

---

## 12. Dependency table

| Dependency | Version / ID | Status | Cost / limits | Link |
|---|---|---|---|---|
| Python | 3.12 | ASSUMPTION (wide compatibility) | Free | python.org |
| uv | 0.12.23 (3 Oct 2026) | VERIFIED | Free | [pypi](https://pypi.org/project/uv/) |
| mcp (Python SDK) | **1.30.0, pinned `<2`** | VERIFIED. 2.3.0 exists but renames FastMCP → MCPServer; the adapters require `<2` | Free | [pypi](https://pypi.org/project/mcp/) |
| langchain-mcp-adapters | 0.3.2, requires `mcp>=1.24,<2` | VERIFIED | Free | [pypi](https://pypi.org/project/langchain-mcp-adapters/) |
| langgraph (+ SQLite checkpointer) | 1.2.14 | VERIFIED (checkpointer package version checked at setup) | Free | [pypi](https://pypi.org/project/langgraph/) |
| langchain-core / langchain-ollama | 1.6.7 / 1.1.0 | VERIFIED | Free | [pypi](https://pypi.org/project/langchain-ollama/) |
| Ollama: `qwen3:8b` (5.2 GB, default), `qwen3:14b` (9.3 GB, comparison) | 40K max context; tools supported | VERIFIED. Default `num_ctx` and thinking toggle UNKNOWN, checked in S3 | Free, local | [ollama](https://ollama.com/library/qwen3) |
| @modelcontextprotocol/server-filesystem | 2026.8.31, not deprecated; `write_file` overwrites (upstream behaviour, re-checked in S4) | VERIFIED version | Free; Node | [npm](https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem) |
| Streamlit (only if time remains) | 1.65.0 | VERIFIED | Free | [pypi](https://pypi.org/project/streamlit/) |
| Kenya PPRA OCDS data | 2023–2025 snapshot | VERIFIED; **licence UNKNOWN** | Free | [registry](https://data.open-contracting.org/en/publication/147) |

**Spikes. Milestone 1, before any agent code:**
- **S1:** field coverage per indicator across ~10 candidate buyers. This picks the 3 indicators and the eval buyers.
- **S2:** portal URL pattern, timeboxed to 30 min.
- **S3:** 10 **multi-turn** tool-calling sequences per model with thinking off and `num_ctx` 16k. Measure valid-call rate, tokens/sec, RAM and swap.
- **S4:** the pinned filesystem server runs via `node …/dist/index.js` on Windows, and its overwrite behaviour.
- **S5:** the format of the organisers' test tenders.

---

## 13. Decision log

| # | Decision | Options | Choice and why | Reversible? |
|---|---|---|---|---|
| D1 | Data half | Real awards / synthetic bids | Real awards: they score on criterion 1, and every finding can be sourced | Hard |
| D2 | Project | Screener / price desk / supplier file / AGPO | Red-flag screener: most agentic decisions, easy to extend live | Hard |
| D3 | Price benchmark | Keep / drop | Drop: 0 items in source (VERIFIED) | Easy |
| D4 | MCP SDK | v2 / v1 | Pin v1.30.0: the adapters require `<2` | Easy |
| D5 | Borrowed server | SQLite (archived) / Filesystem | Filesystem, pinned, launched with node | Easy |
| D6 | Model | 14B default / 8B default | **8B default**, 14B comparison: memory and speed on 16 GB Windows (S3 confirms) | Easy |
| D7 | Setup | Docker / uv + native Ollama | uv + native | Easy |
| D8 | Data | Live API / snapshot | Snapshot plus a today-dated live-tender filter | Easy |
| D9 | Eval budget | 5× all × 2 models / targeted | 8B on all, 14B on 4 tasks, 3× each, ≈ 6 h | Easy |
| D10 | Approval UI | Terminal / web | Terminal; Streamlit only if everything else is done by Wed 14 | Easy |
| D11 | Reach | Counties / all Kenya / 9 countries | All Kenyan buyers; country seam | Easy |
| D12 | Bid docs | Build / seam | Seam only | Easy |
| D13 | Claim authorship | Model writes claims / tool templates | **Tool templates**; the model writes no claim (and, since D31, no explanation either) | Med |
| D14 | Name display | Company-word heuristic / incorporation markers only | **Incorporation markers only**: errs toward privacy | Easy |
| D15 | Logging location | Per-server / client wrapper | **Client wrapper**: covers the borrowed server and creates `tool_call_id` | Med |
| D16 | Filing | Model calls `write_file` / code calls it | **Code**, hash-bound, no-overwrite check | Easy |
| D17 | Indicator count | 4–5 / 3 | **3** plus rehearsal candidates | Easy |
| D18 | Buyer choice | In-graph interrupt / CLI before graph | **CLI**: one interrupt only | Easy |
| D19 | Judge run without download | None / replay | **Labelled replay** of a recorded real run | Easy |
| D20 | Live-tender filter | SPEC §4 status rules / award + end-date rule | **Keep a process only if it has ≥ 1 award and `tenderPeriod.endDate` is missing or before today.** Status fields are 100% empty in all 148,777 processes, so the status rules cannot fire. An unreadable end date counts as not past (excluded). Stricter than the original: it drops the 91,195 award-less processes, which no indicator uses. Agreed 11 Oct | Easy |
| D21 | Supplier key | Raw ID, else readable name / raw ID, else hashed name | **Raw ID, else `name:` + first 12 hex of SHA-256 of the normalised name.** A readable name-based key would leak a withheld name (§6.2) through the key and the display text | Easy |
| D22 | Date for `--from/--to` | `tenderPeriod.startDate` / `endDate` / `contract.dateSigned` / `contractPeriod.startDate` | **`tenderPeriod.endDate` (tender closing date)**: plausible in 99.9% of kept processes, against 81% for `dateSigned` and 75% for `contractPeriod.startDate`; its years line up with signing dates. `startDate` is not a real opening date. Processes with no usable end date are left out of ranged queries and counted. | Easy |
| D23 | The 3 indicators (S1 result) | Top 3 runnable in SPEC 3.2 preference order | **`repeat_winner`, `value_outlier`, `non_competitive_share`**, each above the 60% joint-coverage rule. `single_bidder` (0% `numberOfTenderers`) and `short_window` (91% of windows under 7 days) are out; `date_sequence` is the rehearsal candidate. **`repeat_winner` parameters decided 11 Oct: N = 3 wins, X = 10% of value** (replaces the A2 placeholders, which fire at 1 of 240 buyers; 10% fires at 6). It will rarely fire; a count-based test goes on the README's "next indicators" list. Supplier IDs may split one company into several IDs, which is stated as a limit. Parameters live in `config/indicators.yaml` with a `rationale:` line, never from the model | Easy |
| D24 | Eval buyers | 11 candidates in `docs/data_coverage.md` | **Kenya Literature Bureau (5015, 266 processes), Bungoma County Government (5223, 451), National Transport and Safety Authority (5144, 871), Kenya Maritime Authority (5174, 169), Nairobi County Government (5241, 64).** Mixed county and national; three have 300 processes or fewer. KLB has repeat-winner and non-competitive signals; Bungoma has a high non-competitive share; NTSA is the largest, for timing; Kenya Maritime is small with few signals, to test "nothing found"; Nairobi is thin, for `widen_range`. Name variants such as "County Assembly Of Bungoma" are separate buyers (A4) | Easy |
| D26 | How indicator hits are identified and counted (M2.1) | Random IDs stored in a `hits` table / deterministic IDs | **Deterministic `hit_id`**: `RW-` + first 10 hex of SHA-256 of indicator, buyer, range, supplier and thresholds. The same question always gives the same ID; nothing is written to the snapshot (it is opened read-only). The `hits` and `findings` tables belong to run state and arrive with M2.3. A repeat-winner "win" is an **award**, but each hit also states how many **tender processes** those awards fall in, in its claim text, because lots of one tender are not a pattern over time (seen at Kenya Literature Bureau). An award with several suppliers splits its value evenly between them | Easy |
| D35 | Second indicator, built after submission | `single_bidder` / `value_outlier` / `non_competitive_share` | **`non_competitive_share`.** `single_bidder` is still impossible (`numberOfTenderers` is 0% populated), so the earlier plan to add it was wrong. `value_outlier` fires at 237 of 240 buyers and needs a tighter rule first. `non_competitive_share` lists a buyer when at least 3 awards used the configured methods (`direct`, `selective`) and carry at least 25% of its award value: 13 of 220 buyers with 50+ usable awards in 2023-2025 (6%). One hit per buyer, claim from a fixed template, severity from the share (medium from 40%, high from 60%), fixed reasons `direct_awards_present` / `restricted_bidding_only` / `needs_context` (D34 picks the most specific). Needs only `value` and `method`, so it is runnable where `repeat_winner` is not. Direct and selective awards are lawful in set circumstances, so a hit says "look", not "wrong". Real-data check: Kenya Literature Bureau, 74 of 453 awards, 95% of value | Easy |
| D34 | Who chooses severity and the specific reason (after the E9S and E7R eval failures) | Model chooses / code chooses | **Code chooses.** `repeat_winner` computes each hit's `severity` from the hit's own value share, using bands in `config/indicators.yaml` (low under 25%, medium from 25%, high from 50%; an ASSUMPTION that only orders a reading list). `record_finding` stores that value and ignores the model's, so text planted in a supplier name cannot steer it (E9S). For the reason, the verifier stores the most specific true reason (the first one listed for the hit) whatever the model picked; the model keeps one choice, `needs_context`, for a pattern it thinks cannot be judged. Prompt version m3.1-3 tells the model to copy the hit's severity. Cost: the model's judgement no longer adds anything to severity, which the baseline comparison in EVALS.md already suggested | Easy |
| D33 | The human gate and filing (M3.2) | Model files / code files after approval | **`human_gate` contains only `interrupt()`** (SQLite checkpointer, thread = run ID, `data/checkpoints.sqlite`), so resuming repeats no work (tested: no model turn or tool call is repeated). A separate `decide` step validates the typed name (2 to 80 characters, letters, no control characters), the decision and the memo hash, then writes the one `approvals` row per run (approver, **OS user**, note, the memo's SHA-256, and the **run log's head hash as the approver saw it**, which closes most of the log-forgery gap in D28) and an `approval` event in the log. **`file` is code, not the model:** it re-verifies the log chain, re-hashes the draft on disk against the *approved* hash (the same bytes are then written, with no second read), asks the borrowed server's `list_directory` whether `<run_id>.md` exists (because `write_file` overwrites silently, spike S4), calls `write_file`, reads the file back and checks the hash again. A rejection never starts the Filesystem server. Refusals leave `filed/` unchanged and are reported: `draft_changed`, `duplicate_filename`, `listing_failed`, `write_failed`, `written_differently`, `log_unverifiable`, `filesystem_unavailable`. The model is offered no filesystem tool and our server exposes none. The memo's banner now reads true before and after filing ("A screening aid, not a decision"), because the filed bytes are the approved bytes. Known limit: the list-then-write check is not atomic, which is acceptable for one local user. Found by testing against the real Node server: a relative `filed` folder was resolved against the server's own root (`filed/filed`), and an injected `tool_call_id` text block, if joined into a directory listing, would have hidden an existing file from the duplicate check; both fixed with regression tests | Easy |
| D32 | How the memo is drafted (M3.1) | Model writes the memo / template over verified facts | **A Jinja2 template filled by code from the hash-verified run log, the stored findings and the snapshot's source-file table.** The model writes only the summary paragraph, and `draft_memo` itself checks it (rule 3: any supplier or OCID must belong to a finding and be printed exactly as the claim prints it; every number must appear in a filed claim; no spelled-out numbers, accusing words or capitalised names; one line, 40 to 700 characters), so the check cannot be skipped. Up to two model attempts, each refusal's reasons fed back; then a fixed summary. A refused summary writes no file. **The bytes are deterministic** (no wall-clock time; the header uses the run's own start time), so one set of inputs always gives one SHA-256, which is what the approval will bind to. The memo records the run log's head hash at drafting. Every route ends in a memo, including no awards, nothing runnable and an empty plan. Written to `drafts/<run_id>.md`; the latest draft's hash is in `memos` | Easy |
| D31 | Finding explanations (M2.4 review) | Free-text rationale / closed list / limited vocabulary | **Closed list of fixed reasons per indicator (option b).** The real 8B run wrote "a supplier received a significant share of awards from this buyer in a short period": the data showed one tender process, so "significant" and "short period" were the model's own, unsourced wording, and the verifier cannot catch vague claims. Now the model picks a `reason_code`; the wording is fixed text in the indicator file; each reason has an `applies(hit)` rule so it can only be chosen when true for that hit; the stored `rationale` is that text. The model still makes real choices (severity and which reason fits), but writes no sentence. Cost: less nuance; the 8B model often picks the generic reason (`value_share_at_threshold`) over the more informative one (`lots_of_one_tender`). Possible later: allow several codes per finding. Plan skip reasons stay free text, guarded by the wording rules | Easy |
| D30 | How the model is given tools (M2.4) | Model passes buyer and dates / harness fixes them | **The harness fixes `buyer_key`, `date_from` and `date_to`** and hides them from the model's tool schema, so the model calls `repeat_winner` with no arguments and cannot drift to another buyer or mistype a date (SPEC 3.1: the officer chose the buyer before the agent started). Tools are loaded with `langchain-mcp-adapters` through the logging wrapper, then bound. The model is offered only the planned indicator tools and `record_finding`. Limits as SPEC section 7: 25 tool calls and 15 min for investigation, 30 min per run, an Ollama call 180 s with one retry, a failed tool call retried once then logged as a gap. A finding may be repaired twice; a 4th attempt for the same hit is not sent (a `refused_call` record is logged). **The tool menu is narrowed per turn:** `record_finding` is offered only after some tool has returned a hit, and an indicator already run is refused if called again. Found in the first real run: with zero hits the 8B model ignored the prompt and invented hit IDs (the verifier refused every one; prompting alone did not stop it). **Two wasted tool calls in a row** (refused, repeated, or citing a hit that does not exist) **end the investigation** and are reported as a gap. Prompts are versioned (`PROMPT_VERSION`, in every log) | Easy |
| D29 | How `record_finding` verifies (M2.3) | Server trusts the model / server checks the run log | **The server is launched with the run's ID and re-verifies that run's hash chain on every call**, then looks the `hit_id` up in the logged tool outputs; the claim text, OCIDs and source references are copied from the log by code. Findings go to a separate `data/run_state.sqlite` (the snapshot stays read-only). `hits` is keyed by (`run_id`, `hit_id`), not `hit_id` alone, because IDs are deterministic (D26). Refused attempts are kept as `rejected` rows without their text. **Rule 5 (no withheld names) is enforced by shape**: withheld names are never stored, so they cannot be looked up; a rationale is one plain sentence, so any capitalised word after its first is refused. Two additions beyond the SPEC wording, both easy to remove: spelled-out numbers ("four", "half") are refused like digits, and accusatory stems beyond SPEC's list (launder, collu, kickback, embezzl, theft, scam, rigged…) are banned. Rule 3 (summary paragraph) arrives with M3.1 | Easy |
| D28 | Run log design (M2.2) | Per-server logs / one client-side chain | **One hash-chained JSONL per run, written by a client-side `LoggedSession`** that offers only `list_tools` and `call_tool` (no way round it). Each line: `seq`, `ts`, `run_id`, `event`, `prev_hash`, `hash` (SHA-256 of the canonical JSON of every other field). Tool calls get `tc-0001`, `tc-0002`, … , shared by our server and the Filesystem server, with a `server` label. Our server's own output is logged unchanged; the model sees it plus `tool_call_id`. Records after `run_end`, missing `run_end` (reported as INCOMPLETE), moved, removed, inserted and edited lines are all detected. **Limit, stated honestly:** there is no secret key, so someone who rewrites every line and every hash is not caught from the file alone; `verify-log` prints the head hash so the approval record can store it (M3.2). `RunLog.resume` continues the same chain after the human gate | Med |
| D27 | Extreme-value cap (SPEC 3.2) | No cap / cap | **KES 10 billion** (`data_quality.value_sanity_cap_kes`): 3 of 67,944 positive awards exceed it and carry 18% of all award value. ASSUMPTION; awards above it, in another currency, or with zero value are counted as data-quality notes and left out of statistics. New dependency: `pyyaml` (config file) | Easy |
| D25 | Portal link (S2 result) | `portal_url = null` / build it | **Build it**: `https://tenders.go.ke/tenders/<number>`, `<number>` = middle part of the raw `tender.id`. Verified on 4 of 4 sampled historical tenders; stored in `processes.portal_url` (all 57,576 kept processes have one). Pattern and limits in `docs/spikes.md` | Easy |

## 14. Assumption ledger

- **A1:** Python 3.12 and Node 20+ are installable on your Windows laptop.
- **A2:** The indicator parameters (N=3 wins, X=30% value, Y=40% non-competitive, D=7 days, k=3) are placeholders until checked against the OCP red-flag guidance.
- **A3:** The §8 timings hold within ±50%.
- **A4:** Identifying buyers by raw ID, falling back to the normalised name, is good enough. Name variants show up as separate candidates.
- **A5:** Withholding names without incorporation markers is acceptable to the organisers.
- **A6:** Terminal output is acceptable for the demo video.
- **A7:** The organisers' test tenders are in OCDS format.
- **A8:** A typed approver name plus the OS username meets "named human approving".
- **A9:** 16k context is enough when tool outputs are capped at ~4k tokens.

## 15. Risk register (biggest first)

| Risk | Likelihood | Impact | Mitigation | Revisit trigger |
|---|---|---|---|---|
| R1 Solo, 7 days, slip | High | High | Vertical slices; Streamlit and the 14B comparison can be dropped; docs written alongside | Behind the implementation plan by end of Sat 10 Oct |
| R2 Qwen3 unreliable at multi-turn tool calls | Med | High | S3 first; narrow tool menu per node; argument validation and repair; claims built by tools | S3 < 8/10 sequences valid |
| R3 Context overflow / swap (`num_ctx`, 16 GB) | Med | High | `num_ctx` 16k, capped outputs, 8B default, RAM measured in S3 | Swap or truncation seen in S3 |
| R4 Key fields mostly empty | Med | High | S1 first; 6 candidates; top 2 rely on fields known to exist | S1 result |
| R5 A flag reads as an accusation | Low | Very high | Templates, verifier, caveats, peer baseline, human gate | Any eval output with an accusatory tone |
| R6 Run time > 20 min | Med | Med | Small eval buyers, paging, caps | S3 timing |
| R7 Data licence unclear | Med | Med | Ask on day 1; data never committed | Organiser answer |
| R8 Live-review change too slow | Med | Med | Indicator registry; rehearse 3 times with a spare indicator | Rehearsal > 10 min |
| R9 Windows Node / launch quirks | Med | Med | Pinned `node dist/index.js`; S4 on day 1 | S4 result |
| R10 Personal-data leak via supplier names | Med | Med | D14 plus redaction tests across all outputs | Organiser answer |
| R11 No practitioner interview | High | Med | Outreach today; fallback to published OAG reports and red-flag guides, stated honestly | No reply by Sat 10 Oct |

## 16. Open questions

| Question | Who answers |
|---|---|
| Exact deadline time and time zone | Organisers (community channel) |
| PPRA OCDS data licence | Organisers / PPRA |
| Is a coding agent allowed in the live review? | Organisers |
| Test tenders: link and format | You / organisers |
| Is withholding non-incorporated supplier names acceptable or required? | Organisers |
| Per-record portal URL | Spike S2 |
| A procurement or audit practitioner for 20 min | Outreach |

## 17. Adversarial review record

A fresh reviewer agent, which had not seen the planning conversation, reviewed spec v1. Every point was addressed in v2:
- an unverified summary paragraph (now verified)
- logging that could not cover the borrowed server (client wrapper)
- a gate that could be bypassed or overwritten (code-only, hash-bound filing)
- a sole-trader name leak (incorporation markers only)
- a live-tender filter tied to the snapshot date (now today's date and status)
- model-controlled thresholds (locked)
- a verifier rule that could not work (template claims)
- weak agentic depth (hit triage, peer baseline, process detail, `widen_range`, a no-model baseline)
- `num_ctx` and thinking-mode risks
- Windows `npx`
- an eval budget that was too large
- an unfixed failure that was not actually a failure

**Not fixed, and accepted:**
- The accuracy labels are ours, not a practitioner's. This is stated honestly.
- The 14B model may not fit in memory. In that case the comparison becomes 8B against `qwen3:4b`.
