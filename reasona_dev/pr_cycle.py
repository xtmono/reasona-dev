"""Drives one PR unit through dev-ralf's actual cycle -- worker.md's
"Pipeline you run": ``develop -> verify (max 8 cycles) -> bug+compliance
scan (parallel, max 8 cycles)``. The `sync-main -> /gh-pr -> /gh-review ->
up-to-date gate -> conditional final audit -> squash-merge` tail is out of
scope here (`reasona_dev.squash`/`gate_check` pick up after this returns).

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
for each role it writes a tiny one-step `plan.yaml` and shells out to
``bernstein run <that plan> --auto-approve`` -- the exact same CLI surface
an operator types by hand -- then reads the agent's raw output back from a
file the step's own prompt instructs the agent to write as its last action.
No Bernstein HTTP API is touched or guessed at.

**Not yet live-verified.** Every other mechanism in this project was
checked against a real `bernstein` invocation (`plan validate`, `review
--pipeline --validate-only`, `doctor`) before being called done. This
module has NOT had that pass yet -- `run_role()`'s actual `bernstein run`
subprocess call is untested against a live server (that requires spending
real agent budget on a real repo, deliberately deferred -- see README
"Next"). Treat `run_role()` as the one unverified boundary; everything
above it (`FixBudget`/`RecurrenceTracker`/`evaluate()`-driven looping) is
plain, already-tested Python.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from reasona_dev.cycle_gate import FixBudget, RecurrenceTracker, evaluate
from reasona_dev.finding_adapter import (
    ReviewResult,
    RoleStatus,
    merge,
    parse_kv_contract,
    parse_text_contract,
)
from reasona_dev.model_config import ResolvedModel
from reasona_dev.prompt_profile import resolve_prompt

# worker.md -> *Role I/O*: bugbot/compliance emit the external-skill KV wire
# shape (`finding_adapter.py --input kv`); review/final_audit emit the `||`
# text contract (`--input text`). This is a property of the ROLE, not the
# profile -- a profile can change WHAT the prompt asks for, never how its
# answer is shaped.
_KV_ROLES = frozenset({"bugbot", "compliance"})


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


def _write_role_plan(
    *,
    role: str,
    title: str,
    prompt: str,
    model: ResolvedModel,
    raw_output_path: Path,
    plan_path: Path,
) -> None:
    """One-step plan.yaml whose step instructs the agent to write its
    COMPLETE output (markdown + RESULT block, verbatim) to
    `raw_output_path` as its last action -- see module docstring for why
    this is the handoff mechanism instead of a Bernstein API call.
    """
    step = {
        "title": title,
        "description": (
            f"{prompt}\n\n"
            "---\n"
            "When you are completely done, write your ENTIRE output above "
            "(the markdown report AND the RESULT block, verbatim, nothing "
            f"added or removed) to the file `{raw_output_path}` as your "
            "final action, then stop."
        ),
        "role": role,
        "model": model.model,
        "effort": model.effort,
        "completion_signals": [{"type": "test_passes", "command": f"test -s {raw_output_path}"}],
    }
    plan = {
        "name": f"reasona-dev-{role}",
        "description": f"reasona-dev {role} dispatch",
        "cli": model.adapter,
        "stages": [{"name": role, "steps": [step]}],
    }
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False, allow_unicode=True))


def run_role(
    *,
    workdir: Path,
    role: str,
    title: str,
    prompt: str,
    model: ResolvedModel,
    rundir: Path,
    cycle: int,
) -> RoleRunResult:
    """Dispatch one role once, synchronously, via `bernstein run`.

    Returns `role_status=ERROR` (never a silent PASS) if the agent's
    output file never appears -- matches worker.md -> *RESULT parsing*:
    "Missing block ... -> cycle FAIL", not "treat as clean".
    """
    raw_output_path = rundir / f"{role}-c{cycle}.raw.txt"
    plan_path = rundir / f"{role}-c{cycle}.plan.yaml"
    rundir.mkdir(parents=True, exist_ok=True)
    if raw_output_path.exists():
        raw_output_path.unlink()

    _write_role_plan(
        role=role, title=title, prompt=prompt, model=model,
        raw_output_path=raw_output_path, plan_path=plan_path,
    )

    subprocess.run(
        ["bernstein", "run", str(plan_path), "--auto-approve"],
        cwd=workdir,
        check=False,
    )

    if not raw_output_path.is_file():
        return RoleRunResult(
            role=role, cycle=cycle,
            review_result=ReviewResult(role_status=RoleStatus.ERROR),
            raw_output_path=raw_output_path,
        )

    text = raw_output_path.read_text(encoding="utf-8")
    parser = parse_kv_contract if role in _KV_ROLES else parse_text_contract
    return RoleRunResult(role=role, cycle=cycle, review_result=parser(text), raw_output_path=raw_output_path)


def _build_fix_prompt(pr_title: str, findings) -> str:
    """worker.md -> *Loop control*: "dispatch dev (fix-cycle $DEV_PROMPT +
    `must_fix` list, `contract`/`scenario`/`fix` fields included
    verbatim)". Every field is passed through unedited -- this driver
    never rewrites or summarizes a finding on the way to dev.
    """
    lines = [
        f"Fix the following MUST_FIX findings from review on {pr_title}. "
        "Do not address anything not listed here.",
        "",
    ]
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
    return "\n".join(lines)


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


def run_pr_cycle(
    *,
    workdir: str | Path,
    pr_title: str,
    resolved: dict[str, ResolvedModel],
    rundir: str | Path,
    profile: str,
    run_role_fn=run_role,
) -> CycleResult:
    """develop -> verify(review) -> bug+compliance scan, worker.md-faithful.

    Assumes `dev`'s cycle-0 implementation already happened (this driver
    picks up at *Verify cycles* -- `plan_compile.py`'s own step covers
    cycle-0 development, gated by `$CI_FAST`-equivalent `completion_signals`
    the way it already is).

    `run_role_fn` is injectable purely for testing -- production callers
    never pass it (defaults to the real `run_role`, which shells out to
    `bernstein run`).
    """
    workdir = Path(workdir)
    rundir = Path(rundir)
    recurrence = RecurrenceTracker()
    review_budget = FixBudget()
    scan_budget = FixBudget()
    role_results: list[RoleRunResult] = []

    review_profile_prompt = resolve_prompt("review", profile=profile, workdir=workdir)
    if review_profile_prompt is None:
        return CycleResult(verdict="ABORT", stage="review", reason=f"no review prompt for profile {profile!r}")

    # --- Verify cycles (review), max 8 -- worker.md -> *Develop & verify* ---
    cycle = 0
    while True:
        cycle += 1
        result = run_role_fn(
            workdir=workdir, role="reviewer", title=f"{pr_title} -- review c{cycle}",
            prompt=review_profile_prompt, model=resolved["review"], rundir=rundir, cycle=cycle,
        )
        role_results.append(result)
        if cycle > 1:
            # This review followed a dev fix -- whatever MUST_FIX is still
            # here just SURVIVED that fix. Record it BEFORE evaluate() so
            # `RecurrenceTracker.decide()` sees the updated count (dev-ralf
            # §3.5: a key surviving one completed fix earns exactly one
            # bounded escalation before FAIL).
            recurrence.record_post_fix(result.review_result.must_fix)
        decision = evaluate(
            result.review_result, review_budget, "review", recurrence,
            inconclusive_attempts=0, escalation_model=resolved["dev_escalation"].model,
        )
        if decision.action in ("pass",):
            break
        if decision.action in ("fail", "abort"):
            return CycleResult(
                verdict="FAIL", stage="review", reason=decision.reason,
                review_cycles=cycle, role_results=role_results,
            )
        if decision.action == "inconclusive_retry":
            continue  # re-run the SAME reviewer, no dev dispatch, no budget spend
        # spawn_fix / spawn_fix_escalated: dispatch dev, then FULL re-review
        # (not the bounded confirm/regression recheck -- `cycle_gate.
        # recheck_route()` needs pre-fix-head/finding-file tracking this
        # driver does not do yet; every fix cycle here re-runs the complete
        # review prompt, worker.md's "recheck" narrowing is a follow-up).
        fix_result = _run_dev_fix(
            workdir=workdir, pr_title=pr_title, findings=result.review_result.must_fix,
            dev_model=resolved["dev"], escalated_model=decision.escalated_model,
            rundir=rundir, cycle=cycle, run_role_fn=run_role_fn,
        )
        role_results.append(fix_result)

    review_cycles_used = cycle

    # --- Bug + compliance scan, parallel, max 8 -- worker.md -> *Pipeline* ---
    bugbot_prompt = resolve_prompt("bugbot", profile=profile, workdir=workdir)
    compliance_prompt = resolve_prompt("compliance", profile=profile, workdir=workdir)
    if bugbot_prompt is None or compliance_prompt is None:
        return CycleResult(
            verdict="ABORT", stage="scan", reason=f"no bugbot/compliance prompt for profile {profile!r}",
            review_cycles=review_cycles_used, role_results=role_results,
        )

    cycle = 0
    while True:
        cycle += 1
        bugbot_result = run_role_fn(
            workdir=workdir, role="bugbot", title=f"{pr_title} -- bugbot c{cycle}",
            prompt=bugbot_prompt, model=resolved["bugbot"], rundir=rundir, cycle=cycle,
        )
        compliance_result = run_role_fn(
            workdir=workdir, role="compliance", title=f"{pr_title} -- compliance c{cycle}",
            prompt=compliance_prompt, model=resolved["verify"], rundir=rundir, cycle=cycle,
        )
        role_results.extend((bugbot_result, compliance_result))
        merged = merge(bugbot_result.review_result, compliance_result.review_result)
        if cycle > 1:
            recurrence.record_post_fix(merged.must_fix)
        decision = evaluate(
            merged, scan_budget, "scan", recurrence,
            inconclusive_attempts=0, escalation_model=resolved["dev_escalation"].model,
        )
        if decision.action == "pass":
            break
        if decision.action in ("fail", "abort"):
            return CycleResult(
                verdict="FAIL", stage="scan", reason=decision.reason,
                review_cycles=review_cycles_used, scan_cycles=cycle, role_results=role_results,
            )
        if decision.action == "inconclusive_retry":
            continue
        # spawn_fix / spawn_fix_escalated
        fix_result = _run_dev_fix(
            workdir=workdir, pr_title=pr_title, findings=merged.must_fix,
            dev_model=resolved["dev"], escalated_model=decision.escalated_model,
            rundir=rundir, cycle=cycle, run_role_fn=run_role_fn,
        )
        role_results.append(fix_result)

    return CycleResult(
        verdict="PASS", stage="scan", reason="review + bug/compliance scan clean",
        review_cycles=review_cycles_used, scan_cycles=cycle, role_results=role_results,
    )
