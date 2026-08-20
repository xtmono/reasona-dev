import pytest

from reasona_dev import orchestrate
from reasona_dev.model_config import ResolvedModel
from reasona_dev.orchestrate import (
    PlanRunResult,
    UnitPlan,
    order_units,
    resolve_plan_units,
    run_plan,
)
from reasona_dev.plan_compile import PRUnit, PlanError
from reasona_dev.pr_cycle import CycleResult
from reasona_dev.ship_gate import GateOutcome, ShipDecision

_RESOLVED = {
    "dev": ResolvedModel("dev", "sonnet", "claude", "high", "default"),
    "review": ResolvedModel("review", "opus", "claude", "high", "default"),
    "recheck": ResolvedModel("recheck", "sonnet", "claude", "medium", "default"),
    "bugbot": ResolvedModel("bugbot", "deepseek-v4-pro", "kilo", "high", "default"),
    "compliance": ResolvedModel("compliance", "sonnet", "claude", "high", "default"),
    "dev_escalation": ResolvedModel("dev_escalation", "opus", "claude", "high", "default"),
}

MIXED_PLAN = """\
---
plan: mixed
pr_units:
  - index: 1
    title: "shared contract"
    files: [crates/core/src/lib.rs]
  - index: 2
    title: "rust consumer"
    depends_on: [1]
    files: [crates/flow/src/x.rs]
  - index: 3
    title: "python consumer"
    depends_on: [1]
    files: [services/api/ingest.py]
---

## PR 1: shared contract

- [ ] define it

## PR 2: rust consumer

- [ ] use it

## PR 3: python consumer

- [ ] use it
"""


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".reasona").mkdir(parents=True)
    (repo / ".reasona" / "reasona.yaml").write_text(
        "dev-profile: generic\n"
        "dev-profile-map:\n"
        '  "crates/**": rust\n'
        '  "services/**/*.py": python\n'
    )
    return repo


# --- profile resolution -----------------------------------------------------

def test_each_unit_gets_the_profile_its_own_files_resolve_to(tmp_path):
    units = resolve_plan_units(MIXED_PLAN, _repo(tmp_path))
    assert {u.stage_name: u.profile for u in units} == {
        "pr-1": "rust", "pr-2": "rust", "pr-3": "python",
    }


def test_repo_default_used_when_nothing_maps(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".reasona").mkdir(parents=True)
    (repo / ".reasona" / "reasona.yaml").write_text("dev-profile: house-style\n")
    plan = "---\npr_units:\n  - index: 1\n    title: x\n    files: [README.md]\n---\n\n## PR 1: x\n"
    assert resolve_plan_units(plan, repo)[0].profile == "house-style"


def test_every_conflicting_unit_is_reported_not_just_the_first(tmp_path):
    plan = """\
---
pr_units:
  - index: 1
    title: a
    files: [crates/a.rs, services/api/a.py]
  - index: 2
    title: b
    files: [crates/b.rs, services/api/b.py]
---

## PR 1: a

## PR 2: b
"""
    with pytest.raises(PlanError) as exc:
        resolve_plan_units(plan, _repo(tmp_path))
    assert "PR 1 spans" in str(exc.value)
    assert "PR 2 spans" in str(exc.value)


def test_conflicts_surface_before_anything_runs(tmp_path):
    """A two-language unit must be refused before the first agent spawns,
    not after four units already merged."""
    plan = MIXED_PLAN.replace(
        "    files: [crates/flow/src/x.rs]",
        "    files: [crates/flow/src/x.rs, services/api/y.py]",
    )
    started = []
    with pytest.raises(PlanError):
        run_plan(
            workdir=_repo(tmp_path), plan_name="testplan", plan_text=plan, resolved=_RESOLVED,
            rundir=tmp_path / "run",
        )
    assert started == []


# --- ordering ---------------------------------------------------------------

def _up(index, depends_on=()):
    return UnitPlan(
        unit=PRUnit(index=index, title=index, depends_on=list(depends_on)),
        stage_name=f"pr-{index}", profile="generic",
    )


def test_dependencies_run_before_dependents():
    ordered = order_units([_up("3", ["1"]), _up("1"), _up("2", ["1"])])
    assert ordered[0].index == "1"
    assert {u.index for u in ordered[1:]} == {"2", "3"}


def test_dependency_outside_this_plan_is_ignored():
    """A split plan may depend on a unit that merged in an earlier plan."""
    ordered = order_units([_up("1", ["99"])])
    assert [u.index for u in ordered] == ["1"]


def test_dependency_cycle_is_fatal():
    with pytest.raises(PlanError, match="cycle"):
        order_units([_up("1", ["2"]), _up("2", ["1"])])


# --- run_plan ---------------------------------------------------------------

def _pass_cycle():
    return CycleResult(verdict="PASS", stage="scan", reason="clean")


