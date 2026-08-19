"""The last third of worker.md, restructured: `sync -> final_audit ->
ship_gate` as one self-verifying loop, then `gh-pr -> squash-merge`.

**Why ship_gate moved behind sync and final_audit.** `ship_gate.evaluate()`
runs `acceptance.run_all()` against whatever is on disk at call time. Both
`sync_main()` (merging `origin/main` in) and `run_final_audit()`'s own
dev-fix loop change what is on disk. Calling ship_gate BEFORE either of them
-- the original order -- stamps a PASS on code that is not what actually
ends up in the PR: a sync-time conflict fix or an audit fix made after that
PASS is never re-verified before the squash-merge that ships it. Moving
ship_gate to run only after sync and the audit have both settled closes
that gap; see docs/ARCHITECTURE.md §3.11.2 for the fuller argument.

**Why sync got its own bounded fix loop instead of a one-shot block.** A
merge conflict is a defect exactly like a review finding -- dev can resolve
it -- not an external failure like a missing `gh` binary. Treating it as an
immediate terminal block contradicted this project's own completion
contract: reasona-dev is supposed to run to a shipped PR unless something
truly outside its control (network, `gh`, `git` itself) stops it.
`run_sync_cycle()` now dispatches dev to resolve conflict markers and
conclude the merge, bounded by the `"sync"` stage of the same `FixBudget`
(`MAX_SYNC_CYCLES`), the same shape as every other stage in this pipeline.

**Why the tail is a round-bounded outer loop, not three linear steps.**
Either sync or final_audit can still change code in a given pass. If either
one did, that pass's ship_gate verdict already covers something the OTHER
step never saw operating on the pre-change tree, so the whole
sync -> final_audit -> ship_gate sequence runs again. `run_final_phase()`
tracks whether anything changed in a round and only accepts the round's
ship_gate verdict once a round changes nothing -- bounded by
`MAX_FINAL_PHASE_ROUNDS`, on the same reasoning as every other cap in this
pipeline: base moving faster than this pipeline can settle is not something
retrying forever would fix.

**Merging is opt-in.** `merge=False` is the default and stops after the PR
exists. A squash-merge is outward-facing and hard to undo -- it rewrites the
default branch of a real repository -- so the caller has to ask for it
explicitly rather than discover it happened. Everything up to that point
(sync, audit, message construction, PR creation) is safe to run repeatedly.

**What still fails loudly rather than being retried.** `gh` missing, `gh`
unauthenticated, a non-conflict sync failure (fetch failure, unreadable
remote), a malformed squash title, a PR behind its base at the final
pre-merge check: these are genuinely outside dev's ability to fix by
editing files, so each still returns a `blocked` status immediately rather
than entering a fix loop that could never converge.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from reasona_dev import cycles_log, ledger, squash
from reasona_dev.cycle_gate import (
    MAX_FINAL_PHASE_ROUNDS,
    FixBudget,
    RecurrenceTracker,
    evaluate,
)
from reasona_dev.model_config import ResolvedModel
from reasona_dev.pr_cycle import RoleRunResult, _run_dev_fix, run_role
from reasona_dev.prompt_profile import resolve_prompt
from reasona_dev.ship_gate import ShipDecision
from reasona_dev.squash import SquashMessage

MERGED = "merged"
PR_OPEN = "pr_open"
BLOCKED = "blocked"


@dataclass
class TailResult:
    stage_name: str
    status: str  # MERGED | PR_OPEN | BLOCKED
    reason: str
    pr_url: str | None = None
    squash_message: SquashMessage | None = None
    final_audit: RoleRunResult | None = None
    role_results: list[RoleRunResult] = field(default_factory=list)
    ship_decision: ShipDecision | None = None

    @property
    def blocked(self) -> bool:
        return self.status == BLOCKED

    def render(self) -> str:
        line = f"  [{self.status:>8}] {self.stage_name}: {self.reason}"
        return f"{line}\n             {self.pr_url}" if self.pr_url else line


# --- shell helpers ----------------------------------------------------------

def _run(cmd: list[str], workdir: Path, *, timeout: int = 300) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=str(workdir), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(cmd)}: timed out after {timeout}s"
    except OSError as exc:
        return 1, "", str(exc)
    return p.returncode, p.stdout, p.stderr


def gh_available(workdir: Path) -> str | None:
    """None when `gh` is usable, else the reason it is not.

    Checked before anything is attempted rather than discovered halfway
    through: a tail that syncs and audits and only then finds it cannot
    create a PR has spent an audit for nothing.
    """
    if shutil.which("gh") is None:
        return "gh CLI is not on PATH"
    code, _, err = _run(["gh", "auth", "status"], workdir, timeout=30)
    if code != 0:
        return f"gh is not authenticated ({err.strip().splitlines()[0] if err.strip() else 'auth status failed'})"
    return None


# --- steps ------------------------------------------------------------------

def _conflicted_files(workdir: Path) -> list[str]:
    code, out, _ = _run(["git", "diff", "--name-only", "--diff-filter=U"], workdir)
    return [p for p in out.splitlines() if p.strip()] if code == 0 else []


def sync_main(workdir: Path, *, base: str = "origin/main") -> tuple[bool, str]:
    """Fetch and merge `base` into the current branch.

    Merge, not rebase: the branch may already be pushed, and rebasing
    published history forces every later push to be a force-push, which the
    final pre-merge check then cannot distinguish from someone else's work
    being overwritten.

    **A conflict is left in place, not auto-aborted.** `run_sync_cycle()`
    resolves it by dispatching dev against the in-progress merge (conflict
    markers still on disk, `MERGE_HEAD` still set) -- aborting here would
    erase exactly the state dev needs to see. Any OTHER merge failure (fetch
    failed, no conflict markers to point to) is not something dev editing
    files can fix, so that case still aborts and returns a plain failure.
    """
    remote = base.split("/", 1)[0] if "/" in base else "origin"
    code, _, err = _run(["git", "fetch", remote], workdir)
    if code != 0:
        return False, f"git fetch {remote} failed: {err.strip()[:200]}"

    code, out, err = _run(["git", "merge", "--no-edit", base], workdir)
    if code == 0:
        return True, "up to date with base"

    paths = _conflicted_files(workdir)
    if paths:
        return False, f"merge conflict with {base} in: {', '.join(paths[:5])}"
    _run(["git", "merge", "--abort"], workdir)
    return False, f"merge with {base} failed: {(err or out).strip()[:200]}"


def _build_conflict_fix_prompt(base: str, conflicted_files: list[str]) -> str:
    listed = "\n".join(f"- {p}" for p in conflicted_files)
    return "\n".join(
        [
            f"Merging {base} into this branch produced conflicts in the files "
            "below. Resolve every conflict marker in each of them, keeping "
            f"both this branch's intent and {base}'s changes where they do "
            "not contradict, then stage the resolved files and run "
            "`git commit --no-edit` to conclude the merge. Do not touch any "
            "file not listed here.",
            "",
            listed,
        ]
    )


def _run_conflict_fix(
    *, workdir: Path, pr_title: str, base: str, conflicted_files: list[str],
    dev_model: ResolvedModel, rundir: Path, cycle: int, run_role_fn,
) -> RoleRunResult:
    return run_role_fn(
        workdir=workdir, role="backend",
        title=f"{pr_title} -- resolve merge conflict c{cycle}",
        prompt=_build_conflict_fix_prompt(base, conflicted_files),
        model=dev_model, rundir=rundir, cycle=cycle,
    )


def run_sync_cycle(
    *,
    workdir: Path,
    pr_title: str,
    resolved: dict[str, ResolvedModel],
    rundir: Path,
    budget: FixBudget,
    base: str = "origin/main",
    run_role_fn=run_role,
) -> tuple[str, str, list[RoleRunResult], bool]:
    """Sync with `base`, resolving a conflict via dev instead of blocking on it.

    Returns `(status, reason, dispatches, changed)`. `status` is `"ok"` (in
    sync, whether or not a conflict needed resolving) or `"blocked"` (a
    non-conflict sync failure, or the `"sync"` stage budget ran out while
    still conflicted). `changed` is True iff a conflict-resolution commit
    was made this call -- the caller uses it to decide whether `final_audit`
    and `ship_gate` need to run again on top of it.
    """
    dispatches: list[RoleRunResult] = []
    changed = False
    cycle = 0
    while True:
        # Idempotent cleanup: aborts a merge left over from a prior
        # iteration where dev edited the conflict markers but did not
        # conclude the merge with a commit. A no-op when nothing is
        # in progress.
        _run(["git", "merge", "--abort"], workdir)
        ok, reason = sync_main(workdir, base=base)
        if ok:
            return "ok", reason, dispatches, changed

        conflicted = _conflicted_files(workdir)
        if not conflicted:
            return "blocked", reason, dispatches, changed
        if not budget.can_spend("sync"):
            _run(["git", "merge", "--abort"], workdir)
            return "blocked", f"sync budget exhausted: {reason}", dispatches, changed

        budget.spend("sync")
        cycle += 1
        dispatches.append(_run_conflict_fix(
            workdir=workdir, pr_title=pr_title, base=base, conflicted_files=conflicted,
            dev_model=resolved["dev"], rundir=rundir, cycle=cycle, run_role_fn=run_role_fn,
        ))
        changed = True


def is_up_to_date(workdir: Path, *, base: str = "origin/main") -> tuple[bool, str]:
    """True when HEAD already contains the tip of `base`.

    Re-checked immediately before merging, not only after `sync_main`: base
    can advance between the two, and merging a PR that no longer contains
    its base is how a green PR lands red.
    """
    code, out, err = _run(["git", "merge-base", "--is-ancestor", base, "HEAD"], workdir)
    if code == 0:
        return True, "HEAD contains base"
    if code == 1:
        return False, f"branch is behind {base} -- re-run sync"
    return False, f"could not compare against {base}: {err.strip()[:160]}"


def build_squash_message(
    *, unit_type: str | None, title: str, body_lines: list[str] | None = None
) -> tuple[SquashMessage | None, str]:
    """Construct and independently re-check the squash message.

    `squash.build` is the only constructor and `squash.guard` re-derives
    validity without consulting it, so a violation means the two disagree --
    never "go fix the message by hand". A `T#` (title) violation blocks the
    merge outright; a `B#` (body-only) violation merges with the title alone,
    which is `squash.classify`'s TITLE_ONLY verdict.
    """
    msg = squash.build(unit_type or "feat", title, body_lines or [])
    verdict = squash.classify(squash.guard(msg))
    if verdict == "FAIL":
        return None, f"squash message rejected by its own guard: {'; '.join(squash.guard(msg))}"
    if verdict == "TITLE_ONLY":
        return SquashMessage(title=msg.title, body=""), "body dropped (guard: body-only violation)"
    return msg, "ok"


def existing_pr_url(workdir: Path) -> str | None:
    code, out, _ = _run(
        ["gh", "pr", "view", "--json", "url", "--jq", ".url"], workdir, timeout=60
    )
    return out.strip() if code == 0 and out.strip() else None


def create_pr(workdir: Path, msg: SquashMessage, *, known_pr_url: str | None = None) -> tuple[str | None, str]:
    """Create the PR, or return the one that already exists.

    Idempotent on purpose: the tail is safe to re-run after a blocked step,
    and a second `gh pr create` for the same branch fails rather than
    duplicating -- surfacing that failure as an error would make retry look
    broken when it is the normal path.

    `known_pr_url`, when given (`reasona_dev.ledger`'s record of a PR this
    same unit already created in an earlier, interrupted run), is checked
    only as a FALLBACK -- `gh pr view` below is still the live source of
    truth and wins whenever it has an answer. The ledger only matters when
    `gh` itself comes up empty (e.g. this call runs before the working tree
    is on the PR's branch): using the remembered URL there avoids `gh pr
    create` producing a duplicate that a live-but-incomplete `gh` query
    missed, without ever trusting a stale ledger over a live one.
    """
    existing = existing_pr_url(workdir)
    if existing:
        return existing, "reused existing PR"
    if known_pr_url:
        return known_pr_url, "reused PR recorded in the ledger (gh pr view did not see it)"

    code, _, err = _run(["git", "push", "-u", "origin", "HEAD"], workdir, timeout=180)
    if code != 0:
        return None, f"git push failed: {err.strip()[:200]}"

    code, out, err = _run(
        ["gh", "pr", "create", "--title", msg.title, "--body", msg.body or msg.title],
        workdir, timeout=120,
    )
    if code != 0:
        return None, f"gh pr create failed: {err.strip()[:200]}"
    url = out.strip().splitlines()[-1] if out.strip() else existing_pr_url(workdir)
    return url, "PR created"


def squash_merge(workdir: Path, msg: SquashMessage) -> tuple[bool, str]:
    """`gh pr merge --squash` with the built title and body.

    GitHub appends ` (#<pr>)` to a squash title itself, which is why
    `squash.build` never adds one.
    """
    cmd = ["gh", "pr", "merge", "--squash", "--subject", msg.title]
    if msg.body:
        cmd += ["--body", msg.body]
    code, out, err = _run(cmd, workdir, timeout=180)
    if code != 0:
        return False, f"gh pr merge failed: {(err or out).strip()[:200]}"
    return True, "squash-merged"


# --- final audit ------------------------------------------------------------

def should_run_final_audit(budget: FixBudget) -> bool:
    """True when the unit needed at least one fix. See module docstring."""
    return budget.total_used > 0


def run_final_audit(
    *,
    workdir: Path,
    stage_name: str,
    pr_title: str,
    profile: str,
    resolved: dict[str, ResolvedModel],
    rundir: Path,
    budget: FixBudget,
    recurrence: RecurrenceTracker,
    run_role_fn=run_role,
) -> tuple[bool, str, list[RoleRunResult]]:
    """A fresh whole-PR audit, with its own bounded fix loop.

    Dispatched as the `compliance` role rather than `final_audit`: the audit's
    prompt is what makes it an audit, and Bernstein's role whitelist plus its
    per-role worktree conventions are shared. Using a role the seed already
    declares keeps the tail from depending on a `role_model_policy` entry a
    target repo may not have. The MODEL still comes from
    `resolved["final_audit"]`, so the audit runs on the model configured for
    it.

    Returns `(passed, reason, dispatches)`. A MUST_FIX here spends the
    `"final"` stage budget (cap `MAX_FINAL_CYCLES`), which no other caller
    produces -- the audit is the only thing that runs after the scan stage has
    already passed, so a finding at this point is by definition something
    every earlier role missed.
    """
    prompt = resolve_prompt("final_audit", profile=profile, workdir=workdir)
    if prompt is None:
        # Not fatal: a profile that ships no final_audit.md has declared it
        # does not want one. Silently skipping a REQUESTED audit would be
        # wrong; skipping an undefined one is the profile's own decision.
        return True, "profile defines no final_audit prompt -- skipped", []

    dispatches: list[RoleRunResult] = []
    cycle = 0
    while True:
        cycle += 1
        result = run_role_fn(
            workdir=workdir, role="compliance",
            title=f"{pr_title} -- final audit c{cycle}",
            prompt=prompt, model=resolved["final_audit"], rundir=rundir, cycle=cycle,
        )
        dispatches.append(result)
        cycles_log.record_dispatch(
            workdir=workdir, stage_name=stage_name, stage="final", cycle=cycle,
            role="final_audit", model=resolved["final_audit"].model,
            adapter=resolved["final_audit"].adapter, result=result.review_result,
        )
        if cycle > 1:
            recurrence.record_post_fix(result.review_result.must_fix)

        decision = evaluate(
            result.review_result, budget, "final", recurrence,
            inconclusive_attempts=0, escalation_model=resolved["dev_escalation"].model,
        )
        cycles_log.record_decision(
            workdir=workdir, stage_name=stage_name, stage="final", cycle=cycle,
            action=decision.action, reason=decision.reason,
            escalated_model=decision.escalated_model,
        )
        if decision.action == "pass":
            return True, f"final audit clean after {cycle} cycle(s)", dispatches
        if decision.action in ("fail", "abort"):
            return False, f"final audit: {decision.reason}", dispatches
        if decision.action == "inconclusive_retry":
            # The audit is the last gate; an INCONCLUSIVE one cannot be
            # retried indefinitely here without the counter the review/scan
            # loops carry, and re-auditing is cheaper to simply refuse.
            return False, "final audit inconclusive -- verification did not run", dispatches

        fix = _run_dev_fix(
            workdir=workdir, pr_title=pr_title,
            findings=result.review_result.must_fix, dev_model=resolved["dev"],
            escalated_model=decision.escalated_model, rundir=rundir,
            cycle=cycle, run_role_fn=run_role_fn,
        )
        dispatches.append(fix)


# --- ship_gate: bounded dev-fix loop for a failing acceptance criterion ----

def _build_ship_fix_prompt(decision: ShipDecision) -> str:
    listed = "\n".join(f"- {o.name}: {o.detail}" for o in decision.failures)
    return "\n".join(
        [
            "The ship gate found the following unmet criteria. Fix them so "
            "the plan's own executable acceptance criteria pass. Do not "
            "address anything not listed here.",
            "",
            listed,
        ]
    )


def _run_ship_fix(
    *, workdir: Path, pr_title: str, decision: ShipDecision,
    dev_model: ResolvedModel, rundir: Path, cycle: int, run_role_fn,
) -> RoleRunResult:
    return run_role_fn(
        workdir=workdir, role="backend",
        title=f"{pr_title} -- ship gate fix c{cycle}",
        prompt=_build_ship_fix_prompt(decision), model=dev_model, rundir=rundir, cycle=cycle,
    )


def run_ship_cycle(
    *,
    workdir: Path,
    stage_name: str,
    pr_title: str,
    resolved: dict[str, ResolvedModel],
    rundir: Path,
    budget: FixBudget,
    cycle_verdict: str,
    ship_gate_fn,
    run_role_fn=run_role,
) -> tuple[ShipDecision, bool, list[RoleRunResult]]:
    """Runs `ship_gate_fn` and, on a failing acceptance criterion, dispatches
    dev against it and retries -- bounded by the `"ship"` stage of `budget`
    (`MAX_SHIP_CYCLES`). The review axis is already guaranteed to pass by
    the time this runs (`orchestrate.py` only calls the final phase when
    the review/scan cycle itself passed), so a failure here is always the
    acceptance axis.

    Returns `(decision, changed, dispatches)`. `changed` is True iff a ship
    fix was dispatched this call -- `run_final_phase()` uses it exactly like
    `sync_changed`/`audit_changed`: a fix here means the whole round needs
    to re-verify from sync, not just this check. `decision.passed` tells
    the caller whether this returned because it settled or because the
    `"ship"` budget ran out while still failing.
    """
    dispatches: list[RoleRunResult] = []
    changed = False
    cycle = 0
    while True:
        decision = ship_gate_fn(workdir, stage_name, cycle_verdict=cycle_verdict)
        if decision.passed or not budget.can_spend("ship"):
            return decision, changed, dispatches
        budget.spend("ship")
        cycle += 1
        dispatches.append(_run_ship_fix(
            workdir=workdir, pr_title=pr_title, decision=decision,
            dev_model=resolved["dev"], rundir=rundir, cycle=cycle, run_role_fn=run_role_fn,
        ))
        changed = True


# --- final phase: sync -> final_audit -> ship_gate, re-verified as a whole -

def run_final_phase(
    *,
    workdir: Path,
    stage_name: str,
    pr_title: str,
    profile: str,
    resolved: dict[str, ResolvedModel],
    rundir: Path,
    budget: FixBudget,
    recurrence: RecurrenceTracker,
    cycle_verdict: str,
    ship_gate_fn,
    base: str = "origin/main",
    run_role_fn=run_role,
) -> tuple[ShipDecision | None, str, list[RoleRunResult], str]:
    """`sync -> (conditional) final_audit -> ship_gate (with its own bounded
    dev-fix loop, `run_ship_cycle()`)`, re-run whole as long as a round
    actually changed something. See the module docstring's "why the tail is
    a round-bounded outer loop" for the reasoning.

    Returns `(ship_decision, status, dispatches, reason)`. `status` is
    `"passed"` (a round changed nothing and its `ship_decision.passed` is
    True) or `"blocked"` (a sync/audit/ship step's own bounded dev-fix
    budget ran out while still failing, or `MAX_FINAL_PHASE_ROUNDS` was
    reached without settling). By the time this runs, review/scan already
    passed, so nothing failing this deep is treated as an ordinary
    review-found defect (`failed`) -- see `cycle_gate.MAX_SHIP_CYCLES`'s own
    docstring. `ship_decision` is None only when the block happened before
    ship_gate ever ran.
    """
    dispatches: list[RoleRunResult] = []
    for round_ in range(1, MAX_FINAL_PHASE_ROUNDS + 1):
        sync_status, sync_reason, sync_dispatches, sync_changed = run_sync_cycle(
            workdir=workdir, pr_title=pr_title, resolved=resolved, rundir=rundir,
            budget=budget, base=base, run_role_fn=run_role_fn,
        )
        dispatches.extend(sync_dispatches)
        if sync_status != "ok":
            return None, "blocked", dispatches, sync_reason

        audit_changed = False
        if should_run_final_audit(budget):
            passed, audit_reason, audit_dispatches = run_final_audit(
                workdir=workdir, stage_name=stage_name, pr_title=pr_title,
                profile=profile, resolved=resolved, rundir=rundir, budget=budget,
                recurrence=recurrence, run_role_fn=run_role_fn,
            )
            dispatches.extend(audit_dispatches)
            if not passed:
                return None, "blocked", dispatches, audit_reason
            audit_changed = len(audit_dispatches) > 1  # more than the clean first read means a fix ran

        decision, ship_changed, ship_dispatches = run_ship_cycle(
            workdir=workdir, stage_name=stage_name, pr_title=pr_title, resolved=resolved,
            rundir=rundir, budget=budget, cycle_verdict=cycle_verdict, ship_gate_fn=ship_gate_fn,
            run_role_fn=run_role_fn,
        )
        dispatches.extend(ship_dispatches)
        if not decision.passed:
            return decision, "blocked", dispatches, f"ship gate did not pass: {decision.reason}"

        if not sync_changed and not audit_changed and not ship_changed:
            return decision, "passed", dispatches, decision.reason
        # Something changed this round -- the ship_gate verdict just above
        # covers code the OTHER step in this same round never saw. Loop.

    return None, "blocked", dispatches, f"final phase did not converge after {MAX_FINAL_PHASE_ROUNDS} round(s)"


# --- composition ------------------------------------------------------------

def run_final_stage(
    *,
    workdir: str | Path,
    stage_name: str,
    pr_title: str,
    unit_type: str | None,
    profile: str,
    resolved: dict[str, ResolvedModel],
    rundir: str | Path,
    cycle_verdict: str,
    budget: FixBudget,
    recurrence: RecurrenceTracker,
    ship_gate_fn,
    base: str = "origin/main",
    merge: bool = False,
    plan_name: str | None = None,
    run_role_fn=run_role,
) -> TailResult:
    """gh -> final phase (sync -> final_audit -> ship_gate) -> PR -> squash-merge.

    Named separately from `run_final_phase()` because it is a superset: `gh`
    availability, PR creation, and squash-merge are not part of the
    round-verified sync/audit/ship_gate loop (they run once, after that loop
    has already settled), so they stay outside `run_final_phase()` and are
    composed here instead. See the module docstring's ASCII diagram.

    `cycle_verdict` and `ship_gate_fn` replace the old pre-computed
    `ship_decision` parameter: the verdict can no longer be computed by the
    caller before this runs, since it has to be evaluated on whatever the
    final phase's own sync/audit fixes leave on disk -- see
    `run_final_phase()` and the module docstring.

    `plan_name`, when given, is `orchestrate`'s resume flag passed through:
    `reasona_dev.ledger`'s recorded PR url for this unit (if any) is
    offered to `create_pr()` as a fallback -- see its own docstring for why
    that's a fallback and not a replacement for the live `gh pr view`
    check. A newly created PR's url is recorded back to the same ledger
    entry so the NEXT call (e.g. the `--merge` run after a `--ship` run
    already opened the PR) has it available too.
    """
    workdir = Path(workdir)
    rundir = Path(rundir)
    known_pr_url = ledger.known_pr_url(workdir, plan_name, stage_name) if plan_name else None

    def _blocked(reason: str, **kw) -> TailResult:
        result = TailResult(stage_name=stage_name, status=BLOCKED, reason=reason, **kw)
        cycles_log.record_ship(
            workdir=workdir, stage_name=stage_name, passed=False,
            gates={"final_stage": False}, reason=reason,
        )
        return result

    unavailable = gh_available(workdir)
    if unavailable:
        return _blocked(unavailable)

    decision, status, dispatches, reason = run_final_phase(
        workdir=workdir, stage_name=stage_name, pr_title=pr_title, profile=profile,
        resolved=resolved, rundir=rundir, budget=budget, recurrence=recurrence,
        cycle_verdict=cycle_verdict, ship_gate_fn=ship_gate_fn, base=base,
        run_role_fn=run_role_fn,
    )
    audit = next((d for d in dispatches if d.role == "compliance"), None)
    if status != "passed":
        return _blocked(reason, final_audit=audit, role_results=dispatches, ship_decision=decision)

    msg, msg_reason = build_squash_message(unit_type=unit_type, title=pr_title)
    if msg is None:
        return _blocked(msg_reason, final_audit=audit, role_results=dispatches, ship_decision=decision)

    url, pr_reason = create_pr(workdir, msg, known_pr_url=known_pr_url)
    if url is None:
        return _blocked(pr_reason, squash_message=msg, final_audit=audit,
                        role_results=dispatches, ship_decision=decision)
    if plan_name:
        ledger.mark_pr_created(workdir, plan_name, stage_name, url)

    if not merge:
        return TailResult(
            stage_name=stage_name, status=PR_OPEN,
            reason=f"{pr_reason}; merge not requested (pass merge=True to squash-merge)",
            pr_url=url, squash_message=msg, final_audit=audit, role_results=dispatches,
            ship_decision=decision,
        )

    # Re-checked here, not only inside the final phase: create_pr()'s own
    # push/gh-pr-create round trip takes more time for base to move in.
    # Not looped back into the final phase on failure -- see the module
    # docstring's "what still fails loudly rather than being retried".
    fresh, gate_reason = is_up_to_date(workdir, base=base)
    if not fresh:
        return _blocked(gate_reason, pr_url=url, squash_message=msg,
                        final_audit=audit, role_results=dispatches, ship_decision=decision)

    merged, merge_reason = squash_merge(workdir, msg)
    if not merged:
        return _blocked(merge_reason, pr_url=url, squash_message=msg,
                        final_audit=audit, role_results=dispatches, ship_decision=decision)

    cycles_log.record_ship(
        workdir=workdir, stage_name=stage_name, passed=True,
        gates={"final_stage": True}, reason=merge_reason,
    )
    return TailResult(
        stage_name=stage_name, status=MERGED, reason=merge_reason, pr_url=url,
        squash_message=msg, final_audit=audit, role_results=dispatches,
        ship_decision=decision,
    )
