"""Bernstein pluggy hookimpl: post-hoc detection of model divergence.

**One hook, deliberately.** This module used to also implement
`on_pre_task_create`, vetoing a fix task whose stage had exhausted its budget,
backed by a `.reasona/gate_state.json` written alongside. That design belonged
to a pipeline where fix cycles were re-dispatched as fresh `bernstein run`
invocations and the plugin was the only place that could see them coming.

`reasona_dev.pr_cycle` now decides before dispatching -- `cycle_gate.evaluate()`
runs in the driver, which holds the budget, the recurrence tracker and the
convergence window in memory. Keeping the hook as well meant two authorities
for one decision, reading state from different places, and in practice a dead
one: nothing ever created a `gate_state.json` entry, so the veto never fired
for any task. It is removed rather than repaired, because a second gate that
agrees is redundant and a second gate that disagrees is a bug.

What remains is the half the driver structurally cannot do.
`on_agent_spawned(session_id, role, model)` is the only hookspec carrying the
model a spawn actually used, so it is the only place a divergence between
"what `model_config` resolved" and "what Bernstein spawned" can be observed at
all. It is non-blocking by nature -- the agent already exists -- but recording
it is what keeps the CREDIT-BURN failure (docs/ARCHITECTURE.md §3.6) from
passing silently: Bernstein's tick-loop retry path escalates a model with no
declarative way to prevent it, so detection is the available defence.

Divergences land in `<workdir>/.reasona/model_divergence.jsonl`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from bernstein.plugins import hookimpl  # verified real: pluggy.HookimplMarker("bernstein")

from reasona_dev.model_config import BERNSTEIN_ROLE_TO_PRIMARY_CONFIG_ROLE, resolve_all

logger = logging.getLogger(__name__)

_STATE_DIR = Path(".reasona")
_DIVERGENCE_LOG = _STATE_DIR / "model_divergence.jsonl"

# Extends model_config.BERNSTEIN_ROLE_TO_PRIMARY_CONFIG_ROLE (the shared
# canonical single-role mapping) with a SET of acceptable config roles per
# Bernstein role, since this monitor needs to accept more than one value
# for "reviewer": the same Bernstein role name is reused for both the
# initial review pipeline (resolved["review"]) and the bounded recheck
# pipeline (resolved["recheck"]) -- either is legitimate, so both must be
# accepted to avoid a false positive when bounded recheck is in use.
# "ocr_reviewer" has no model slot (adapter="ocr", stateless tool) and is
# intentionally absent -- there is nothing to compare it against.
_SPAWN_ROLE_TO_CONFIG_ROLES: dict[str, tuple[str, ...]] = {
    bernstein_role: (config_role,) for bernstein_role, config_role in BERNSTEIN_ROLE_TO_PRIMARY_CONFIG_ROLE.items()
}
_SPAWN_ROLE_TO_CONFIG_ROLES["reviewer"] = ("review", "recheck")


class ReasonaGatePlugin:
    """One instance registered per run; `pre_task_state` keys are stage names."""

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


