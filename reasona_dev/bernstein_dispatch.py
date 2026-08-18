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

**Turn budget is the only resource control here, and it is expressed as
`complexity`.** `Task.max_turns` is reachable only through the HTTP API --
the plan-step schema has no such field -- but Bernstein derives max_turns
from a step's `complexity` (`core/agents/claude_max_turns.py`:
low=20, medium=40, high=80, critical=120, adjusted by model tier), and the
claude adapter forwards the result to the CLI's `--max-turns`. So the batch
path keeps the control that fixed the live `error_max_turns` failure, just
declared differently.

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

# Bernstein maps a step's `complexity` onto the agent's turn budget
# (claude_max_turns._BASE_TURNS). `high` is 80 turns before the model-tier
# adjustment. The live failure that made this necessary died at 23 turns
# having completed its analysis and written nothing -- the review prompt
# enumerates every checklist item and named symbol, greps the diff, and only
# then writes its report, so a budget that runs out during exploration loses
# the whole result rather than truncating it.
DEFAULT_ROLE_COMPLEXITY = "high"


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
    complexity: str = DEFAULT_ROLE_COMPLEXITY,
) -> None:
    """A one-step plan.yaml for a single role dispatch.

    `cli` goes at the plan level because the step schema has no adapter
    field -- verified against `core/planning/plan_schema.py`. Since each of
    these plans carries exactly one step, a plan-wide adapter is not a loss
    of expressiveness.

    No `completion_signals`. They are evaluated at the project root BEFORE
    the agent's branch merges, so they cannot see the artifact they would be
    gating on; the driver checks the file itself after the run returns.
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
                        "complexity": complexity,
                    }
                ],
            }
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(plan, sort_keys=False, allow_unicode=True), encoding="utf-8")


def run_plan_file(
    plan_path: Path,
    workdir: Path,
    *,
    port: int = 8052,
    timeout: int = 3600,
) -> DispatchResult:
    """`bernstein run <plan> --auto-approve` and wait for it to finish.

    Synchronous by construction: the run spawns, executes, merges and exits,
    so there is nothing to poll and no server whose lifetime this module has
    to own. A non-zero exit is reported rather than raised -- the caller
    decides what a failed dispatch means, and for most roles the artifact's
    presence is the real verdict anyway.
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

    tail = (proc.stderr or proc.stdout or "").strip()
    return DispatchResult(returncode=proc.returncode, stderr_tail=tail[-400:])
