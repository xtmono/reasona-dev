import subprocess

import pytest

from reasona_dev.cycle_gate import (
    MAX_FINAL_CYCLES,
    MAX_FINAL_PHASE_ROUNDS,
    GH_REVIEW_MAX_CYCLE,
    MAX_INCONCLUSIVE_ATTEMPTS,
    MAX_REVIEW_CYCLES,
    MAX_SCAN_CYCLES,
    MAX_SHIP_CYCLES,
    MAX_SYNC_CYCLES,
    MAX_TOTAL_FIX_CYCLES,
    FixBudget,
    RecurrenceTracker,
    evaluate,
    recheck_route,
)
from reasona_dev.finding_adapter import Finding, ReviewResult, RoleStatus, Disposition, Severity


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "src").mkdir()
    (repo / "src" / "a.rs").write_text("fn a() {}\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    return repo, head


def test_recheck_route_empty_diff_is_full_not_bounded(tmp_path):
    """Real incident (TAS plan 49 PR2, 2026-08-22): a dev-fix dispatch's
    commit never landed on the unit branch (Bernstein's own merge-back
    step did not run), so `pre_fix_head..HEAD` diffed completely empty.
    The empty set is trivially a subset of any `finding_files`, so the old
    `fix_files <= finding_files` check misrouted this as BOUNDED -- as if
    a real, narrowly-scoped fix had landed. Must be FULL: there was no fix
    to confirm at all."""
    repo, head = _git_repo(tmp_path)
    assert recheck_route(str(repo), head, {"src/a.rs"}) == "FULL"


def test_recheck_route_bounded_when_fix_touches_only_finding_files(tmp_path):
    repo, head = _git_repo(tmp_path)
    (repo / "src" / "a.rs").write_text("fn a() { 1 }\n")
    subprocess.run(["git", "commit", "-q", "-am", "fix"], cwd=repo, check=True)
    assert recheck_route(str(repo), head, {"src/a.rs"}) == "BOUNDED"


def test_recheck_route_full_when_fix_touches_a_file_outside_findings(tmp_path):
    repo, head = _git_repo(tmp_path)
    (repo / "src" / "b.rs").write_text("fn b() {}\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fix"], cwd=repo, check=True)
    assert recheck_route(str(repo), head, {"src/a.rs"}) == "FULL"


def test_every_stage_cap_matches_dev_ralfs_budget_py():
    """Pinned against `~/repository/tas-dev-plugins/plugins/dev/skills/
    dev-ralf/tools/budget.py`'s `STAGE_CAPS` -- the single authority there,
    mirrored into worker.md's section headings and SKILL.md's table -- so a
    cap cannot drift from it silently. `MAX_FINAL_CYCLES` did drift once,
    and only a source-level re-check found it (docs/ARCHITECTURE.md
    §3.14.6); it has since moved again, in BOTH projects together, and is
    pinned here at its current shared value rather than exempted (§3.14.8).
    """
    assert MAX_REVIEW_CYCLES == 8       # budget.py STAGE_CAPS["review"]
    assert MAX_SCAN_CYCLES == 8         # budget.py STAGE_CAPS["scan"]
    assert MAX_FINAL_CYCLES == 3        # budget.py STAGE_CAPS["final"]  (was 2 before dev-ralf 5ff9641)
    assert MAX_SYNC_CYCLES == 3         # budget.py STAGE_CAPS["sync"]
    assert MAX_SHIP_CYCLES == 3         # budget.py STAGE_CAPS["ship"]
    assert MAX_TOTAL_FIX_CYCLES == 16   # cycle_gate.py TOTAL_FIX_BUDGET, imported by budget.py
    assert MAX_FINAL_PHASE_ROUNDS == 3  # worker.md: MAX_FINAL_PHASE_ROUNDS=3
    assert GH_REVIEW_MAX_CYCLE == 3     # worker.md /gh-review: --max-cycle 3 (a ceiling, not a stage)
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

    # cycle 1: first sighting -- nothing to have survived yet
    tracker.record_cycle([f])
    d1 = evaluate(_result_with([f]), budget, "review", tracker, 0)
    assert d1.action == "spawn_fix"

    # cycle 2: the SAME key is still here, so it survived cycle 1's fix
    tracker.record_cycle([f])
    d2 = evaluate(_result_with([f]), budget, "review", tracker, 0)
    assert d2.action == "spawn_fix_escalated"
    assert d2.escalated_model is not None
    assert d2.escalation_trigger == "observed_recurrence"

    # cycle 3: it survived the ESCALATED fix too -- stop-the-world
    tracker.record_cycle([f])
    d3 = evaluate(_result_with([f]), budget, "review", tracker, 0)
    assert d3.action == "fail"