def _fail_cycle():
    return CycleResult(verdict="FAIL", stage="review", reason="not converging")


def _abort_cycle():
    return CycleResult(verdict="ABORT", stage="review", reason="role/model unavailable")


def _pass_ship():
    return ShipDecision(stage_name="x", passed=True, outcomes=[GateOutcome("review", True, "PASS")])


def _fail_ship():
    return ShipDecision(
        stage_name="x", passed=False,
        outcomes=[GateOutcome("acceptance", False, "AC-1 failed")],
    )


def _recorder(cycle_by_stage=None, ship_passes=True):
    calls = []

    def cycle_fn(**kw):
        calls.append(kw)
        return (cycle_by_stage or {}).get(kw["stage_name"], _pass_cycle())

    def ship_fn(workdir, stage_name, **kw):
        return _pass_ship() if ship_passes else _fail_ship()

    cycle_fn.calls = calls
    return cycle_fn, ship_fn


def _fake_ensure_worktree(workdir, plan_name, stage_name, *, base):
    """No real git worktree -- these tests exercise orchestration logic, not
    `reasona_dev.worktree` itself (that has its own test module). Reusing
    `workdir` as the "worktree" keeps every existing `workdir=...` assertion
    in this file meaningful without adding a second path to track."""
    return workdir, f"reasona/{plan_name}/{stage_name}"


def _fake_remove_worktree(workdir, plan_name, stage_name):
    pass


def _fake_dispatch_cycle0(**kw):
    return True, "ok"


def _run(
    tmp_path, cycle_fn, ship_fn, plan=MIXED_PLAN, workdir_override=None,
    ensure_worktree_fn=_fake_ensure_worktree, remove_worktree_fn=_fake_remove_worktree,
    dispatch_cycle0_fn=_fake_dispatch_cycle0,
    **kw,
):
    return run_plan(
        workdir=workdir_override if workdir_override is not None else _repo(tmp_path),
        plan_name="testplan", plan_text=plan, resolved=_RESOLVED,
        rundir=tmp_path / "run", run_pr_cycle_fn=cycle_fn, ship_gate_fn=ship_fn,
        ensure_worktree_fn=ensure_worktree_fn, remove_worktree_fn=remove_worktree_fn,
        dispatch_cycle0_fn=dispatch_cycle0_fn,
        **kw,
    )


def test_the_real_ship_gate_fn_is_called_without_unsupported_kwargs(tmp_path):
    """Regression: this used to call `ship_gate_fn(..., base=base,
    head=head)`, which `ship_gate.evaluate()` has never accepted -- so the
    default (real, non-injected) ship_gate_fn raised TypeError on every unit
    whose review/scan cycle passed. `run_plan` here does not override
    `ship_gate_fn`, so this exercises the real function."""
    cycle_fn, _ = _recorder()
    result = run_plan(
        workdir=_repo(tmp_path), plan_name="testplan", plan_text=MIXED_PLAN,
        resolved=_RESOLVED, rundir=tmp_path / "run", run_pr_cycle_fn=cycle_fn,
        ensure_worktree_fn=_fake_ensure_worktree, remove_worktree_fn=_fake_remove_worktree,
        dispatch_cycle0_fn=_fake_dispatch_cycle0,
    )
    assert result.passed


def test_each_unit_is_dispatched_under_its_own_profile(tmp_path):
    cycle_fn, ship_fn = _recorder()
    _run(tmp_path, cycle_fn, ship_fn)
    assert [(c["stage_name"], c["profile"]) for c in cycle_fn.calls] == [
        ("pr-1", "rust"), ("pr-2", "rust"), ("pr-3", "python"),
    ]


def test_unit_files_are_passed_through_for_memory_retrieval(tmp_path):
    cycle_fn, ship_fn = _recorder()
    _run(tmp_path, cycle_fn, ship_fn)
    by_stage = {c["stage_name"]: c["files"] for c in cycle_fn.calls}
    assert by_stage["pr-3"] == ["services/api/ingest.py"]


def test_all_units_shipping_is_a_passing_plan(tmp_path):
    cycle_fn, ship_fn = _recorder()
    result = _run(tmp_path, cycle_fn, ship_fn)
    assert result.passed
    assert len(result.shipped) == 3


def test_dependents_of_a_failed_unit_are_skipped_not_attempted(tmp_path):
    """Reviewing against a contract that never merged produces findings the
    author has to re-derive after the upstream fix."""
    cycle_fn, ship_fn = _recorder(cycle_by_stage={"pr-1": _fail_cycle()})
    result = _run(tmp_path, cycle_fn, ship_fn)

    assert [c["stage_name"] for c in cycle_fn.calls] == ["pr-1"]
    statuses = {o.stage_name: o.status for o in result.outcomes}
    assert statuses == {"pr-1": "failed", "pr-2": "skipped", "pr-3": "skipped"}


