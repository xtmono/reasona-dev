import subprocess
from pathlib import Path

import pytest

from reasona_dev import merge_tail
from reasona_dev.cycle_gate import FixBudget, RecurrenceTracker
from reasona_dev.finding_adapter import ReviewResult, RoleStatus, parse_text_contract
from reasona_dev.merge_tail import (
    BLOCKED,
    MERGED,
    PR_OPEN,
    build_squash_message,
    is_up_to_date,
    run_merge_tail,
    should_run_final_audit,
    sync_main,
)
from reasona_dev.model_config import ResolvedModel
from reasona_dev.pr_cycle import RoleRunResult
from reasona_dev.ship_gate import GateOutcome, ShipDecision

_RESOLVED = {
    "dev": ResolvedModel("dev", "sonnet", "claude", "high", "d"),
    "review": ResolvedModel("review", "opus", "claude", "high", "d"),
    "recheck": ResolvedModel("recheck", "sonnet", "claude", "medium", "d"),
    "bugbot": ResolvedModel("bugbot", "x", "kilo", "high", "d"),
    "verify": ResolvedModel("verify", "sonnet", "claude", "high", "d"),
    "final_audit": ResolvedModel("final_audit", "opus", "claude", "high", "d"),
    "dev_escalation": ResolvedModel("dev_escalation", "opus", "claude", "high", "d"),
}

PASS_TEXT = "VERDICT: PASS\n"
MUST_FIX_TEXT = (
    "MUST_FIX:\n- [HIGH] src/a.py:1 f\n  || contract: c\n  || scenario: s\n  || fix: x\n\nVERDICT: FAIL\n"
)


def _pass_ship():
    return ShipDecision("pr-1", True, [GateOutcome("review", True, "PASS")])


def _fail_ship():
    return ShipDecision("pr-1", False, [GateOutcome("acceptance", False, "AC-1 failed")])


# --- squash message ---------------------------------------------------------

def test_message_is_built_and_independently_guarded():
    msg, reason = build_squash_message(unit_type="feat", title="add subtract()")
    assert msg.title == "feat: add subtract()"
    assert reason == "ok"


def test_a_title_violation_blocks_rather_than_being_repaired():
    """`guard` re-derives validity without consulting `build`; a disagreement
    is a defect in the pair, never something to hand-edit around."""
    msg, reason = build_squash_message(unit_type="feat", title="add subtract() #42")
    assert msg is None
    assert "rejected by its own guard" in reason


def test_a_body_only_violation_merges_with_the_title_alone():
    msg, reason = build_squash_message(
        unit_type="fix", title="repair parser",
        body_lines=["Co-authored-by: someone <x@y>"],
    )
    assert msg is not None and msg.body == ""
    assert "body dropped" in reason


# --- final audit trigger ----------------------------------------------------

def test_a_clean_first_pass_does_not_earn_a_final_audit():
    """Three roles already read it and found nothing; a whole-PR audit there
    mostly re-derives that."""
    assert should_run_final_audit(FixBudget()) is False


def test_any_fix_cycle_triggers_the_audit():
    """Each fix is a change no reviewer saw in its final combined form."""
    b = FixBudget()
    b.spend("review")
    assert should_run_final_audit(b) is True


def test_audit_is_skipped_when_the_profile_defines_no_prompt(tmp_path):
    """Skipping an UNDEFINED audit is the profile's decision; skipping a
    requested one would be a silent gap."""
    passed, reason, dispatches = merge_tail.run_final_audit(
        server=None, workdir=tmp_path, stage_name="pr-1", pr_title="t",
        profile="nonexistent", resolved=_RESOLVED, rundir=tmp_path / "r",
        budget=FixBudget(), recurrence=RecurrenceTracker(),
        run_role_fn=lambda **kw: pytest.fail("must not dispatch"),
    )
    assert passed is True and dispatches == []
    assert "no final_audit prompt" in reason


def test_audit_runs_on_the_final_audit_model(tmp_path, generic_prompts):
    seen = {}

    def _fn(*, server, workdir, role, title, prompt, model, rundir, cycle, approval_required=False):
        seen["model"] = model.model
        seen["role"] = role
        return RoleRunResult(role=role, cycle=cycle,
                             review_result=parse_text_contract(PASS_TEXT),
                             raw_output_path=Path("/dev/null"))

    passed, _, _ = merge_tail.run_final_audit(
        server=None, workdir=tmp_path, stage_name="pr-1", pr_title="t",
        profile="generic", resolved=_RESOLVED, rundir=tmp_path / "r",
        budget=FixBudget(), recurrence=RecurrenceTracker(), run_role_fn=_fn,
    )
    assert passed
    assert seen["model"] == "opus"  # resolved["final_audit"]


def test_audit_findings_spend_the_final_stage_budget(tmp_path, generic_prompts):
    """The `final` stage had no producer until now; MAX_FINAL_CYCLES bounds
    the audit's own fix loop."""
    budget = FixBudget()

    def _fn(*, server, workdir, role, title, prompt, model, rundir, cycle, approval_required=False):
        result = (
            parse_text_contract(MUST_FIX_TEXT) if role == "compliance"
            else ReviewResult(role_status=RoleStatus.COMPLETE)
        )
        return RoleRunResult(role=role, cycle=cycle, review_result=result,
                             raw_output_path=Path("/dev/null"))

    passed, reason, _ = merge_tail.run_final_audit(
        server=None, workdir=tmp_path, stage_name="pr-1", pr_title="t",
        profile="generic", resolved=_RESOLVED, rundir=tmp_path / "r",
        budget=budget, recurrence=RecurrenceTracker(), run_role_fn=_fn,
    )
    assert passed is False
    assert budget.final_cycles > 0
    assert budget.review_cycles == 0  # the audit spends its OWN stage


