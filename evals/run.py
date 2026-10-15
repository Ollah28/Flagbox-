"""Run the model evals: each task several times on the real model, then report.

    uv run python evals/run.py --model qwen3:8b --repeats 3
    uv run python evals/run.py --tasks E8 --repeats 1          # a quick dry run

For every task and repeat this calls the real `flagbox run` code (real model, real MCP servers,
real log), then judges the result from what the run left on disk. It writes
evals/results/<date>-<model>.json and a matching .md summary.

Variation is reported two ways: pass rate over the repeats, and the average pairwise Jaccard
overlap of the OCIDs the findings cited (1.0 means every repeat cited exactly the same records).
"""

import argparse
import asyncio
import ctypes
import itertools
import json
import shutil
import statistics
import subprocess
import time
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from fixtures import RANGE, build_world
from tasks import TASKS, TASKS_BY_ID, Observed, Task, memo_text

from flagbox.agent import prompts
from flagbox.agent.model import ModelSettings, settings_from_env
from flagbox.agent.run import run
from flagbox.agent.runlog import iter_records, verify_log
from flagbox.baseline import run_baseline
from flagbox.server import runstate

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "indicators.yaml"
REAL_DATA = ROOT / "data"


@dataclass
class RunRecord:
    task: str
    repeat: int
    passed: bool
    checks: dict[str, bool]
    seconds: float
    run_id: str = ""
    status: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    findings: int = 0
    cited_ocids: list[str] = field(default_factory=list)
    free_ram_gb: float | None = None
    error: str = ""


