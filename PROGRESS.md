# PROGRESS.md

## Current milestone
M2 in progress (M2.1 done). M0.2 (model spike) still to run by the developer; M2.4 needs it.

## Done
- Thu 8 Oct: SPEC.md v2 written and adversarially reviewed; PLAN.md written.
- Thu 8 Oct: **M0.1** project skeleton, pinned dependencies, first test.
- Sun 11 Oct: **M0.3** Filesystem MCP server pinned (`@modelcontextprotocol/server-filesystem@2026.8.31`),
  runs on Windows via `node ...dist/index.js ./filed`. Writes inside `filed/` work, `../` and absolute
  writes outside are refused, and `write_file` **overwrites silently** (so our code must check
  `list_directory` first). Details in `docs/spikes.md`.
- Sun 11 Oct: **M1.1** `flagbox setup-data` downloads `2023/2024/2025.jsonl.gz` into `data/raw/` and writes
  `data/MANIFEST.json`. Real run: 148,777 lines in 31 s; second run skips all three files in 1.4 s.
- Sun 11 Oct: SPEC §13 **D20** (live-tender filter: needs an award and an end date before today) and
  **D21** (supplier key = raw ID, else hashed name).
- Sun 11 Oct: **M1.2** `flagbox ingest` builds `data/flagbox.sqlite`. Real run, filter date 2026-10-11:
  148,777 read, **57,576 kept**; excluded 91,195 `no_award`, 2 `end_date_not_past`, 4 `end_date_unreadable`.
  17 s. 470 buyers, 68,714 awards, 55,266 award-supplier rows (40% withheld), 68,714 contracts.
  A supplier name containing `@` or 7+ digits is withheld even with an incorporation marker (SPEC §6.2).
- Sun 11 Oct: **M1.3** `scripts/coverage_report.py` writes `docs/data_coverage.md` (23 s). SPEC §13
  **D22** (range date), **D23** (3 indicators), **D24** (5 eval buyers) logged. Summary below.
  Checks: ruff clean, pyright 0 errors, 54 tests pass.
- Sun 11 Oct: D23 parameters decided: `repeat_winner` N = 3 wins, X = 10% of value.
- Sun 11 Oct: **M1.4** portal URL found: `https://tenders.go.ke/tenders/<number>` (number from the raw
  `tender.id`), 4 of 4 sampled tenders verified. Stored as `processes.portal_url` for all 57,576 kept
  processes (SPEC D25, details in `docs/spikes.md`). 62 tests pass, ruff and pyright clean.
- Sun 11 Oct: **M2.1** our MCP server (FastMCP, mcp 1.30) with 4 read tools: `find_buyer`,
  `data_coverage`, `fetch_buyer_awards`, `repeat_winner`. Thresholds only in `config/indicators.yaml`
  (the server refuses to start if a block has no `rationale:`/`source:` line); outputs capped near 4k tokens
  with `returned`/`truncated`; record text flattened and truncated; errors come back as
  `{"error", "message"}`. `uv run flagbox tools` lists them through a real MCP client. 101 tests pass,
  ruff and pyright clean. SPEC D26, D27.
- Sun 11 Oct: **M2.2** client-side logging. `src/flagbox/agent/runlog.py` (hash-chained JSONL writer and
  verifier) and `src/flagbox/agent/logged_tools.py` (`LoggedSession`: assigns `tc-NNNN`, logs arguments, result,
  duration and errors, injects the id into what the model reads, offers no unlogged route). New command
  `flagbox verify-log <run_id>`. Tried by hand on a real log from live calls: OK, then FAILED at line 4 after
  one edited number. 139 tests pass, ruff and pyright clean. SPEC D28.
- Sun 11 Oct: **M2.3** `record_finding(hit_id, severity, rationale)` and the verifier
  (`src/flagbox/agent/verify.py`, SPEC 3.3 rules 1, 2, 4, 5). A refused finding comes back as structured
  `violations` (all broken rules at once, never echoing a supplier name); an accepted one stores the claim text
  copied from the log. Findings live in `data/run_state.sqlite`. 211 tests pass, ruff and pyright clean; a
  mutation check (disabling two rules) made 12 tests fail, so they do bite. SPEC D29.
