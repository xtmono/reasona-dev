import json
import subprocess

from reasona_dev import cycles_log, ship_gate
from reasona_dev.plan_compile import acceptance_path


def _repo(tmp_path, *, files=None, cfg=None):
    for rel, content in (files or {"a.rs": "x\n"}).items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    if cfg:
        (tmp_path / ".reasona").mkdir(exist_ok=True)
        (tmp_path / ".reasona" / "reasona.yaml").write_text(cfg)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp_path, check=True,
    )
    return tmp_path


def _write_criteria(workdir, stage, criteria):
    path = acceptance_path(workdir, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stage_name": stage, "criteria": criteria}))


def test_both_clean_passes(tmp_path):
    repo = _repo(tmp_path)
    _write_criteria(repo, "pr-1", [{"id": "AC-1", "cmd": "true", "expect": "exit0"}])
    d = ship_gate.evaluate(repo, "pr-1", cycle_verdict="PASS")
    assert d.passed
    assert [o.name for o in d.outcomes] == ["review", "acceptance"]


def test_failing_review_fails_the_gate(tmp_path):
    repo = _repo(tmp_path)
    _write_criteria(repo, "pr-1", [{"id": "AC-1", "cmd": "true", "expect": "exit0"}])
    d = ship_gate.evaluate(repo, "pr-1", cycle_verdict="FAIL")
    assert not d.passed
    assert [o.name for o in d.failures] == ["review"]


def test_failing_acceptance_fails_even_when_review_passed(tmp_path):
    """Conjunction with no weighting -- a thorough review does not excuse a
    criterion the plan itself declared and that does not hold."""
    repo = _repo(tmp_path)
    _write_criteria(repo, "pr-1", [{"id": "AC-1", "cmd": "false", "expect": "exit0"}])
    d = ship_gate.evaluate(repo, "pr-1", cycle_verdict="PASS")
    assert not d.passed
    assert [o.name for o in d.failures] == ["acceptance"]
    assert "AC-1" in d.reason


def test_undeclared_criteria_pass_with_a_warning(tmp_path):
    repo = _repo(tmp_path)
    d = ship_gate.evaluate(repo, "pr-1", cycle_verdict="PASS")
    assert d.passed
    ac = [o for o in d.outcomes if o.name == "acceptance"][0]
    assert ac.warning is not None


def test_absent_cycle_verdict_is_reported_as_skipped_not_passed(tmp_path):
    """Silently treating "not asserted" as "passed" is how a gate stops
    meaning anything."""
    repo = _repo(tmp_path)
    d = ship_gate.evaluate(repo, "pr-1")
    review = [o for o in d.outcomes if o.name == "review"][0]
    assert "skipped" in review.detail


def test_verdict_and_acceptance_outcome_are_recorded(tmp_path):
    repo = _repo(tmp_path)
    _write_criteria(repo, "pr-1", [{"id": "AC-1", "cmd": "false", "expect": "exit0"}])
    ship_gate.evaluate(repo, "pr-1", cycle_verdict="PASS")

    rows = cycles_log.read_records(repo)
    ship = [r for r in rows if r.get("kind") == "ship"][0]
    accept = [r for r in rows if r.get("kind") == "acceptance"][0]
    assert ship["passed"] is False
    assert ship["gates"] == {"review": True, "acceptance": False}
    assert accept["declared"] is True
    assert accept["criteria"][0]["id"] == "AC-1"


def test_undeclared_criteria_are_still_recorded(tmp_path):
    """Coverage is what decides when this becomes a refusal -- a row never
    written cannot be counted."""
    repo = _repo(tmp_path)
    ship_gate.evaluate(repo, "pr-1", cycle_verdict="PASS")
    accept = [r for r in cycles_log.read_records(repo) if r.get("kind") == "acceptance"][0]
    assert accept["declared"] is False


def test_no_record_suppresses_logging(tmp_path):
    repo = _repo(tmp_path)
    ship_gate.evaluate(repo, "pr-1", cycle_verdict="PASS", record=False)
    assert cycles_log.read_records(repo) == []


def test_cli_exit_codes(tmp_path):
    ok = _repo(tmp_path / "ok")
    _write_criteria(ok, "pr-1", [{"id": "AC-1", "cmd": "true", "expect": "exit0"}])
    assert ship_gate.main(["pr-1", str(ok)]) == 0

    bad = _repo(tmp_path / "bad")
    _write_criteria(bad, "pr-1", [{"id": "AC-1", "cmd": "false", "expect": "exit0"}])
    assert ship_gate.main(["pr-1", str(bad)]) == 1


def test_both_report_even_after_one_fails(tmp_path):
    """The author should see everything wrong in one round."""
    repo = _repo(tmp_path)
    _write_criteria(repo, "pr-1", [{"id": "AC-1", "cmd": "false", "expect": "exit0"}])
    d = ship_gate.evaluate(repo, "pr-1", cycle_verdict="FAIL")
    assert len(d.outcomes) == 2
    assert {o.name for o in d.failures} == {"review", "acceptance"}
