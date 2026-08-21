from reasona_dev.cycle_gate import ConvergenceTracker, FixBudget, RecurrenceTracker, evaluate
from reasona_dev.finding_adapter import Disposition, Finding, ReviewResult, RoleStatus, Severity


def _must_fix(path: str, contract: str) -> Finding:
    return Finding(
        disposition=Disposition.MUST_FIX, severity=Severity.HIGH, path=path,
        line=1, symbol="f", contract=contract, scenario="s", fix="x",
    )


def _result(n: int, *, cycle: int) -> ReviewResult:
    """n MUST_FIX findings whose keys are unique to this cycle -- the case
    RecurrenceTracker structurally cannot terminate."""
    return ReviewResult(
        role_status=RoleStatus.COMPLETE,
        findings=[_must_fix(f"src/c{cycle}_{i}.rs", f"contract-{cycle}-{i}") for i in range(n)],
    )


def test_window_not_reached_never_diverges():
    t = ConvergenceTracker()
    t.record(5)
    t.record(4)
    assert t.diverging(window=3) is False


def test_steady_reduction_is_not_diverging():
    t = ConvergenceTracker()
    for n in (5, 4, 3):
        t.record(n)
    assert t.diverging(window=3) is False


def test_no_net_reduction_over_window_diverges():
    t = ConvergenceTracker()
    for n in (3, 2, 3):
        t.record(n)
    assert t.diverging(window=3) is True


def test_oscillation_reads_as_no_progress():
    """3 -> 5 -> 3 is a PR trading one defect for another, not converging."""
    t = ConvergenceTracker()
    for n in (3, 5, 3):
        t.record(n)
    assert t.diverging(window=3) is True


def test_fresh_keys_every_cycle_now_terminate_at_the_window():
    """The hole this closes: every cycle produces DIFFERENT finding keys, so
    RecurrenceTracker returns PROCEED forever and the stage used to run to
    its full 8-cycle cap."""
    budget = FixBudget()
    recurrence = RecurrenceTracker()
    convergence = ConvergenceTracker()

    actions = []
    for cycle in range(1, 5):
        decision = evaluate(
            _result(2, cycle=cycle), budget, "review", recurrence,
            inconclusive_attempts=0, convergence=convergence,
        )
        actions.append(decision.action)
        if decision.action == "fail":
            assert "not converging" in decision.reason
            break

    assert actions == ["spawn_fix", "spawn_fix", "fail"]
    # failed at cycle 3, not at the stage cap of 8
    assert budget.review_cycles == 2


def test_same_key_case_still_reports_the_recurrence_reason():
    """Both rules can fire on a stuck finding; the more specific one wins so
    the recorded reason stays diagnostic."""
    budget = FixBudget()
    recurrence = RecurrenceTracker()
    convergence = ConvergenceTracker()
    stuck = ReviewResult(
        role_status=RoleStatus.COMPLETE, findings=[_must_fix("src/a.rs", "same")]
    )

    reasons = []
    for _ in range(4):
        recurrence.record_cycle(stuck.must_fix)
        decision = evaluate(
            stuck, budget, "review", recurrence,
            inconclusive_attempts=0, convergence=convergence,
        )
        reasons.append((decision.action, decision.reason))
        if decision.action == "fail":
            break

    assert reasons[-1][0] == "fail"
    assert "survived escalated fix" in reasons[-1][1]


def test_convergence_is_opt_in_and_absent_tracker_preserves_old_behaviour():
    budget = FixBudget()
    recurrence = RecurrenceTracker()
    for cycle in range(1, 5):
        decision = evaluate(
            _result(2, cycle=cycle), budget, "review", recurrence, inconclusive_attempts=0
        )
        assert decision.action == "spawn_fix"


# --- INCONCLUSIVE bounding ---------------------------------------------------

def test_inconclusive_aborts_at_the_cap_instead_of_retrying_forever():
    """Regression: `evaluate`'s INCONCLUSIVE branch returns BEFORE the budget
    check, so it spends nothing -- and `pr_cycle` passed a literal 0 for the
    attempt count, so the cap was unreachable. A role stuck on INCONCLUSIVE
    re-dispatched a real agent on an unbounded loop."""
    from reasona_dev.cycle_gate import MAX_INCONCLUSIVE_ATTEMPTS

    budget = FixBudget()
    inconclusive = ReviewResult(role_status=RoleStatus.INCONCLUSIVE)

    actions = []
    for attempt in range(MAX_INCONCLUSIVE_ATTEMPTS + 1):
        d = evaluate(
            inconclusive, budget, "review", RecurrenceTracker(),
            inconclusive_attempts=attempt,
        )
        actions.append(d.action)

    assert actions[:MAX_INCONCLUSIVE_ATTEMPTS] == ["inconclusive_retry"] * MAX_INCONCLUSIVE_ATTEMPTS
    assert actions[-1] == "abort"


def test_pr_cycle_carries_the_inconclusive_count_across_cycles(tmp_path, rust_dev_prompts):
    """The counter has to live in the driver: `evaluate` is stateless, so a
    caller that recomputes 0 every cycle can never reach the cap."""
    from pathlib import Path

    from reasona_dev.model_config import ResolvedModel
    from reasona_dev.pr_cycle import RoleRunResult, run_pr_cycle

    resolved = {
        "dev": ResolvedModel("dev", "sonnet", "claude", "high", "d"),
        "review": ResolvedModel("review", "opus", "claude", "high", "d"),
        "recheck": ResolvedModel("recheck", "sonnet", "claude", "medium", "d"),
        "bugbot": ResolvedModel("bugbot", "x", "kilo", "high", "d"),
        "compliance": ResolvedModel("compliance", "sonnet", "claude", "high", "d"),
        "dev_escalation": ResolvedModel("dev_escalation", "opus", "claude", "high", "d"),
    }
    calls = []

    def _fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None):
        calls.append(role)
        return RoleRunResult(
            role=role, cycle=cycle,
            review_result=ReviewResult(role_status=RoleStatus.INCONCLUSIVE),
            raw_output_path=Path("/dev/null"),
        )

    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=resolved, rundir=tmp_path / "run",
        profile="rust-dev", run_role_fn=_fn,
    )

    # bounded, not infinite: 3 retries then abort -- ABORT, not FAIL, since
    # verification never actually ran (orchestrate.py reports this as
    # `blocked`, not `failed`).
    assert result.verdict == "ABORT"
    assert "inconclusive retry budget exhausted" in result.reason
    assert len(calls) == 4


def test_a_conclusive_result_resets_the_inconclusive_streak():
    """Two isolated blips must not add up to an abort."""
    budget = FixBudget()
    recurrence = RecurrenceTracker()
    clean = ReviewResult(role_status=RoleStatus.COMPLETE)

    # a conclusive cycle in between is what the driver uses to reset to 0
    assert evaluate(clean, budget, "review", recurrence, inconclusive_attempts=2).action == "pass"
