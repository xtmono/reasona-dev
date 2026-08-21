"""One git worktree per PR unit, created before that unit's cycle-0 and
reused through review -> scan -> final phase -> gh-pr -> gh-review ->
squash-merge.

**Why this exists.** Cycle-0 used to be dispatched for a WHOLE plan in one
`bernstein run` call (`cli.py`, before this module), and Bernstein's own
merge-back landed every unit's commits on the SAME branch, sequentially, in
`workdir`. By the time unit 2's review started, unit 1's (and unit 3's)
commits were already mixed into that one branch's history -- there was no
way to open a unit-scoped PR without commit surgery. `docs/ARCHITECTURE.md`
§3.11 has the full account of how this was found.

**Fix: each unit gets its own worktree, from before cycle-0 even runs.**
`orchestrate.py` calls `ensure_unit_worktree()` once per unit, dispatches
that unit's cycle-0 into it (`plan_compile.compile_to_bernstein_plan(...,
only_index=...)`), then runs review/scan/final-phase/gh-pr/gh-review against
that same checkout. Nothing else in this pipeline shares it with another
unit, so there is nothing left to interleave.

**Branch naming is unit-based, not issue-based, on purpose.** `/gh-pr`
(`~/repository/tas-dev-plugins/plugins/dev/skills/gh-pr/SKILL.md` §6) names
its branch `issue/<N>-<slug>` because it can be invoked as a standalone
skill against whatever the caller already has checked out, so it has to
create its OWN branch from scratch. reasona-dev never calls its gh-pr port
standalone -- it is always the tail of a pipeline that already created this
unit's worktree -- so there is no issue number yet when the worktree is
created (the issue is a `gh-pr`-stage artifact). The worktree/branch is
named by the PR unit itself; `gh_pr.py` renames it in place
(`git branch -m`) once the issue exists, the same "on a feature/temp
branch: rename" path `/gh-pr` itself takes when it is not sitting on base.

**`bernstein.yaml` needs to exist in the worktree too.** It is gitignored
in every target repo (`docs/INSTALL.md` §4), so a fresh `git worktree add`
-- which only checks out tracked content -- never carries it across. This
module does not handle that itself: `plan_compile.compile_to_bernstein_plan(
..., workdir=<worktree>, write_bernstein_yaml=True)` (the default) already
bootstraps/syncs it from the same project-or-global template cascade
regardless of which directory `workdir` points at, so calling that against
the worktree (which `orchestrate.py` does immediately after this module
creates it, to dispatch cycle-0) is what actually closes this gap -- see
that function's own docstring.
"""

from __future__ import annotations

from pathlib import Path

from reasona_dev import _shell


def unit_worktree_path(workdir: str | Path, plan_name: str, stage_name: str) -> Path:
    return Path(workdir) / ".worktrees" / plan_name / stage_name


def unit_branch_name(plan_name: str, stage_name: str) -> str:
    return f"reasona/{plan_name}/{stage_name}"


def ensure_unit_worktree(
    workdir: str | Path, plan_name: str, stage_name: str, *, base: str = "origin/main",
) -> tuple[Path, str]:
    """Create (or, on resume, reuse) this unit's dedicated worktree+branch.

    Returns `(worktree_path, branch_name)`. If the worktree directory
    already exists, it is assumed to be a valid worktree left by an earlier,
    interrupted run of this same unit and is reused as-is -- `git worktree
    add` would fail on an existing path anyway, and recreating it would
    throw away whatever cycle-0/review/fix work already landed there.
    """
    workdir = Path(workdir)
    path = unit_worktree_path(workdir, plan_name, stage_name)
    branch = unit_branch_name(plan_name, stage_name)
    if path.is_dir():
        return path, branch

    path.parent.mkdir(parents=True, exist_ok=True)
    # A fetch failure here is not fatal -- `base` may already be resolvable
    # locally (a plain branch name, not a remote-tracking ref), and the
    # worktree-add call below is the real, authoritative check.
    _shell.run(
        ["git", "fetch", base.split("/", 1)[0] if "/" in base else "origin"], workdir, timeout=180,
    )
    code, out, err = _shell.run(
        ["git", "worktree", "add", "-b", branch, str(path), base], workdir, timeout=180,
    )
    if code != 0:
        raise RuntimeError(f"git worktree add failed for {stage_name!r}: {(err or out).strip()[:300]}")
    return path, branch


def remove_unit_worktree(workdir: str | Path, plan_name: str, stage_name: str) -> None:
    """Best-effort cleanup after a unit has actually shipped (squash-merged).

    Never called for a failed/blocked unit -- that worktree is left in
    place as the evidence of what happened, for the operator to inspect.
    Failure here is logged by the caller if it wants to, not raised: a
    leftover worktree after a successful ship is a cleanup nit, not a
    correctness problem, and must never turn a real success into a
    reported failure.
    """
    workdir = Path(workdir)
    path = unit_worktree_path(workdir, plan_name, stage_name)
    if not path.is_dir():
        return
    # The branch may have been renamed since creation (`gh_pr.py` renames it
    # to `issue/<N>-<slug>` once the issue exists) -- read whatever it is
    # NOW from the worktree itself rather than assuming `unit_branch_name()`
    # still applies.
    _, branch_out, _ = _shell.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], path, timeout=30)
    branch = branch_out.strip() or unit_branch_name(plan_name, stage_name)
    _reap_worktree_processes(path)
    _shell.run(["git", "worktree", "remove", "--force", str(path)], workdir, timeout=60)
    _shell.run(["git", "branch", "-D", branch], workdir, timeout=30)


def _reap_worktree_processes(path: Path) -> None:
    """Best-effort: kill anything still running inside this worktree before
    it is removed out from under it -- worker.md's post-merge cleanup
    (`pkill -9 -f "$worktree_path/"`), needed once a local CI command
    (`ci_gate.py`) can leave a build/test child process behind. Never
    raises; `git worktree remove --force` right after this is the real
    guarantee, this step only avoids handing a process a yanked cwd.

    The trailing `/` in the pattern is deliberate, not cosmetic: a bare
    path also matches a SIBLING worktree that shares it as a path prefix
    (`.worktrees/pr-1` vs `.worktrees/pr-10`) -- a real risk under `--job>1`
    concurrent unit dispatch, not a hypothetical one. Never pass a bare
    binary name either; that would match unrelated processes system-wide.
    """
    _shell.run(["pkill", "-9", "-f", f"{path}/"], path.parent, timeout=10)