def test_skipped_is_distinct_from_failed_in_the_summary(tmp_path):
    """Reporting five failures when one broke and four never ran misstates
    what happened."""
    cycle_fn, ship_fn = _recorder(cycle_by_stage={"pr-1": _fail_cycle()})
    result = _run(tmp_path, cycle_fn, ship_fn)
    assert len(result.failed) == 1
    assert len(result.skipped) == 2
    assert "dependency PR 1 did not ship" in result.skipped[0].reason


def test_an_independent_unit_still_runs_after_a_sibling_fails(tmp_path):
    plan = MIXED_PLAN.replace("    depends_on: [1]\n    files: [services/api/ingest.py]",
                              "    files: [services/api/ingest.py]")
    cycle_fn, ship_fn = _recorder(cycle_by_stage={"pr-1": _fail_cycle()})
    result = _run(tmp_path, cycle_fn, ship_fn, plan=plan)

    statuses = {o.stage_name: o.status for o in result.outcomes}
    assert statuses["pr-2"] == "skipped"     # depends on the failed unit
    assert statuses["pr-3"] == "shipped"     # independent


def test_an_abort_verdict_blocks_the_unit_rather_than_failing_it(tmp_path):
    """ABORT (role/model unavailable, or an exhausted INCONCLUSIVE retry
    budget) means the code was never actually judged -- an environment
    problem, not a defect -- so the unit reports `blocked`, not `failed`."""
    cycle_fn, ship_fn = _recorder(cycle_by_stage={"pr-1": _abort_cycle()})
    result = _run(tmp_path, cycle_fn, ship_fn)
    statuses = {o.stage_name: o.status for o in result.outcomes}
    assert statuses["pr-1"] == "blocked"


def test_a_failing_ship_gate_fails_the_unit_even_when_the_cycle_passed(tmp_path):
    cycle_fn, ship_fn = _recorder(ship_passes=False)
    result = _run(tmp_path, cycle_fn, ship_fn)
    assert result.outcomes[0].status == "failed"
    assert "AC-1 failed" in result.outcomes[0].reason
    # its dependents are then skipped, same as a cycle failure
    assert {o.status for o in result.outcomes[1:]} == {"skipped"}


def test_ship_gate_receives_the_cycle_verdict(tmp_path):
    seen = []

    def ship_fn(workdir, stage_name, **kw):
        seen.append(kw["cycle_verdict"])
        return _pass_ship()

    cycle_fn, _ = _recorder()
    _run(tmp_path, cycle_fn, ship_fn)
    assert seen == ["PASS", "PASS", "PASS"]


def test_each_unit_gets_its_own_rundir(tmp_path):
    cycle_fn, ship_fn = _recorder()
    _run(tmp_path, cycle_fn, ship_fn)
    rundirs = [c["rundir"].name for c in cycle_fn.calls]
    assert rundirs == ["pr-1", "pr-2", "pr-3"]


def test_render_names_status_and_profile_per_unit(tmp_path):
    cycle_fn, ship_fn = _recorder(cycle_by_stage={"pr-1": _fail_cycle()})
    out = _run(tmp_path, cycle_fn, ship_fn).render()
    assert "1 shipped" not in out  # nothing shipped
    assert "pr-3 (python)" in out
    assert "skipped" in out


# --- merge tail wiring ------------------------------------------------------

def _tail_ok(stage_name):
    from reasona_dev.final_phase import MERGED, TailResult
    return TailResult(stage_name=stage_name, status=MERGED, reason="squash-merged",
                      pr_url=f"https://gh/pr/{stage_name}")


def _tail_blocked(stage_name):
    from reasona_dev.final_phase import BLOCKED, TailResult
    return TailResult(stage_name=stage_name, status=BLOCKED, reason="merge conflict")


def test_the_tail_is_not_run_unless_ship_is_requested(tmp_path):
    cycle_fn, ship_fn = _recorder()
    called = []
    _run(tmp_path, cycle_fn, ship_fn,
         final_stage_fn=lambda **kw: called.append(1))
    assert called == []


def test_ship_runs_the_tail_for_every_passing_unit(tmp_path):
    cycle_fn, ship_fn = _recorder()
    seen = []

    def tail_fn(**kw):
        seen.append(kw["stage_name"])
        return _tail_ok(kw["stage_name"])

    result = _run(tmp_path, cycle_fn, ship_fn, ship=True, final_stage_fn=tail_fn)
    assert seen == ["pr-1", "pr-2", "pr-3"]
    assert result.passed


# --- item 5: mechanical/substantive sync-conflict resync ---------------------

