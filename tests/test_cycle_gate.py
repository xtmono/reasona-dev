from reasona_dev.cycle_gate import (
    MAX_FINAL_CYCLES,
    MAX_FINAL_PHASE_ROUNDS,
    MAX_GH_REVIEW_CYCLES,
    MAX_INCONCLUSIVE_ATTEMPTS,
    MAX_REVIEW_CYCLES,
    MAX_SCAN_CYCLES,
    MAX_SHIP_CYCLES,
    MAX_SYNC_CYCLES,
    MAX_TOTAL_FIX_CYCLES,
    FixBudget,
    RecurrenceTracker,
    evaluate,
)
from reasona_dev.finding_adapter import Finding, ReviewResult, RoleStatus, Disposition, Severity


def test_every_stage_cap_matches_worker_md():
    """Pinned against `~/repository/tas-dev-plugins/plugins/dev/skills/
    dev-ralf/reference/worker.md` directly -- `MAX_FINAL_CYCLES` drifted to
    3 there against worker.md's `max_final_cycles=2` (found during a
    source-level parity re-check, docs/ARCHITECTURE.md §3.14.6); this
    guards every other cap from drifting silently the same way.
    """
    assert MAX_REVIEW_CYCLES == 8       # worker.md: max_review_cycles=8
    assert MAX_SCAN_CYCLES == 8         # worker.md: max_scan_cycles=8
    assert MAX_FINAL_CYCLES == 2        # worker.md: max_final_cycles=2
    assert MAX_SYNC_CYCLES == 3         # worker.md: MAX_SYNC_CYCLES=3
    assert MAX_SHIP_CYCLES == 3         # worker.md: MAX_SHIP_CYCLES=3
    assert MAX_TOTAL_FIX_CYCLES == 16   # worker.md: fix_cycles_max=16
    assert MAX_FINAL_PHASE_ROUNDS == 3  # worker.md: MAX_FINAL_PHASE_ROUNDS=3
    assert MAX_GH_REVIEW_CYCLES == 3    # worker.md /gh-review: --max-cycle 3
    assert MAX_INCONCLUSIVE_ATTEMPTS == 3  # worker.md: "3 attempts total"


def _result_with(findings):
    return ReviewResult(role_status=RoleStatus.COMPLETE, findings=findings)


def _mf(path="a.rs", desc="bug") -> Finding:
    return Finding(
        disposition=Disposition.MUST_FIX, severity=Severity.HIGH,
        path=path, line=1, symbol=None,
        contract="c", scenario="s", fix="f",
    )


def test_pass_when_no_findings():
    d = evaluate(_result_with([]), FixBudget(), "review", RecurrenceTracker(), 0)
    assert d.action == "pass"


def test_spawn_fix_first_occurrence():
    d = evaluate(_result_with([_mf()]), FixBudget(), "review", RecurrenceTracker(), 0)
    assert d.action == "spawn_fix"


def test_recurring_key_escalates_once_then_fails():
    tracker = RecurrenceTracker()
    f = _mf()
    budget = FixBudget()

    # cycle 1: first sighting
    d1 = evaluate(_result_with([f]), budget, "review", tracker, 0)
    assert d1.action == "spawn_fix"

    # fix attempted, finding survives -> record recurrence
    tracker.record_post_fix([f])
    d2 = evaluate(_result_with([f]), budget, "review", tracker, 0)
    assert d2.action == "spawn_fix_escalated"
    assert d2.escalated_model is not None

    # escalated fix also fails to resolve it
    tracker.record_post_fix([f])
    d3 = evaluate(_result_with([f]), budget, "review", tracker, 0)
    assert d3.action == "fail"


def test_cross_reviewer_convergence_escalates_on_the_very_first_sighting():
    """worker.md's `cross_reviewer_convergence`: >=2 independently
    dispatched reviewers flagging the SAME key in the SAME cycle earns the
    one-time escalation immediately -- unlike `observed_recurrence`, it
    does NOT require having already survived a prior fix."""
    tracker = RecurrenceTracker()
    f = _mf()
    budget = FixBudget()

    d1 = evaluate(_result_with([f]), budget, "review", tracker, 0, converged_keys={f.key()})
    assert d1.action == "spawn_fix_escalated"
    assert d1.escalated_model is not None


