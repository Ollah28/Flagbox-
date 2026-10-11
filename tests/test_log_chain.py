import json
import uuid
from pathlib import Path

import pytest

from flagbox.agent.runlog import (
    GENESIS_HASH,
    LogError,
    RunLog,
    canonical_json,
    iter_records,
    log_path,
    parse_run_id,
    verify_log,
)
from flagbox.cli import main


def make_log(tmp_path: Path, tool_calls: int = 3, finish: bool = True) -> RunLog:
    log = RunLog.create(tmp_path, buyer_key="5015")
    for i in range(tool_calls):
        log.append(
            "tool_call",
            tool_call_id=log.next_tool_call_id(),
            tool="repeat_winner",
            arguments={"n": i},
            result={"hits": []},
        )
    if finish:
        log.finish("completed")
    return log


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def edit_line(path: Path, index: int, **changes: object) -> None:
    lines = read_lines(path)
    record = json.loads(lines[index])
    record.update(changes)  # edit a field but leave the old hash in place
    lines[index] = canonical_json(record)
    write_lines(path, lines)


# ---- a healthy log ----------


def test_a_fresh_log_verifies_and_counts_records(tmp_path: Path):
    log = make_log(tmp_path)
    result = verify_log(log.path)
    assert result.ok and result.records == 5  # run_start, 3 tool calls, run_end
    assert result.tool_calls == 3 and result.complete
    assert result.head_hash == log.head_hash != GENESIS_HASH


def test_each_line_points_at_the_one_before(tmp_path: Path):
    records = list(iter_records(make_log(tmp_path).path))
    assert records[0]["prev_hash"] == GENESIS_HASH
    for before, after in zip(records, records[1:], strict=False):
        assert after["prev_hash"] == before["hash"]
    assert [r["seq"] for r in records] == list(range(5))


def test_tool_call_ids_count_up(tmp_path: Path):
    ids = [r["tool_call_id"] for r in iter_records(make_log(tmp_path).path) if "tool_call_id" in r]
    assert ids == ["tc-0001", "tc-0002", "tc-0003"]


def test_log_without_run_end_is_valid_but_incomplete(tmp_path: Path):
    result = verify_log(make_log(tmp_path, finish=False).path)
    assert result.ok and not result.complete


# ---- tampering (the acceptance check: editing a line breaks verification) ----------


def test_editing_a_line_breaks_verification(tmp_path: Path):
    path = make_log(tmp_path).path
    edit_line(path, 2, result={"hits": ["forged"]})
    result = verify_log(path)
    assert not result.ok and result.line == 3 and "edited" in str(result.error)


def test_editing_the_last_line_breaks_verification(tmp_path: Path):
    path = make_log(tmp_path).path
    edit_line(path, 4, status="approved")
    result = verify_log(path)
    assert not result.ok and result.line == 5


def test_deleting_a_line_breaks_verification(tmp_path: Path):
    path = make_log(tmp_path).path
    lines = read_lines(path)
    del lines[2]
    write_lines(path, lines)
    result = verify_log(path)
    assert not result.ok and result.line == 3


def test_swapping_two_lines_breaks_verification(tmp_path: Path):
    path = make_log(tmp_path).path
    lines = read_lines(path)
    lines[1], lines[2] = lines[2], lines[1]
    write_lines(path, lines)
    assert not verify_log(path).ok


def test_inserting_a_forged_line_breaks_verification(tmp_path: Path):
    path = make_log(tmp_path).path
    lines = read_lines(path)
    forged = json.loads(lines[2])
    forged["tool_call_id"] = "tc-0099"
    lines.insert(3, canonical_json(forged))
    write_lines(path, lines)
    assert not verify_log(path).ok


def test_appending_after_run_end_breaks_verification(tmp_path: Path):
    log = make_log(tmp_path)
    forged = {
        "seq": 5,
        "ts": "x",
        "run_id": log.run_id,
        "event": "tool_call",
        "prev_hash": log.head_hash,
    }
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json({**forged, "hash": "0" * 64}) + "\n")
    result = verify_log(log.path)
    assert not result.ok and "after run_end" in str(result.error)


def test_cutting_off_the_end_is_detected_as_incomplete(tmp_path: Path):
    path = make_log(tmp_path).path
    write_lines(path, read_lines(path)[:-2])
    result = verify_log(path)
    assert result.ok and not result.complete  # the chain is intact, but the run never ended


