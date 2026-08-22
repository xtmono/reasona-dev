"""Dispatches one role by shelling out to `bernstein run` with a one-step
plan -- the same CLI surface an operator types by hand.

**Why batch and not a long-lived server.** This module replaces an earlier
one that kept a `bernstein serve` + `bernstein worker` pair alive for a whole
plan and posted each role as `POST /tasks`. That design was chosen to avoid
paying Bernstein's bootstrap once per dispatch. The premise was never
measured, and when it finally was, it did not hold: a `bernstein run`
bootstrap takes ~1.0s against ~90s for the agent it starts. Roughly one
percent.

What the premise cost, on the other hand, was concrete. Three of the defects
found in live verification came from running Bernstein in a shape it does not
support:

- `bernstein start` turned out to be a seed bootstrap, not a bare server, so
  the spawner never came up and every dispatch sat unclaimed.
- The raw orchestrator module IS the batch engine's claim loop and self-stops
  on quiescence by design, stranding every task posted after the first stage
  drained.
- Task completion had to be detected from the artifact rather than the task
  status, because Bernstein's orphan-completion path raises on a
  non-serializable field and parks finished work at `claimed` forever.

`bernstein run` has none of those failure modes: it drives one dispatch to
completion and exits, with Bernstein's own watchdog, retry and worktree
salvage supervising it. Everything this module gave up by going to HTTP comes
back, and the thing it bought turns out to be worth one percent of the
runtime.

**Turn budget is the only resource control here, and it is `scope`.**

`Task.max_turns` reaches the CLI's `--max-turns` directly, but it is settable
only over the HTTP API -- the plan-step schema has no such field. The obvious
substitute, `complexity`, does NOT work: `compute_max_turns()` in
`core/agents/claude_max_turns.py` maps complexity to a turn budget, but it
has no production call site anywhere in Bernstein -- every reference outside
its own module is a comment. Setting `complexity: high` and expecting 80
turns was verified live and failed at 23.

What actually computes the budget, when no explicit override is supplied, is
in the claude adapter:

    max_turns = effort_base_turns[effort] * scope_multipliers[scope]

    effort:  max=100  high=50  medium=30  normal=25  low=15
    scope:   small=1.0  medium=1.5  large=2.0        (default 1.5)

which is why a `effort: low` step with no scope got 15 x 1.5 = 22 -- the
23-turn death observed live, twice. A review dispatch therefore declares
`scope: large`, and its budget follows the effort the role is already
configured with: `low` -> 30, `high` -> 100, `max` -> 200.

That coupling is deliberate. Turn budget and reasoning effort are both
"how much work is this role allowed to do", so a role configured for a
cheap model and low effort SHOULD get fewer turns -- what broke live was
not the coupling but the missing scope multiplier.

Cost capping is deliberately NOT attempted. Bernstein's `--hard-budget`
exists but cannot fire on this path: the agent reports its own spend in its
runner log (`[RESULT] ... cost=$0.1736`) while `runtime/costs/*.json`
records `spent_usd: 0.0` and Bernstein's own retrospective logs the gap
("agent_metrics total was $0.0000, falling back to source=task"). A cap that
cannot observe spend is not a cap, so this module does not pretend to offer
one.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

# `scope` is the turn-budget multiplier the claude adapter actually applies
# (see module docstring: complexity is dead code in Bernstein). `large`
# doubles the effort-derived base, which is the widest the adapter offers
# without an explicit per-task override this path cannot set.
#
# The live failure that made this necessary died at 23 turns having completed
# its analysis and written nothing: the review prompt enumerates every
# checklist item and named symbol, greps the diff, and only then writes its
# report -- so a budget that runs out during exploration loses the whole
# result rather than truncating it.
DEFAULT_ROLE_SCOPE = "large"

# Every plan.yaml `write_role_plan()` writes has exactly ONE stage and
# exactly ONE step (one role, one dispatch -- module docstring). Bernstein's
# own plan loader assigns a task id of `f"plan-{stage_index}-{step_index}"`
# from a plain 0-based `enumerate()` over stages and steps (verified against
# the currently installed 3.16.0 source, `core/planning/plan_loader.py`
# `_parse_step()`/`_parse_stage()`) -- so this dispatch shape's task id is
# ALWAYS exactly this string, knowable before the run even starts, with no
# need to read it back from Bernstein. See `pr_cycle._build_role_description()`
# for what this is used for and the incident that motivated it
# (`docs/ARCHITECTURE.md` §3.21).
SINGLE_STEP_TASK_ID = "plan-0-0"

# dev-ralf's own per-role dispatch timeouts (`reference/dispatch.md`:
# "dev 3600s (60 min, also applies to an escalated dev dispatch -- it is a
# dev-shaped fix cycle on a different executor, not a review); all other
# roles (review/recheck/bugbot/compliance/final_audit) 900s (15 min)").
# reasona-dev's dev/fix dispatches -- cycle-0, review-fix, scan-fix,
# sync-conflict-fix, ship-gate-fix, final-audit-fix, gh-review's own
# auto-fix, and the PR-body summary dispatch (`gh_pr.generate_pr_summary()`)
# -- all go out under the Bernstein role string `"backend"`; every other
# role this project dispatches (`reviewer`, `ocr_reviewer`, `bugbot`,
# `compliance`, `final_audit`) is review-shaped in dev-ralf's own sense.
# Previously this project used one flat 3600s for every role regardless --
# found during a timeout survey against dev-ralf's own reference docs
# (`docs/ARCHITECTURE.md` §3.22): a review/compliance/bugbot dispatch could
# silently run up to four times longer than dev-ralf ever allowed it to.
ROLE_DISPATCH_TIMEOUT_SECONDS = 900
DEV_ROLE_DISPATCH_TIMEOUT_SECONDS = 3600
DEV_SHAPED_ROLES = frozenset({"backend"})


def role_dispatch_timeout(role: str) -> int:
    """The per-role dispatch timeout `run_role()` passes to `run_plan_file()`."""
    return DEV_ROLE_DISPATCH_TIMEOUT_SECONDS if role in DEV_SHAPED_ROLES else ROLE_DISPATCH_TIMEOUT_SECONDS


@dataclass
class DispatchResult:
    """What `bernstein run` did, for diagnostics only.

    The role's actual output is the artifact file, never this -- see
    `pr_cycle`'s file-handoff convention. This exists so a missing artifact
    can be explained (non-zero exit, stderr tail) instead of reported as a
    bare "no output".
    """

    returncode: int
    stderr_tail: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def write_role_plan(
    *,
    path: Path,
    role: str,
    title: str,
    description: str,
    model: str,
    effort: str,
    cli: str,
    scope: str = DEFAULT_ROLE_SCOPE,
    files: list[str] | None = None,
) -> None:
    """A one-step plan.yaml for a single role dispatch.

    `cli` goes at the plan level because the step schema has no adapter
    field -- verified against `core/planning/plan_schema.py`. Since each of
    these plans carries exactly one step, a plan-wide adapter is not a loss
    of expressiveness.

    No `completion_signals`. They are evaluated at the project root BEFORE
    the agent's branch merges, so they cannot see the artifact they would be
    gating on; the driver checks the file itself after the run returns.

    `files`, when given, becomes the step's `files:` field -- Bernstein's
    own plan schema maps this to `Task.owned_files` (`core/planning/
    plan_loader.py`). This is NOT for reasona-dev's own use (the driver
    never reads it back); it exists so Bernstein's OWN internal janitor can
    attribute a completed task's work to real changed files. A real
    incident (TAS plan 49 PR2, 2026-08-22, `docs/ARCHITECTURE.md` §3.20/
    §3.21) showed what happens without it: the janitor's attribution logic
    (`core/quality/janitor.py`'s `_attribute_task_work()`) tries `git log
    --grep=<task_id>` first (reasona-dev's own commit messages never
    contain Bernstein's task id -- they follow this project's own
    Conventional-Commits convention), then falls back to a `git diff`
    scoped to `owned_files` -- and gives up entirely, attributing NOTHING,
    when `owned_files` is empty too. With nothing attributed and no
    `completion_signals` to fall back on either, the janitor's "empty-diff
    guard" hard-rejects a task that genuinely did real, committed work,
    Bernstein skips the merge-back of that commit onto the unit branch
    entirely, and its own worktree-cleanup step (unconditional, does not
    check whether the merge actually happened) deletes the only branch
    that ever pointed at it -- the commit survives only as a dangling,
    unreachable git object. Passing `files` (the unit's own manifest
    `files:` list) gives the fallback attribution path real ground to
    stand on even though reasona-dev still does not stamp task ids into
    commit messages.
    """
    plan = {
        "name": f"reasona-dev-{role}",
        "description": f"reasona-dev {role} dispatch",
        "cli": cli,
        "stages": [
            {
                "name": role,
                "steps": [
                    {
                        "title": title,
                        "description": description,
                        "role": role,
                        "model": model,
                        "effort": effort,
                        "scope": scope,
                        **({"files": files} if files else {}),
                    }
                ],
            }
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(plan, sort_keys=False, allow_unicode=True), encoding="utf-8")


def stop_leftovers(workdir: Path) -> None:
    """`bernstein stop` between dispatches -- required, not hygiene.

    `bernstein run` detaches its task server and watchdog and does NOT reap
    them on exit. A driver that issues one `bernstein run` per role dispatch
    -- six or more per PR unit -- has to reap between them or the next
    dispatch in the SAME workdir finds a previous run's server still
    listening, holding a different auth token:

        bernstein run exit=1
        Client error '401 Unauthorized' for url 'http://127.0.0.1:8052/tasks'

    with three leftover uvicorn/watchdog processes still alive in the
    workdir. `bernstein stop`, called with `cwd=workdir`, only targets
    processes whose OWN cwd matches (confirmed against the installed
    package's `cli/commands/stop_cmd.py`: `process_cwd(pid) != workdir` is
    filtered out before anything is touched) -- so this is scoped per unit
    worktree, never cross-unit.

    **Port propagation was re-verified against the currently installed
    Bernstein (3.16.0) and is correct.** An earlier note here claimed
    `bernstein run` "does not propagate `--port` to the orchestrator
    subprocess, which re-derives the 8052 default regardless of what the
    CLI was given" -- true of 3.15.1, no longer true: `run_bootstrap.run()`
    -> `bootstrap_from_seed(port=port)` -> `_start_spawner(workdir, port,
    ...)`/`_start_server(workdir, port, ...)` all thread the CLI's own
    `--port` value through to the actual `uvicorn ... --port <port>` bind,
    traced end to end (`docs/ARCHITECTURE.md` §3.14.3). `run-plan --job K`
    (K concurrent units, one port each) is safe on this version for
    exactly the reason this function's docstring already argued it needed
    to be.
    """
    subprocess.run(
        ["bernstein", "stop"], cwd=str(workdir),
        capture_output=True, text=True, timeout=120, check=False,
    )


def run_plan_file(
    plan_path: Path,
    workdir: Path,
    *,
    port: int = 8052,
    timeout: int = 3600,
) -> DispatchResult:
    """`bernstein run <plan> --auto-approve`, wait, then reap.

    Synchronous by construction: the run spawns, executes, merges and exits,
    so there is nothing to poll. It does leave its detached server and
    watchdog behind, so `stop_leftovers` runs afterwards -- see there for why
    that is required rather than tidy.

    A non-zero exit is reported rather than raised: the caller decides what a
    failed dispatch means, and for most roles the artifact's presence is the
    real verdict anyway.
    """
    try:
        proc = subprocess.run(
            [
                "bernstein", "run", str(plan_path),
                "--auto-approve", "--quiet", "--port", str(port),
            ],
            cwd=str(workdir), capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return DispatchResult(returncode=127, stderr_tail="bernstein: not found on PATH")
    except subprocess.TimeoutExpired:
        return DispatchResult(returncode=124, stderr_tail=f"bernstein run timed out after {timeout}s")
    except OSError as exc:
        return DispatchResult(returncode=1, stderr_tail=str(exc))
    finally:
        # In `finally`: a timed-out or crashed run leaves the same detached
        # processes behind as a clean one, and the NEXT dispatch is the thing
        # that breaks if they survive.
        try:
            stop_leftovers(workdir)
        except (subprocess.SubprocessError, OSError):
            pass

    tail = (proc.stderr or proc.stdout or "").strip()
    return DispatchResult(returncode=proc.returncode, stderr_tail=tail[-400:])
