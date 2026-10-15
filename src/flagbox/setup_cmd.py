"""`flagbox setup`: take a fresh clone to "ready to run", one step at a time (PLAN M6.1).

Every step checks first and skips what is already done, so running it twice is cheap and safe.
The steps:
  1. Node 20 or newer is installed (the borrowed Filesystem server needs it);
  2. `npm ci` installs the pinned Filesystem server;
  3. the 2023-2025 PPRA files are downloaded and their hashes checked;
  4. the redacted SQLite snapshot is built;
  5. the language model is present in Ollama (pulled if Ollama is running but lacks it).

Nothing here needs a model; step 5 only asks Ollama what it has.
"""

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from flagbox.agent.model import ModelSettings, ModelUnavailable, check_ollama, settings_from_env
from flagbox.data.download import setup_data
from flagbox.data.ingest import DB_NAME, ingest
from flagbox.mcp_client import FILESYSTEM_SERVER_JS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIN_NODE_MAJOR = 20

DONE, SKIPPED, WARNING, FAILED = "done", "skipped", "warning", "FAILED"


@dataclass(frozen=True)
class Step:
    name: str
    status: str
    detail: str


def node_major(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int | None:
    """The installed Node's major version, or None if Node is missing or unreadable."""
    node = shutil.which("node")
    if node is None:
        return None
    try:
        out = runner([node, "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"v(\d+)\.", out.stdout.strip())
    return int(match.group(1)) if match else None


def run_setup(
    data_dir: Path,
    say: Callable[[str], None] = print,
    force: bool = False,
    skip_model: bool = False,
    settings: ModelSettings | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    download: Callable[[Path], list] = setup_data,
    build_snapshot: Callable[..., object] = ingest,
    check_model: Callable[[ModelSettings], None] = check_ollama,
    project_root: Path = PROJECT_ROOT,
) -> list[Step]:
    settings = settings or settings_from_env()
    steps: list[Step] = []

    def record(step: Step) -> Step:
        steps.append(step)
        say(f"  [{step.status}] {step.name}: {step.detail}")
        return step

    say("Setting up Flagbox. Each step is skipped if it is already done.")

    # 1. Node
    major = node_major(runner)
    if major is None or major < MIN_NODE_MAJOR:
        found = "not found" if major is None else f"version {major}"
        record(
            Step(
                "Node",
                FAILED,
                f"Node {MIN_NODE_MAJOR}+ is needed ({found}). Install from nodejs.org.",
            )
        )
        return steps
    record(Step("Node", DONE, f"version {major}"))

    # 2. the pinned Filesystem server
    server = project_root / FILESYSTEM_SERVER_JS
    if server.exists() and not force:
        record(Step("Filesystem server", SKIPPED, "already installed"))
    else:
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if npm is None:
            record(Step("Filesystem server", FAILED, "npm was not found (it comes with Node)."))
            return steps
        result = runner([npm, "ci"], cwd=project_root, capture_output=True, text=True)
        if result.returncode != 0 or not server.exists():
            record(
                Step("Filesystem server", FAILED, f"`npm ci` failed: {result.stderr.strip()[:200]}")
            )
            return steps
        record(Step("Filesystem server", DONE, "installed with `npm ci` (version pinned)"))

    # 3. data
    try:
        results = download(data_dir)
    except Exception as error:  # network down, disk full, a corrupt download...
        record(Step("PPRA data", FAILED, f"{type(error).__name__}: {error}"))
        return steps
    fetched = sum(r.status == "downloaded" for r in results)
    record(
        Step(
            "PPRA data",
            DONE if fetched else SKIPPED,
            f"{fetched} downloaded, {len(results) - fetched} already present and verified",
        )
    )

    # 4. the snapshot
    if (data_dir / DB_NAME).exists() and not force:
        record(Step("Snapshot", SKIPPED, f"{data_dir / DB_NAME} exists (use --force to rebuild)"))
    else:
        try:
            report = build_snapshot(data_dir)
        except (FileNotFoundError, ValueError) as error:
            record(Step("Snapshot", FAILED, str(error)))
            return steps
        kept = getattr(report, "kept", "?")
        record(Step("Snapshot", DONE, f"{kept} processes kept after the live-tender filter"))

    # 5. the model
    if skip_model:
        record(Step("Model", SKIPPED, "--skip-model given"))
        return steps
    try:
        check_model(settings)
        record(Step("Model", SKIPPED, f"{settings.model} is installed in Ollama"))
    except ModelUnavailable as error:
        if "not installed" in str(error):
            say(f"  Pulling {settings.model} (about 5 GB; this can take a while)...")
            pulled = runner(["ollama", "pull", settings.model])
            ok = pulled.returncode == 0
            record(Step("Model", DONE if ok else FAILED, f"ollama pull {settings.model}"))
        else:
            record(Step("Model", WARNING, f"{error} Then run `ollama pull {settings.model}`."))

    return steps


def summary(steps: list[Step]) -> tuple[bool, str]:
    failed = [s for s in steps if s.status == FAILED]
    if failed:
        return (
            False,
            f"Setup stopped at '{failed[0].name}'. Fix that and run `flagbox setup` again.",
        )
    warned = [s for s in steps if s.status == WARNING]
    if warned:
        return True, "Ready, except for the model: start Ollama and pull it, then you can run."
    return (
        True,
        'Ready. Try: uv run flagbox run --buyer "Kenya Literature Bureau" '
        "--from 2023-01-01 --to 2025-12-31",
    )
