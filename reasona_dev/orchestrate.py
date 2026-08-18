"""Runs a whole plan's PR units through review -> scan -> ship, in
dependency order, each under the profile its own files resolve to.

**The gap this closes.** `plan_compile` knows a plan's units, their
`files:`, their `depends_on:`, and their profiles. `pr_cycle` reviews one
unit under one profile. `ship_gate` decides whether one unit may merge.
Nothing joined them, so the per-unit profile work had no caller and running
a plan meant invoking three modules by hand, once per unit, with the right
arguments derived by the operator. That is the same "available, if you
remember" state `ship_gate` was built to remove -- one level up.

**Where the dev step is.** Not here. `plan_compile` emits a Bernstein
plan.yaml whose stages carry each unit's cycle-0 implementation step, and
Bernstein's own scheduler runs that DAG. This module picks up exactly where
`pr_cycle` documents its own entry point: after the implementation exists.
Owning the dev step here would mean re-implementing a DAG scheduler that
already runs, which is the opposite of this project's premise.

**Dependency order, and what a failure does to dependents.** Units run in
topological order of their declared `depends_on`. A unit whose dependency
did not ship is SKIPPED, not attempted: its premise is a contract that was
never merged, so any review of it is a review against a shape that does not
exist yet, and any finding it produces is noise the author has to re-derive
after the upstream unit is fixed. Skipped is a distinct outcome from failed
-- conflating them would report a plan as five failures when one unit broke
and four were never run.

**One server for the plan.** Started once here and passed into every
`run_pr_cycle` call. Same reasoning that moved role dispatch off per-role
subprocesses -- the bootstrap is real work, and paying it once per unit is
as arbitrary as paying it once per role.

**Profile conflicts surface before anything runs.** Every unit's profile is
resolved up front, so a plan with a two-language unit is refused before the
first agent spawns rather than after four units have already merged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from reasona_dev import config_file, merge_tail as merge_tail_mod, ship_gate
from reasona_dev.bernstein_server import ServerHandle, start_server, stop_server
from reasona_dev.model_config import ResolvedModel
from reasona_dev.plan_compile import PRUnit, PlanError, _stage_name, parse_manifest_units, parse_plan_units
from reasona_dev.cycle_gate import FixBudget, RecurrenceTracker
from reasona_dev.merge_tail import TailResult
from reasona_dev.pr_cycle import CycleResult, run_pr_cycle
from reasona_dev.prompt_profile import (
    ProfileConflict,
    resolve_profile_name,
    resolve_unit_profile,
)
from reasona_dev.ship_gate import ShipDecision


@dataclass
class UnitPlan:
    """One PR unit with everything the drivers below need already resolved."""

    unit: PRUnit
    stage_name: str
    profile: str

    @property
    def index(self) -> str:
        return self.unit.index

    @property
    def title(self) -> str:
        return self.unit.title


@dataclass
class UnitOutcome:
    stage_name: str
    profile: str
    status: str  # "shipped" | "failed" | "skipped"
    reason: str
    cycle_result: CycleResult | None = None
    ship_decision: ShipDecision | None = None
    tail: TailResult | None = None


@dataclass
class PlanRunResult:
    outcomes: list[UnitOutcome] = field(default_factory=list)

    @property
    def shipped(self) -> list[UnitOutcome]:
        return [o for o in self.outcomes if o.status == "shipped"]

    @property
    def failed(self) -> list[UnitOutcome]:
        return [o for o in self.outcomes if o.status == "failed"]

    @property
    def skipped(self) -> list[UnitOutcome]:
        return [o for o in self.outcomes if o.status == "skipped"]

    @property
    def passed(self) -> bool:
        return bool(self.outcomes) and all(o.status == "shipped" for o in self.outcomes)

    def render(self) -> str:
        lines = [
            f"plan run: {len(self.shipped)} shipped, "
            f"{len(self.failed)} failed, {len(self.skipped)} skipped"
        ]
        for o in self.outcomes:
            lines.append(f"  [{o.status:>7}] {o.stage_name} ({o.profile}): {o.reason}")
        return "\n".join(lines)


def resolve_plan_units(plan_text: str, workdir: str | Path) -> list[UnitPlan]:
    """Parse a plan and resolve every unit's profile, up front.

    Raises `PlanError` listing EVERY conflicting unit rather than the first
    -- the same reason `parse_criteria` collects its errors: an author
    fixing one conflict per compile is friction that ends with the feature
    being avoided.
    """
    workdir = Path(workdir)
    units, manifest_errors = parse_manifest_units(plan_text)
    if manifest_errors:
        raise PlanError("plan has defect(s):\n  - " + "\n  - ".join(manifest_errors))
    if not units:
        units = parse_plan_units(plan_text)

    project_cfg = config_file.load_project(workdir)
    global_cfg = config_file.load_global()
    repo_default = resolve_profile_name(project_cfg=project_cfg, global_cfg=global_cfg)

    resolved: list[UnitPlan] = []
    conflicts: list[str] = []
    for u in units:
        try:
            profile = resolve_unit_profile(
                files=u.files, unit_profile=u.profile, unit_index=u.index,
                project_cfg=project_cfg, global_cfg=global_cfg, fallback=repo_default,
            )
        except ProfileConflict as exc:
            conflicts.append(str(exc))
            continue
        resolved.append(UnitPlan(unit=u, stage_name=_stage_name(u.index), profile=profile))

    if conflicts:
        raise PlanError("\n\n".join(conflicts))
    return resolved


def order_units(units: list[UnitPlan]) -> list[UnitPlan]:
    """Topological order by declared `depends_on`.

    A dependency naming an index that does not exist in this plan is
    IGNORED rather than fatal -- a plan may legitimately depend on a unit
    that already merged in an earlier plan, and refusing that would make
    split plans (which the 5-unit cap encourages) unusable.

    A cycle is fatal: there is no order that satisfies it, and running the
    units in declaration order instead would silently review at least one
    against an unbuilt dependency.
    """
    by_index = {u.index: u for u in units}
    ordered: list[UnitPlan] = []
    placed: set[str] = set()

    remaining = list(units)
    while remaining:
        progressed = False
        still: list[UnitPlan] = []
        for u in remaining:
            deps = [d for d in u.unit.depends_on if d in by_index]
            if all(d in placed for d in deps):
                ordered.append(u)
                placed.add(u.index)
                progressed = True
            else:
                still.append(u)
        if not progressed:
            stuck = ", ".join(u.index for u in still)
            raise PlanError(
                f"depends_on has a cycle among PR units: {stuck}. "
                "No execution order satisfies it."
            )
        remaining = still
    return ordered


def _blocking_dependency(unit: UnitPlan, outcomes: dict[str, UnitOutcome], known: set[str]) -> str | None:
    for dep in unit.unit.depends_on:
        if dep not in known:
            continue  # merged in an earlier plan -- see order_units()
        outcome = outcomes.get(dep)
        if outcome is not None and outcome.status != "shipped":
            return dep
    return None


def run_plan(
    *,
    workdir: str | Path,
    plan_text: str,
    resolved: dict[str, ResolvedModel],
    rundir: str | Path,
    port: int = 8052,
    base: str = "origin/main",
    head: str = "HEAD",
    ship: bool = False,
    merge: bool = False,
    run_pr_cycle_fn=run_pr_cycle,
    ship_gate_fn=ship_gate.evaluate,
    merge_tail_fn=merge_tail_mod.run_merge_tail,
    start_server_fn=start_server,
    stop_server_fn=stop_server,
) -> PlanRunResult:
    """review -> scan -> ship, per unit, in dependency order.

    Assumes each unit's cycle-0 implementation already exists (see module
    docstring on where the dev step lives).

    Every `*_fn` is injectable purely for testing; production callers pass
    none of them.
    """
    workdir = Path(workdir)
    rundir = Path(rundir)
    units = order_units(resolve_plan_units(plan_text, workdir))
    result = PlanRunResult()
    if not units:
        return result

    known = {u.index for u in units}
    by_index: dict[str, UnitOutcome] = {}
    server: ServerHandle | None = start_server_fn(workdir, port=port)
    try:
        for up in units:
            blocked_by = _blocking_dependency(up, by_index, known)
            if blocked_by is not None:
                outcome = UnitOutcome(
                    stage_name=up.stage_name, profile=up.profile, status="skipped",
                    reason=f"dependency PR {blocked_by} did not ship",
                )
                result.outcomes.append(outcome)
                by_index[up.index] = outcome
                continue

            cycle = run_pr_cycle_fn(
                workdir=workdir,
                pr_title=f"PR {up.index}: {up.title}",
                resolved=resolved,
                rundir=rundir / up.stage_name,
                profile=up.profile,
                stage_name=up.stage_name,
                files=up.unit.files,
                server=server,
            )

            if cycle.verdict not in ("PASS", "PASS_WITH_NOTES"):
                outcome = UnitOutcome(
                    stage_name=up.stage_name, profile=up.profile, status="failed",
                    reason=f"{cycle.stage}: {cycle.reason}", cycle_result=cycle,
                )
            else:
                decision = ship_gate_fn(
                    workdir, up.stage_name,
                    cycle_verdict=cycle.verdict, base=base, head=head,
                )
                tail: TailResult | None = None
                if decision.passed and ship:
                    tail = merge_tail_fn(
                        server=server, workdir=workdir, stage_name=up.stage_name,
                        pr_title=f"{up.title}", unit_type=up.unit.unit_type,
                        profile=up.profile, resolved=resolved,
                        rundir=rundir / up.stage_name, ship_decision=decision,
                        budget=cycle.budget or FixBudget(),
                        recurrence=cycle.recurrence or RecurrenceTracker(),
                        base=base, merge=merge,
                    )
                if tail is not None and tail.blocked:
                    status, reason = "failed", tail.reason
                elif tail is not None:
                    status, reason = "shipped", tail.reason
                else:
                    status = "shipped" if decision.passed else "failed"
                    reason = decision.reason
                outcome = UnitOutcome(
                    stage_name=up.stage_name, profile=up.profile,
                    status=status, reason=reason,
                    cycle_result=cycle, ship_decision=decision, tail=tail,
                )
            result.outcomes.append(outcome)
            by_index[up.index] = outcome
    finally:
        if server is not None:
            stop_server_fn(server, workdir=workdir)
    return result
