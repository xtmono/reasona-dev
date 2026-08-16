"""pluggy hookimpl wiring reasona_dev.cycle_gate into Bernstein's lifecycle.

Registered under Bernstein's real entry-point group ``bernstein.plugins``
(verified: `plugins/manager.py` -> ``entry_points(group="bernstein.plugins")``,
`plugins/__init__.py` -> ``hookimpl = pluggy.HookimplMarker("bernstein")``).

Honest limitation: ``on_pre_task_create``'s hookspec signature is
``(task_id, role, title, description) -> None`` -- it can VETO (raise to
block, per its docstring) but cannot rewrite the task's model/adapter before
creation. The bounded dev-escalation this project adds (recurring MUST_FIX
key -> one stronger-model attempt) therefore is NOT something this hook can
do by itself. The gate decision is computed here and persisted to
``.reasona/gate_state.json``; the caller that re-invokes
``bernstein run --from-plan`` for the next fix cycle is responsible for
reading ``escalated_model`` from that file and passing it as the step's
``model:`` override. This module supplies the decision, not the re-dispatch.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from bernstein.plugins import hookimpl  # verified real: pluggy.HookimplMarker("bernstein")

from reasona_dev.cycle_gate import FixBudget, GateDecision, RecurrenceTracker, evaluate
from reasona_dev.finding_adapter import ReviewResult

_STATE_DIR = Path(".reasona")
_STATE_FILE = _STATE_DIR / "gate_state.json"


def _load_state() -> dict:
    if _STATE_FILE.exists():
        return json.loads(_STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    _STATE_DIR.mkdir(exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


class ReasonaGatePlugin:
    """One instance registered per run; `pre_task_state` keys are stage names."""

    @hookimpl
    def on_pre_task_create(
        self,
        task_id: str,
        role: str,
        title: str,
        description: str,
    ) -> None:
        """Veto a fix-task's creation once its stage/PR has exhausted budget.

        Only vetoes tasks whose title matches our own fix-task naming
        convention (`title` is set by whatever re-dispatch code calls
        `bernstein run` for a fix cycle -- see module docstring). Tasks this
        plugin has no gate state for (e.g. the PR's initial "implement"
        task) pass through untouched -- this hook is additive, never a
        blanket gate on ordinary task creation.
        """
        if not title.startswith("fix:"):
            return

        stage_key = title.split(":", 1)[1].strip().split(" ", 1)[0]
        state = _load_state()
        entry = state.get(stage_key)
        if entry is None:
            return  # no prior review result recorded for this stage -- nothing to gate on

        result = ReviewResult(**entry["review_result"])
        budget = FixBudget(**entry["budget"])
        recurrence = RecurrenceTracker(
            survived=entry["recurrence"]["survived"],
            escalated=set(entry["recurrence"]["escalated"]),
        )

        decision: GateDecision = evaluate(
            result=result,
            budget=budget,
            stage=entry.get("stage", "review"),
            recurrence=recurrence,
            inconclusive_attempts=entry.get("inconclusive_attempts", 0),
        )

        entry["budget"] = budget.__dict__
        entry["recurrence"]["survived"] = recurrence.survived
        entry["recurrence"]["escalated"] = list(recurrence.escalated)
        entry["last_decision"] = decision.__dict__
        state[stage_key] = entry
        _save_state(state)

        if decision.action in ("fail", "abort"):
            # Raising here is how a hookimpl blocks task creation
            # (hookspecs.py docstring: "hooks may block by raising").
            raise RuntimeError(
                f"reasona-dev gate blocked fix task for {stage_key}: {decision.reason}"
            )
        # spawn_fix / spawn_fix_escalated / pass all proceed; the escalated
        # model, if any, is picked up by the re-dispatch caller from
        # `.reasona/gate_state.json[stage_key]['last_decision']['escalated_model']`.


def record_review_result(stage_key: str, stage: str, result: ReviewResult) -> None:
    """Called by the code that just parsed a reviewer's raw output, before
    the next fix-task creation is attempted -- populates what the hook
    above reads.
    """
    state = _load_state()
    entry = state.setdefault(
        stage_key,
        {
            "budget": FixBudget().__dict__,
            "recurrence": {"survived": {}, "escalated": []},
            "inconclusive_attempts": 0,
        },
    )
    entry["stage"] = stage
    entry["review_result"] = {
        "role_status": result.role_status,
        "findings": result.findings,
        "verdict_tail": result.verdict_tail,
        "contract_mismatch": result.contract_mismatch,
        "schema_version": result.schema_version,
    }
    state[stage_key] = entry
    _save_state(state)