def test_a_substantive_sync_conflict_re_runs_review_before_the_final_stage_retries(tmp_path):
    """`final_stage_fn` reporting `NEEDS_REVIEW` (sync resolved a
    substantive merge conflict) must trigger a fresh `run_pr_cycle_fn`
    dispatch before the final stage is retried -- worker.md's mechanical/
    substantive rule for conflict resolution."""
    from reasona_dev.final_phase import NEEDS_REVIEW, TailResult

    cycle_calls = []
    tail_calls = []

    def cycle_fn(**kw):
        cycle_calls.append(kw["stage_name"])
        return _pass_cycle()

    def ship_fn(workdir, stage_name, **kw):
        return _pass_ship()

    def tail_fn(**kw):
        stage = kw["stage_name"]
        tail_calls.append(stage)
        if stage == "pr-1" and tail_calls.count("pr-1") == 1:
            return TailResult(stage_name=stage, status=NEEDS_REVIEW,
                              reason="sync resolved a substantive merge conflict")
        return _tail_ok(stage)

    result = _run(tmp_path, cycle_fn, ship_fn, ship=True, final_stage_fn=tail_fn)
    assert result.passed
    assert cycle_calls.count("pr-1") == 2  # re-reviewed once after the substantive resync
    assert tail_calls.count("pr-1") == 2  # final stage retried once


def test_a_persistently_substantive_sync_conflict_is_blocked_after_the_resync_cap(tmp_path):
    from reasona_dev.cycle_gate import MAX_SUBSTANTIVE_RESYNC_ROUNDS
    from reasona_dev.final_phase import NEEDS_REVIEW, TailResult

    cycle_calls = []
    tail_calls = []

    def cycle_fn(**kw):
        cycle_calls.append(kw["stage_name"])
        return _pass_cycle()

    def ship_fn(workdir, stage_name, **kw):
        return _pass_ship()

    def tail_fn(**kw):
        stage = kw["stage_name"]
        tail_calls.append(stage)
        if stage == "pr-1":
            return TailResult(stage_name=stage, status=NEEDS_REVIEW,
                              reason="sync resolved a substantive merge conflict")
        return _tail_ok(stage)

    result = _run(tmp_path, cycle_fn, ship_fn, ship=True, final_stage_fn=tail_fn)
    statuses = {o.stage_name: o.status for o in result.outcomes}
    assert statuses["pr-1"] == "blocked"
    pr1_outcome = next(o for o in result.outcomes if o.stage_name == "pr-1")
    assert "exhausted" in pr1_outcome.reason
    assert cycle_calls.count("pr-1") == MAX_SUBSTANTIVE_RESYNC_ROUNDS + 1
    assert tail_calls.count("pr-1") == MAX_SUBSTANTIVE_RESYNC_ROUNDS + 1
    # a unit blocked this way never shipped -- its dependents must not run
    assert statuses["pr-2"] == "skipped" and statuses["pr-3"] == "skipped"


def test_a_failed_re_review_after_a_substantive_sync_conflict_reports_failed_not_blocked(tmp_path):
    """If the forced re-review actually finds a real defect, that is an
    ordinary review-found failure, not a stall -- `failed`, not
    `blocked`, and the final stage must not be retried a second time."""
    from reasona_dev.final_phase import NEEDS_REVIEW, TailResult

    pr1_cycles = {"n": 0}
    tail_calls = []

    def cycle_fn(**kw):
        if kw["stage_name"] != "pr-1":
            return _pass_cycle()
        pr1_cycles["n"] += 1
        return _pass_cycle() if pr1_cycles["n"] == 1 else _fail_cycle()

    def ship_fn(workdir, stage_name, **kw):
        return _pass_ship()

    def tail_fn(**kw):
        stage = kw["stage_name"]
        tail_calls.append(stage)
        if stage == "pr-1":
            return TailResult(stage_name=stage, status=NEEDS_REVIEW,
                              reason="sync resolved a substantive merge conflict")
        return _tail_ok(stage)

    result = _run(tmp_path, cycle_fn, ship_fn, ship=True, final_stage_fn=tail_fn)
    statuses = {o.stage_name: o.status for o in result.outcomes}
    assert statuses["pr-1"] == "failed"
    assert pr1_cycles["n"] == 2
    assert tail_calls.count("pr-1") == 1  # the final stage is never retried once review itself fails


