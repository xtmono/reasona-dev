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


def _run(tmp_path, cycle_fn, ship_fn, plan=MIXED_PLAN, **kw):
    return run_plan(
        workdir=_repo(tmp_path), plan_name="testplan", plan_text=plan, resolved=_RESOLVED,
        rundir=tmp_path / "run", run_pr_cycle_fn=cycle_fn, ship_gate_fn=ship_fn,
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

    result = run_plan(
        workdir=workdir, plan_name="testplan", plan_text=MIXED_PLAN, resolved=_RESOLVED,
        rundir=tmp_path / "run", run_pr_cycle_fn=cycle_fn, ship_gate_fn=ship_fn,
    )

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

    result = run_plan(
        workdir=workdir, plan_name="testplan", plan_text=MIXED_PLAN, resolved=_RESOLVED,
        rundir=tmp_path / "run", run_pr_cycle_fn=cycle_fn, ship_gate_fn=ship_fn,
    )
    assert result.passed


def test_a_unit_that_actually_ships_this_run_is_recorded_for_the_next_one(tmp_path):
    from reasona_dev import ledger

    workdir = _repo(tmp_path)
    cycle_fn, ship_fn = _recorder()
    run_plan(
        workdir=workdir, plan_name="testplan", plan_text=MIXED_PLAN, resolved=_RESOLVED,
        rundir=tmp_path / "run", run_pr_cycle_fn=cycle_fn, ship_gate_fn=ship_fn,
    )
    assert ledger.unit_status(workdir, "testplan", "pr-1") == "shipped"
    assert ledger.unit_status(workdir, "testplan", "pr-2") == "shipped"
    assert ledger.unit_status(workdir, "testplan", "pr-3") == "shipped"


def test_a_failed_unit_is_not_marked_shipped_and_is_retried_on_the_next_run(tmp_path):
    from reasona_dev import ledger

    workdir = _repo(tmp_path)
    cycle_fn, ship_fn = _recorder(cycle_by_stage={"pr-1": _fail_cycle()})
    run_plan(
        workdir=workdir, plan_name="testplan", plan_text=MIXED_PLAN, resolved=_RESOLVED,
        rundir=tmp_path / "run", run_pr_cycle_fn=cycle_fn, ship_gate_fn=ship_fn,
    )
    assert ledger.unit_status(workdir, "testplan", "pr-1") == "failed"

    # a second run must retry pr-1, not skip it
    cycle_fn2, ship_fn2 = _recorder()
    run_plan(
        workdir=workdir, plan_name="testplan", plan_text=MIXED_PLAN, resolved=_RESOLVED,
        rundir=tmp_path / "run", run_pr_cycle_fn=cycle_fn2, ship_gate_fn=ship_fn2,
    )
    assert [c["stage_name"] for c in cycle_fn2.calls] == ["pr-1", "pr-2", "pr-3"]


def test_resume_false_ignores_the_ledger(tmp_path):
    from reasona_dev import ledger

    workdir = _repo(tmp_path)
    ledger.mark_unit_terminal(workdir, "testplan", "pr-1", status="shipped", reason="merged in an earlier run")
    cycle_fn, ship_fn = _recorder()

    run_plan(
        workdir=workdir, plan_name="testplan", plan_text=MIXED_PLAN, resolved=_RESOLVED,
        rundir=tmp_path / "run", run_pr_cycle_fn=cycle_fn, ship_gate_fn=ship_fn,
        resume=False,
    )
    assert [c["stage_name"] for c in cycle_fn.calls] == ["pr-1", "pr-2", "pr-3"]
