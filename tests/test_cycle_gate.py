from reasona_dev.cycle_gate import FixBudget, RecurrenceTracker, evaluate
from reasona_dev.finding_adapter import Finding, ReviewResult, RoleStatus, Disposition, Severity


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