# --- composition ------------------------------------------------------------

def _stub(monkeypatch, *, gh=None, sync=(True, "ok"), up=(True, "ok"),
          pr=("https://gh/pr/1", "PR created"), merged=(True, "squash-merged")):
    monkeypatch.setattr(merge_tail, "gh_available", lambda w: gh)
    monkeypatch.setattr(merge_tail, "sync_main", lambda w, *, base: sync)
    monkeypatch.setattr(merge_tail, "is_up_to_date", lambda w, *, base: up)
    monkeypatch.setattr(merge_tail, "create_pr", lambda w, m: pr)
    monkeypatch.setattr(merge_tail, "squash_merge", lambda w, m: merged)


def _tail(tmp_path, decision=None, **kw):
    return run_merge_tail(
        server=None, workdir=tmp_path, stage_name="pr-1", pr_title="add subtract()",
        unit_type="feat", profile="generic", resolved=_RESOLVED, rundir=tmp_path / "r",
        ship_decision=decision or _pass_ship(), budget=FixBudget(),
        recurrence=RecurrenceTracker(), **kw,
    )


def test_a_failing_ship_gate_is_refused_before_anything_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(merge_tail, "gh_available", lambda w: pytest.fail("must not reach gh"))
    r = _tail(tmp_path, decision=_fail_ship())
    assert r.status == BLOCKED and "ship gate did not pass" in r.reason


def test_missing_gh_blocks_before_an_audit_is_spent(tmp_path, monkeypatch):
    _stub(monkeypatch, gh="gh CLI is not on PATH")
    monkeypatch.setattr(merge_tail, "sync_main", lambda w, *, base: pytest.fail("must not sync"))
    r = _tail(tmp_path)
    assert r.status == BLOCKED and "not on PATH" in r.reason


def test_a_sync_conflict_blocks(tmp_path, monkeypatch):
    _stub(monkeypatch, sync=(False, "merge conflict with origin/main in: src/a.py"))
    r = _tail(tmp_path)
    assert r.status == BLOCKED and "merge conflict" in r.reason


def test_merge_is_opt_in_and_stops_at_the_open_pr(tmp_path, monkeypatch):
    """A squash-merge rewrites a real default branch; the caller has to ask."""
    _stub(monkeypatch)
    monkeypatch.setattr(merge_tail, "squash_merge", lambda w, m: pytest.fail("must not merge"))
    r = _tail(tmp_path)
    assert r.status == PR_OPEN
    assert r.pr_url == "https://gh/pr/1"
    assert "merge not requested" in r.reason


def test_merge_true_squash_merges(tmp_path, monkeypatch):
    _stub(monkeypatch)
    r = _tail(tmp_path, merge=True)
    assert r.status == MERGED and r.squash_message.title == "feat: add subtract()"


def test_up_to_date_is_rechecked_immediately_before_merging(tmp_path, monkeypatch):
    """Base can advance between sync and merge; merging a PR that no longer
    contains its base is how a green PR lands red."""
    _stub(monkeypatch, up=(False, "branch is behind origin/main -- re-run sync"))
    r = _tail(tmp_path, merge=True)
    assert r.status == BLOCKED and "behind origin/main" in r.reason


def test_a_failed_merge_call_blocks_rather_than_reporting_success(tmp_path, monkeypatch):
    _stub(monkeypatch, merged=(False, "gh pr merge failed: not mergeable"))
    r = _tail(tmp_path, merge=True)
    assert r.status == BLOCKED and "not mergeable" in r.reason


def test_a_failing_audit_blocks_before_a_pr_is_created(tmp_path, monkeypatch, generic_prompts):
    _stub(monkeypatch)
    monkeypatch.setattr(merge_tail, "create_pr", lambda w, m: pytest.fail("must not create PR"))
    budget = FixBudget()
    budget.spend("review")  # earns an audit

    def _fn(*, server, workdir, role, title, prompt, model, rundir, cycle, approval_required=False):
        return RoleRunResult(role=role, cycle=cycle,
                             review_result=ReviewResult(role_status=RoleStatus.ERROR),
                             raw_output_path=Path("/dev/null"))

    r = run_merge_tail(
        server=None, workdir=tmp_path, stage_name="pr-1", pr_title="add subtract()",
        unit_type="feat", profile="generic", resolved=_RESOLVED, rundir=tmp_path / "r",
        ship_decision=_pass_ship(), budget=budget, recurrence=RecurrenceTracker(),
        merge=True, run_role_fn=_fn,
    )
    assert r.status == BLOCKED and "final audit" in r.reason


# --- git helpers against a real repo ----------------------------------------

def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_up_to_date_against_a_real_repo(tmp_path):
    repo = _repo(tmp_path)
    ok, reason = is_up_to_date(repo, base="HEAD")
    assert ok is True and "contains base" in reason


def test_up_to_date_reports_a_missing_base_as_a_comparison_failure(tmp_path):
    repo = _repo(tmp_path)
    ok, reason = is_up_to_date(repo, base="origin/nonexistent")
    assert ok is False and "could not compare" in reason


def test_sync_reports_a_missing_remote_rather_than_pretending(tmp_path):
    repo = _repo(tmp_path)
    ok, reason = sync_main(repo, base="origin/main")
    assert ok is False and "fetch" in reason
