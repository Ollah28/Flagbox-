# Flagbox demo video: script and shot list (target 2:45, limit 3:00)

Everything below was checked against the real run of 17 Oct (buyer Kenya Literature Bureau, 2023-01-01 to 2025-12-31). Do not say anything the screen does not show.

## Before you record (10 minutes)

1. Ollama running (`ollama serve` in a spare terminal), `qwen3:8b` pulled.
2. In the project folder, on branch `dev`: `uv run flagbox version` works.
3. **A real run takes about 5 minutes. Do not record it live.** Two options:
   - Record the run once, then speed the waiting part up 8x in your editor and say "sped up" on screen; or
   - Run it once beforehand and, in the video, show the finished memo and the approval step from that run. Say "recorded earlier" on screen. (Hard rule 7: never present a replay as live.)
4. Delete old files so the screen is clean: empty `filed/` and `drafts/`.
5. Terminal font large (at least 18 pt), dark background, window 1280x720. Close notifications.
6. Have two things open in other tabs: `ARCHITECTURE` diagram (`docs/flagbox_architecture.png`) and the GitHub repo page.

## Script

| Time | On screen | Say (plain, unhurried) |
|---|---|---|
| 0:00 to 0:15 | Title card: "Flagbox: sourced red flags for public tenders. The committee decides." | "Kenya publishes its tenders and awards as open data, but audit offices are too small to read them. Flagbox gives an officer a sourced shortlist for one buyer. It never awards, recommends or accuses." |
| 0:15 to 0:35 | Architecture diagram | "One local AI agent, Qwen3 eight billion on the officer's own laptop. Two MCP servers: our own for the indicators, and the standard Filesystem server for filing. Every tool call goes through one logging wrapper." |
| 0:35 to 0:50 | Terminal: `uv run flagbox baseline --buyer "Kenya Literature Bureau" --from 2023-01-01 --to 2025-12-31` | "First the honest baseline: no model at all. The same indicators find three hits in a tenth of a second. So the model is not here to detect more. It is here to triage, explain and run the workflow." |
| 0:50 to 1:20 | Terminal: `uv run flagbox run --buyer "Kenya Literature Bureau" --from 2023-01-01 --to 2025-12-31`. Show the plan line, tool calls, one refusal if it happens. Speed up the waiting. | "The agent reads the data coverage, plans which indicators to run, calls the tools, and records a finding for each hit. It can only choose a hit, and a reason from a fixed list. Severity and wording are computed by code. If it tries something the verifier does not allow, the call is refused, and you can see the refusal here." |
| 1:20 to 1:55 | Scroll the memo: finding 3 (74 of 453 awards, 95 percent, non-open methods, severity high) then finding 1. Point at the OCID, file and line table. | "Every finding is a fixed sentence from the tool, and every one lists the exact source records: file, line, OCID and the tender page. This one says seventy-four of four hundred fifty-three awards, ninety-five percent of the value, went through direct or selective methods. That is lawful in set circumstances, so the memo says it warrants review. It does not say it is wrong." |
| 1:55 to 2:25 | The gate prompt. Type a name, `a`, a short note. Then the FILED line with matching SHA-256. | "Now the human gate. The agent stops. A named person reads the memo and approves. The approval is tied to the memo's SHA-256. Only our code, never the model, then calls the Filesystem server to write the file, and it checks the saved bytes match what was approved. If you reject, nothing is filed." |
| 2:25 to 2:40 | Terminal: `uv run flagbox verify-log <run id>` shows OK | "The whole run is a hash-chained log. Edit one character and verification fails." |
| 2:40 to 3:00 | GitHub repo page, then EVALS.md headings | "It is open source, with 356 tests. I report my failures: before the last fix, a planted instruction in a supplier name steered the model's severity ratings. I moved that choice into code and re-ran. What is not proven: accuracy against labelled cases, and the thresholds have not been checked by a procurement practitioner. Flagbox: the committee decides." |

## Lines to avoid (they would be untrue or overclaim)

- Do not say "detects corruption", "finds fraud", "catches" or "flags suspects". Say "patterns that warrant review".
- Do not say it "runs live" on the portal. It uses a downloaded snapshot of 2023 to 2025 award files.
- Do not claim more than two indicators. Do not mention single-bidder (the field is empty in the data).
- Do not say the model improves detection. The baseline finds the same records.

## After recording

1. Upload to YouTube as **Unlisted** (or Google Drive with "anyone with the link"), and test the link in a private window.
2. Put the link at the top of `README.md` and in the submission form's video field, if the organisers still accept edits.
3. Tag the commit you recorded from: `git tag v1.1-demo`.
