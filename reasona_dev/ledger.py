"""Per-plan, per-PR-unit progress ledger -- lets `run-plan` resume after an
interruption (network failure, killed process) without redoing finished
work, automatically, on a plain re-run of the same command.

**Layout: `<workdir>/.reasona/log/<plan_name>/<stage_name>/`, not a flat
`.reasona/`.** Every runtime artifact used to live directly under
`.reasona/` keyed only by `stage_name` (`.reasona/runs/pr-1/...`,
`.reasona/ledger-pr-1.json`) -- fine for one plan at a time, but two
different plans that both happen to name a unit `pr-1` (a common name,
since `plan_compile._stage_name()` just does `f"pr-{index}"`) would
silently share the same files and corrupt each other's state and raw role
output. Namespacing by `plan_name` first (the plan document's own stem,
the same value `cli.py` already uses for `plan_name=` when compiling)
makes every path plan-scoped, and namespacing by `stage_name` under that
keeps every PR unit's own ledger/runs separate within a plan -- both
levels double as a natural place to go look at what actually happened
later (`.reasona/log/<plan>/<pr-N>/*.raw.txt` is a real, browsable per-unit
history sitting right next to that unit's own ledger, not a shared bucket
a second plan can stomp on).

**Two kinds of state, both under the same directory:**

    ledger-plan.json                 one flag: has cycle-0 for this plan
                                       already been dispatched?
    <stage>/ledger.json               review/scan cycle-level checkpoint,
                                       the PR unit's terminal outcome, and a
                                       merge-tail PR-url hint (see below)
    <stage>/<role>-c<cycle>.raw.txt   raw per-role output (same file shape
                                       `pr_cycle.run_role()` always wrote,
                                       just under the new path, alongside
                                       ledger.json rather than a separate
                                       runs/ subdirectory)

**What actually gets checkpointed, and why this closes the FixBudget gap.**
`pr_cycle.run_pr_cycle()` now writes its progress (`FixBudget`,
`RecurrenceTracker`, `ConvergenceTracker`, the current cycle number, the
recheck route, the pending MUST_FIX list to confirm) to this ledger after
every review/scan cycle, not just once at the very end. A resumed run
loads that progress and restores those objects instead of constructing
fresh ones at cycle 0 -- the same class of fix "ask git/gh what actually
happened" gives `final_phase.py` for free (§3.9), applied here explicitly
because `FixBudget`/`RecurrenceTracker` are in-memory Python objects with
no git/gh equivalent to re-derive them from.

**The merge-tail PR-url hint is an optimization, not a correctness
requirement.** `final_phase.create_pr()` was already idempotent on its own
(`gh pr view` before creating) -- recording the URL here just lets a
resumed run skip that lookup when it already knows the answer, falling
back to asking `gh` exactly as before when it doesn't.
"""

from __future__ import annotations

import json
from pathlib import Path


def _read(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def log_dir(workdir: str | Path, plan_name: str) -> Path:
    return Path(workdir) / ".reasona" / "log" / plan_name


def unit_dir(workdir: str | Path, plan_name: str, stage_name: str) -> Path:
    """`<workdir>/.reasona/log/<plan_name>/<stage_name>/` -- also where
    `pr_cycle.run_role()` writes each cycle's raw per-role output
    (`<role>-c<cycle>.raw.txt`) directly, alongside this unit's `ledger.json`;
    no further nesting, so `ls` on one PR unit's directory already shows
    everything that happened in it."""
    return log_dir(workdir, plan_name) / stage_name


def _plan_ledger_path(workdir: str | Path, plan_name: str) -> Path:
    return log_dir(workdir, plan_name) / "ledger-plan.json"


def _unit_ledger_path(workdir: str | Path, plan_name: str, stage_name: str) -> Path:
    return unit_dir(workdir, plan_name, stage_name) / "ledger.json"


# --- plan-level: cycle-0 -----------------------------------------------------

def dev_already_dispatched(workdir: str | Path, plan_name: str) -> bool:
    return _read(_plan_ledger_path(workdir, plan_name)).get("dev") == "done"


def mark_dev_dispatched(workdir: str | Path, plan_name: str) -> None:
    _write(_plan_ledger_path(workdir, plan_name), {"dev": "done"})


# --- unit-level: review/scan cycle progress ----------------------------------

def save_progress(workdir: str | Path, plan_name: str, stage_name: str, progress: dict) -> None:
    """`progress` is whatever `pr_cycle.run_pr_cycle()` needs back to resume
    -- this module does not interpret its shape, only persists it alongside
    whatever else is already in the unit's ledger (terminal status, the
    merge-tail PR-url hint)."""
    path = _unit_ledger_path(workdir, plan_name, stage_name)
    data = _read(path)
    data["progress"] = progress
    _write(path, data)


def load_progress(workdir: str | Path, plan_name: str, stage_name: str) -> dict | None:
    return _read(_unit_ledger_path(workdir, plan_name, stage_name)).get("progress")


def clear_progress(workdir: str | Path, plan_name: str, stage_name: str) -> None:
    """Called once a unit reaches a terminal outcome -- a shipped/failed unit
    has nothing left to resume, and leaving a stale in-progress checkpoint
    around would outlive its meaning if the SAME stage name were ever reused
    (a manifest edited to restart a unit's index, for instance)."""
    path = _unit_ledger_path(workdir, plan_name, stage_name)
    data = _read(path)
    data.pop("progress", None)
    _write(path, data)


# --- unit-level: terminal outcome --------------------------------------------

def unit_status(workdir: str | Path, plan_name: str, stage_name: str) -> str | None:
    """The outcome status ("shipped" | "failed") recorded for this unit's
    last completed run in this plan, or None if it never reached one."""
    return _read(_unit_ledger_path(workdir, plan_name, stage_name)).get("status")


def mark_unit_terminal(workdir: str | Path, plan_name: str, stage_name: str, *, status: str, reason: str) -> None:
    path = _unit_ledger_path(workdir, plan_name, stage_name)
    data = _read(path)
    data["status"] = status
    data["reason"] = reason
    data.pop("progress", None)  # terminal -- nothing left to resume
    _write(path, data)


# --- unit-level: merge-tail PR-url hint ---------------------------------------

def known_pr_url(workdir: str | Path, plan_name: str, stage_name: str) -> str | None:
    return _read(_unit_ledger_path(workdir, plan_name, stage_name)).get("pr_url")


def mark_pr_created(workdir: str | Path, plan_name: str, stage_name: str, pr_url: str) -> None:
    path = _unit_ledger_path(workdir, plan_name, stage_name)
    data = _read(path)
    data["pr_url"] = pr_url
    _write(path, data)


# --- clearing (--restart) ----------------------------------------------------

def clear(workdir: str | Path, plan_name: str, stage_names: list[str]) -> None:
    """Wipe every ledger file for this plan -- `--restart` uses this to force
    a full re-run instead of resuming. Raw per-role output already written
    under each stage's directory is left alone; it's a record of what
    happened, not resume state, and `--restart` overwriting it in place
    (same cycle numbers, fresh content) is no worse than a first run ever
    was."""
    _plan_ledger_path(workdir, plan_name).unlink(missing_ok=True)
    for name in stage_names:
        _unit_ledger_path(workdir, plan_name, name).unlink(missing_ok=True)