def test_a_brand_new_finding_on_a_later_cycle_is_not_a_recurrence():
    """worker.md / `finding_merge.escalation_decision`: observed_recurrence
    is `set(current) & set(prior)`. A finding that appears for the FIRST
    time on cycle 2 never survived anything, so it must not escalate --
    `record_cycle` used to increment every MUST_FIX present from cycle 2 on,
    which made a fresh finding look like a survivor."""
    tracker = RecurrenceTracker()
    first, second = _mf(path="a.rs"), _mf(path="b.rs")
    budget = FixBudget()

    tracker.record_cycle([first])
    assert evaluate(_result_with([first]), budget, "review", tracker, 0).action == "spawn_fix"

    tracker.record_cycle([second])  # a DIFFERENT key -- no intersection
    d = evaluate(_result_with([second]), budget, "review", tracker, 0)
    assert d.action == "spawn_fix"
    assert d.escalation_trigger is None


def test_cross_reviewer_convergence_escalates_on_the_very_first_sighting():
    """worker.md's `cross_reviewer_convergence`: >=2 independently
    dispatched reviewers flagging the SAME key in the SAME cycle earns the
    one-time escalation immediately -- unlike `observed_recurrence`, it
    does NOT require having already survived a prior fix."""
    tracker = RecurrenceTracker()
    f = _mf()
    budget = FixBudget()

    tracker.record_cycle([f])
    d1 = evaluate(_result_with([f]), budget, "review", tracker, 0, convergent_keys={f.key()})
    assert d1.action == "spawn_fix_escalated"
    assert d1.escalated_model is not None
    assert d1.escalation_trigger == "cross_reviewer_convergence"


def test_scope_exceeded_is_the_lowest_priority_trigger():
    """`finding_merge.escalation_decision`'s order is convergence >
    recurrence > scope_exceeded, and it names exactly one."""
    tracker = RecurrenceTracker()
    f = _mf()
    budget = FixBudget()

    tracker.record_cycle([f])
    d = evaluate(_result_with([f]), budget, "review", tracker, 0, route_full=True)
    assert d.action == "spawn_fix_escalated"
    assert d.escalation_trigger == "scope_exceeded"


def test_convergence_outranks_recurrence_and_scope():
    tracker = RecurrenceTracker()
    f = _mf()
    budget = FixBudget()

    tracker.record_cycle([f])
    tracker.record_cycle([f])  # now also a recurrence
    d = evaluate(
        _result_with([f]), budget, "review", tracker, 0,
        convergent_keys={f.key()}, route_full=True,
    )
    assert d.escalation_trigger == "cross_reviewer_convergence"


def test_a_key_without_any_trigger_does_not_escalate():
    tracker = RecurrenceTracker()
    f = _mf()
    budget = FixBudget()

    tracker.record_cycle([f])
    d = evaluate(_result_with([f]), budget, "review", tracker, 0)
    assert d.action == "spawn_fix"
    assert d.escalation_trigger is None


def test_escalation_is_capped_at_one_per_pr_not_one_per_key():
    """worker.md: "ONE escalation per PR ... not an uncapped ladder", and
    "one per PR total, not one per stage". A SECOND, unrelated key must not
    earn its own escalation once the PR's single one is spent."""
    tracker = RecurrenceTracker()
    first, second = _mf(path="a.rs"), _mf(path="b.rs")
    budget = FixBudget()

    tracker.record_cycle([first])
    d1 = evaluate(_result_with([first]), budget, "review", tracker, 0, convergent_keys={first.key()})
    assert d1.action == "spawn_fix_escalated"

    tracker.record_cycle([second])
    d2 = evaluate(_result_with([second]), budget, "review", tracker, 0, convergent_keys={second.key()})
    assert d2.action == "spawn_fix"  # ordinary fix -- the one escalation is spent
    assert d2.escalation_trigger is None


def test_a_key_surviving_the_escalated_fix_fails():
    tracker = RecurrenceTracker()
    f = _mf()
    budget = FixBudget()

    tracker.record_cycle([f])
    assert evaluate(_result_with([f]), budget, "review", tracker, 0,
                    convergent_keys={f.key()}).action == "spawn_fix_escalated"

    tracker.record_cycle([f])  # survived the escalated fix
    assert evaluate(_result_with([f]), budget, "review", tracker, 0).action == "fail"


def test_recurrence_tracker_round_trips_through_json():
    tracker = RecurrenceTracker()
    f = _mf()
    tracker.record_cycle([f])
    tracker.record_cycle([f])
    tracker.escalated = True

    back = RecurrenceTracker.from_dict(tracker.to_dict())
    assert back.survived == tracker.survived
    assert back.escalated is True
    assert back.previous_keys == {f.key()}


def test_recurrence_tracker_accepts_the_older_per_key_escalated_list():
    """A ledger written before `escalated` became a per-PR boolean stored a
    LIST of escalated keys; a non-empty one means the PR's single
    escalation was already spent."""
    assert RecurrenceTracker.from_dict({"escalated": ["k1"]}).escalated is True
    assert RecurrenceTracker.from_dict({"escalated": []}).escalated is False