- Sun 11 Oct: **M2.4** the minimal agent graph and `flagbox run` (LangGraph: check_coverage -> plan ->
  investigate -> verify). Real runs on qwen3:8b: **Kenya Literature Bureau 1 min 51 s, 2 verified findings,
  0 refusals**; **Kenya Maritime Authority 54 s, justified "nothing found"**. Both logs verify. The other
  states were tried by hand: buyer not found (exit 1), ambiguous buyer with the numbered picker and cancel
  (exit 1), Ollama down (exit 2), bad dates (exit 2). 227 tests pass (16 of them drive the graph with a
  scripted fake model, so CI needs no Ollama), ruff and pyright clean. SPEC D30.
- Sun 11 Oct: **Rationale option (b), SPEC D31.** `record_finding(hit_id, severity, reason_code)`: the model
  picks a code from a closed list; the wording is fixed text in the indicator (`REASONS` in
  `repeat_winner.py`), each with an `applies(hit)` rule so it can only be chosen when true. Real run on Kenya
  Literature Bureau after the change: 2 verified findings, 0 refusals, 2 min 23 s; log verifies. 237 tests
  pass, ruff and pyright clean.
- Mon 12 Oct: **M3.1** `draft_memo` and the memo (`src/flagbox/memo/`, template `template.md.j2`). Sections:
  summary, findings (verbatim claim, fixed reason, OCID, source file and line, portal page), coverage, checks run
  with their settings, checks skipped or not completed, dismissed-hits appendix, data-quality notes, caveats,
  sources with hashes. Deterministic bytes, SHA-256 returned and stored (`memos` table). The model's summary is
  checked inside the tool (rule 3) and replaced by a fixed one after two refusals. A `draft` node follows
  `verify`; every route ends in a memo. `flagbox run` now prints the whole memo and its hash. Real runs on
  qwen3:8b: Kenya Literature Bureau 1 min 15 s with a model summary that passed on the second attempt, and 2 min
  55 s when both attempts were refused and the fixed one was used; Kenya Maritime Authority 1 min 8 s, "no
  finding". 275 tests pass, ruff and pyright clean; disabling the uncited-supplier and number checks makes 4
  tests fail. SPEC D32.
- Mon 12 Oct: **M3.2** the human gate and filing. `human_gate` is only `interrupt()` (SQLite checkpointer);
  `decide` validates the typed name, the decision and the memo hash and writes the `approvals` row (approver,
  OS user, memo SHA-256, the log head hash the approver saw) plus an `approval` log event; `file` re-verifies the
  log, re-hashes the draft against the approved hash, uses `list_directory` to refuse a duplicate name, calls
  the Filesystem server's `write_file`, reads back and re-checks the hash. A rejection never starts that
  server. New modules `agent/gate.py` and `agent/filing.py`; `flagbox run` gained `--drafts-dir` and
  `--filed-dir`. **Verified on the real stack** (qwen3:8b, our MCP server, the real Node Filesystem server):
  approval filed a byte-identical memo (hash matches the approved one) and the log verifies; a rejection filed
  nothing. 333 tests pass, ruff and pyright clean; disabling the draft-hash check and the duplicate check each
  makes tests fail. SPEC D33.
  Two real bugs found only by using the real server, both fixed with regression tests: a relative `filed`
  folder was resolved by the server against its own root (`filed/filed`), and the `tool_call_id` text block the
  logging wrapper adds would have been glued onto the last filename of a directory listing, hiding an
  existing file from the duplicate check (so `write_file` would have overwritten it).

- Thu 15 Oct: README (setup, run, how it works, built vs borrowed servers, safety table, data, honest limits,
  how to add an indicator) and ARCHITECTURE.md written. `flagbox baseline`, `flagbox setup` and the `evals/`
  runner with fixture buyers added (339 tests). **The 3-repeat model evals have NOT been run** (the overnight run
  was killed when the session ended); the README and EVALS state this plainly. `flagbox setup` was run once on
  this machine (all steps skip correctly) but not yet in a fresh clone.

