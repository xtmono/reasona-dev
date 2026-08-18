"""Drives one PR unit through dev-ralf's actual cycle -- worker.md's
"Pipeline you run": ``develop -> verify (max 8 cycles) -> bug+compliance
scan (parallel, max 8 cycles)``. The `sync-main -> /gh-pr -> /gh-review ->
up-to-date gate -> conditional final audit -> squash-merge` tail is out of
scope here (`reasona_dev.ship_gate` picks up after this returns).

**Why this exists instead of a bigger `plan.yaml`.** The number of review
cycles, whether bugbot+compliance even run, and whether a MUST_FIX
recurrence earns a bounded model escalation are all facts only known AFTER
a role's actual output is parsed -- this can't be predeclared as a fixed
`plan.yaml` DAG (docs/ARCHITECTURE.md §1). Bernstein itself has no hook that
can safely create a follow-up task from inside a running request: pluggy's
`on_task_completed` fires synchronously from within the task server's own
request handler (`task_crud.py`), so a same-process HTTP call back to
`/tasks` from inside it risks event-loop reentrancy -- untested and not
worth risking. Instead this driver runs OUTSIDE Bernstein, one level up:
`run_pr_cycle()` starts ONE persistent Bernstein server
(`reasona_dev.bernstein_server.start_server()`) for the whole cycle, then
each role dispatch is a `POST /tasks` against it (`run_role()`), polled via
`GET /tasks/{id}`. The agent's structured output comes back through a file
the task description tells it to write -- see `bernstein_server.py`'s
module docstring for why that handoff survives the move to HTTP.

**Three budget mechanisms, three different failure modes.** The 8/8/16
cycle caps this inherited from dev-ralf are a ceiling, not a cost; what
actually spends money is how expensive each cycle is and how many of them
run before a doomed PR gives up. Each is addressed by a different piece:

- *Cost per cycle* -- `cycle_gate.recheck_route()`. When a dev fix touched
  only files that were already named in the findings it was fixing, the
  next pass is a BOUNDED recheck (confirm + regression, `recheck.md`,
  the cheaper `recheck` model) instead of a full omission hunt. Scan roles
  get the same treatment through a scope suffix rather than a separate
  prompt, since their prompts come from an external skill contract.
- *Number of cycles* -- `cycle_gate.ConvergenceTracker`. `RecurrenceTracker`
  only fires when the SAME finding survives, so a PR emitting fresh
  findings every cycle used to burn the whole stage cap. Non-convergence
  now exits at 3 cycles.
- *Which rule ended it* -- `cycles_log`. Every dispatch and every gate
  decision is recorded, so the caps above can eventually be re-derived from
  measurement instead of inherited on faith.

**Profiles are per PR unit, not per repo.** `profile` is a parameter here
rather than something this driver resolves, because the resolution needs
the unit's declared `files:` and the repo's `dev-profile-map:` -- both of
which `plan_compile` already holds, and which it validates at compile time
so a two-language unit is refused while the author still has the plan open.
See `prompt_profile.resolve_unit_profile()`.

**Human approval.** `approval_required` maps onto Bernstein's own per-task
gate. It is deliberately a caller-supplied flag rather than something this
module decides: the argument for gating is that the FIRST PR of a plan
fixes contract shapes every later PR inherits, and this function sees one
PR unit at a time and cannot know which one that is. Note the scope limit
-- it gates the dev fix dispatches this driver makes, not the eventual
squash-merge, because the merge tail is not built yet (README "Next").

**Not yet live-verified.** Every individual HTTP primitive `run_role()`
uses was checked against a real running Bernstein server; this driver's OWN
composition of them -- one server serving every role dispatch across a
whole cycle -- has not been run end-to-end against a live paid server yet.
Treat `run_role()` as the one unverified boundary; everything above it is
plain, already-tested Python.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from reasona_dev import cycles_log, memory
from reasona_dev.bernstein_server import ServerHandle, dispatch_task, poll_task, start_server, stop_server
from reasona_dev.cycle_gate import (
    ConvergenceTracker,
    FixBudget,
    RecurrenceTracker,
    evaluate,
    recheck_route,
)
from reasona_dev.finding_adapter import (
    ReviewResult,
    RoleStatus,
    merge,
    parse_role_output,
)
from reasona_dev.model_config import ResolvedModel
from reasona_dev.prompt_profile import available_profiles, resolve_prompt

# The wire shape is a property of the PROMPT, not the role -- see
# `finding_adapter.parse_role_output()`. This module used to keep a
# role -> parser table (`bugbot`/`compliance` -> KV), which was correct only
# for a profile that delegates those roles to dev-ralf's external skills.
# The packaged `generic` profile asks all of them for the `||` text
# contract, and the table turned that valid output into a hard scan abort.


@dataclass
class RoleRunResult:
    role: str
    cycle: int
    review_result: ReviewResult
    raw_output_path: Path


@dataclass
class CycleResult:
    verdict: str  # "PASS" | "PASS_WITH_NOTES" | "FAIL" | "ABORT" | "INCONCLUSIVE"
    stage: str  # "review" | "scan" -- which stage produced the terminal verdict
    reason: str
    review_cycles: int = 0
    scan_cycles: int = 0
    role_results: list[RoleRunResult] = field(default_factory=list)
    # Carried out so the merge tail can continue the SAME budget and
    # recurrence state rather than starting fresh. A final audit that got its
    # own budget would let a PR spend 8+8+2 fix cycles while every stage
    # reports itself within cap, and a fresh RecurrenceTracker would forget
    # that a finding the audit raises had already survived a fix earlier.
    budget: FixBudget | None = None
    recurrence: RecurrenceTracker | None = None


def _build_role_description(prompt: str, raw_output_path: Path) -> str:
    """Appends the file-handoff instruction to a role's prompt -- unchanged
    from the old plan-step description, just no longer embedded in a YAML
    file (see module docstring).
    """
    return (
        f"{prompt}\n\n"
        "---\n"
        "When you are completely done, write your ENTIRE output above "
        "(the markdown report AND the RESULT block, verbatim, nothing "
        f"added or removed) to the file `{raw_output_path}` as your "
        "final action, then stop."
    )


def run_role(
    *,
    server: ServerHandle,
    workdir: Path,
    role: str,
    title: str,
    prompt: str,
    model: ResolvedModel,
    rundir: Path,
    cycle: int,
    approval_required: bool = False,
) -> RoleRunResult:
    """Dispatch one role once against the shared, already-running
    `server`, via `POST /tasks`, then poll `GET /tasks/{id}` to completion.

    Returns `role_status=ERROR` (never a silent PASS) if the task never
    reaches `done` or the agent's output file never appears -- matches
    worker.md -> *RESULT parsing*: "Missing block ... -> cycle FAIL", not
    "treat as clean".
    """
    # ABSOLUTE, always. The agent runs inside a per-task git worktree, so a
    # relative path in its instructions resolves against THAT tree, not the
    # project root the driver reads from. Live symptom when this was
    # relative (`--workdir .`): the agent wrote
    # `.sdd/worktrees/<id>/.reasona/runs/pr-1/reviewer-c1.raw.txt`, spent its
    # remaining turns hunting for the file the driver was asking about, and
    # died on `error_max_turns` -- while the driver reported the role as
    # ERROR because nothing appeared where it looked.
    rundir = rundir.resolve()
    raw_output_path = rundir / f"{role}-c{cycle}.raw.txt"
    rundir.mkdir(parents=True, exist_ok=True)
    if raw_output_path.exists():
        raw_output_path.unlink()

    task_id = dispatch_task(
        server,
        role=role, title=title,
        description=_build_role_description(prompt, raw_output_path),
        model=model.model, effort=model.effort, cli=model.adapter,
        raw_output_path=raw_output_path,
        approval_required=approval_required,
    )
    task = poll_task(server, task_id, output_path=raw_output_path)

    # The FILE decides, not the status. A task can sit at `claimed` forever
    # through an upstream completion-payload bug (see `poll_task`), and a
    # task can reach `done` without the agent having written anything.
    # Only the second of those is an error here.
    if not raw_output_path.is_file():
        return RoleRunResult(
            role=role, cycle=cycle,
            review_result=ReviewResult(role_status=RoleStatus.ERROR),
            raw_output_path=raw_output_path,
        )

    text = raw_output_path.read_text(encoding="utf-8")
    return RoleRunResult(
        role=role, cycle=cycle,
        review_result=parse_role_output(text), raw_output_path=raw_output_path,
    )


def _render_findings(findings) -> list[str]:
    """One block per finding with every evidence field verbatim -- shared by
    the dev fix prompt and the bounded recheck prompt so both name findings
    the same way, and neither summarizes.
    """
    lines: list[str] = []
    for f in findings:
        loc = f.path + (f":{f.line}" if f.line else "") + (f" {f.symbol}" if f.symbol else "")
        lines.append(f"- [{f.severity.value if f.severity else '?'}] {loc}")
        if f.contract:
            lines.append(f"  || contract: {f.contract}")
        if f.scenario:
            lines.append(f"  || scenario: {f.scenario}")
        if f.fix:
            lines.append(f"  || fix: {f.fix}")
        if f.note:
            lines.append(f"  || note: {f.note}")
        lines.append("")
    return lines


def _build_fix_prompt(pr_title: str, findings) -> str:
    """worker.md -> *Loop control*: "dispatch dev (fix-cycle $DEV_PROMPT +
    `must_fix` list, `contract`/`scenario`/`fix` fields included
    verbatim)". Every field is passed through unedited -- this driver
    never rewrites or summarizes a finding on the way to dev.
    """
    return "\n".join(
        [
            f"Fix the following MUST_FIX findings from review on {pr_title}. "
            "Do not address anything not listed here.",
            "",
            *_render_findings(findings),
        ]
    )


def _build_recheck_prompt(recheck_profile_prompt: str, findings) -> str:
    """`recheck.md` + the exact findings to confirm.

    The prompt file states the BOUNDED contract (confirm + regression, no
    fresh omission hunt); this appends which findings are being confirmed,
    with the same verbatim evidence the fix dispatch received, so the
    recheck is judged against the same stated contract the fix was.
    """
    return "\n".join(
        [
            recheck_profile_prompt,
            "",
            "---",
            "Findings to CONFIRM (these are what the fix claimed to address):",
            "",
            *_render_findings(findings),
        ]
    )


def _bounded_scope_suffix(fix_files: set[str]) -> str:
    """Scope restriction for scan roles on a BOUNDED cycle.

    bugbot/compliance prompts come from an external skill contract, so
    they get a suffix rather than a separate bounded prompt file the way
    review does. The saving is the same: the role re-examines only what the
    fix touched instead of re-scanning the entire diff.
    """
    listed = "\n".join(f"- {p}" for p in sorted(fix_files))
    return (
        "\n\n---\n"
        "SCOPE RESTRICTION for this cycle: the previous fix touched only the "
        "files listed below, and every other file is unchanged since your "
        "last scan of it. Examine ONLY these files; do not re-report findings "
        "elsewhere.\n\n"
        f"{listed}\n"
    )


def _head_sha(workdir: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(workdir), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    return out.stdout.strip() or None


def _changed_files(workdir: Path, pre_fix_head: str) -> set[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(workdir), "diff", "--name-only", f"{pre_fix_head}..HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def _safe_recheck_route(workdir: Path, pre_fix_head: str | None, finding_files: set[str]) -> str:
    """`recheck_route()` with a conservative failure mode.

    Any condition that makes the routing question unanswerable -- no git
    repo, an unknown pre-fix HEAD, a failed diff -- returns FULL. Bounding a
    recheck is an optimization; doing it on an unverified premise would
    narrow the review's scope without evidence that narrowing is safe, which
    is the one direction this pipeline must never guess in.
    """
    if pre_fix_head is None or not finding_files:
        return "FULL"
    try:
        return recheck_route(str(workdir), pre_fix_head, finding_files)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "FULL"


def _finding_files(findings) -> set[str]:
    return {f.path for f in findings if f.path}


def _run_dev_fix(
    *,
    server: ServerHandle,
    workdir: Path,
    pr_title: str,
    findings,
    dev_model: ResolvedModel,
    escalated_model: str | None,
    rundir: Path,
    cycle: int,
    run_role_fn,
    approval_required: bool = False,
) -> RoleRunResult:
    """Dispatch one dev fix-cycle. `escalated_model` (from
    `GateDecision.escalated_model`) overrides `dev_model.model` for exactly
    this dispatch when set -- the bounded, logged, one-time escalation
    `cycle_gate.evaluate()` already decided on, never a silent swap.
    """
    model = dev_model if escalated_model is None else ResolvedModel(
        role="dev", model=escalated_model, adapter=dev_model.adapter,
        effort=dev_model.effort, source="cycle_gate:escalated",
    )
    return run_role_fn(
        server=server, workdir=workdir, role="backend", title=f"{pr_title} -- fix c{cycle}",
        prompt=_build_fix_prompt(pr_title, findings), model=model, rundir=rundir, cycle=cycle,
        approval_required=approval_required,
    )


def _missing_prompt_reason(role: str, profile: str, workdir: Path) -> str:
    """An abort message that says what to do about it.

    Prompts resolve through exactly two layers with nothing packaged
    underneath, so "not configured anywhere" is now a real and reachable
    state rather than an impossible one -- and the bare form of this message
    ("no review prompt for profile 'generic'") cannot be told apart from a
    typo'd profile name. Naming both searched paths and what IS present
    separates those two cases at the point of failure.
    """
    found = available_profiles(workdir)
    if found:
        have = "; ".join(f"{name} ({', '.join(roles)})" for name, roles in found.items())
        detail = f"available: {have}"
    else:
        detail = "no prompt profile found in either layer"
    return (
        f"no {role} prompt for profile {profile!r} -- searched "
        f"{workdir}/.reasona/prompts/{profile}/{role}.md then "
        f"~/.reasona/prompts/{profile}/{role}.md; {detail}"
    )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "pr"


def run_pr_cycle(
    *,
    workdir: str | Path,
    pr_title: str,
    resolved: dict[str, ResolvedModel],
    rundir: str | Path,
    profile: str,
    port: int = 8052,
    approval_required: bool = False,
    stage_name: str | None = None,
    files: list[str] | None = None,
    server: ServerHandle | None = None,
    run_role_fn=run_role,
    start_server_fn=start_server,
    stop_server_fn=stop_server,
) -> CycleResult:
    """develop -> verify(review) -> bug+compliance scan, worker.md-faithful.

    Assumes `dev`'s cycle-0 implementation already happened (this driver
    picks up at *Verify cycles* -- `plan_compile.py`'s own step covers
    cycle-0 development, gated by `$CI_FAST`-equivalent `completion_signals`
    the way it already is).

    One Bernstein server is started here, once, and shared across every
    role dispatch in this cycle (see module docstring) -- stopped in a
    `finally` so a mid-cycle exception never leaves it running.

    `server`, when supplied, is an already-running handle this function
    uses and does NOT stop -- see the comment at its use site.

    `run_role_fn`/`start_server_fn`/`stop_server_fn` are injectable purely
    for testing -- production callers never pass them.
    """
    workdir = Path(workdir)
    rundir = Path(rundir)
    stage_name = stage_name or _slug(pr_title)
    recurrence = RecurrenceTracker()
    review_budget = FixBudget()
    scan_budget = FixBudget()
    review_convergence = ConvergenceTracker()
    scan_convergence = ConvergenceTracker()
    # Carried across cycles, reset the moment a stage produces a conclusive
    # result. Passing a literal 0 (as this did) makes `evaluate`'s
    # INCONCLUSIVE branch unable to ever reach its own cap.
    review_inconclusive = 0
    scan_inconclusive = 0
    role_results: list[RoleRunResult] = []

    def _log(stage: str, cycle: int, result: RoleRunResult, model: ResolvedModel) -> None:
        cycles_log.record_dispatch(
            workdir=workdir, stage_name=stage_name, stage=stage, cycle=cycle,
            role=result.role, model=model.model, adapter=model.adapter,
            result=result.review_result,
        )

    def _log_decision(stage: str, cycle: int, decision) -> None:
        cycles_log.record_decision(
            workdir=workdir, stage_name=stage_name, stage=stage, cycle=cycle,
            action=decision.action, reason=decision.reason,
            escalated_model=decision.escalated_model,
        )

    # Priors derived from THIS repo's own recorded review history, scoped to
    # the files this unit declares (`memory.select`). Empty when the unit
    # declares no files, when nothing has recurred yet, or when nothing
    # intersects -- so a fresh repo and an unrelated PR both get an unchanged
    # prompt rather than a growing preamble.
    memory_block = memory.render_for_prompt(memory.select(workdir, files or []))

    review_profile_prompt = resolve_prompt("review", profile=profile, workdir=workdir)
    if review_profile_prompt is None:
        return CycleResult(
            verdict="ABORT", stage="review",
            reason=_missing_prompt_reason("review", profile, workdir),
        )
    review_profile_prompt += memory_block
    # Absent `recheck.md` is not fatal -- it only means every cycle stays
    # FULL, which is the pre-existing behaviour. A profile opts into the
    # cheaper path by shipping the file, and never silently gets a bounded
    # review it did not define the contract for.
    recheck_profile_prompt = resolve_prompt("recheck", profile=profile, workdir=workdir)

    # A caller running several PR units in a row (`reasona_dev.orchestrate`)
    # supplies one server for the whole plan. Same argument that moved role
    # dispatch off per-role subprocesses: the bootstrap is real work and
    # paying it once per unit is as arbitrary as paying it once per role.
    # Whoever creates the server also stops it -- this function never stops
    # one it did not start.
    owns_server = server is None
    if owns_server:
        server = start_server_fn(workdir, port=port)
    try:
        # --- Verify cycles (review), max 8 -- worker.md -> *Develop & verify* ---
        cycle = 0
        route = "FULL"
        pending_confirm: list = []
        while True:
            cycle += 1
            bounded = route == "BOUNDED" and recheck_profile_prompt is not None
            model = resolved["recheck"] if bounded else resolved["review"]
            prompt = (
                _build_recheck_prompt(recheck_profile_prompt, pending_confirm)
                if bounded else review_profile_prompt
            )
            result = run_role_fn(
                server=server, workdir=workdir, role="reviewer",
                title=f"{pr_title} -- {'recheck' if bounded else 'review'} c{cycle}",
                prompt=prompt, model=model, rundir=rundir, cycle=cycle,
            )
            role_results.append(result)
            _log("review", cycle, result, model)
            if cycle > 1:
                # This review followed a dev fix -- whatever MUST_FIX is still
                # here just SURVIVED that fix. Record it BEFORE evaluate() so
                # `RecurrenceTracker.decide()` sees the updated count (dev-ralf
                # §3.5: a key surviving one completed fix earns exactly one
                # bounded escalation before FAIL).
                recurrence.record_post_fix(result.review_result.must_fix)
            decision = evaluate(
                result.review_result, review_budget, "review", recurrence,
                inconclusive_attempts=review_inconclusive,
                escalation_model=resolved["dev_escalation"].model,
                convergence=review_convergence,
            )
            _log_decision("review", cycle, decision)
            if decision.action in ("pass",):
                break
            if decision.action in ("fail", "abort"):
                return CycleResult(
                    verdict="FAIL", stage="review", reason=decision.reason,
                    review_cycles=cycle, role_results=role_results,
                )
            if decision.action == "inconclusive_retry":
                review_inconclusive += 1
                continue  # re-run the SAME reviewer, no dev dispatch, no budget spend
            review_inconclusive = 0  # conclusive result -- the streak is over
            # spawn_fix / spawn_fix_escalated
            pending_confirm = list(result.review_result.must_fix)
            finding_files = _finding_files(pending_confirm)
            pre_fix_head = _head_sha(workdir)
            fix_result = _run_dev_fix(
                server=server, workdir=workdir, pr_title=pr_title, findings=pending_confirm,
                dev_model=resolved["dev"], escalated_model=decision.escalated_model,
                rundir=rundir, cycle=cycle, run_role_fn=run_role_fn,
                approval_required=approval_required,
            )
            role_results.append(fix_result)
            _log("review", cycle, fix_result, resolved["dev"])
            route = _safe_recheck_route(workdir, pre_fix_head, finding_files)

        review_cycles_used = cycle

        # --- Bug + compliance scan, parallel, max 8 -- worker.md -> *Pipeline* ---
        bugbot_prompt = resolve_prompt("bugbot", profile=profile, workdir=workdir)
        compliance_prompt = resolve_prompt("compliance", profile=profile, workdir=workdir)
        if bugbot_prompt is None or compliance_prompt is None:
            missing = "bugbot" if bugbot_prompt is None else "compliance"
            return CycleResult(
                verdict="ABORT", stage="scan",
                reason=_missing_prompt_reason(missing, profile, workdir),
                review_cycles=review_cycles_used, role_results=role_results,
            )
        bugbot_prompt += memory_block
        compliance_prompt += memory_block

        cycle = 0
        scope_suffix = ""
        while True:
            cycle += 1
            bugbot_result = run_role_fn(
                server=server, workdir=workdir, role="bugbot", title=f"{pr_title} -- bugbot c{cycle}",
                prompt=bugbot_prompt + scope_suffix, model=resolved["bugbot"], rundir=rundir, cycle=cycle,
            )
            compliance_result = run_role_fn(
                server=server, workdir=workdir, role="compliance", title=f"{pr_title} -- compliance c{cycle}",
                prompt=compliance_prompt + scope_suffix, model=resolved["verify"], rundir=rundir, cycle=cycle,
            )
            role_results.extend((bugbot_result, compliance_result))
            _log("scan", cycle, bugbot_result, resolved["bugbot"])
            _log("scan", cycle, compliance_result, resolved["verify"])
            merged = merge(bugbot_result.review_result, compliance_result.review_result)
            if cycle > 1:
                recurrence.record_post_fix(merged.must_fix)
            decision = evaluate(
                merged, scan_budget, "scan", recurrence,
                inconclusive_attempts=scan_inconclusive,
                escalation_model=resolved["dev_escalation"].model,
                convergence=scan_convergence,
            )
            _log_decision("scan", cycle, decision)
            if decision.action == "pass":
                break
            if decision.action in ("fail", "abort"):
                return CycleResult(
                    verdict="FAIL", stage="scan", reason=decision.reason,
                    review_cycles=review_cycles_used, scan_cycles=cycle, role_results=role_results,
                )
            if decision.action == "inconclusive_retry":
                scan_inconclusive += 1
                continue
            scan_inconclusive = 0  # conclusive result -- the streak is over
            # spawn_fix / spawn_fix_escalated
            finding_files = _finding_files(merged.must_fix)
            pre_fix_head = _head_sha(workdir)
            fix_result = _run_dev_fix(
                server=server, workdir=workdir, pr_title=pr_title, findings=merged.must_fix,
                dev_model=resolved["dev"], escalated_model=decision.escalated_model,
                rundir=rundir, cycle=cycle, run_role_fn=run_role_fn,
                approval_required=approval_required,
            )
            role_results.append(fix_result)
            _log("scan", cycle, fix_result, resolved["dev"])
            if _safe_recheck_route(workdir, pre_fix_head, finding_files) == "BOUNDED":
                scope_suffix = _bounded_scope_suffix(_changed_files(workdir, pre_fix_head))
            else:
                scope_suffix = ""

        return CycleResult(
            verdict="PASS", stage="scan", reason="review + bug/compliance scan clean",
            review_cycles=review_cycles_used, scan_cycles=cycle, role_results=role_results,
            budget=scan_budget, recurrence=recurrence,
        )
    finally:
        if owns_server:
            stop_server_fn(server, workdir=workdir)
        # Regenerated from the records this cycle just appended, so the NEXT
        # unit's priors already include whatever recurred in this one. In the
        # `finally` because a failed cycle is exactly the one whose findings
        # are worth carrying forward.
        try:
            memory.regenerate(workdir)
        except Exception:  # noqa: BLE001 -- derived data, never worth failing a cycle over
            pass