def test_a_substantive_resync_clears_the_stale_ledger_progress(tmp_path, monkeypatch):
    """Without clearing the checkpoint, a resumed `run_pr_cycle_fn` could
    see the OLD run's "review already passed" progress and skip the very
    re-review this mechanism exists to force."""
    from reasona_dev import ledger
    from reasona_dev.final_phase import NEEDS_REVIEW, TailResult

    cleared = []
    monkeypatch.setattr(ledger, "clear_progress", lambda workdir, plan_name, stage_name: cleared.append(stage_name))

    def cycle_fn(**kw):
        return _pass_cycle()

    def ship_fn(workdir, stage_name, **kw):
        return _pass_ship()

    def tail_fn(**kw):
        stage = kw["stage_name"]
        if stage == "pr-1" and "pr-1" not in cleared:
            return TailResult(stage_name=stage, status=NEEDS_REVIEW,
                              reason="sync resolved a substantive merge conflict")
        return _tail_ok(stage)

    _run(tmp_path, cycle_fn, ship_fn, ship=True, final_stage_fn=tail_fn)
    assert cleared == ["pr-1"]


def test_a_blocked_tail_blocks_the_unit_and_skips_its_dependents(tmp_path):
    """A unit whose merge was refused did not ship, so anything depending on
    its contract is reviewing against something that is not on main. The
    unit itself reports `blocked`, not `failed` -- its code was never
    judged deficient, something outside code-quality judgment (here: an
    unresolved sync conflict) stopped it."""
    cycle_fn, ship_fn = _recorder()

    def tail_fn(**kw):
        return _tail_blocked(kw["stage_name"]) if kw["stage_name"] == "pr-1" else _tail_ok(kw["stage_name"])

    result = _run(tmp_path, cycle_fn, ship_fn, ship=True, final_stage_fn=tail_fn)
    statuses = {o.stage_name: o.status for o in result.outcomes}
    assert statuses == {"pr-1": "blocked", "pr-2": "skipped", "pr-3": "skipped"}
    assert "merge conflict" in result.outcomes[0].reason


def test_the_tail_receives_the_units_type_for_the_squash_title(tmp_path):
    cycle_fn, ship_fn = _recorder()
    seen = {}

    def tail_fn(**kw):
        seen[kw["stage_name"]] = (kw["unit_type"], kw["merge"])
        return _tail_ok(kw["stage_name"])

    _run(tmp_path, cycle_fn, ship_fn, ship=True, merge=True, final_stage_fn=tail_fn)
    assert seen["pr-1"][1] is True


def test_gh_review_max_wait_seconds_reaches_the_final_stage(tmp_path):
    cycle_fn, ship_fn = _recorder()
    seen = {}

    def tail_fn(**kw):
        seen[kw["stage_name"]] = kw["gh_review_max_wait_seconds"]
        return _tail_ok(kw["stage_name"])

    _run(tmp_path, cycle_fn, ship_fn, ship=True, final_stage_fn=tail_fn, gh_review_max_wait_seconds=120)
    assert seen["pr-1"] == 120


def test_port_reaches_run_pr_cycle_fn(tmp_path):
    """`run_plan(port=...)` used to reach only `dispatch_cycle0_fn` --
    `run_pr_cycle_fn` silently kept the default 8052 regardless. Needed for
    concurrent unit dispatch (`job=...`), where each in-flight unit must
    carry its own distinct port."""
    cycle_fn, ship_fn = _recorder()
    _run(tmp_path, cycle_fn, ship_fn, port=19999)
    assert [c["port"] for c in cycle_fn.calls] == [19999, 19999, 19999]


def test_port_reaches_final_stage_fn(tmp_path):
    cycle_fn, ship_fn = _recorder()
    seen = []

    def tail_fn(**kw):
        seen.append(kw["port"])
        return _tail_ok(kw["stage_name"])

    _run(tmp_path, cycle_fn, ship_fn, ship=True, final_stage_fn=tail_fn, port=19999)
    assert seen == [19999, 19999, 19999]


# --- job > 1: concurrent unit dispatch ---------------------------------------

def test_job_greater_than_one_runs_independent_units_concurrently(tmp_path):
    """pr-2 and pr-3 both depend only on pr-1 -- independent of each other --
    so with `job=2` both should be in flight at once. A `threading.Barrier`
    both must reach proves it: if they ran sequentially, the first one
    would block on the barrier forever (nothing else would ever call
    `cycle_fn` to reach it), and the barrier times out with an exception --
    this is not a timing-based flake, it is a real deadlock unless both
    threads are actually alive at once.
    """
    import threading

    barrier = threading.Barrier(2, timeout=5)
    order = []
    lock = threading.Lock()

    def cycle_fn(**kw):
        with lock:
            order.append(kw["stage_name"])
        if kw["stage_name"] in ("pr-2", "pr-3"):
            barrier.wait()
        return _pass_cycle()

    def ship_fn(workdir, stage_name, **kw):
        return _pass_ship()

    result = _run(tmp_path, cycle_fn, ship_fn, job=2)
    assert result.passed
    assert order[0] == "pr-1"  # the dependency still ran first
    assert set(order[1:]) == {"pr-2", "pr-3"}