## M1.3 results in one screen
- **Indicators chosen:** `repeat_winner`, `value_outlier`, `non_competitive_share` (all above 60% joint
  coverage). Dropped: `single_bidder` (`numberOfTenderers` is 0% populated) and `short_window`
  (91% of tender windows are under 7 days; `startDate` is a system timestamp). `date_sequence` stays as the
  rehearsal candidate, but it shows a hit at 219 of 240 buyers, so it is noisy.
- **Range date:** `tenderPeriod.endDate` (99.9% plausible).
- **Eval buyers:** Kenya Literature Bureau, Bungoma County Government, National Transport and Safety
  Authority, Kenya Maritime Authority, Nairobi County Government.
- **Spec name mismatch:** SPEC's example "Nairobi City County" does not exist in the data. The buyer is
  "Nairobi County Government" (64 processes). The user journey example should use that name.

## Decided
- `repeat_winner`: N = 3 wins, X = 10% of value (fires at 6 of 240 buyers; the SPEC placeholder of 30% fires at
  1). Goes into `config/indicators.yaml` with a `rationale:` line in M2.1. A count-based test is listed under
  "next indicators".
- D22-D24 still await an explicit OK from the developer (they were chosen by the SPEC 3.2 rule).

## Next
- **Developer:** M0.2 (model tool-calling spike, needs Ollama), then paste the result into `docs/spikes.md`.
- Commit and push (see Known issues), then **M3.3**: first full real run on 2 eval buyers with timing recorded here (the code path now works end to end; I have one approval run at 6 min 54 s and one rejection run at 1 min 54 s). Close memory-hungry apps before timing: see Known issues.
- M0.2 (formal S3 measurement: 10 multi-turn sequences, then qwen3:14b) is still open. Ollama is reachable from this session, so it can be run here; say so and it will be done.

## Measurements
| Date | What | Result |
|---|---|---|
| | S3 qwen3:8b valid sequences / tok/s / RAM | |
| | S3 qwen3:14b valid sequences / tok/s / RAM | |
| 11 Oct | `setup-data` first run, 3 files, 26.6 MB | 31 s |
| 11 Oct | `setup-data` second run (all skipped) | 1.4 s |
| 11 Oct | `ingest`, 3 files, 148,777 processes | 17 s |
| 11 Oct | `coverage_report.py` | 23 s |
| 11 Oct | Full run, Kenya Literature Bureau (453 awards), qwen3:8b | 1 min 51 s (plan 9.5 s; 4 model calls; 4,816 tokens in / 153 out) |
| 11 Oct | Full run, Kenya Maritime Authority (158 awards), qwen3:8b | 54 s (3 model calls; 1,861 in / 97 out) |
| 11 Oct | qwen3:8b in Ollama at num_ctx 16384 | 8.3 GB loaded, 92% CPU / 8% GPU (Quadro M1000M, 2 GB), 5.6 GB RAM free before loading |
| 12 Oct | Real run to approval and filing, Kenya Literature Bureau, qwen3:8b | 6 min 54 s, with only 0.4 GB RAM free (model 6.7 GB resident); the same buyer took 1 min 15 s to 2 min 55 s earlier |
| 12 Oct | Real run to rejection, Kenya Maritime Authority | 1 min 54 s |

## Known issues
- **Run time depends heavily on free memory.** With 0.4 GB RAM free the same run took about 3 times as long
  (model turns of 3 minutes instead of 1). The model alone is 6.7 GB resident; on this 16 GB laptop, browsers
  and other apps decide whether a run takes 2 minutes or 7. For M3.3 timing and the evals, close other apps
  first and record free RAM next to each time.
- The test approvals made while checking M3.2 are in `data/run_state.sqlite` (approver "Test Approver (automated
  check)"). They are test rows, not real approvals. `filed/` was emptied afterwards.
