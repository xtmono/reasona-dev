"""The last third of worker.md: `sync-main -> gh-pr -> up-to-date gate ->
conditional final_audit -> squash-merge`.

**What this consumes rather than re-decides.** `ship_gate` has already
returned a verdict for the unit and `orchestrate` has already recorded it.
Nothing here re-runs review, acceptance or structure checks; a unit arriving
with a failing verdict is refused immediately. Keeping the judgment upstream
is what stops this module from becoming a second, quieter gate.

**Merging is opt-in.** `merge=False` is the default and stops after the PR
exists. A squash-merge is outward-facing and hard to undo -- it rewrites the
default branch of a real repository -- so the caller has to ask for it
explicitly rather than discover it happened. Everything up to that point
(sync, audit, message construction, PR creation) is safe to run repeatedly.

**Every step fails loudly or not at all.** `gh` missing, `gh` unauthenticated,
a sync conflict, a malformed squash title, a PR behind its base: each returns
a `blocked` status naming the condition. None of them degrade into "merged
anyway" or "skipped quietly", because a merge tail that sometimes silently
does nothing is worse than one that refuses -- the operator believes the work
shipped.

**Why final_audit is conditional.** A unit that passed review and both scan
roles on the first cycle has been read by three independent roles with
nothing found; a fresh whole-PR audit there mostly re-derives that. The audit
earns its cost where fixes ACCUMULATED -- each fix is a change no reviewer
saw in its final, combined form, and cross-fix interaction is exactly what a
per-cycle review cannot see. So the trigger is `budget.total_used > 0`: the
PR needed at least one fix. `MAX_FINAL_CYCLES` (2) bounds the audit's own
fix loop through the same `FixBudget`, under the `"final"` stage that until
now had no producer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from reasona_dev import cycles_log, squash
from reasona_dev.bernstein_server import ServerHandle
from reasona_dev.cycle_gate import FixBudget, RecurrenceTracker, evaluate
from reasona_dev.model_config import ResolvedModel
from reasona_dev.pr_cycle import RoleRunResult, _build_fix_prompt, _run_dev_fix, run_role
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

def sync_main(workdir: Path, *, base: str = "origin/main") -> tuple[bool, str]:
    """Fetch and merge `base` into the current branch.

    Merge, not rebase: the branch may already be pushed, and rebasing
    published history forces every later push to be a force-push, which the
    up-to-date gate below then cannot distinguish from someone else's work
    being overwritten.

    A conflict is returned as a failure with the conflicting paths named --
    never auto-resolved. A conflict means main changed something this PR also
    changed, which is a semantic question no deterministic rule here can
    answer.
    """
    remote = base.split("/", 1)[0] if "/" in base else "origin"
    code, _, err = _run(["git", "fetch", remote], workdir)
    if code != 0:
        return False, f"git fetch {remote} failed: {err.strip()[:200]}"

    code, out, err = _run(["git", "merge", "--no-edit", base], workdir)
    if code == 0:
        return True, "up to date with base"

    conflict_code, conflicts, _ = _run(
        ["git", "diff", "--name-only", "--diff-filter=U"], workdir
    )
    paths = [p for p in conflicts.splitlines() if p.strip()] if conflict_code == 0 else []
    _run(["git", "merge", "--abort"], workdir)
    if paths:
        return False, f"merge conflict with {base} in: {', '.join(paths[:5])}"
    return False, f"merge with {base} failed: {(err or out).strip()[:200]}"


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


def create_pr(workdir: Path, msg: SquashMessage) -> tuple[str | None, str]:
    """Create the PR, or return the one that already exists.

    Idempotent on purpose: the tail is safe to re-run after a blocked step,
    and a second `gh pr create` for the same branch fails rather than
    duplicating -- surfacing that failure as an error would make retry look
    broken when it is the normal path.
    """
    existing = existing_pr_url(workdir)
    if existing:
        return existing, "reused existing PR"

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
    server: ServerHandle,
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
            server=server, workdir=workdir, role="compliance",
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
            server=server, workdir=workdir, pr_title=pr_title,
            findings=result.review_result.must_fix, dev_model=resolved["dev"],
            escalated_model=decision.escalated_model, rundir=rundir,
            cycle=cycle, run_role_fn=run_role_fn,
        )
        dispatches.append(fix)


# --- composition ------------------------------------------------------------

def run_merge_tail(
    *,
    server: ServerHandle,
    workdir: str | Path,
    stage_name: str,
    pr_title: str,
    unit_type: str | None,
    profile: str,
    resolved: dict[str, ResolvedModel],
    rundir: str | Path,
    ship_decision: ShipDecision,
    budget: FixBudget,
    recurrence: RecurrenceTracker,
    base: str = "origin/main",
    merge: bool = False,
    run_role_fn=run_role,
) -> TailResult:
    """sync -> audit -> message -> PR -> up-to-date -> (optional) squash-merge."""
    workdir = Path(workdir)
    rundir = Path(rundir)

    def _blocked(reason: str, **kw) -> TailResult:
        result = TailResult(stage_name=stage_name, status=BLOCKED, reason=reason, **kw)
        cycles_log.record_ship(
            workdir=workdir, stage_name=stage_name, passed=False,
            gates={"merge_tail": False}, reason=reason,
        )
        return result

    if not ship_decision.passed:
        return _blocked(f"ship gate did not pass: {ship_decision.reason}")

    unavailable = gh_available(workdir)
    if unavailable:
        return _blocked(unavailable)

    ok, reason = sync_main(workdir, base=base)
    if not ok:
        return _blocked(reason)

    role_results: list[RoleRunResult] = []
    audit: RoleRunResult | None = None
    if should_run_final_audit(budget):
        passed, audit_reason, dispatches = run_final_audit(
            server=server, workdir=workdir, stage_name=stage_name, pr_title=pr_title,
            profile=profile, resolved=resolved, rundir=rundir, budget=budget,
            recurrence=recurrence, run_role_fn=run_role_fn,
        )
        role_results.extend(dispatches)
        audit = dispatches[0] if dispatches else None
        if not passed:
            return _blocked(audit_reason, final_audit=audit, role_results=role_results)

    msg, msg_reason = build_squash_message(unit_type=unit_type, title=pr_title)
    if msg is None:
        return _blocked(msg_reason, final_audit=audit, role_results=role_results)

    url, pr_reason = create_pr(workdir, msg)
    if url is None:
        return _blocked(pr_reason, squash_message=msg, final_audit=audit, role_results=role_results)

    if not merge:
        return TailResult(
            stage_name=stage_name, status=PR_OPEN,
            reason=f"{pr_reason}; merge not requested (pass merge=True to squash-merge)",
            pr_url=url, squash_message=msg, final_audit=audit, role_results=role_results,
        )

    # Re-checked here, not only after sync: base can advance in between.
    fresh, gate_reason = is_up_to_date(workdir, base=base)
    if not fresh:
        return _blocked(gate_reason, pr_url=url, squash_message=msg,
                        final_audit=audit, role_results=role_results)

    merged, merge_reason = squash_merge(workdir, msg)
    if not merged:
        return _blocked(merge_reason, pr_url=url, squash_message=msg,
                        final_audit=audit, role_results=role_results)

    cycles_log.record_ship(
        workdir=workdir, stage_name=stage_name, passed=True,
        gates={"merge_tail": True}, reason=merge_reason,
    )
    return TailResult(
        stage_name=stage_name, status=MERGED, reason=merge_reason, pr_url=url,
        squash_message=msg, final_audit=audit, role_results=role_results,
    )