def test_escalation_from_equals_escalation_to_fails_only_for_observed_recurrence():
    """worker.md: when escalation_from == escalation_to verbatim, the
    'escalated' dispatch is an identical re-run at the same tier -- no
    capability increase. Skip it and go straight to the outcome a
    NON-escalated fix would have reached. For observed_recurrence that
    outcome is the key's second unresolved occurrence, i.e. immediate FAIL
    (worker.md's own words), without spending the stage budget on the
    wasted cycle."""
    tracker = RecurrenceTracker()
    f = _mf()
    budget = FixBudget()

    tracker.record_cycle([f])  # cycle 1: establishes previous_keys
    tracker.record_cycle([f])  # cycle 2: same key survived -> observed_recurrence
    d = evaluate(
        _result_with([f]), budget, "review", tracker, 0,
        escalation_model="sonnet", dev_model="sonnet",
    )
    assert d.action == "fail"
    assert "escalation_from == escalation_to" in d.reason
    assert d.escalation_trigger == "observed_recurrence"  # still attributable
    assert budget.review_cycles == 0  # the skipped cycle was never spent


def test_escalation_from_equals_escalation_to_still_dispatches_a_normal_fix_for_convergence():
    """The SAME guard for cross_reviewer_convergence (and, by the same
    reasoning, scope_exceeded) does NOT fail the PR -- a non-escalated fix
    reaching either trigger would have been an ordinary spawn_fix, never a
    stop-the-world FAIL, so the tier-collision guard must fall through to
    that instead of manufacturing a FAIL the trigger itself never implies."""
    tracker = RecurrenceTracker()
    f = _mf()
    budget = FixBudget()

    d = evaluate(
        _result_with([f]), budget, "review", tracker, 0,
        convergent_keys={f.key()}, escalation_model="sonnet", dev_model="sonnet",
    )
    assert d.action == "spawn_fix"
    assert d.escalation_trigger == "cross_reviewer_convergence"  # still attributable
    assert budget.review_cycles == 1  # a normal fix cycle WAS spent, unlike observed_recurrence


def test_escalation_from_different_from_escalation_to_still_escalates():
    tracker = RecurrenceTracker()
    f = _mf()
    budget = FixBudget()

    tracker.record_cycle([f])
    d = evaluate(
        _result_with([f]), budget, "review", tracker, 0,
        convergent_keys={f.key()}, escalation_model="opus", dev_model="sonnet",
    )
    assert d.action == "spawn_fix_escalated"
    assert budget.review_cycles == 1


def test_omitting_dev_model_keeps_the_escalated_dispatch_unconditional():
    """Existing callers that never pass `dev_model` see no behavior
    change -- the comparison is opt-in."""
    tracker = RecurrenceTracker()
    f = _mf()
    budget = FixBudget()

    tracker.record_cycle([f])
    d = evaluate(_result_with([f]), budget, "review", tracker, 0,
                 convergent_keys={f.key()}, escalation_model="opus")
    assert d.action == "spawn_fix_escalated"


def test_the_total_is_what_actually_bounds_a_pr_not_the_stage_caps():
    """budget.py's own docstring: "The total is the actual bound: the caps
    alone sum to 24, past 16." Still true at final=3 (they sum to 25), so
    the extra final-audit cycle is only reachable by a PR that has not
    already spent its pool elsewhere."""
    assert (
        MAX_REVIEW_CYCLES + MAX_SCAN_CYCLES + MAX_FINAL_CYCLES
        + MAX_SYNC_CYCLES + MAX_SHIP_CYCLES
    ) > MAX_TOTAL_FIX_CYCLES


def test_the_budget_has_exactly_dev_ralfs_five_stages():
    """dev-ralf `budget.py`'s `STAGE_CAPS` is `review/scan/final/sync/ship`
    -- five stages, and gh-review is NOT one of them. reasona-dev had a
    sixth `gh_review` stage, which handed gh-review its own 3 cycles ON TOP
    of review's 8 instead of out of them."""
    b = FixBudget()
    for stage in ("review", "scan", "final", "sync", "ship"):
        assert b.can_spend(stage) is True
    with pytest.raises(KeyError):
        b.spend("gh_review")


def test_gh_review_cap_shrinks_with_the_shared_pool_and_floors_at_zero():
    b = FixBudget()
    assert b.gh_review_cap() == GH_REVIEW_MAX_CYCLE  # fresh: the fixed ceiling
    b.total_used = MAX_TOTAL_FIX_CYCLES - 2
    assert b.gh_review_cap() == 2                    # pool is the binding side now
    b.total_used = MAX_TOTAL_FIX_CYCLES
    assert b.gh_review_cap() == 0                    # never negative


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
