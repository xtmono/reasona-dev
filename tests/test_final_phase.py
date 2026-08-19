import subprocess
from pathlib import Path

import pytest

from reasona_dev import final_phase
from reasona_dev.cycle_gate import (
    MAX_FINAL_PHASE_ROUNDS,
    MAX_SHIP_CYCLES,
    MAX_SYNC_CYCLES,
    FixBudget,
    RecurrenceTracker,
)
from reasona_dev.finding_adapter import ReviewResult, RoleStatus, parse_text_contract
from reasona_dev.final_phase import (
    BLOCKED,
    MERGED,
    PR_OPEN,
    build_squash_message,
    is_up_to_date,
    run_final_stage,
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
    "compliance": ResolvedModel("compliance", "sonnet", "claude", "high", "d"),
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


def test_a_trailer_only_body_is_cleaned_by_the_builder_not_the_guard():
    # squash.build() strips a trailer line outright (dev-ralf squash_build.py
    # clean_body step 5), so guard() finds nothing left to object to -- the
    # body ends up empty either way, but via the builder doing its job, not
    # via the TITLE_ONLY fallback below.
    msg, reason = build_squash_message(
        unit_type="fix", title="repair parser",
        body_lines=["Co-authored-by: someone <x@y>"],
    )
    assert msg is not None and msg.body == ""
    assert reason == "ok"


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
    passed, reason, dispatches = final_phase.run_final_audit(
        workdir=tmp_path, stage_name="pr-1", pr_title="t",
        profile="nonexistent", resolved=_RESOLVED, rundir=tmp_path / "r",
        budget=FixBudget(), recurrence=RecurrenceTracker(),
        run_role_fn=lambda **kw: pytest.fail("must not dispatch"),
    )
    assert passed is True and dispatches == []
    assert "no final_audit prompt" in reason


def test_audit_runs_on_the_final_audit_model(tmp_path, generic_prompts):
    seen = {}

    def _fn(*, workdir, role, title, prompt, model, rundir, cycle):
        seen["model"] = model.model
        seen["role"] = role
        return RoleRunResult(role=role, cycle=cycle,
                             review_result=parse_text_contract(PASS_TEXT),
                             raw_output_path=Path("/dev/null"))

    passed, _, _ = final_phase.run_final_audit(
        workdir=tmp_path, stage_name="pr-1", pr_title="t",
        profile="generic", resolved=_RESOLVED, rundir=tmp_path / "r",
        budget=FixBudget(), recurrence=RecurrenceTracker(), run_role_fn=_fn,
    )
    assert passed
    assert seen["model"] == "opus"  # resolved["final_audit"]


def test_audit_findings_spend_the_final_stage_budget(tmp_path, generic_prompts):
    """The `final` stage had no producer until now; MAX_FINAL_CYCLES bounds
    the audit's own fix loop."""
    budget = FixBudget()

    def _fn(*, workdir, role, title, prompt, model, rundir, cycle):
        result = (
            parse_text_contract(MUST_FIX_TEXT) if role == "compliance"
            else ReviewResult(role_status=RoleStatus.COMPLETE)
        )
        return RoleRunResult(role=role, cycle=cycle, review_result=result,
                             raw_output_path=Path("/dev/null"))

    passed, reason, _ = final_phase.run_final_audit(
        workdir=tmp_path, stage_name="pr-1", pr_title="t",
        profile="generic", resolved=_RESOLVED, rundir=tmp_path / "r",
        budget=budget, recurrence=RecurrenceTracker(), run_role_fn=_fn,
    )
    assert passed is False
    assert budget.final_cycles > 0
    assert budget.review_cycles == 0  # the audit spends its OWN stage


# --- composition ------------------------------------------------------------

def _stub(monkeypatch, *, gh=None, sync=(True, "ok"), up=(True, "ok"),
          pr=("https://gh/pr/1", "PR created"), merged=(True, "squash-merged")):
    monkeypatch.setattr(final_phase, "gh_available", lambda w: gh)
    monkeypatch.setattr(final_phase, "sync_main", lambda w, *, base: sync)
    monkeypatch.setattr(final_phase, "is_up_to_date", lambda w, *, base: up)
    monkeypatch.setattr(final_phase, "create_pr", lambda w, m, **kw: pr)
    monkeypatch.setattr(final_phase, "squash_merge", lambda w, m: merged)


def _ship_fn(*, passed=True, reason="ok"):
    outcome = GateOutcome("review", passed, reason)

    def fn(workdir, stage_name, *, cycle_verdict):
        return ShipDecision(stage_name=stage_name, passed=passed, outcomes=[outcome])

    return fn


def _tail(tmp_path, ship_gate_fn=None, **kw):
    return run_final_stage(
        workdir=tmp_path, stage_name="pr-1", pr_title="add subtract()",
        unit_type="feat", profile="generic", resolved=_RESOLVED, rundir=tmp_path / "r",
        cycle_verdict="PASS", ship_gate_fn=ship_gate_fn or _ship_fn(),
        budget=FixBudget(), recurrence=RecurrenceTracker(), **kw,
    )


def test_a_failing_ship_gate_dispatches_a_bounded_fix_before_blocking(tmp_path, monkeypatch):
    """ship_gate now runs only after sync/final_audit have settled (see
    final_phase.run_final_phase()), so a failing verdict is discovered AFTER
    those steps, not instead of reaching them -- and, unlike the old
    immediate-terminal behaviour, it gets its own bounded dev-fix attempts
    (`run_ship_cycle()`, `MAX_SHIP_CYCLES`) before the unit is reported
    `blocked`, exactly like every other check in this pipeline."""
    _stub(monkeypatch)
    dispatched = []

    def _fn(*, workdir, role, title, prompt, model, rundir, cycle):
        dispatched.append(cycle)
        return RoleRunResult(role=role, cycle=cycle,
                             review_result=ReviewResult(role_status=RoleStatus.COMPLETE),
                             raw_output_path=Path("/dev/null"))

    r = _tail(
        tmp_path, ship_gate_fn=_ship_fn(passed=False, reason="AC-1 failed"), run_role_fn=_fn,
    )
    assert r.status == BLOCKED and "ship gate did not pass" in r.reason
    assert dispatched == list(range(1, MAX_SHIP_CYCLES + 1))


def test_run_ship_cycle_stops_dispatching_once_ship_gate_passes(tmp_path):
    """A dev fix that actually resolves the failing criterion must not
    burn the rest of the `ship` budget -- `run_ship_cycle()` re-checks
    `ship_gate_fn` after every fix and returns as soon as it passes."""
    calls = {"n": 0}

    def flaky_ship_gate(workdir, stage_name, *, cycle_verdict):
        calls["n"] += 1
        return _ship_fn(passed=calls["n"] > 1, reason="AC-1 failed")(
            workdir, stage_name, cycle_verdict=cycle_verdict
        )

    budget = FixBudget()

    def _fn(*, workdir, role, title, prompt, model, rundir, cycle):
        return RoleRunResult(role=role, cycle=cycle,
                             review_result=ReviewResult(role_status=RoleStatus.COMPLETE),
                             raw_output_path=Path("/dev/null"))

    decision, changed, dispatches = final_phase.run_ship_cycle(
        workdir=tmp_path, stage_name="pr-1", pr_title="t", resolved=_RESOLVED,
        rundir=tmp_path / "r", budget=budget, cycle_verdict="PASS",
        ship_gate_fn=flaky_ship_gate, run_role_fn=_fn,
    )
    assert decision.passed and changed is True
    assert len(dispatches) == 1 and budget.ship_cycles == 1


def test_missing_gh_blocks_before_an_audit_is_spent(tmp_path, monkeypatch):
    _stub(monkeypatch, gh="gh CLI is not on PATH")
    monkeypatch.setattr(final_phase, "sync_main", lambda w, *, base: pytest.fail("must not sync"))
    r = _tail(tmp_path)
    assert r.status == BLOCKED and "not on PATH" in r.reason


def test_a_non_conflict_sync_failure_blocks(tmp_path, monkeypatch):
    """A fetch failure (no conflicting paths to point dev at) is not
    something a dev-fix can resolve -- unlike a real conflict, it still
    blocks immediately rather than entering the sync fix loop."""
    _stub(monkeypatch, sync=(False, "git fetch origin failed: could not resolve host"))
    r = _tail(tmp_path)
    assert r.status == BLOCKED and "could not resolve host" in r.reason


def test_merge_is_opt_in_and_stops_at_the_open_pr(tmp_path, monkeypatch):
    """A squash-merge rewrites a real default branch; the caller has to ask."""
    _stub(monkeypatch)
    monkeypatch.setattr(final_phase, "squash_merge", lambda w, m: pytest.fail("must not merge"))
    r = _tail(tmp_path)
    assert r.status == PR_OPEN
    assert r.pr_url == "https://gh/pr/1"
    assert "merge not requested" in r.reason


def test_merge_true_squash_merges(tmp_path, monkeypatch):
    _stub(monkeypatch)
    r = _tail(tmp_path, merge=True)
    assert r.status == MERGED and r.squash_message.title == "feat: add subtract()"


def test_up_to_date_is_rechecked_immediately_before_merging(tmp_path, monkeypatch):
    """Base can advance between the final phase settling and the actual
    merge call; merging a PR that no longer contains its base is how a
    green PR lands red."""
    _stub(monkeypatch, up=(False, "branch is behind origin/main -- re-run sync"))
    r = _tail(tmp_path, merge=True)
    assert r.status == BLOCKED and "behind origin/main" in r.reason


def test_a_failed_merge_call_blocks_rather_than_reporting_success(tmp_path, monkeypatch):
    _stub(monkeypatch, merged=(False, "gh pr merge failed: not mergeable"))
    r = _tail(tmp_path, merge=True)
    assert r.status == BLOCKED and "not mergeable" in r.reason


def test_a_failing_audit_blocks_before_a_pr_is_created(tmp_path, monkeypatch, generic_prompts):
    _stub(monkeypatch)
    monkeypatch.setattr(final_phase, "create_pr", lambda w, m, **kw: pytest.fail("must not create PR"))
    budget = FixBudget()
    budget.spend("review")  # earns an audit

    def _fn(*, workdir, role, title, prompt, model, rundir, cycle):
        return RoleRunResult(role=role, cycle=cycle,
                             review_result=ReviewResult(role_status=RoleStatus.ERROR),
                             raw_output_path=Path("/dev/null"))

    r = run_final_stage(
        workdir=tmp_path, stage_name="pr-1", pr_title="add subtract()",
        unit_type="feat", profile="generic", resolved=_RESOLVED, rundir=tmp_path / "r",
        cycle_verdict="PASS", ship_gate_fn=_ship_fn(), budget=budget, recurrence=RecurrenceTracker(),
        merge=True, run_role_fn=_fn,
    )
    assert r.status == BLOCKED and "final audit" in r.reason


# --- run_final_phase: the sync -> final_audit -> ship_gate round loop -------

def test_final_phase_reruns_from_sync_when_a_round_changed_something(tmp_path, monkeypatch):
    """A round in which sync had to resolve a conflict means that round's
    ship_gate verdict already covers code final_audit never saw -- the
    whole sequence must run again until a round changes nothing."""
    calls = {"sync": 0, "ship": 0}

    def fake_sync(**kw):
        calls["sync"] += 1
        return "ok", "ok", [], calls["sync"] == 1  # only round 1 "changes" anything

    def ship_fn(workdir, stage_name, *, cycle_verdict):
        calls["ship"] += 1
        return _ship_fn()(workdir, stage_name, cycle_verdict=cycle_verdict)

    monkeypatch.setattr(final_phase, "run_sync_cycle", fake_sync)
    monkeypatch.setattr(final_phase, "should_run_final_audit", lambda budget: False)

    decision, status, dispatches, reason = final_phase.run_final_phase(
        workdir=tmp_path, stage_name="pr-1", pr_title="t", profile="generic",
        resolved=_RESOLVED, rundir=tmp_path / "r", budget=FixBudget(),
        recurrence=RecurrenceTracker(), cycle_verdict="PASS", ship_gate_fn=ship_fn,
    )
    assert status == "passed" and decision.passed
    assert calls == {"sync": 2, "ship": 2}


def test_final_phase_gives_up_if_it_never_settles(tmp_path, monkeypatch):
    monkeypatch.setattr(final_phase, "run_sync_cycle", lambda **kw: ("ok", "ok", [], True))
    monkeypatch.setattr(final_phase, "should_run_final_audit", lambda budget: False)
    ship_calls = []

    def ship_fn(workdir, stage_name, *, cycle_verdict):
        ship_calls.append(1)
        return _ship_fn()(workdir, stage_name, cycle_verdict=cycle_verdict)

    decision, status, dispatches, reason = final_phase.run_final_phase(
        workdir=tmp_path, stage_name="pr-1", pr_title="t", profile="generic",
        resolved=_RESOLVED, rundir=tmp_path / "r", budget=FixBudget(),
        recurrence=RecurrenceTracker(), cycle_verdict="PASS", ship_gate_fn=ship_fn,
    )
    assert status == "blocked" and "did not converge" in reason
    assert len(ship_calls) == MAX_FINAL_PHASE_ROUNDS


# --- git helpers against a real repo ----------------------------------------

def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo_with_conflicting_origin(tmp_path):
    """A work repo whose `origin/main` and local `main` both changed the
    same line of the same file differently -- a real, unresolved merge
    conflict for `run_sync_cycle` to encounter."""
    seed, origin, work, other = (tmp_path / n for n in ("seed", "origin.git", "work", "other"))
    seed.mkdir()
    _git(["init", "-q", "-b", "main"], seed)
    (seed / "a.txt").write_text("base\n")
    _git(["add", "-A"], seed)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], seed)
    _git(["clone", "-q", "--bare", str(seed), str(origin)], tmp_path)

    # `work` clones and diverges FIRST, before origin/main moves --
    # otherwise its clone would already contain `other`'s change and the
    # two histories would never actually conflict on fetch.
    _git(["clone", "-q", str(origin), str(work)], tmp_path)
    _git(["-C", str(work), "config", "user.email", "t@t"], tmp_path)
    _git(["-C", str(work), "config", "user.name", "t"], tmp_path)
    (work / "a.txt").write_text("from work\n")
    _git(["-C", str(work), "commit", "-qam", "work change"], tmp_path)

    _git(["clone", "-q", str(origin), str(other)], tmp_path)
    _git(["-C", str(other), "config", "user.email", "t@t"], tmp_path)
    _git(["-C", str(other), "config", "user.name", "t"], tmp_path)
    (other / "a.txt").write_text("from origin\n")
    _git(["-C", str(other), "commit", "-qam", "origin change"], tmp_path)
    _git(["-C", str(other), "push", "-q", "origin", "main"], tmp_path)
    return work


