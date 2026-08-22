"""Runs a whole plan's PR units through dev-0 -> review -> scan -> ship, in
dependency order, each in its own git worktree and under the profile its
own files resolve to.

**The gap this closes.** `plan_compile` knows a plan's units, their
`files:`, their `depends_on:`, and their profiles. `pr_cycle` reviews one
unit under one profile. `ship_gate` decides whether one unit may merge.
Nothing joined them, so the per-unit profile work had no caller and running
a plan meant invoking three modules by hand, once per unit, with the right
arguments derived by the operator. That is the same "available, if you
remember" state `ship_gate` was built to remove -- one level up.

**Where the dev step is: HERE now, per unit, in that unit's own worktree.**
This used to be different -- `cli.py` compiled the WHOLE plan into one
Bernstein plan.yaml (one stage per unit, wired with `depends_on`) and
dispatched it as a single `bernstein run`, entirely before this module ever
started. That put every unit's cycle-0 commits on the SAME shared
`workdir` checkout, sequentially, before any unit-level isolation could
exist -- so a real per-unit branch/PR was structurally impossible without
commit surgery (`docs/ARCHITECTURE.md` §3.11 has the full account). Fixed
by moving cycle-0 in here: each unit gets its own worktree
(`reasona_dev.worktree.ensure_unit_worktree()`) before its cycle-0 is
dispatched (`plan_compile.compile_to_bernstein_plan(..., only_index=...)`,
one unit at a time), and every later stage for that unit -- review, scan,
the final phase, gh-pr, gh-review, squash-merge -- runs against that same
worktree. Dependency ordering no longer needs to be expressed as a
Bernstein DAG at all: this module's own sequential unit loop already
enforces it.

**Dependency order, and what a failure does to dependents.** Units run in
topological order of their declared `depends_on`. A unit whose dependency
did not ship is SKIPPED, not attempted: its premise is a contract that was
never merged, so any review of it is a review against a shape that does not
exist yet, and any finding it produces is noise the author has to re-derive
after the upstream unit is fixed. Skipped is a distinct outcome from failed
-- conflating them would report a plan as five failures when one unit broke
and four were never run.

**Profile conflicts surface before anything runs.** Every unit's profile is
resolved up front, so a plan with a two-language unit is refused before the
first agent spawns rather than after four units have already merged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from reasona_dev import _shell, bernstein_dispatch, ci_gate, config_file, final_phase as final_phase_mod, gh_pr, ledger, open_decisions, worktree
from reasona_dev.plan_report import _SOURCE_EXT
from reasona_dev import gh_review as gh_review_mod
from reasona_dev import ship_gate
from reasona_dev.model_config import ResolvedModel
from reasona_dev.plan_compile import (
    PRUnit,
    PlanError,
    _stage_name,
    parse_manifest_units,
    parse_plan_units,
    write_plan_yaml,
)
from reasona_dev.cycle_gate import MAX_SUBSTANTIVE_RESYNC_ROUNDS, FixBudget, RecurrenceTracker
from reasona_dev.final_phase import TailResult
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
    status: str  # "shipped" | "failed" | "blocked" | "skipped"
    reason: str
    cycle_result: CycleResult | None = None
    ship_decision: ShipDecision | None = None
    tail: TailResult | None = None
    # The PR unit this outcome is for -- carried so the plan-level teardown
    # report (`reasona_dev.plan_report`) can compare what the unit DECLARED
    # in its `files:` against what `TailResult.changed_files` says it
    # actually touched. Optional so a hand-built outcome in a test need not
    # supply one.
    unit: PRUnit | None = None


@dataclass
class PlanRunResult:
    outcomes: list[UnitOutcome] = field(default_factory=list)

    @property
    def shipped(self) -> list[UnitOutcome]:
        return [o for o in self.outcomes if o.status == "shipped"]

    @property
    def failed(self) -> list[UnitOutcome]:
        """Review/scan actually evaluated this unit's code and it did not
        meet the bar -- a genuine defect, distinct from `blocked` (see that
        property)."""
        return [o for o in self.outcomes if o.status == "failed"]

    @property
    def blocked(self) -> list[UnitOutcome]:
        """Something outside code-quality judgment stopped this unit: `gh`
        unavailable, a merge conflict or acceptance failure whose bounded
        dev-fix budget ran out, an ERROR/INCONCLUSIVE role, or the final
        phase not settling within `MAX_FINAL_PHASE_ROUNDS`. Not the same as
        `failed` -- a `blocked` unit's code was never actually judged
        deficient, so re-running the exact same command later (once the
        blocker clears) is the right response, not editing the plan."""
        return [o for o in self.outcomes if o.status == "blocked"]

    @property
    def skipped(self) -> list[UnitOutcome]:
        return [o for o in self.outcomes if o.status == "skipped"]

    @property
    def passed(self) -> bool:
        return bool(self.outcomes) and all(o.status == "shipped" for o in self.outcomes)

    def render(self) -> str:
        lines = [
            f"plan run: {len(self.shipped)} shipped, "
            f"{len(self.failed)} failed, {len(self.blocked)} blocked, "
            f"{len(self.skipped)} skipped"
        ]
        for o in self.outcomes:
            lines.append(f"  [{o.status:>7}] {o.stage_name} ({o.profile}): {o.reason}")
        return "\n".join(lines)


def plan_upstream_warning(workdir: str | Path, plan_path: str | Path, *, base: str = "origin/main") -> str | None:
    """dev-ralf preflight P2 ("plan already merged into `base`"),
    downgraded here from a hard ABORT to a warning. `worktree.py`'s
    `ensure_unit_worktree()` always cuts a unit's worktree from `base`, so
    anything a dispatched role reads from the checked-out TREE is invisible
    if the plan is only local -- but `pr_cycle.py`'s `_pr_unit_context_block()`
    (A-1) now passes each unit's own PR-section text by value, not by
    reading the plan file from the worktree, which removed the primary
    reason dev-ralf hard-blocks on this. The remaining risk is narrower:
    something ELSE the plan implicitly relies on being present in the tree
    (a referenced doc, a schema file the plan describes but does not
    declare in `files:`). A hard ABORT would also wrongly refuse a
    perfectly normal case dev-ralf's own worker never has to handle:
    `--workdir` pointing at a repo with no pushed `origin/main` state for
    this plan at all, e.g. a plan still being iterated on locally.

    Returns `None` when the check passes OR cannot be meaningfully run
    (`plan_path` outside `workdir`, no `base` ref, `workdir` not a git repo)
    -- silence, not a false alarm, is the safe default for an advisory.
    """
    workdir = Path(workdir)
    try:
        rel = Path(plan_path).resolve().relative_to(workdir.resolve())
    except ValueError:
        return None
    code, _out, _err = _shell.run(["git", "cat-file", "-e", f"{base}:{rel.as_posix()}"], workdir, timeout=15)
    if code == 0:
        return None
    return (
        f"plan file {rel} is not visible on {base} yet -- every PR unit's worktree is cut "
        f"from {base}, so anything this plan relies on besides its own PR sections (already "
        "passed by value, per-unit) may be invisible to a dispatched role. Not blocking -- "
        "merge the plan first if a unit's cycle-0/review seems to be missing context."
    )


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

    # B-1: the Open Decisions Gate -- dev-ralf's own hard blocker
    # (worker.md), never ported until now even though plan-ralf's own
    # Report already tells the human reasona-dev enforces it. Every entry
    # in `## Open decisions (human)` must carry `decided: <choice>` before
    # a single agent is dispatched -- a `default-if-unresolved` silently
    # taking effect because nobody decided is exactly the failure this
    # blocks, and it must fire before ANY unit's cycle-0, not just the one
    # a decision happens to be about (a decision left in the plan body,
    # not scoped to one PR, may bear on units the author never connected
    # it to).
    undecided = open_decisions.undecided_entries(plan_text)
    if undecided:
        listed = "\n  - ".join(open_decisions.entry_summary(e) for e in undecided)
        raise PlanError(
            f"plan has {len(undecided)} undecided entr{'y' if len(undecided) == 1 else 'ies'} "
            f"in `## Open decisions (human)` -- add `decided: <choice>` to each before running "
            f"(even choosing the printed default is a decision that must be recorded):\n  - {listed}"
        )

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


def _shares_source_files(a: UnitPlan, b: UnitPlan) -> bool:
    """dev-ralf's implicit DAG edge (execution-plan.md): two PR units that
    both declare the SAME source file (not docs/config) in `files:`, with
    no explicit `depends_on` between them, are serialized -- `job=1`'s
    sequential scheduler already gets this for free (declaration order),
    but `job>1`'s concurrent scheduler (`_run_units_concurrently()`)
    dispatches purely by dependency-readiness and would otherwise let two
    units edit the same source file in two worktrees at once. Uses the
    SAME `_SOURCE_EXT` list `plan_report.py`'s own advisory does (itself
    dev-ralf `scope_report.py`'s `SOURCE_EXT`), so the plan Report's "these
    two share a file" note and this scheduling guard never disagree on
    what counts as source.
    """
    a_src = {p for p in a.unit.files if Path(p).suffix.lower() in _SOURCE_EXT}
    b_src = {p for p in b.unit.files if Path(p).suffix.lower() in _SOURCE_EXT}
    return bool(a_src & b_src)


def _shipped_on_github(merged_pr_titles: dict[str, tuple[int, str]], unit: PRUnit) -> tuple[int | None, str | None]:
    """B-4: a lightweight fallback for `ledger.unit_status()` -- when the
    local ledger does not know a unit shipped (lost, cleared by
    `--restart`, or simply never seen on this machine), check whether
    `merged_pr_titles` (ONE `gh_pr.list_merged_pr_titles()` fetch per
    `run_plan()` call, not per unit -- see that function's own docstring)
    already shows a MERGED PR with this unit's exact title before
    re-developing it from scratch. Pure dict lookup, no `gh` call of its
    own, safe to call for every unit on every run.
    """
    unit_type, subject = gh_pr.resolve_type_subject(unit)
    title = gh_pr.build_pr_title(unit_type, subject)
    return merged_pr_titles.get(title, (None, None))


def _blocking_dependency(unit: UnitPlan, outcomes: dict[str, UnitOutcome], known: set[str]) -> str | None:
    for dep in unit.unit.depends_on:
        if dep not in known:
            continue  # merged in an earlier plan -- see order_units()
        outcome = outcomes.get(dep)
        if outcome is not None and outcome.status != "shipped":
            return dep
    return None


def dispatch_unit_cycle0(
    *,
    workdir: Path,
    worktree_path: Path,
    plan_name: str,
    plan_text: str,
    only_index: str,
    dev_flag: str | None = None,
    policy_flags: dict[str, str] | None = None,
    port: int = 8052,
) -> tuple[bool, str]:
    """Compile a single-unit plan.yaml (`only_index`) and dispatch it into
    `worktree_path` -- this unit's own worktree, not the top-level repo. The
    compiled plan.yaml itself is written under the top-level `workdir`'s log
    directory (alongside this unit's other run artifacts), even though it
    gets DISPATCHED against the worktree; only the git checkout moves, not
    where logs/ledger live (`reasona_dev.ledger`'s own layout is unaffected).

    Returns `(ok, reason)`.
    """
    plan_yaml_path = ledger.run_dir(workdir, plan_name) / only_index / "plan.yaml"
    plan_yaml_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_plan_yaml(
            plan_text, str(plan_yaml_path),
            plan_name=plan_name, description=f"Compiled from plan (PR {only_index})",
            dev_flag=dev_flag, workdir=worktree_path, policy_flags=policy_flags,
            only_index=only_index,
        )
    except PlanError as exc:
        return False, str(exc)
    dispatch = bernstein_dispatch.run_plan_file(plan_yaml_path, worktree_path, port=port)
    if dispatch.returncode != 0:
        return False, (
            f"dev cycle-0 failed (bernstein run exit {dispatch.returncode}): {dispatch.stderr_tail}"
        )
    return True, "ok"


def _process_unit(
    *,
    up: UnitPlan,
    workdir: Path,
    plan_name: str,
    plan_text: str,
    resolved: dict[str, ResolvedModel],
    log_base: Path,
    port: int,
    base: str,
    ship: bool,
    merge: bool,
    resume: bool,
    skip_dev: bool,
    dev_flag: str | None,
    policy_flags: dict[str, str] | None,
    gh_review_max_wait_seconds: int,
    ensure_worktree_fn,
    remove_worktree_fn,
    dispatch_cycle0_fn,
    run_pr_cycle_fn,
    ship_gate_fn,
    final_stage_fn,
) -> UnitOutcome:
    """Worktree -> (conditional) cycle-0 -> review/scan -> (conditional) ship
    tail, for ONE unit already known to be ready to run (caller has already
    resolved the "skip: unresolved dependency" / "resumed: already shipped"
    cases -- those never reach here, see `run_plan()`'s two callers of this
    function). Always terminal: returns a `UnitOutcome`, records it to the
    ledger itself (when `resume`), never raises for anything this project's
    own gates would classify as `blocked`/`failed`.

    Pulled out of `run_plan()`'s loop body so both the sequential path
    (`job=1`, the default) and the concurrent scheduler (`job>1`,
    `_run_units_concurrently()`) share the exact same per-unit logic --
    concurrency changes ONLY how many of these run at once and which `port`
    each gets, never what happens inside one.
    """
    try:
        unit_workdir, _branch = ensure_worktree_fn(workdir, plan_name, up.stage_name, base=base)
    except RuntimeError as exc:
        # Cannot even get a checkout for this unit -- outside
        # code-quality judgment entirely, same class as `gh`
        # unavailable (§ final_phase.py's blocked/failed split).
        outcome = UnitOutcome(
            unit=up.unit, stage_name=up.stage_name, profile=up.profile, status="blocked",
            reason=f"worktree: {exc}",
        )
        if resume:
            ledger.mark_unit_terminal(
                workdir, plan_name, up.stage_name, status=outcome.status, reason=outcome.reason,
            )
        return outcome

    dev_needed = not skip_dev and not (
        resume and ledger.dev_already_dispatched(workdir, plan_name, up.stage_name)
    )
    if dev_needed:
        ok, reason = dispatch_cycle0_fn(
            workdir=workdir, worktree_path=unit_workdir, plan_name=plan_name,
            plan_text=plan_text, only_index=up.index,
            dev_flag=dev_flag, policy_flags=policy_flags, port=port,
        )
        if not ok:
            outcome = UnitOutcome(
                unit=up.unit, stage_name=up.stage_name, profile=up.profile, status="blocked",
                reason=reason,
            )
            if resume:
                ledger.mark_unit_terminal(
                    workdir, plan_name, up.stage_name, status=outcome.status, reason=outcome.reason,
                )
            return outcome
        # N-A: worker.md -> *Develop & review*, Cycle 0: "① dispatch the
        # skeleton ② verify $CI_FAST is green -- else PR ABORT." This was
        # the one hole B-5's own coverage left: `ci_gate` only reached the
        # review/scan/final-audit fix loops (`pr_cycle._run_dev_fix()`) and
        # the pre-`/gh-pr` full gate, never cycle-0 itself, so a skeleton
        # that does not even compile used to sail straight into review. No
        # revert here (unlike a later fix cycle) -- cycle-0 IS the first
        # commit on this unit's branch, there is no prior state to revert
        # to, and worker.md itself does not revert on this failure either,
        # it aborts.
        #
        # `mark_dev_dispatched` is deliberately NOT called until AFTER this
        # gate passes: marking it right after `dispatch_cycle0_fn()` (as an
        # earlier version of this function did) would make a RESUMED run,
        # after a cycle-0-CI-blocked unit, see `dev_already_dispatched() ==
        # True` and skip cycle-0 (and this very gate) entirely on retry --
        # sending a still-broken skeleton straight into review.
        ci_fast_command = config_file.resolve_ci_command(
            "fast", config_file.load_project(workdir), config_file.load_global(),
        )
        ci_ok, ci_tail = ci_gate.run_fast(unit_workdir, ci_fast_command, pre_fix_head=None)
        if not ci_ok:
            outcome = UnitOutcome(
                unit=up.unit, stage_name=up.stage_name, profile=up.profile, status="blocked",
                reason=f"cycle-0 CI failed: {ci_tail[-500:]}",
            )
            if resume:
                ledger.mark_unit_terminal(
                    workdir, plan_name, up.stage_name, status=outcome.status, reason=outcome.reason,
                )
            return outcome

        if resume:
            ledger.mark_dev_dispatched(workdir, plan_name, up.stage_name)

    def _dispatch_cycle() -> CycleResult:
        return run_pr_cycle_fn(
            workdir=unit_workdir,
            repo_workdir=workdir,
            pr_title=f"PR {up.index}: {up.title}",
            resolved=resolved,
            rundir=log_base / up.stage_name,
            profile=up.profile,
            stage_name=up.stage_name,
            plan_name=plan_name,
            resume=resume,
            files=up.unit.files,
            pr_index=up.index,
            pr_section=up.unit.section,
            port=port,
        )

    cycle = _dispatch_cycle()
    outcome: UnitOutcome | None = None
    resync_rounds = 0
    while outcome is None:
        if cycle.verdict not in ("PASS", "PASS_WITH_NOTES"):
            # ABORT (role/model unavailable, or an INCONCLUSIVE role's
            # retry budget ran out -- verification never actually ran)
            # is `cycle_gate.evaluate()`'s own "environment problem, not
            # a code one" case (see pr_cycle.py's review/scan branches
            # on this), so it reports `blocked`, not `failed`.
            status = "blocked" if cycle.verdict == "ABORT" else "failed"
            outcome = UnitOutcome(
                unit=up.unit, stage_name=up.stage_name, profile=up.profile, status=status,
                reason=f"{cycle.stage}: {cycle.reason}", cycle_result=cycle,
            )
            break

        tail: TailResult | None = None
        if ship:
            # ship_gate itself now runs INSIDE the final stage, after
            # sync and final_audit have both settled -- see
            # final_phase.run_final_phase() on why it can no longer be
            # evaluated up front here.
            tail = final_stage_fn(
                workdir=unit_workdir, repo_workdir=workdir, stage_name=up.stage_name,
                pr_title=f"{up.title}", unit_type=up.unit.unit_type, unit=up.unit,
                profile=up.profile, resolved=resolved,
                rundir=log_base / up.stage_name,
                cycle_verdict=cycle.verdict, ship_gate_fn=ship_gate_fn,
                budget=cycle.budget or FixBudget(),
                recurrence=cycle.recurrence or RecurrenceTracker(),
                base=base, merge=merge,
                plan_name=plan_name if resume else None,
                gh_review_max_wait_seconds=gh_review_max_wait_seconds,
                port=port,
            )
            if tail.status == final_phase_mod.NEEDS_REVIEW and resync_rounds < MAX_SUBSTANTIVE_RESYNC_ROUNDS:
                # `run_sync_cycle()` resolved a SUBSTANTIVE conflict this
                # unit's own review/scan never saw (worker.md's mechanical/
                # substantive rule, docs/ARCHITECTURE.md §3.14.4) -- gh-pr/
                # gh-review/squash-merge never ran. Re-review from scratch
                # (clearing the stale checkpoint, so `run_pr_cycle_fn`
                # cannot resume into an already-"passed" phase and skip the
                # re-review this exists to force), then retry the final
                # stage. Bounded: a target repo whose base keeps moving
                # faster than this can settle is not something retrying
                # indefinitely would fix.
                resync_rounds += 1
                if resume:
                    ledger.clear_progress(workdir, plan_name, up.stage_name)
                cycle = _dispatch_cycle()
                continue
            decision = tail.ship_decision
            if tail.status == final_phase_mod.MERGED:
                # Shipped -- the worktree has done its job. A
                # failed/blocked unit's worktree is left in place
                # deliberately (module docstring on why).
                remove_worktree_fn(workdir, plan_name, up.stage_name)
        else:
            # No `--ship`: no sync, no final_audit, no PR, no merge --
            # just the review/scan verdict's own preview of whether
            # ship_gate would pass right now. Not the authoritative
            # check (that only exists inside the merge tail, on
            # post-sync/post-audit code), but a real signal costs
            # nothing extra here since nothing merges either way.
            decision = ship_gate_fn(unit_workdir, up.stage_name, cycle_verdict=cycle.verdict, log_workdir=workdir)
        if tail is not None and tail.status == final_phase_mod.NEEDS_REVIEW:
            # The resync bound above was exhausted and it is still
            # substantive -- report it, don't silently proceed to
            # gh-pr/gh-review/squash-merge on unreviewed code.
            status, reason = "blocked", (
                f"{tail.reason} (exhausted {MAX_SUBSTANTIVE_RESYNC_ROUNDS} re-review round(s))"
            )
        elif tail is not None and tail.blocked:
            # Every non-passing outcome inside the final phase (gh
            # unavailable, a sync conflict or ship-gate fix budget
            # exhausted, final_audit failing, non-convergence) is
            # `blocked`, never `failed` -- see final_phase.py's
            # module docstring and `cycle_gate.MAX_SHIP_CYCLES`.
            status, reason = "blocked", tail.reason
        elif tail is not None:
            status, reason = "shipped", tail.reason
        else:
            status = "shipped" if decision.passed else "failed"
            reason = decision.reason
        outcome = UnitOutcome(
            unit=up.unit, stage_name=up.stage_name, profile=up.profile,
            status=status, reason=reason,
            cycle_result=cycle, ship_decision=decision, tail=tail,
        )
    if resume:
        ledger.mark_unit_terminal(
            workdir, plan_name, up.stage_name, status=outcome.status, reason=outcome.reason,
        )
    return outcome


def _run_units_concurrently(
    *,
    units: list[UnitPlan],
    job: int,
    workdir: Path,
    plan_name: str,
    plan_text: str,
    resolved: dict[str, ResolvedModel],
    log_base: Path,
    port: int,
    base: str,
    ship: bool,
    merge: bool,
    resume: bool,
    skip_dev: bool,
    dev_flag: str | None,
    policy_flags: dict[str, str] | None,
    gh_review_max_wait_seconds: int,
    ensure_worktree_fn,
    remove_worktree_fn,
    dispatch_cycle0_fn,
    run_pr_cycle_fn,
    ship_gate_fn,
    final_stage_fn,
    known: set[str],
    merged_pr_titles: dict[str, tuple[int, str]],
) -> list[UnitOutcome]:
    """Bounded-concurrency topological scheduler: up to `job` units run at
    once via `_process_unit()`, each on its own TCP port (`port, port+1,
    ..., port+job-1`, round-robin as units finish) so two concurrently
    running `bernstein run` dispatches never collide. A unit is submitted
    the moment its dependencies are known (shipped or outside this run's
    `known` set) -- not in a synchronized round, so a fast 2-cycle unit and
    a slow 6-cycle one never block each other.

    `ledger.json` is namespaced by `stage_name` under the shared log dir
    (`reasona_dev.ledger.unit_dir()`) -- two units never write the same
    ledger path, so nothing to lock there. `cycles.jsonl` and `.reasona/
    log/memory/*.md` are DIFFERENT: both are deliberately a single
    repo-wide file/directory, not per-unit (§3.18 -- they have to survive
    `worktree.remove_unit_worktree()` deleting a shipped unit's own
    worktree, so they were moved OFF unit-scoped paths entirely). Under
    `job>1`, multiple units' threads append to the SAME `cycles.jsonl`
    concurrently. Still safe without a lock: `cycles_log.record_*()` opens
    the file in append mode and writes each record as ONE `f.write()` call
    -- POSIX guarantees an `O_APPEND`-mode write of that size is atomic, so
    concurrent writers interleave whole lines, never partial ones.
    `memory.regenerate()` recomputing `.reasona/log/memory/*.md` from
    `cycles.jsonl` concurrently is a real, accepted race (two threads can
    each read a slightly different snapshot and the last writer's version
    wins) -- benign because it is a full, deterministic recomputation, not
    a merge: the NEXT unit's `regenerate()` call (there is always a next
    one, or the run is ending) recomputes from the by-then-complete file
    and corrects any staleness, so nothing is ever silently wrong for long.
    The only OTHER state genuinely shared between threads is this
    function's own in-memory `by_index` (read by `_blocking_dependency()`
    to decide what is ready next), guarded by `_lock`.
    """
    import threading
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    lock = threading.Lock()
    by_index: dict[str, UnitOutcome] = {}
    index_of = {u.index: i for i, u in enumerate(units)}
    units_by_index = {u.index: u for u in units}
    outcomes: list[UnitOutcome | None] = [None] * len(units)
    free_ports: list[int] = list(range(port, port + job))
    pending: list[UnitPlan] = list(units)
    in_flight: dict[str, tuple] = {}  # unit index -> (Future, port)

    def _settle(up: UnitPlan, outcome: UnitOutcome) -> None:
        with lock:
            by_index[up.index] = outcome
        outcomes[index_of[up.index]] = outcome

    def _deps_resolved(up: UnitPlan) -> bool:
        """True once every known dependency has actually FINISHED (is in
        `by_index`) -- `_blocking_dependency()` alone is not enough here:
        it treats a dependency simply absent from `by_index` the same as
        "no dependency" (safe in the sequential loop, where topological
        order guarantees every earlier unit already finished by the time a
        later one is considered -- NOT safe here, where a known dependency
        may still be in flight or not yet even submitted). A unit whose
        dependency has not finished yet must wait, not be treated as ready.
        """
        return all(dep not in known or dep in by_index for dep in up.unit.depends_on)

    def _resolve_immediately(up: UnitPlan) -> bool:
        """Skip/already-shipped units never occupy a worker slot or spend a
        port -- same as the sequential path, these are decided from state
        already on disk (the ledger), not from a dispatch. Only called once
        `_deps_resolved(up)` is True."""
        blocked_by = _blocking_dependency(up, by_index, known)
        if blocked_by is not None:
            _settle(up, UnitOutcome(
                unit=up.unit, stage_name=up.stage_name, profile=up.profile, status="skipped",
                reason=f"dependency PR {blocked_by} did not ship",
            ))
            return True
        if resume and ledger.unit_status(workdir, plan_name, up.stage_name) == "shipped":
            _settle(up, UnitOutcome(
                unit=up.unit, stage_name=up.stage_name, profile=up.profile, status="shipped",
                reason="resumed: already shipped in an earlier run of this plan",
            ))
            return True
        if resume:
            gh_pr_num, gh_pr_url = _shipped_on_github(merged_pr_titles, up.unit)
            if gh_pr_num is not None:
                _settle(up, UnitOutcome(
                    unit=up.unit, stage_name=up.stage_name, profile=up.profile, status="shipped",
                    reason=f"GitHub already shows PR #{gh_pr_num} merged for this unit ({gh_pr_url}) -- the ledger did not know",
                ))
                return True
        return False

    with ThreadPoolExecutor(max_workers=job) as executor:
        while pending or in_flight:
            still: list[UnitPlan] = []
            for up in pending:
                if _deps_resolved(up) and _resolve_immediately(up):
                    continue
                still.append(up)
            pending = still

            still = []
            for up in pending:
                # B-3: the implicit DAG edge -- never dispatch a unit
                # alongside another currently IN-FLIGHT unit that shares a
                # declared source file, even with no explicit `depends_on`
                # between them (`_shares_source_files()`). This unit simply
                # waits its turn on the next round rather than being
                # skipped or failed; it is not blocked forever, only until
                # the conflicting unit finishes and frees its slot.
                source_conflict = any(
                    _shares_source_files(up, units_by_index[idx]) for idx in in_flight
                )
                if free_ports and _deps_resolved(up) and not source_conflict:
                    unit_port = free_ports.pop()
                    fut = executor.submit(
                        _process_unit, up=up, workdir=workdir, plan_name=plan_name,
                        plan_text=plan_text, resolved=resolved, log_base=log_base,
                        port=unit_port, base=base, ship=ship, merge=merge, resume=resume,
                        skip_dev=skip_dev, dev_flag=dev_flag, policy_flags=policy_flags,
                        gh_review_max_wait_seconds=gh_review_max_wait_seconds,
                        ensure_worktree_fn=ensure_worktree_fn, remove_worktree_fn=remove_worktree_fn,
                        dispatch_cycle0_fn=dispatch_cycle0_fn, run_pr_cycle_fn=run_pr_cycle_fn,
                        ship_gate_fn=ship_gate_fn, final_stage_fn=final_stage_fn,
                    )
                    in_flight[up.index] = (fut, unit_port)
                else:
                    still.append(up)
            pending = still

            if not in_flight:
                # Nothing dispatched this round and nothing pending resolved
                # immediately either -- every remaining unit is blocked on a
                # dependency that is itself still pending, which cannot
                # happen after `order_units()`'s own cycle check. Guard
                # against an infinite loop anyway rather than trusting that.
                if pending:
                    raise PlanError(
                        "internal: no unit became ready this round "
                        f"(remaining: {', '.join(u.index for u in pending)})"
                    )
                break

            futures = {fut: idx for idx, (fut, _p) in in_flight.items()}
            done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
            for fut in done:
                idx = futures[fut]
                _fut, unit_port = in_flight.pop(idx)
                free_ports.append(unit_port)
                up = next(u for u in units if u.index == idx)
                _settle(up, fut.result())

    return [o for o in outcomes if o is not None]


def run_plan(
    *,
    workdir: str | Path,
    plan_name: str,
    plan_text: str,
    resolved: dict[str, ResolvedModel],
    rundir: str | Path | None = None,
    port: int = 8052,
    job: int = 1,
    base: str = "origin/main",
    head: str = "HEAD",
    ship: bool = False,
    merge: bool = False,
    from_pr: str | None = None,
    resume: bool = True,
    skip_dev: bool = False,
    dev_flag: str | None = None,
    policy_flags: dict[str, str] | None = None,
    gh_review_max_wait_seconds: int = gh_review_mod.DEFAULT_MAX_WAIT_SECONDS,
    ensure_worktree_fn=worktree.ensure_unit_worktree,
    remove_worktree_fn=worktree.remove_unit_worktree,
    dispatch_cycle0_fn=dispatch_unit_cycle0,
    run_pr_cycle_fn=run_pr_cycle,
    ship_gate_fn=ship_gate.evaluate,
    final_stage_fn=final_phase_mod.run_final_stage,
) -> PlanRunResult:
    """dev-0 -> review -> scan -> ship, per unit, in dependency order, each
    in its own git worktree (see module docstring).

    **`job`** (default 1 here -- a conservative library default for a bare
    Python call; `cli.py`'s own `--job` flag defaults to 4 instead, matching
    dev-ralf, since the CLI is the ergonomic entry point most operators
    actually use) bounds how many PR units run AT ONCE. `job>1` uses
    `_run_units_concurrently()`: a topological scheduler that dispatches a
    unit the moment its dependencies are known, not in synchronized rounds,
    each on its own TCP port so concurrent `bernstein run` dispatches never
    collide (`port` through `port+job-1`). Independent units (no shared
    `depends_on` edge) genuinely overlap; a unit whose dependency is still
    in flight simply waits its turn, same topological order as `job=1`
    would produce, just not necessarily the same WALL-CLOCK order of
    completion (`result.outcomes`' order still matches the plan's
    topological order, not completion order -- see `_run_units_concurrently`).

    **`skip_dev`** force-skips cycle-0 dispatch for every unit regardless of
    the ledger -- for the rare case a unit's worktree/cycle-0 was set up by
    hand. `dev_flag`/`policy_flags` are the raw CLI flag layer
    (`cli.py`'s `_collect_flags()`), threaded through to
    `dispatch_unit_cycle0()`'s `plan_compile.compile_to_bernstein_plan()`
    call the same way `compile-plan` uses them.

    **Automatic resume (`resume=True`, the default).** Before dispatching a
    unit, its ledger (`reasona_dev.ledger`) is checked -- a unit already
    marked `shipped` from a prior run of this same plan is reused as-is
    (no re-dispatch), so re-running `run_plan` after an interruption picks
    up at the first unit that has not shipped yet, automatically. A unit
    that actually ships in THIS run is recorded the same way, for the next
    resume. Pass `resume=False` to force every unit to run fresh regardless
    of what an earlier run recorded (`--restart` at the CLI).

    `from_pr` is a manual override of the same idea: every unit ordered
    BEFORE the one named by `from_pr` is dropped from this run entirely,
    not re-attempted and not reported, regardless of ledger state. This
    reuses `_blocking_dependency`'s existing rule for a dependency this run
    never touches -- a unit outside `known` is treated as already merged.
    Useful when the ledger itself is unavailable or wrong.

    `plan_name` namespaces every ledger/run-output path under
    `<workdir>/.reasona/log/dev/<plan_name>/<stage_name>/` (`reasona_dev.ledger`)
    -- two plans that both happen to name a unit `pr-1` (a common name,
    since `plan_compile._stage_name()` is just `f"pr-{index}"`) do not
    share files or corrupt each other's resume state. `rundir` overrides
    the log base directory (rarely needed); it defaults to
    `reasona_dev.ledger.run_dir(workdir, plan_name)`.

    Every `*_fn` is injectable purely for testing; production callers pass
    none of them.
    """
    workdir = Path(workdir)
    log_base = Path(rundir) if rundir is not None else ledger.run_dir(workdir, plan_name)
    units = order_units(resolve_plan_units(plan_text, workdir))
    result = PlanRunResult()
    if not units:
        return result

    if from_pr is not None:
        positions = {u.index: i for i, u in enumerate(units)}
        if from_pr not in positions:
            raise PlanError(
                f"--from-pr {from_pr!r} does not match any PR unit in this plan "
                f"(have: {', '.join(u.index for u in units)})"
            )
        units = units[positions[from_pr]:]

    known = {u.index for u in units}
    # B-4: ONE `gh pr list --state merged` fetch for the whole run (never
    # per unit -- see `gh_pr.list_merged_pr_titles()`), only when `resume`
    # is even in play; a fresh (`resume=False`) run has nothing to recover
    # and must not pay this network cost.
    merged_pr_titles = gh_pr.list_merged_pr_titles(workdir) if resume else {}

    if job > 1:
        result.outcomes = _run_units_concurrently(
            units=units, job=job, workdir=workdir, plan_name=plan_name, plan_text=plan_text,
            resolved=resolved, log_base=log_base, port=port, base=base, ship=ship, merge=merge,
            resume=resume, skip_dev=skip_dev, dev_flag=dev_flag, policy_flags=policy_flags,
            gh_review_max_wait_seconds=gh_review_max_wait_seconds,
            ensure_worktree_fn=ensure_worktree_fn, remove_worktree_fn=remove_worktree_fn,
            dispatch_cycle0_fn=dispatch_cycle0_fn, run_pr_cycle_fn=run_pr_cycle_fn,
            ship_gate_fn=ship_gate_fn, final_stage_fn=final_stage_fn, known=known,
            merged_pr_titles=merged_pr_titles,
        )
        return result

    by_index: dict[str, UnitOutcome] = {}
    for up in units:
        blocked_by = _blocking_dependency(up, by_index, known)
        if blocked_by is not None:
            outcome = UnitOutcome(
                unit=up.unit, stage_name=up.stage_name, profile=up.profile, status="skipped",
                reason=f"dependency PR {blocked_by} did not ship",
            )
            result.outcomes.append(outcome)
            by_index[up.index] = outcome
            continue

        if resume and ledger.unit_status(workdir, plan_name, up.stage_name) == "shipped":
            outcome = UnitOutcome(
                unit=up.unit, stage_name=up.stage_name, profile=up.profile, status="shipped",
                reason="resumed: already shipped in an earlier run of this plan",
            )
            result.outcomes.append(outcome)
            by_index[up.index] = outcome
            continue

        if resume:
            gh_pr_num, gh_pr_url = _shipped_on_github(merged_pr_titles, up.unit)
            if gh_pr_num is not None:
                outcome = UnitOutcome(
                    unit=up.unit, stage_name=up.stage_name, profile=up.profile, status="shipped",
                    reason=f"GitHub already shows PR #{gh_pr_num} merged for this unit ({gh_pr_url}) -- the ledger did not know",
                )
                result.outcomes.append(outcome)
                by_index[up.index] = outcome
                continue

        outcome = _process_unit(
            up=up, workdir=workdir, plan_name=plan_name, plan_text=plan_text,
            resolved=resolved, log_base=log_base, port=port, base=base, ship=ship, merge=merge,
            resume=resume, skip_dev=skip_dev, dev_flag=dev_flag, policy_flags=policy_flags,
            gh_review_max_wait_seconds=gh_review_max_wait_seconds,
            ensure_worktree_fn=ensure_worktree_fn, remove_worktree_fn=remove_worktree_fn,
            dispatch_cycle0_fn=dispatch_cycle0_fn, run_pr_cycle_fn=run_pr_cycle_fn,
            ship_gate_fn=ship_gate_fn, final_stage_fn=final_stage_fn,
        )
        result.outcomes.append(outcome)
        by_index[up.index] = outcome
    return result
