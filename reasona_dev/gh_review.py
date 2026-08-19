"""Ports `/gh-review`'s auto-fix loop (`~/repository/tas-dev-plugins/
plugins/dev/skills/gh-review/SKILL.md` §3): poll `gh_review_watch`'s three
signals (CI, TAS PR Compliance Review, Claude BugBot Analysis) on the PR's
current head SHA, dispatch dev against whatever is actionable, push once,
re-poll -- bounded by both a wall-clock budget and a cycle-count budget.

**Two different kinds of budget, tracked separately.** Waiting for CI/bot
workflows to finish is wall-clock time, not a dev-fix attempt --
`max_wait_seconds` (`time.monotonic()`) bounds that, independent of
`FixBudget`. Actually dispatching a fix IS the same kind of resource every
other stage in this pipeline spends -- `budget`'s `"gh_review"` stage
(`cycle_gate.MAX_GH_REVIEW_CYCLES`) bounds that, pooled into the same
`MAX_TOTAL_FIX_CYCLES` ceiling the rest of the pipeline shares, mirroring
dev-ralf's own `min(max_cycle, fix_cycles_max - fix_cycles_total)` pooling
rule (confirmed against `/gh-review`'s reference material).

**Exhausting either budget is `blocked`, not `failed`.** By the time this
runs, review, scan, and ship_gate have already passed -- CI/compliance/
bugbot failing here is either a defect those earlier, LOCAL checks could
not see (this is exactly why the watched signals are a genuinely separate
check, not a duplicate -- see `gh_review_watch.py`'s own docstring) or an
external stall (workflow never completing, `gh` flake). Neither is treated
as a code-quality verdict this deep in the pipeline; see
`cycle_gate.MAX_SHIP_CYCLES`'s docstring for the identical reasoning
applied to ship_gate's own bounded fix loop.

**One push per cycle, after every actionable signal in that cycle is
handled** -- `/gh-review` SKILL.md §3.3's own rule: a single new head SHA
re-triggers every workflow at once, so batching avoids re-triggering CI
multiple times for findings that arrived in the same snapshot. Unlike the
original skill (which runs "in the dispatching agent" and writes its own
prose replies to compliance/bugbot comments after reading their content),
this dispatches `reasona_dev.pr_cycle.run_role`'s `backend` role to make
the actual fix, then pushes deterministically itself -- the same split
`reasona_dev.final_phase`'s sync-conflict/ship-gate fix loops already use.
Reply bullets are not fabricated from a summary this deterministic layer
does not actually have; the posted reply names the fixing commit only.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from reasona_dev import _shell, gh_review_watch as watch
from reasona_dev.cycle_gate import MAX_GH_REVIEW_CYCLES, MAX_TOTAL_FIX_CYCLES, FixBudget
from reasona_dev.model_config import ResolvedModel
from reasona_dev.pr_cycle import RoleRunResult, run_role

POLL_INTERVAL_SECONDS = 30
DEFAULT_MAX_WAIT_SECONDS = 900


@dataclass
class GhReviewResult:
    passed: bool
    reason: str
    ci_green: bool = False
    bots_approved: int = 0
    bots_pending: int = 0
    bots_unfixable: int = 0
    fix_commits: list[str] = field(default_factory=list)
    watcher_calls: int = 0
    elapsed_sec: float = 0.0
    dispatches: list[RoleRunResult] = field(default_factory=list)


def owner_repo_from_pr_url(pr_url: str) -> tuple[str, str] | None:
    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/\d+", pr_url)
    if not m:
        return None
    return m.group(1), m.group(2)


def _current_branch(workdir: Path) -> str | None:
    code, out, _ = _shell.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], workdir, timeout=30)
    return out.strip() if code == 0 else None


def _ci_fix_prompt(workdir: Path, failing_checks: list[str]) -> str:
    branch = _current_branch(workdir) or "HEAD"
    excerpts = []
    for name in failing_checks[:5]:  # cap -- context economy, matches the original skill's own note
        code, out, err = _shell.run(
            ["gh", "run", "list", "--branch", branch, "--json", "databaseId,name",
             "--jq", f'.[] | select(.name == "{name}") | .databaseId'],
            workdir, timeout=30,
        )
        run_id = out.strip().splitlines()[0] if code == 0 and out.strip() else None
        if run_id is None:
            excerpts.append(f"- {name}: could not resolve a run id via `gh run list`")
            continue
        _, log_out, _ = _shell.run(["gh", "run", "view", run_id, "--log-failed"], workdir, timeout=60)
        head = "\n".join(log_out.splitlines()[:300])
        excerpts.append(f"### {name}\n```\n{head}\n```")
    return "\n\n".join(
        [
            "CI is failing on the following checks. Apply the smallest "
            "targeted fix for each and reproduce locally where possible. "
            "Do not touch anything unrelated to these failures.",
            *excerpts,
        ]
    )


def _compliance_fix_prompt(body: str) -> str:
    return (
        "A GitHub Compliance Review bot found blocking issues on this PR. "
        "Apply minimal, repo-rule-compliant fixes for every blocking finding "
        "below; non-blocking items may be left as-is.\n\n" + body
    )


def _bugbot_fix_prompt(body: str) -> str:
    return (
        "A GitHub BugBot review found issues on this PR. Fix every finding "
        "below, touching only the added/modified lines it points at.\n\n" + body
    )


def _post_reply(
    workdir: Path, *, owner_repo: tuple[str, str], pr_num: int, label: str,
    anchor: str, short_sha: str,
) -> None:
    owner, repo = owner_repo
    body = f"Re: [{label}](https://github.com/{owner}/{repo}/pull/{pr_num}#{anchor}) -- fixed in {short_sha}"
    # Best-effort -- a failed reply does not undo the fix that was already
    # pushed, and is not worth blocking the unit over.
    _shell.run(
        ["gh", "api", "-X", "POST", f"repos/{owner}/{repo}/issues/{pr_num}/comments", "-f", f"body={body}"],
        workdir, timeout=30,
    )


def run_gh_review(
    *,
    workdir: str | Path,
    pr_url: str,
    pr_num: int,
    pr_title: str,
    resolved: dict[str, ResolvedModel],
    rundir: str | Path,
    budget: FixBudget,
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS,
    poll_interval_seconds: int = POLL_INTERVAL_SECONDS,
    run_role_fn=run_role,
) -> GhReviewResult:
    workdir = Path(workdir)
    rundir = Path(rundir)
    owner_repo = owner_repo_from_pr_url(pr_url)
    if owner_repo is None:
        return GhReviewResult(passed=False, reason=f"could not parse owner/repo from PR url: {pr_url}")

    max_cycles = min(MAX_GH_REVIEW_CYCLES, MAX_TOTAL_FIX_CYCLES - budget.total_used)
    start = time.monotonic()
    watcher_calls = 0
    dispatches: list[RoleRunResult] = []
    fix_commits: list[str] = []
    cycle = 0

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= max_wait_seconds:
            return GhReviewResult(
                passed=False, reason=f"timeout: {max_wait_seconds}s exhausted with pending state",
                fix_commits=fix_commits, watcher_calls=watcher_calls, elapsed_sec=elapsed,
                dispatches=dispatches,
            )

        watcher_calls += 1
        try:
            snap = watch.take_snapshot(owner_repo[0], owner_repo[1], pr_num, workdir)
        except watch.FetchError as exc:
            msg = str(exc)
            if msg.startswith("pr not open"):
                return GhReviewResult(
                    passed=False, reason=msg, fix_commits=fix_commits,
                    watcher_calls=watcher_calls, elapsed_sec=time.monotonic() - start, dispatches=dispatches,
                )
            time.sleep(poll_interval_seconds)
            continue

        status = watch.classify(snap)
        elapsed = time.monotonic() - start

        if status == "terminal":
            return GhReviewResult(
                passed=True, reason="ci green, compliance pass, bugbot clean",
                ci_green=True, bots_approved=2, fix_commits=fix_commits,
                watcher_calls=watcher_calls, elapsed_sec=elapsed, dispatches=dispatches,
            )

        if status == "continue":
            time.sleep(poll_interval_seconds)
            continue

        # status == "actionable"
        if cycle >= max_cycles:
            approved = sum(1 for s in (snap["compliance"]["state"], snap["bugbot"]["state"]) if s in ("pass", "clean"))
            unfixable = sum(1 for s in (snap["compliance"]["state"], snap["bugbot"]["state"]) if s in ("fail", "found"))
            return GhReviewResult(
                passed=False, reason=f"fix budget exhausted ({max_cycles} cycles)",
                ci_green=snap["ci"]["state"] == "passing", bots_approved=approved, bots_unfixable=unfixable,
                fix_commits=fix_commits, watcher_calls=watcher_calls, elapsed_sec=elapsed, dispatches=dispatches,
            )

        cycle += 1
        budget.spend("gh_review")
        prompts = []
        if snap["ci"]["state"] == "failing":
            prompts.append(_ci_fix_prompt(workdir, snap["ci"]["failing_checks"]))
        if snap["compliance"]["state"] == "fail":
            prompts.append(_compliance_fix_prompt(snap["compliance"]["body"]))
        if snap["bugbot"]["state"] == "found":
            prompts.append(_bugbot_fix_prompt(snap["bugbot"]["body"]))

        result = run_role_fn(
            workdir=workdir, role="backend", title=f"{pr_title} -- gh-review fix c{cycle}",
            prompt="\n\n---\n\n".join(prompts), model=resolved["dev"], rundir=rundir, cycle=cycle,
        )
        dispatches.append(result)

        code, _, err = _shell.run(["git", "push", "origin", "HEAD"], workdir, timeout=180)
        if code != 0:
            return GhReviewResult(
                passed=False, reason=f"dev agent error: git push failed: {err.strip()[:200]}",
                fix_commits=fix_commits, watcher_calls=watcher_calls,
                elapsed_sec=time.monotonic() - start, dispatches=dispatches,
            )
        _, sha_out, _ = _shell.run(["git", "rev-parse", "--short", "HEAD"], workdir, timeout=30)
        short_sha = sha_out.strip()
        if short_sha:
            fix_commits.append(short_sha)

        if snap["compliance"]["state"] == "fail" and snap["compliance"]["comment_id"] is not None:
            _post_reply(
                workdir, owner_repo=owner_repo, pr_num=pr_num, label="Compliance Review",
                anchor=f"issuecomment-{snap['compliance']['comment_id']}", short_sha=short_sha,
            )
        if snap["bugbot"]["state"] == "found" and snap["bugbot"]["review_id"] is not None:
            _post_reply(
                workdir, owner_repo=owner_repo, pr_num=pr_num, label="Claude BugBot Analysis",
                anchor=f"pullrequestreview-{snap['bugbot']['review_id']}", short_sha=short_sha,
            )
        # Loop -- the push above produced a new head SHA, re-poll from scratch.