def test_job_one_keeps_units_strictly_sequential(tmp_path):
    """Sanity check for the test above: with the default `job=1`, pr-2 and
    pr-3 do NOT run concurrently -- proving the barrier test actually
    distinguishes the two cases rather than passing regardless."""
    import threading

    barrier = threading.Barrier(2, timeout=0.3)
    broke = []

    def cycle_fn(**kw):
        if kw["stage_name"] in ("pr-2", "pr-3"):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                broke.append(kw["stage_name"])
        return _pass_cycle()

    def ship_fn(workdir, stage_name, **kw):
        return _pass_ship()

    _run(tmp_path, cycle_fn, ship_fn, job=1)
    assert broke  # at least one of them timed out waiting for a peer that never came


def test_job_greater_than_one_gives_each_concurrent_unit_a_distinct_port(tmp_path):
    seen_ports = {}
    lock = __import__("threading").Lock()

    def cycle_fn(**kw):
        with lock:
            seen_ports[kw["stage_name"]] = kw["port"]
        return _pass_cycle()

    def ship_fn(workdir, stage_name, **kw):
        return _pass_ship()

    _run(tmp_path, cycle_fn, ship_fn, job=2, port=20000)
    assert seen_ports["pr-2"] != seen_ports["pr-3"]
    assert all(20000 <= p < 20002 for p in seen_ports.values())


def test_job_greater_than_one_result_order_matches_topological_order(tmp_path):
    """`result.outcomes` stays in the plan's own topological order
    regardless of which concurrently-dispatched unit actually finishes
    first -- callers (e.g. `PlanRunResult.render()`) read this list
    positionally and must not see it reordered by completion timing."""
    import time

    def cycle_fn(**kw):
        if kw["stage_name"] == "pr-3":
            time.sleep(0.05)  # finishes AFTER pr-2 despite starting around the same time
        return _pass_cycle()

    def ship_fn(workdir, stage_name, **kw):
        return _pass_ship()

    result = _run(tmp_path, cycle_fn, ship_fn, job=2)
    assert [o.stage_name for o in result.outcomes] == ["pr-1", "pr-2", "pr-3"]


def test_job_greater_than_one_still_respects_dependency_order(tmp_path):
    """A unit's dependency must actually have shipped before it is
    dispatched, even when there is a free concurrency slot -- job>1 must
    not turn `depends_on` into a hint."""
    import threading

    started = []
    lock = threading.Lock()

    def cycle_fn(**kw):
        with lock:
            started.append(kw["stage_name"])
        if kw["stage_name"] == "pr-1":
            return _fail_cycle()  # pr-1 fails to ship
        return _pass_cycle()

    def ship_fn(workdir, stage_name, **kw):
        return _pass_ship()

    result = _run(tmp_path, cycle_fn, ship_fn, job=3)
    assert started == ["pr-1"]  # pr-2/pr-3 never dispatched -- both skip on pr-1's failure
    statuses = {o.stage_name: o.status for o in result.outcomes}
    assert statuses == {"pr-1": "failed", "pr-2": "skipped", "pr-3": "skipped"}


def test_run_plan_job_flag_reaches_orchestrate(tmp_path, monkeypatch):
    from reasona_dev.cli import main

    plan, workdir = _cli_plan(tmp_path)
    seen = {}

    def _fake_run_plan(**kw):
        seen["job"] = kw["job"]
        from reasona_dev.orchestrate import PlanRunResult, UnitOutcome
        return PlanRunResult(outcomes=[UnitOutcome(stage_name="pr-1", profile="generic", status="shipped", reason="ok")])

    monkeypatch.setattr(orchestrate, "run_plan", _fake_run_plan)
    rc = main(["run-plan", str(plan), "--workdir", str(workdir), "--job", "3"])
    assert rc == 0
    assert seen["job"] == 3


