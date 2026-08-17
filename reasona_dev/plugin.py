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

CREDIT-BURN post-hoc monitor (docs/ARCHITECTURE.md §3.6): Bernstein's own
retry-escalation (``task_lifecycle.py``'s two independent retry paths --
``retry_or_fail_task`` and ``maybe_retry_task``) can silently bump a retried
task's model to a stronger tier, and there is no declarative
``bernstein.yaml``/``plan.yaml`` surface that fully prevents it (the
``maybe_retry_task`` tick-loop path has no ``role_model_policy`` parameter
at all -- it stamps a Claude tier name unconditionally). ``on_pre_task_create``
cannot detect this even after the fact: its hookspec omits ``model`` entirely,
even though the server has the value in scope at the point it fires the hook.
``on_agent_spawned(session_id, role, model)`` is the one lifecycle hook that
actually receives the model a session was spawned with (verified live call
site: ``core/agents/spawner_core.py:4648``), so that is where this monitor is
wired instead. It cannot block -- the agent is already running by the time
this fires -- but it logs loudly and records the divergence, which is the
"never silently" half of CREDIT-BURN even where the "never" half is currently
unenforceable.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from bernstein.plugins import hookimpl  # verified real: pluggy.HookimplMarker("bernstein")

from reasona_dev.cycle_gate import FixBudget, GateDecision, RecurrenceTracker, evaluate
from reasona_dev.finding_adapter import ReviewResult
from reasona_dev.model_config import resolve_all

logger = logging.getLogger(__name__)

_STATE_DIR = Path(".reasona")
_STATE_FILE = _STATE_DIR / "gate_state.json"
_DIVERGENCE_LOG = _STATE_DIR / "model_divergence.jsonl"

# Bernstein's own agent-role vocabulary (plan.yaml step `role`, review.yaml
# agent `role`) differs from reasona_dev.model_config's role keys -- this is
# the same mapping plan_compile.py / review_pipeline.py already establish by
# construction (dev_role="backend" default; review.yaml agent roles
# "reviewer"/"bugbot"/"compliance"). "reviewer" maps to two config roles
# because the same Bernstein role name is reused for both the initial
# review pipeline (resolved["review"]) and the bounded recheck pipeline
# (resolved["recheck"]) -- either is a legitimate expected value, so both
# must be accepted to avoid a false positive when bounded recheck is in use.
# "ocr_reviewer" has no model slot (adapter="ocr", stateless tool) and is
# intentionally absent -- there is nothing to compare it against.
_SPAWN_ROLE_TO_CONFIG_ROLES: dict[str, tuple[str, ...]] = {
    "backend": ("dev",),
    "reviewer": ("review", "recheck"),
    "bugbot": ("bugbot",),
    "compliance": ("verify",),
}


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
            escalation_model=resolve_all()["dev_escalation"].model,
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

    @hookimpl
    def on_agent_spawned(self, session_id: str, role: str, model: str) -> None:
        """Log loudly when a spawned session's model diverges from what
        `reasona_dev.model_config` resolved for its role.

        Cannot block (see module docstring) -- this is detection, not
        prevention. `role` not present in `_SPAWN_ROLE_TO_CONFIG_ROLES`
        (e.g. `ocr_reviewer`, or any role this project didn't define) is
        silently skipped -- there is no expectation to compare against.
        """
        expected = _expected_models(role)
        if not expected or model in expected:
            return
        _record_divergence(session_id=session_id, role=role, expected=expected, actual=model)


def _expected_models(role: str) -> set[str]:
    config_roles = _SPAWN_ROLE_TO_CONFIG_ROLES.get(role)
    if not config_roles:
        return set()
    resolved = resolve_all()
    return {resolved[r].model for r in config_roles if r in resolved}


def _record_divergence(*, session_id: str, role: str, expected: set[str], actual: str) -> None:
    _STATE_DIR.mkdir(exist_ok=True)
    record = {
        "session_id": session_id,
        "role": role,
        "expected_models": sorted(expected),
        "actual_model": actual,
    }
    with _DIVERGENCE_LOG.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
    logger.warning(
        "reasona-dev CREDIT-BURN monitor: session %s (role=%r) spawned with model=%r, "
        "expected one of %s -- reasona_dev.model_config was not the source of this model "
        "(likely Bernstein retry escalation; see docs/ARCHITECTURE.md §3.6)",
        session_id,
        role,
        actual,
        sorted(expected),
    )


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
