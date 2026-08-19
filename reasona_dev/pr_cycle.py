"""Drives one PR unit through dev-ralf's actual cycle -- worker.md's
"Pipeline you run": ``develop -> review (max 8 cycles) -> bug+compliance
scan (parallel, max 8 cycles)``. `reasona_dev.ship_gate` and
`reasona_dev.merge_tail` pick up after this returns.

**Why this exists instead of a bigger `plan.yaml`.** The number of review
cycles, whether bugbot+compliance even run, and whether a MUST_FIX
recurrence earns a bounded model escalation are all facts only known AFTER a
role's actual output is parsed -- this cannot be predeclared as a fixed
`plan.yaml` DAG (docs/ARCHITECTURE.md §1). Bernstein can create follow-up
tasks from inside a run (`POST /tasks/self-create`), but that puts the
decision in the AGENT, which is the one thing this project exists to avoid:
disposition comes from section membership, and budget/recurrence/convergence
from `cycle_gate`, with no model in the loop.

So the driver runs OUTSIDE Bernstein, one level up. Each role dispatch is a
one-step `plan.yaml` executed by `bernstein run` -- the same CLI surface an
operator types by hand -- and the agent's structured output comes back
through a file the step's description tells it to write. See
`reasona_dev.bernstein_dispatch` for why this is a batch call rather than
the HTTP dispatch it briefly was.

**Two budget mechanisms, two different failure modes.** The 8/8/16 cycle
caps inherited from dev-ralf are a ceiling, not a cost; what actually spends
is how expensive each cycle is and how many run before a doomed PR gives up:

- *Cost per cycle* -- `cycle_gate.recheck_route()`. When a dev fix touched
  only files already named in the findings it was fixing, the next pass is a
  BOUNDED recheck (confirm + regression, `recheck.md`, the cheaper `recheck`
  model) instead of a full omission hunt. Scan roles get the same treatment
  through a scope suffix rather than a separate prompt, since their prompts
  come from an external skill contract.
- *Number of cycles* -- `cycle_gate.ConvergenceTracker`. `RecurrenceTracker`
  only fires when the SAME finding survives, so a PR emitting fresh findings
  every cycle used to burn the whole stage cap. Non-convergence now exits at
  3 cycles, and a stuck INCONCLUSIVE role at 3 attempts.

Which rule ended a unit is recorded by `cycles_log`, so the caps can
eventually be re-derived from measurement instead of inherited on faith.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from reasona_dev import cycles_log, memory
from reasona_dev.bernstein_dispatch import DEFAULT_ROLE_SCOPE, run_plan_file, write_role_plan
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
    error_detail: str | None = None


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
    workdir: Path,
    role: str,
    title: str,
    prompt: str,
    model: ResolvedModel,
    rundir: Path,
    cycle: int,
    port: int = 8052,
    scope: str = DEFAULT_ROLE_SCOPE,
) -> RoleRunResult:
    """Dispatch one role once via `bernstein run` on a one-step plan.

    Synchronous: the run drives the agent to completion and exits, so there
    is no polling and no server lifetime to own (see
    `reasona_dev.bernstein_dispatch` on why this is a batch call rather than
    an HTTP dispatch).

    Returns `role_status=ERROR` -- never a silent PASS -- when the agent's
    output file does not appear, matching worker.md -> *RESULT parsing*:
    "Missing block ... -> cycle FAIL", not "treat as clean". The run's exit
    code and stderr tail are carried in `error_detail` so a turn-budget death
    and a misconfigured adapter are distinguishable; `cycle_gate` collapses
    both into the same abort, which is the right gate decision and a useless
    diagnostic on its own.
    """
    # ABSOLUTE, always. The agent runs inside a per-task git worktree, so a
    # relative path in its instructions resolves against THAT tree, not the
    # project root the driver reads from. Live symptom when this was
    # relative: the agent wrote into its own worktree, spent its remaining
    # turns hunting for the file the driver was asking about, and died on
    # `error_max_turns` while the driver reported ERROR.
    rundir = rundir.resolve()
    raw_output_path = rundir / f"{role}-c{cycle}.raw.txt"
    plan_path = rundir / f"{role}-c{cycle}.plan.yaml"
    rundir.mkdir(parents=True, exist_ok=True)
    if raw_output_path.exists():
        raw_output_path.unlink()

    write_role_plan(
        path=plan_path, role=role, title=title,
        description=_build_role_description(prompt, raw_output_path),
        model=model.model, effort=model.effort, cli=model.adapter,
        scope=scope,
    )
    dispatch = run_plan_file(plan_path, workdir, port=port)

    if not raw_output_path.is_file():
        return RoleRunResult(
            role=role, cycle=cycle,
            review_result=ReviewResult(role_status=RoleStatus.ERROR),
            raw_output_path=raw_output_path,
            error_detail=(
                f"no output at {raw_output_path.name}; "
                f"bernstein run exit={dispatch.returncode} "
                f"stderr={dispatch.stderr_tail[:200]!r}"
            ),
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
    workdir: Path,
    pr_title: str,
    findings,
    dev_model: ResolvedModel,
    escalated_model: str | None,
    rundir: Path,
    cycle: int,
    run_role_fn,
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
        workdir=workdir, role="backend", title=f"{pr_title} -- fix c{cycle}",
        prompt=_build_fix_prompt(pr_title, findings), model=model, rundir=rundir, cycle=cycle,
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
    stage_name: str | None = None,
    files: list[str] | None = None,
    run_role_fn=run_role,
) -> CycleResult:
    """develop -> review -> bug+compliance scan, worker.md-faithful.

    Assumes `dev`'s cycle-0 implementation already happened (this driver
    picks up at *Review cycles* -- `plan_compile.py`'s own step covers
    cycle-0 development, gated by `$CI_FAST`-equivalent `completion_signals`
    the way it already is).

    Each role dispatch is its own `bernstein run`, so there is no server
    lifetime to manage here and no cleanup path to get wrong.

    `run_role_fn` is injectable purely for testing -- production callers
    never pass it.
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
            result=result.review_result, error_detail=result.error_detail,
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

    try:
        # --- Review cycles, max 8 -- worker.md -> *Develop & review* ---
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
                workdir=workdir, role="reviewer",
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
                workdir=workdir, pr_title=pr_title, findings=pending_confirm,
                dev_model=resolved["dev"], escalated_model=decision.escalated_model,
                rundir=rundir, cycle=cycle, run_role_fn=run_role_fn,
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
                workdir=workdir, role="bugbot", title=f"{pr_title} -- bugbot c{cycle}",
                prompt=bugbot_prompt + scope_suffix, model=resolved["bugbot"], rundir=rundir, cycle=cycle,
            )
            compliance_result = run_role_fn(
                workdir=workdir, role="compliance", title=f"{pr_title} -- compliance c{cycle}",
                prompt=compliance_prompt + scope_suffix, model=resolved["compliance"], rundir=rundir, cycle=cycle,
            )
            role_results.extend((bugbot_result, compliance_result))
            _log("scan", cycle, bugbot_result, resolved["bugbot"])
            _log("scan", cycle, compliance_result, resolved["compliance"])
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
                workdir=workdir, pr_title=pr_title, findings=merged.must_fix,
                dev_model=resolved["dev"], escalated_model=decision.escalated_model,
                rundir=rundir, cycle=cycle, run_role_fn=run_role_fn,
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
        # Regenerated from the records this cycle just appended, so the NEXT
        # unit's priors already include whatever recurred in this one. In the
        # `finally` because a failed cycle is exactly the one whose findings
        # are worth carrying forward.
        try:
            memory.regenerate(workdir)
        except Exception:  # noqa: BLE001 -- derived data, never worth failing a cycle over
            pass