def _cli_plan(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("## PR 1: bootstrap\ntype: feat\ndepends_on: none\n\n- [ ] x\n")
    workdir = tmp_path / "target-repo"
    workdir.mkdir()
    return plan, workdir


# --- resuming with from_pr ---------------------------------------------------

def test_from_pr_skips_units_ordered_before_it(tmp_path):
    cycle_fn, ship_fn = _recorder()
    result = _run(tmp_path, cycle_fn, ship_fn, from_pr="2")
    assert [c["stage_name"] for c in cycle_fn.calls] == ["pr-2", "pr-3"]
    assert {o.stage_name for o in result.outcomes} == {"pr-2", "pr-3"}


def test_from_pr_treats_earlier_units_as_already_shipped(tmp_path):
    """A dependency on a unit this resumed run never touches is ignored,
    the same rule an earlier plan's already-merged unit already gets --
    resuming asserts pr-1 shipped in a prior attempt, it does not re-verify it."""
    cycle_fn, ship_fn = _recorder()
    _run(tmp_path, cycle_fn, ship_fn, from_pr="2")
    # pr-2 depends on pr-1, which is not in this run -- must not be blocked
    assert [c["stage_name"] for c in cycle_fn.calls] == ["pr-2", "pr-3"]


def test_from_pr_matching_the_first_unit_is_a_no_op(tmp_path):
    cycle_fn, ship_fn = _recorder()
    result = _run(tmp_path, cycle_fn, ship_fn, from_pr="1")
    assert len(result.outcomes) == 3


def test_from_pr_with_an_unknown_index_raises(tmp_path):
    cycle_fn, ship_fn = _recorder()
    with pytest.raises(PlanError, match="from-pr"):
        _run(tmp_path, cycle_fn, ship_fn, from_pr="99")


# --- automatic resume via the ledger -----------------------------------------

def test_a_unit_already_marked_shipped_is_not_redispatched(tmp_path):
    from reasona_dev import ledger

    workdir = _repo(tmp_path)
    ledger.mark_unit_terminal(workdir, "testplan", "pr-1", status="shipped", reason="merged in an earlier run")
    cycle_fn, ship_fn = _recorder()

    result = _run(tmp_path, cycle_fn, ship_fn, workdir_override=workdir)

    assert [c["stage_name"] for c in cycle_fn.calls] == ["pr-2", "pr-3"]
    by_stage = {o.stage_name: o.status for o in result.outcomes}
    assert by_stage == {"pr-1": "shipped", "pr-2": "shipped", "pr-3": "shipped"}


def test_resuming_does_not_re_block_a_dependent_of_a_resumed_unit(tmp_path):
    """pr-2/pr-3 depend on pr-1 -- a pr-1 resumed from the ledger (not
    re-run) must still count as shipped for that dependency check."""
    from reasona_dev import ledger

    workdir = _repo(tmp_path)
    ledger.mark_unit_terminal(workdir, "testplan", "pr-1", status="shipped", reason="merged in an earlier run")
    cycle_fn, ship_fn = _recorder()

    result = _run(tmp_path, cycle_fn, ship_fn, workdir_override=workdir)
    assert result.passed


def test_a_unit_that_actually_ships_this_run_is_recorded_for_the_next_one(tmp_path):
    from reasona_dev import ledger

    workdir = _repo(tmp_path)
    cycle_fn, ship_fn = _recorder()
    _run(tmp_path, cycle_fn, ship_fn, workdir_override=workdir)
    assert ledger.unit_status(workdir, "testplan", "pr-1") == "shipped"
    assert ledger.unit_status(workdir, "testplan", "pr-2") == "shipped"
    assert ledger.unit_status(workdir, "testplan", "pr-3") == "shipped"


def test_a_failed_unit_is_not_marked_shipped_and_is_retried_on_the_next_run(tmp_path):
    from reasona_dev import ledger

    workdir = _repo(tmp_path)
    cycle_fn, ship_fn = _recorder(cycle_by_stage={"pr-1": _fail_cycle()})
    _run(tmp_path, cycle_fn, ship_fn, workdir_override=workdir)
    assert ledger.unit_status(workdir, "testplan", "pr-1") == "failed"

    # a second run must retry pr-1, not skip it
    cycle_fn2, ship_fn2 = _recorder()
    _run(tmp_path, cycle_fn2, ship_fn2, workdir_override=workdir)
    assert [c["stage_name"] for c in cycle_fn2.calls] == ["pr-1", "pr-2", "pr-3"]


def test_resume_false_ignores_the_ledger(tmp_path):
    from reasona_dev import ledger

    workdir = _repo(tmp_path)
    ledger.mark_unit_terminal(workdir, "testplan", "pr-1", status="shipped", reason="merged in an earlier run")
    cycle_fn, ship_fn = _recorder()

    _run(tmp_path, cycle_fn, ship_fn, workdir_override=workdir, resume=False)
    assert [c["stage_name"] for c in cycle_fn.calls] == ["pr-1", "pr-2", "pr-3"]


# --- per-unit cycle-0 dispatch ------------------------------------------------

def test_each_unit_gets_its_own_worktree_before_cycle0(tmp_path):
    """The bug the whole worktree-per-unit design exists to fix: every unit
    must get a DIFFERENT worktree, dispatched before that unit's own
    review/scan -- never the shared top-level workdir."""
    seen = []

    def ensure_fn(workdir, plan_name, stage_name, *, base):
        seen.append(stage_name)
        return workdir / stage_name, f"reasona/{plan_name}/{stage_name}"

    cycle_fn, ship_fn = _recorder()
    result = _run(
        tmp_path, cycle_fn, ship_fn,
        ensure_worktree_fn=ensure_fn, remove_worktree_fn=_fake_remove_worktree,
        dispatch_cycle0_fn=_fake_dispatch_cycle0,
    )
    assert seen == ["pr-1", "pr-2", "pr-3"]
    workdirs = {c["stage_name"]: c["workdir"] for c in cycle_fn.calls}
    assert len({str(w) for w in workdirs.values()}) == 3  # every unit got a distinct workdir
    assert result.passed


def test_skip_dev_never_calls_the_cycle0_dispatcher(tmp_path):
    cycle_fn, ship_fn = _recorder()

    def _fail_dispatch(**kw):
        pytest.fail("must not dispatch cycle-0 when skip_dev is set")

    result = _run(tmp_path, cycle_fn, ship_fn, skip_dev=True, dispatch_cycle0_fn=_fail_dispatch)
    assert result.passed


def test_skip_dev_still_creates_the_worktree(tmp_path):
    """`skip_dev` only skips the DISPATCH -- a unit's worktree is still
    where review/scan runs against, and `--skip-dev` exists precisely for
    "cycle-0/the worktree was already set up by hand"."""
    seen = []

    def ensure_fn(workdir, plan_name, stage_name, *, base):
        seen.append(stage_name)
        return workdir, f"reasona/{plan_name}/{stage_name}"

    cycle_fn, ship_fn = _recorder()

    def _fail_dispatch(**kw):
        pytest.fail("must not dispatch")

    _run(tmp_path, cycle_fn, ship_fn, skip_dev=True, ensure_worktree_fn=ensure_fn,
         dispatch_cycle0_fn=_fail_dispatch)
    assert seen == ["pr-1", "pr-2", "pr-3"]


def test_an_already_dispatched_unit_is_not_dispatched_again_on_resume(tmp_path):
    from reasona_dev import ledger

    workdir = _repo(tmp_path)
    ledger.mark_dev_dispatched(workdir, "testplan", "pr-1")
    dispatched = []

    def _dispatch(*, workdir, worktree_path, plan_name, plan_text, only_index, **kw):
        dispatched.append(only_index)
        return True, "ok"

    cycle_fn, ship_fn = _recorder()
    _run(tmp_path, cycle_fn, ship_fn, workdir_override=workdir, dispatch_cycle0_fn=_dispatch)
    assert dispatched == ["2", "3"]  # pr-1's cycle-0 already ran -- not redispatched


def test_a_failing_cycle0_dispatch_blocks_the_unit_without_running_review(tmp_path):
    cycle_fn, ship_fn = _recorder()

    def _fail_dispatch(**kw):
        return False, "dev cycle-0 failed (bernstein run exit 1): boom"

    result = _run(tmp_path, cycle_fn, ship_fn, dispatch_cycle0_fn=_fail_dispatch)
    statuses = {o.stage_name: o.status for o in result.outcomes}
    # pr-1 blocked before review ever ran; its dependents (pr-2, pr-3) skip
    assert statuses == {"pr-1": "blocked", "pr-2": "skipped", "pr-3": "skipped"}
    assert cycle_fn.calls == []
    assert "boom" in result.outcomes[0].reason


def test_a_failing_worktree_creation_blocks_the_unit(tmp_path):
    cycle_fn, ship_fn = _recorder()

    def _fail_ensure(workdir, plan_name, stage_name, *, base):
        raise RuntimeError("git worktree add failed: disk full")

    result = _run(tmp_path, cycle_fn, ship_fn, ensure_worktree_fn=_fail_ensure)
    assert result.outcomes[0].status == "blocked"
    assert "disk full" in result.outcomes[0].reason
    assert cycle_fn.calls == []


def test_a_shipped_units_worktree_is_removed(tmp_path):
    removed = []

    def _remove(workdir, plan_name, stage_name):
        removed.append(stage_name)

    def tail_fn(**kw):
        return _tail_ok(kw["stage_name"])

    cycle_fn, ship_fn = _recorder()
    result = _run(tmp_path, cycle_fn, ship_fn, ship=True, final_stage_fn=tail_fn, remove_worktree_fn=_remove)
    assert result.passed
    assert removed == ["pr-1", "pr-2", "pr-3"]


def test_a_pr_open_units_worktree_is_not_removed(tmp_path):
    """Only an actual MERGED outcome earns cleanup -- PR_OPEN still has an
    open PR pointing at that branch."""
    from reasona_dev.final_phase import PR_OPEN, TailResult

    removed = []

    def _remove(workdir, plan_name, stage_name):
        removed.append(stage_name)

    def tail_fn(**kw):
        return TailResult(
            stage_name=kw["stage_name"], status=PR_OPEN, reason="merge not requested",
            pr_url="https://gh/pr/1", ship_decision=_pass_ship(),
        )

    cycle_fn, ship_fn = _recorder()
    _run(tmp_path, cycle_fn, ship_fn, ship=True, final_stage_fn=tail_fn, remove_worktree_fn=_remove)
    assert removed == []