def free_ram_gb() -> float | None:
    """Free physical memory in GB (Windows only; None elsewhere)."""
    try:

        class Status(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("load", ctypes.c_ulong),
                ("total_phys", ctypes.c_ulonglong),
                ("avail_phys", ctypes.c_ulonglong),
                ("total_page", ctypes.c_ulonglong),
                ("avail_page", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("avail_virtual", ctypes.c_ulonglong),
                ("avail_ext", ctypes.c_ulonglong),
            ]

        status = Status()
        status.length = ctypes.sizeof(Status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
        return round(status.avail_phys / 1024**3, 1)
    except (AttributeError, OSError):
        return None


def observe(
    data_dir: Path, work: Path, run_id: str, lines: list[str], exit_code: int
) -> tuple[Observed, dict[str, Any]]:
    log_path = work / "logs" / f"{run_id}.jsonl"
    verified = verify_log(log_path)
    records = list(iter_records(log_path)) if verified.ok else []
    with closing(runstate.connect(data_dir)) as conn:
        findings = [
            {
                "hit_id": row["hit_id"],
                "severity": row["severity"],
                "reason_code": row["reason_code"],
                "claim_text": row["claim_text"],
                "ocids": [ref["ocid"] for ref in json.loads(row["source_refs"])],
            }
            for row in runstate.kept_findings(conn, run_id)
        ]
        approval = runstate.get_approval(conn, run_id)
        run_row = runstate.get_run(conn, run_id)
        dump = []
        for table in ("hits", "findings", "approvals", "memos", "runs"):
            for row in conn.execute(f"SELECT * FROM {table}"):  # fixed table names
                dump.append(" ".join(str(v) for v in tuple(row)))
        rejected = runstate.count_rejected(conn, run_id)
    filed = work / "filed"
    observed = Observed(
        exit_code=exit_code,
        output="\n".join(lines),
        run_id=run_id,
        log=records,
        log_ok=verified.ok,
        log_complete=verified.complete,
        memo=memo_text(work / "drafts" / f"{run_id}.md"),
        findings=findings,
        rejected_attempts=rejected,
        filed_files=sorted(p.name for p in filed.glob("*")) if filed.exists() else [],
        approval=dict(approval) if approval else None,
        db_text="\n".join(dump),
        status=run_row["status"] if run_row else "",
    )
    return observed, dict(run_row) if run_row else {}


async def run_one(task: Task, repeat: int, worlds: dict[str, Path], model: str, root: Path):
    work = root / "runs" / f"{task.id}-{repeat}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    lines: list[str] = []
    queue = iter(task.answers)

    def ask(prompt: str) -> str:
        try:
            return next(queue)
        except StopIteration:
            raise EOFError from None

    started = time.perf_counter()
    ram = free_ram_gb()
    try:
        code = await run(
            task.buyer,
            *task.dates,
            worlds[task.world],
            work / "logs",
            CONFIG,
            say=lines.append,
            ask=ask,
            settings=ModelSettings(model=model, host=settings_from_env().host),
            drafts_dir=work / "drafts",
            filed_dir=work / "filed",
        )
    except Exception as error:  # a crash is a failed run, and the reason is kept
        return RunRecord(
            task.id, repeat, False, {"completed_without_crash": False},
            round(time.perf_counter() - started, 1), free_ram_gb=ram,
            error=f"{type(error).__name__}: {error}",
        )  # fmt: skip
    seconds = round(time.perf_counter() - started, 1)
    first = lines[0] if lines else ""
    run_id = first.split()[1] if first.startswith("Run ") else ""
    if not run_id:
        return RunRecord(task.id, repeat, False, {"run_started": False}, seconds, free_ram_gb=ram,
                         error=first or "no output")  # fmt: skip
    observed, row = observe(worlds[task.world], work, run_id, lines, code)
    checks = task.check(observed)
    return RunRecord(
        task=task.id,
        repeat=repeat,
        passed=all(checks.values()),
        checks=checks,
        seconds=seconds,
        run_id=run_id,
        status=observed.status,
        tokens_in=row.get("tokens_in") or 0,
        tokens_out=row.get("tokens_out") or 0,
        llm_calls=row.get("llm_calls") or 0,
        tool_calls=row.get("tool_calls") or 0,
        findings=len(observed.findings),
        cited_ocids=sorted(observed.cited_ocids),
        free_ram_gb=ram,
    )


def jaccard(a: set[str], b: set[str]) -> float:
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def mean_overlap(records: list[RunRecord]) -> float | None:
    sets = [set(r.cited_ocids) for r in records if r.error == ""]
    if len(sets) < 2:
        return None
    return round(statistics.mean(jaccard(a, b) for a, b in itertools.combinations(sets, 2)), 3)


def summarise(records: list[RunRecord]) -> list[dict[str, Any]]:
    rows = []
    for task in TASKS:
        mine = [r for r in records if r.task == task.id]
        if not mine:
            continue
        failing: dict[str, int] = {}
        for r in mine:
            for name, ok in r.checks.items():
                if not ok:
                    failing[name] = failing.get(name, 0) + 1
        rows.append(
            {
                "task": task.id,
                "title": task.title,
                "passes": sum(r.passed for r in mine),
                "runs": len(mine),
                "mean_seconds": round(statistics.mean(r.seconds for r in mine), 1),
                "mean_tokens_in": round(statistics.mean(r.tokens_in for r in mine)),
                "overlap": mean_overlap(mine) if task.measures_overlap else None,
                "failing_checks": failing,
                "errors": [r.error for r in mine if r.error],
            }
        )
    return rows


def baseline_comparison(records: list[RunRecord]) -> list[dict[str, Any]]:
    """Eval B: the no-model baseline against the agent on E1 and E3."""
    out = []
    for task_id, buyer, rng, world in (
        ("E1", "Kenya Literature Bureau", ("2023-01-01", "2025-12-31"), REAL_DATA),
        ("E3", "Eval Buyer A", RANGE, None),
    ):
        data_dir = world or WORLD_DIR
        base = run_baseline(data_dir, CONFIG, buyer, *rng)
        base_ocids = {o for f in base["findings"] for o in f["ocids"]}
        agent = [r for r in records if r.task == task_id and not r.error]
        out.append(
            {
                "task": task_id,
                "baseline_hits": len(base["findings"]),
                "baseline_seconds": base["seconds"],
                "baseline_model_calls": 0,
                "agent_mean_seconds": round(statistics.mean(r.seconds for r in agent), 1)
                if agent
                else None,
                "agent_findings": [r.findings for r in agent],
                "agent_cited_same_records_as_baseline": [
                    set(r.cited_ocids) == base_ocids for r in agent
                ],
            }
        )
    return out


WORLD_DIR = Path()


def git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT
        )
        return result.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def render_markdown(
    meta: dict[str, Any], rows: list[dict[str, Any]], base: list[dict[str, Any]]
) -> str:
    lines = [
        f"# Eval results: {meta['model']}, {meta['date']}",
        "",
        f"Commit `{meta['commit']}`, prompts `{meta['prompt_version']}`, {meta['repeats']} repeats per task, "
        f"free RAM at start {meta['free_ram_gb']} GB.",
        "",
        "| Task | What | Passed | Mean time | Mean tokens in | Overlap of cited OCIDs | Failing checks |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        overlap = "n/a" if row["overlap"] is None else f"{row['overlap']:.2f}"
        failing = ", ".join(f"{k} ({v}x)" for k, v in row["failing_checks"].items()) or "none"
        lines.append(
            f"| {row['task']} | {row['title']} | {row['passes']}/{row['runs']} | "
            f"{row['mean_seconds']} s | {row['mean_tokens_in']} | {overlap} | {failing} |"
        )
    lines += ["", "## B: no-model baseline against the agent", ""]
    lines += ["| Task | Baseline hits | Baseline time | Agent mean time | Agent cited the same records |",
              "|---|---|---|---|---|"]  # fmt: skip
    for b in base:
        same = b["agent_cited_same_records_as_baseline"]
        lines.append(
            f"| {b['task']} | {b['baseline_hits']} | {b['baseline_seconds']} s, 0 model calls | "
            f"{b['agent_mean_seconds']} s | {sum(same)}/{len(same)} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    global WORLD_DIR
    parser = argparse.ArgumentParser(description="Run the Flagbox model evals.")
    parser.add_argument("--model", default=settings_from_env().model)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--tasks", default="", help="Comma-separated ids, e.g. E3,E8 (default all)")
    parser.add_argument("--work", type=Path, default=ROOT / "evals" / "work")
    parser.add_argument("--out", type=Path, default=ROOT / "evals" / "results")
    args = parser.parse_args()

    wanted = [t.strip() for t in args.tasks.split(",") if t.strip()]
    unknown = [t for t in wanted if t not in TASKS_BY_ID]
    if unknown:
        print(f"Unknown task ids: {unknown}. Known: {sorted(TASKS_BY_ID)}")
        return 2
    tasks = [TASKS_BY_ID[t] for t in wanted] if wanted else TASKS

    shutil.rmtree(args.work, ignore_errors=True)
    WORLD_DIR = build_world(args.work / "world")
    real = args.work / "real"
    real.mkdir(parents=True)
    shutil.copy(
        REAL_DATA / "flagbox.sqlite", real / "flagbox.sqlite"
    )  # a copy keeps real run state clean
    worlds = {"world": WORLD_DIR, "real": real}

    meta = {
        "model": args.model,
        "date": date.today().isoformat(),
        "commit": git_commit(),
        "prompt_version": prompts.PROMPT_VERSION,
        "repeats": args.repeats,
        "free_ram_gb": free_ram_gb(),
    }
    print(
        f"Evals: {len(tasks)} tasks x {args.repeats} repeats on {args.model} (free RAM {meta['free_ram_gb']} GB)"
    )
    records: list[RunRecord] = []
    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.out / f"{meta['date']}-{args.model.replace(':', '-')}"
    for task in tasks:
        for repeat in range(1, args.repeats + 1):
            record = asyncio.run(run_one(task, repeat, worlds, args.model, args.work))
            records.append(record)
            verdict = "PASS" if record.passed else "FAIL"
            failed = [k for k, ok in record.checks.items() if not ok]
            print(
                f"{task.id} #{repeat}: {verdict}  {record.seconds:.0f}s  findings {record.findings}"
                + (f"  failed: {', '.join(failed)}" if failed else "")
                + (f"  ERROR {record.error}" if record.error else ""),
                flush=True,
            )
            stem.with_suffix(".json").write_text(  # saved after every run, so a crash loses little
                json.dumps({"meta": meta, "runs": [asdict(r) for r in records]}, indent=2),
                encoding="utf-8",
            )

    rows = summarise(records)
    base = baseline_comparison(records)
    payload = {
        "meta": meta,
        "runs": [asdict(r) for r in records],
        "summary": rows,
        "baseline": base,
    }
    stem.with_suffix(".json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    markdown = render_markdown(meta, rows, base)
    stem.with_suffix(".md").write_text(markdown, encoding="utf-8")
    print("\n" + markdown)
    print(f"Saved {stem.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
