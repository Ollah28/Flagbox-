# HANDOFF.md — read this first in any new session

You are helping build **Flagbox**, a solo entry for the Bid Box Challenge (Governance track). The deadline is **Thu 15 Oct 2026** (exact time TBC). The developer is a Windows user who works mainly through Claude Cowork and wants plain explanations, because they must explain every line of code in a 20-minute live review.

## Read these files, in this order
1. **CLAUDE.md**: hard rules and technical rules. Follow them exactly.
2. **SPEC.md**: what we're building (v2, adversarially reviewed). The decision log is in §13.
3. **PLAN.md**: milestones M0–M7 with tasks and acceptance checks. Work strictly in order, one task at a time.
4. **PROGRESS.md**: what's done and what's next. Update it after every task.

## Where we are (Thu 8 Oct, 21:30)
- ✅ Planning done: SPEC.md, PLAN.md, CLAUDE.md written.
- ✅ **M0.1 done.** Project skeleton, pinned dependencies (`mcp==1.30.0`), `uv run flagbox version` → `flagbox 0.1.0`, and pytest passes on Windows. **Tick it in PLAN.md.**
- ✅ Installed on the laptop: Git, uv 0.12.23, Ollama 0.40.1, Claude Code. Node: unknown, so check `node --version` (need 20+).
- ✅ One local git commit exists. ❌ **Not pushed yet.** The GitHub repo `Ollah28/flagbox` must be created (empty, public, no README/licence) and then pushed.
- ⏳ **Model downloading.** `ollama pull qwen3:8b` kept timing out (Cloudflare R2 unreachable on this network). The developer is fetching the official Qwen GGUF from Hugging Face (`hf.co/Qwen/Qwen3-8B-GGUF:Q4_K_M`, or a browser download plus `ollama create qwen3:8b`). The model will be named `qwen3:8b` either way. Note its source in the README.
- **Next tasks:** M0.3 (Filesystem MCP server on Windows) and M1.1 (data download), which don't need the model. Then M0.2 (model tool-calling test) once `ollama list` shows `qwen3:8b`.

## How this setup works (important)
- The project folder is `C:\Users\ZB 15 G3\OneDrive\Desktop\CODE PROJECTS\flagbox`. OneDrive sync is paused.
- **Cowork's shell is a Linux VM, not the developer's Windows.** It can edit files and run `uv run pytest`, ruff and pyright (use a separate venv inside the VM; never touch the Windows `.venv`). It **cannot** reach Ollama on the developer's laptop (localhost isn't reachable).
  → Anything that calls the model (M0.2, agent runs, evals) is written by you and **run by the developer in PowerShell**. Give the exact copy-paste commands and ask them to paste the output back.
- **Git: the developer runs commit and push in PowerShell.** Cowork can't delete files by default, which breaks git's lock files. After each task, give a short command block:
  ```powershell
  git add .
  git commit -m "<task id>: <what changed>"
  git push
  ```
  **Push at least once a day.** It's evidence that the work was built during the submission period.
- Windows paths: use `pathlib`; launch the Filesystem server with `node node_modules/@modelcontextprotocol/server-filesystem/dist/index.js ./filed`, never `npx`.

## Working style the developer wants
- Explain each piece in plain language, like a patient teacher.
- Small steps. After each task, report: what was built, the test results, and the exact commands they should run.
- Don't add features that aren't in SPEC.md. If something doesn't match the spec (an empty data field, an API that behaves differently), stop, explain, and propose an update to SPEC §13.

## Open questions (waiting on others)
- Exact deadline time; PPRA data licence; whether a coding agent is allowed in the live review (posted to the community channel?).
- Link and format of the organisers' "test tenders" (the developer will share it).
- Practitioner interview: outreach messages were drafted. Check whether they were sent.