def test_sync_cycle_ok_without_dispatch_when_already_in_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(final_phase, "sync_main", lambda w, *, base: (True, "up to date with base"))
    budget = FixBudget()
    status, reason, dispatches, changed = final_phase.run_sync_cycle(
        workdir=tmp_path, pr_title="t", resolved=_RESOLVED, rundir=tmp_path / "r",
        budget=budget, run_role_fn=lambda **kw: pytest.fail("must not dispatch dev"),
    )
    assert status == "ok" and dispatches == [] and changed is False and budget.sync_cycles == 0


def test_sync_cycle_resolves_a_real_conflict_via_dev(tmp_path):
    work = _repo_with_conflicting_origin(tmp_path)
    budget = FixBudget()

    def _resolve(*, workdir, role, title, prompt, model, rundir, cycle):
        (workdir / "a.txt").write_text("resolved\n")
        _git(["add", "a.txt"], workdir)
        _git(["commit", "-q", "--no-edit"], workdir)
        return RoleRunResult(role=role, cycle=cycle,
                             review_result=ReviewResult(role_status=RoleStatus.COMPLETE),
                             raw_output_path=Path("/dev/null"))

    status, reason, dispatches, changed = final_phase.run_sync_cycle(
        workdir=work, pr_title="t", resolved=_RESOLVED, rundir=tmp_path / "r",
        budget=budget, run_role_fn=_resolve,
    )
    assert status == "ok" and changed is True
    assert budget.sync_cycles == 1
    assert len(dispatches) == 1
    assert (work / "a.txt").read_text() == "resolved\n"


def test_sync_cycle_gives_up_after_its_budget_is_exhausted(tmp_path):
    work = _repo_with_conflicting_origin(tmp_path)
    budget = FixBudget()

    def _never_resolve(*, workdir, role, title, prompt, model, rundir, cycle):
        return RoleRunResult(role=role, cycle=cycle,
                             review_result=ReviewResult(role_status=RoleStatus.COMPLETE),
                             raw_output_path=Path("/dev/null"))

    status, reason, dispatches, changed = final_phase.run_sync_cycle(
        workdir=work, pr_title="t", resolved=_RESOLVED, rundir=tmp_path / "r",
        budget=budget, run_role_fn=_never_resolve,
    )
    assert status == "blocked"
    assert budget.sync_cycles == MAX_SYNC_CYCLES
    assert len(dispatches) == MAX_SYNC_CYCLES
    # never left mid-merge -- the last cleanup abort must have run
    code = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        cwd=work, capture_output=True,
    ).returncode
    assert code != 0


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