def test_a_key_without_convergence_or_recurrence_does_not_escalate():
    tracker = RecurrenceTracker()
    f = _mf()
    budget = FixBudget()

    d = evaluate(_result_with([f]), budget, "review", tracker, 0, converged_keys=set())
    assert d.action == "spawn_fix"


def test_a_converged_key_that_survives_its_escalation_still_fails():
    tracker = RecurrenceTracker()
    f = _mf()
    budget = FixBudget()

    d1 = evaluate(_result_with([f]), budget, "review", tracker, 0, converged_keys={f.key()})
    assert d1.action == "spawn_fix_escalated"

    # the escalated fix did not resolve it -- key survives, no fix was recorded
    d2 = evaluate(_result_with([f]), budget, "review", tracker, 0, converged_keys={f.key()})
    assert d2.action == "fail"


def test_recurrence_tracker_decide_converged_matches_survived_semantics():
    tracker = RecurrenceTracker()
    f = _mf()
    key = f.key()
    assert tracker.decide(key, converged=False) == "PROCEED"
    assert tracker.decide(key, converged=True) == "ESCALATE_ONCE"
    # a further sighting after escalation, with no new signal, is not yet a
    # verdict -- it only fails once the escalated fix demonstrably did not
    # resolve it (a real post-fix survival, or convergence again)
    assert tracker.decide(key, converged=False) == "PROCEED"
    tracker.record_post_fix([f])  # simulate the escalated fix surviving
    assert tracker.decide(key, converged=False) == "FAIL"


def test_escalation_from_equals_escalation_to_skips_the_dispatch():
    """worker.md: when escalation_from == escalation_to verbatim, the
    'escalated' dispatch is an identical re-run at the same tier -- no
    capability increase. Skip it and go straight to FAIL, without spending
    the stage budget on the wasted cycle."""
    tracker = RecurrenceTracker()
    f = _mf()
    budget = FixBudget()

    d = evaluate(
        _result_with([f]), budget, "review", tracker, 0,
        converged_keys={f.key()}, escalation_model="sonnet", dev_model="sonnet",
    )
    assert d.action == "fail"
    assert "escalation_from == escalation_to" in d.reason
    assert budget.review_cycles == 0  # the skipped cycle was never spent


def test_escalation_from_different_from_escalation_to_still_escalates():
    tracker = RecurrenceTracker()
    f = _mf()
    budget = FixBudget()

    d = evaluate(
        _result_with([f]), budget, "review", tracker, 0,
        converged_keys={f.key()}, escalation_model="opus", dev_model="sonnet",
    )
    assert d.action == "spawn_fix_escalated"
    assert budget.review_cycles == 1


def test_omitting_dev_model_keeps_the_escalated_dispatch_unconditional():
    """Existing callers that never pass `dev_model` see no behavior
    change -- the comparison is opt-in."""
    tracker = RecurrenceTracker()
    f = _mf()
    budget = FixBudget()

    d = evaluate(_result_with([f]), budget, "review", tracker, 0, converged_keys={f.key()}, escalation_model="opus")
    assert d.action == "spawn_fix_escalated"


def test_budget_exhaustion_fails():
    budget = FixBudget(review_cycles=8)  # at MAX_REVIEW_CYCLES
    d = evaluate(_result_with([_mf()]), budget, "review", RecurrenceTracker(), 0)
    assert d.action == "fail"
    assert "budget" in d.reason


def test_inconclusive_retries_then_aborts():
    inc = ReviewResult(role_status=RoleStatus.INCONCLUSIVE, findings=[])
    for n in range(3):
        d = evaluate(inc, FixBudget(), "review", RecurrenceTracker(), n)
        assert d.action == "inconclusive_retry"
    d = evaluate(inc, FixBudget(), "review", RecurrenceTracker(), 3)
    assert d.action == "abort"


def test_error_is_hard_blocker_not_retryable():
    err = ReviewResult(role_status=RoleStatus.ERROR, findings=[])
    d = evaluate(err, FixBudget(), "review", RecurrenceTracker(), 0)
    assert d.action == "abort"