- Resuming a paused run in a *different process* (a `flagbox approve <run_id>` command) is not built. The
  checkpoint database exists and the gate is resumable in principle, but today the approval is typed in the
  same terminal session that drafted the memo. If the terminal is closed at the gate, the run is not resumable
  (the draft stays in `drafts/`). Worth adding only if there is time.
- The duplicate check (`list_directory`, then `write_file`) is not atomic. Fine for one local user; noted in D33.
- **The 8B model is weak at the summary paragraph.** First attempts usually add a count word ("Two suppliers")
  and are refused; with a style example in the prompt the second attempt passes, but it then copies the
  example almost word for word, so the "model-written" summary is close to templated. Left as is: the rules
  hold either way, and the memo says which kind of summary it has. An eval candidate ("summary pass rate"),
  and a choice to revisit: drop the model summary and always use the fixed one.
- Plan "skip" reasons are still free model text (checked by the wording rules). The empty-plan case lets the
  model skip the only indicator; fine while there is one indicator, to revisit when there are three.
- **NOTHING SINCE M0.1 IS COMMITTED OR PUSHED.** `git log` shows only the 8 Oct commits; about 40 files and
  the whole of M0.3 to M2.4 are uncommitted, and there is no `m1` tag. HANDOFF says a daily push is the
  evidence the work was done in the submission period. Commit and push now (suggested: one commit per
  milestone, or one big one tonight, then tag `m1` and `m2`).
- **Resolved (D31):** model-written rationales could contain unsupported claims ("in a short period"); the
  explanation is now fixed text chosen by code. Remaining free text from the model: plan skip reasons (guarded
  by the wording rules) and, from M3.1, the memo summary paragraph.
- On both Kenya Literature Bureau hits the 8B model picked the generic reason (`value_share_at_threshold`)
  although the more informative `lots_of_one_tender` was also true. Real but weak triage; an eval candidate,
  and a reason to consider allowing several codes per finding.
- **8B ignored a prompt instruction** ("if hits_total is 0, do not call record_finding") and invented hit
  IDs. Fixed structurally (tool menu narrowing, SPEC D30), not by more prompt text. Worth an eval (E-task):
  "model asked to record a finding when there are no hits".
- In the first real run the model made both `record_finding` calls in one turn and took about 70 s for it;
  a single turn with several tool calls is the slowest step. Fine at this scale.
- The verifier is strict on purpose (capitalised words, spelled numbers, a long banned list). If the 8B model
  triggers too many refusals in M2.4, loosen one rule at a time and log it in SPEC section 13; do not let the
  model's convenience weaken the rule silently. Rejection counts per rule are in `findings.reason`, ready for
  the evals.
- **The log proves "not edited by accident", not "cannot be forged".** No secret key is involved, so a person
  who recomputes every hash can rewrite a log. `verify-log` prints the head hash; M3.2 stores it in the
  approval record. The README should say this plainly.
- **`repeat_winner` finds lots of one tender.** At Kenya Literature Bureau (eval buyer) both hits are a supplier
  with 3-4 awards inside a single tender process. The claim text now says "in 1 tender process", and the agent
  must triage these in M4.2. Whether a repeat winner should count processes instead of awards is a question for
  the README's limits; not changed, because N=3 was decided on award counts.
- `flagbox tools` prints a `Processing request of type ListToolsRequest` line: it is the MCP server's own log,
  passed through on stderr. Harmless.
- `spikes/spike_tools.py` (the developer's M0.2 draft) is untracked. Lint rules E401/E501/I001 are relaxed
  for `spikes/`, and E501 for `scripts/`, in `pyproject.toml`.
- `scripts/` is not covered by pyright (config includes `src` and `tests` only).
- Data licence is still UNKNOWN (the registry shows "-"). `data/` is git-ignored and never committed.
- The registry also offers a 2026 file (3 MB). Not downloaded: the plan and SPEC freeze 2023-2025.
- Supplier IDs may fragment one company into several IDs, which weakens `repeat_winner` and
  `supplier_footprint`. Not measured yet; a caveat for the README's honest limits.

## Open questions waiting on others
- Deadline time, data licence, coding agent allowed in live review (community channel)
- Test tenders link and format
- Practitioner interview
