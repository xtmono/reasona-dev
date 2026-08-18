import json

from reasona_dev import cycles_log
from reasona_dev.finding_adapter import Disposition, Finding, ReviewResult, RoleStatus, Severity


def _result(*findings) -> ReviewResult:
    return ReviewResult(role_status=RoleStatus.COMPLETE, findings=list(findings))


def _finding(path="src/a.rs", symbol="foo", contract="c", disposition=Disposition.MUST_FIX):
    return Finding(
        disposition=disposition, severity=Severity.HIGH, path=path, line=10,
        symbol=symbol, contract=contract, scenario="s", fix="f",
    )


def test_dispatch_row_carries_finding_keys_and_counts(tmp_path):
    cycles_log.record_dispatch(
        workdir=tmp_path, stage_name="pr-1", stage="review", cycle=1,
        role="reviewer", model="opus", adapter="claude",
        result=_result(_finding(), _finding(path="src/b.rs", disposition=Disposition.ADVISORY)),
    )

    rows = cycles_log.read_records(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["stage"] == "review"
    assert row["role"] == "reviewer"
    assert row["model"] == "opus"
    assert row["gate"] == "FIX_REQUIRED"
    assert row["must_fix_count"] == 1
    assert row["advisory_count"] == 1
    # The key is what makes a finding followable across cycles and roles --
    # this is the join column every attribution query depends on.
    assert all(f["key"] for f in row["findings"])
    assert {f["path"] for f in row["findings"]} == {"src/a.rs", "src/b.rs"}


def test_finding_key_is_stable_across_line_shifts(tmp_path):
    """A fix that shifts line numbers must not make the same finding look
    new -- otherwise recurrence attribution silently under-counts."""
    a = _finding()
    b = _finding()
    b.line = 998
    cycles_log.record_dispatch(
        workdir=tmp_path, stage_name="pr-1", stage="review", cycle=1,
        role="reviewer", model="opus", adapter="claude", result=_result(a),
    )
    cycles_log.record_dispatch(
        workdir=tmp_path, stage_name="pr-1", stage="review", cycle=2,
        role="reviewer", model="opus", adapter="claude", result=_result(b),
    )
    rows = cycles_log.read_records(tmp_path)
    assert rows[0]["findings"][0]["key"] == rows[1]["findings"][0]["key"]
    assert rows[0]["findings"][0]["line"] != rows[1]["findings"][0]["line"]


def test_decision_row_records_which_rule_ended_the_cycle(tmp_path):
    cycles_log.record_decision(
        workdir=tmp_path, stage_name="pr-1", stage="review", cycle=3,
        action="fail", reason="not converging", escalated_model=None,
    )
    row = cycles_log.read_records(tmp_path)[0]
    assert row["kind"] == "decision"
    assert row["action"] == "fail"
    assert row["reason"] == "not converging"


def test_logging_never_raises_on_unwritable_path(tmp_path):
    """Instrumentation must not be able to fail a PR cycle."""
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory")
    cycles_log.record_dispatch(
        workdir=blocked, stage_name="pr-1", stage="review", cycle=1,
        role="reviewer", model="opus", adapter="claude", result=_result(),
    )
    assert cycles_log.read_records(blocked) == []


def test_malformed_trailing_line_does_not_poison_history(tmp_path):
    cycles_log.record_dispatch(
        workdir=tmp_path, stage_name="pr-1", stage="review", cycle=1,
        role="reviewer", model="opus", adapter="claude", result=_result(),
    )
    with cycles_log.cycles_path(tmp_path).open("a") as f:
        f.write('{"partial": tru')  # killed mid-write
    rows = cycles_log.read_records(tmp_path)
    assert len(rows) == 1
    assert rows[0]["role"] == "reviewer"


def test_records_are_one_json_object_per_line(tmp_path):
    for c in (1, 2):
        cycles_log.record_dispatch(
            workdir=tmp_path, stage_name="pr-1", stage="scan", cycle=c,
            role="bugbot", model="deepseek-v4-pro", adapter="kilo", result=_result(),
        )
    lines = cycles_log.cycles_path(tmp_path).read_text().strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # each line independently parseable