@pytest.mark.parametrize("junk", ["", "not json", "[1, 2]", '{"seq": 0}'])
def test_garbage_lines_fail_cleanly(tmp_path: Path, junk: str):
    path = make_log(tmp_path).path
    lines = read_lines(path)
    lines[1] = junk
    write_lines(path, lines)
    assert not verify_log(path).ok


def test_empty_and_missing_logs_fail(tmp_path: Path):
    empty = tmp_path / f"{uuid.uuid4()}.jsonl"
    empty.write_text("", encoding="utf-8")
    assert not verify_log(empty).ok
    assert not verify_log(tmp_path / f"{uuid.uuid4()}.jsonl").ok


def test_a_log_copied_under_another_run_id_fails(tmp_path: Path):
    log = make_log(tmp_path)
    other = tmp_path / f"{uuid.uuid4()}.jsonl"
    other.write_text(log.path.read_text(encoding="utf-8"), encoding="utf-8")
    result = verify_log(other)
    assert not result.ok and "different run" in str(result.error)


def test_two_edits_that_recompute_nothing_still_fail(tmp_path: Path):
    path = make_log(tmp_path).path
    edit_line(path, 1, tool="peer_baseline")
    edit_line(path, 3, tool="peer_baseline")
    assert verify_log(path).line == 2  # the first problem is reported


# ---- run ids, creating and resuming ----------


@pytest.mark.parametrize(
    "bad",
    ["", "../x", "..\\x", "not-a-uuid", str(uuid.uuid4()).upper(), "{" + str(uuid.uuid4()) + "}"],
)
def test_run_id_must_be_a_canonical_uuid(bad: str):
    with pytest.raises(ValueError):
        parse_run_id(bad)


def test_log_path_stays_inside_the_logs_dir(tmp_path: Path):
    run_id = str(uuid.uuid4())
    assert log_path(tmp_path, run_id).parent == tmp_path
    with pytest.raises(ValueError):
        log_path(tmp_path, "../../etc/passwd")


def test_create_refuses_an_existing_log(tmp_path: Path):
    log = make_log(tmp_path)
    with pytest.raises(FileExistsError):
        RunLog.create(tmp_path, run_id=log.run_id)


def test_reserved_field_names_cannot_be_overridden(tmp_path: Path):
    log = RunLog.create(tmp_path)
    with pytest.raises(ValueError, match="reserved"):
        log.append("tool_call", hash="forged")


def test_resume_continues_the_same_chain_and_numbering(tmp_path: Path):
    first = make_log(tmp_path, tool_calls=2, finish=False)
    resumed = RunLog.resume(tmp_path, first.run_id)
    assert resumed.next_tool_call_id() == "tc-0003"
    resumed.append("approval", decision="approved")
    resumed.finish("completed")
    result = verify_log(first.path)
    assert result.ok and result.complete and result.records == 5


def test_resume_refuses_a_tampered_or_finished_log(tmp_path: Path):
    finished = make_log(tmp_path)
    with pytest.raises(LogError, match="run_end"):
        RunLog.resume(tmp_path, finished.run_id)
    open_log = make_log(tmp_path, finish=False)
    edit_line(open_log.path, 1, tool="forged")
    with pytest.raises(LogError, match="cannot resume"):
        RunLog.resume(tmp_path, open_log.run_id)


# ---- the command ----------


def test_verify_log_command_ok_and_failed(tmp_path: Path, capsys):
    log = make_log(tmp_path)
    assert main(["verify-log", log.run_id, "--logs-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "OK" in out and "3 tool calls" in out and log.head_hash in out

    edit_line(log.path, 2, tool="forged")
    assert main(["verify-log", log.run_id, "--logs-dir", str(tmp_path)]) == 1
    assert "FAILED  line 3" in capsys.readouterr().out


def test_verify_log_command_rejects_bad_ids_and_missing_logs(tmp_path: Path, capsys):
    assert main(["verify-log", "../x", "--logs-dir", str(tmp_path)]) == 2
    assert main(["verify-log", str(uuid.uuid4()), "--logs-dir", str(tmp_path)]) == 2
    capsys.readouterr()


def test_verify_log_command_flags_an_incomplete_run(tmp_path: Path, capsys):
    log = make_log(tmp_path, finish=False)
    assert main(["verify-log", log.run_id, "--logs-dir", str(tmp_path)]) == 0
    assert "INCOMPLETE" in capsys.readouterr().out
