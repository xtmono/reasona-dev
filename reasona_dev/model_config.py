"""Per-role model/adapter/effort resolution with priority chains and recorded provenance.

Ports dev-ralf-renewal-claude.md §3.7 onto reasona-dev, renaming the env
vars (`DEV_RALF_*` -> `REASONA_DEV_*`) but keeping the exact same priority
order and defaults (dev-ralf-renewal-codex.md §7 model topology).

    flag > env var > project config file > global config file > default

Every layer accepts the SAME string shape dev-ralf itself used
(`DEV_RALF_DEV_MODEL=claude:sonnet:high`, i.e. `tool:model:effort` -- see
`_split_composite`), not just a bare model name. This is what closes the
gap this module used to have: the adapter (`claude`/`kilo`/...) and effort
were previously hardcoded as literals in `review_pipeline.py` and simply
absent from `plan_compile.py`'s generated step -- neither followed this
priority chain at all. A bare string (no `:`) still works and is treated as
"override the model only, keep this role's default adapter/effort" -- so
existing `--dev opus`-style flags/config entries are unaffected.

    dev:          --dev         -> REASONA_DEV_DEV_MODEL         -> project cfg -> global cfg -> claude:sonnet:high
    review:       --review      -> REASONA_DEV_REVIEW_MODEL      -> project cfg -> global cfg -> claude:opus:high
    recheck:      --recheck     -> REASONA_DEV_RECHECK_MODEL     -> project cfg -> global cfg -> resolved review spec
    bugbot:       --bugbot      -> REASONA_DEV_BUGBOT_MODEL      -> project cfg -> global cfg -> [compliance slot, same 4 steps] -> kilo:deepseek-v4-pro:high
    compliance:   --compliance  -> REASONA_DEV_COMPLIANCE_MODEL  -> project cfg -> global cfg -> claude:sonnet:high
    final_audit:  --final-audit -> REASONA_DEV_FINAL_AUDIT_MODEL -> project cfg -> global cfg -> [compliance slot, same 4 steps] -> claude:opus:high
    dev_escalation: --dev-escalation -> REASONA_DEV_DEV_ESCALATION_MODEL -> project cfg -> global cfg -> claude:opus:high

This module never applies BERNSTEIN_ROUTING (bandit) logic -- it is the
single source of truth for the `model:`/adapter/`effort:` values written
onto every generated plan.yaml step and review.yaml agent, and BanditRouter
treats an explicit `model:` as authoritative regardless of routing mode
(docs/ARCHITECTURE.md §3.1). Resolving everything here IS what keeps the
run "dev-ralf-faithful": whatever this module picks is exactly what
executes, nothing adaptive, nothing hardcoded downstream.

CONDUCTOR-COLLAPSE guard (dev-ralf-renewal-claude.md §3.7, condition 2):
every resolution records not just the value but WHERE it came from
(flag/env/config/fallback/default) so a wrong model is diagnosable after
the fact -- never re-derive silently at a later point in the pipeline.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from reasona_dev import config_file

# role: (model, adapter, effort) -- the ONLY place a model/adapter/effort
# default may be written as a literal in this project. Every consumer
# (plan_compile.py, pr_cycle.py, plugin.py) reads a ResolvedModel
# instead of naming an adapter or effort itself.
_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "dev": ("sonnet", "claude", "high"),
    "review": ("opus", "claude", "high"),
    "bugbot": ("deepseek-v4-pro", "kilo", "high"),
    "compliance": ("sonnet", "claude", "high"),
    "final_audit": ("opus", "claude", "high"),
    "dev_escalation": ("opus", "claude", "high"),
}

# "recheck" has no bare-model default of its own -- it fully inherits
# review's resolved spec when nothing overrides it (see resolve()) -- but
# still needs an adapter/effort to apply IF something overrides only the
# model (e.g. a bare `--recheck sonnet` flag).
_RECHECK_FALLBACK_ADAPTER_EFFORT: tuple[str, str] = ("claude", "high")

# Bernstein's own role vocabulary (plan.yaml step `role`, review.yaml agent
# `role`) differs from this module's role keys. This is the single
# canonical mapping -- every consumer that needs to cross from one
# vocabulary to the other (reasona_dev.bernstein_config's role_model_policy
# sync, reasona_dev.plugin's on_agent_spawned monitor,
# tests/test_bernstein_yaml_consistency.py) imports this instead of
# re-declaring the same role-name pairs.
BERNSTEIN_ROLE_TO_PRIMARY_CONFIG_ROLE: dict[str, str] = {
    "backend": "dev",
    "reviewer": "review",
    "bugbot": "bugbot",
    "compliance": "compliance",
}

_ENV_VARS: dict[str, str] = {
    "dev": "REASONA_DEV_DEV_MODEL",
    "review": "REASONA_DEV_REVIEW_MODEL",
    "recheck": "REASONA_DEV_RECHECK_MODEL",
    "bugbot": "REASONA_DEV_BUGBOT_MODEL",
    "compliance": "REASONA_DEV_COMPLIANCE_MODEL",
    "final_audit": "REASONA_DEV_FINAL_AUDIT_MODEL",
    "dev_escalation": "REASONA_DEV_DEV_ESCALATION_MODEL",
}


@dataclass(frozen=True)
class ResolvedModel:
    role: str
    model: str
    adapter: str
    effort: str
    source: str  # "flag" | "env:<VAR>" | "config:project:<role>" | "config:global:<role>" | "fallback:<role>" | "default"
    # dev-ralf's `,ocr` co-reviewer marker (see `_split_composite`), carried
    # on whichever spec actually won resolution. Only meaningful for
    # `role == "review"` -- `pr_cycle.py`'s review cycle checks this to
    # decide whether to also dispatch the OCR reviewer alongside this one.
    ocr: bool = False


def _split_composite(raw: str) -> tuple[str | None, str, str | None, bool]:
    """Parse dev-ralf's own `tool:model:effort[,extra]` shape.

    `"opus"` (no colon)        -> (None, "opus", None, False) -- caller fills adapter/effort from role defaults.
    `"claude:opus"`            -> ("claude", "opus", None, False) -- effort from role defaults.
    `"claude:opus:high"`       -> ("claude", "opus", "high", False)
    `"claude:sonnet:high,ocr"` -> ("claude", "sonnet", "high", True) -- the `,ocr` suffix is dev-ralf's
        "also run the OCR reviewer" marker. `pr_cycle.py`'s review cycle dispatches the OCR
        reviewer (`reasona_dev/adapters/ocr.py`) alongside the primary one when this is set --
        see `ResolvedModel.ocr`.
    """
    parts = [p.strip() for p in raw.split(":")]
    if len(parts) == 1:
        model, ocr = parts[0].split(",", 1)[0], "," in parts[0]
        return None, model, None, ocr
    if len(parts) == 2:
        model, ocr = parts[1].split(",", 1)[0], "," in parts[1]
        return parts[0], model, None, ocr
    effort_field = parts[2]
    effort, ocr = effort_field.split(",", 1)[0].strip(), "," in effort_field
    return parts[0], parts[1], effort, ocr


def _role_defaults(role: str) -> tuple[str | None, str, str]:
    if role in _DEFAULTS:
        return _DEFAULTS[role]
    if role == "recheck":
        adapter, effort = _RECHECK_FALLBACK_ADAPTER_EFFORT
        return None, adapter, effort
    raise KeyError(f"no defaults registered for role {role!r}")


def _env(var: str, env: dict[str, str]) -> str | None:
    val = env.get(var)
    return val.strip() if val and val.strip() else None


def _config(role: str, project_cfg: dict, global_cfg: dict) -> tuple[str, str] | None:
    """Check project config, then global config, for `models.<role>`."""
    val = config_file.model_for(role, project_cfg)
    if val:
        return val, f"config:project:{role}"
    val = config_file.model_for(role, global_cfg)
    if val:
        return val, f"config:global:{role}"
    return None


def resolve(
    role: str,
    *,
    flag: str | None = None,
    env: dict[str, str] | None = None,
    project_cfg: dict | None = None,
    global_cfg: dict | None = None,
    review_resolved: ResolvedModel | None = None,
) -> ResolvedModel:
    """Resolve one role.

    `env` defaults to `os.environ`; `project_cfg`/`global_cfg` default to
    loading nothing (`{}`) rather than touching the filesystem, so tests
    and any caller that has already loaded these once (`resolve_all` does)
    stay explicit and side-effect-free. Use `config_file.load_global()` /
    `config_file.load_project(workdir)` to actually load them.

    Every raw string accepted at any layer (`flag`, the env var, or a
    `models.<role>` config entry) may be either a bare model name or the
    full `tool:model:effort` composite (see `_split_composite`) -- a bare
    name only overrides the model, leaving this role's adapter/effort
    defaults (or, for `recheck`, review's resolved adapter/effort) in place.

    `review_resolved` is the already-resolved `review` outcome that
    `recheck` falls back onto ("first-pass reviewers" per
    dev-ralf-renewal-claude.md §3.7) -- callers resolve `review` first and
    pass it through. `bugbot`
    and `final_audit` do NOT take an equivalent `compliance_resolved` parameter:
    per the same spec they fall back only to the `compliance` role's OWN env
    var / config-file slot, never to compliance's fully-resolved value (see the
    `bugbot`/`final_audit` branch below for why that distinction matters --
    this was a real bug in an earlier draft, see tests/test_model_config.py
    `test_bugbot_does_not_inherit_compliances_own_default`).
    """
    env = env if env is not None else dict(os.environ)
    project_cfg = project_cfg if project_cfg is not None else {}
    global_cfg = global_cfg if global_cfg is not None else {}

    def _spec_from(raw: str, source: str, fb_adapter: str, fb_effort: str) -> ResolvedModel:
        adapter, model, effort, ocr = _split_composite(raw)
        return ResolvedModel(
            role=role,
            model=model,
            adapter=adapter or fb_adapter,
            effort=effort or fb_effort,
            source=source,
            ocr=ocr,
        )

    if flag:
        _, fb_adapter, fb_effort = _role_defaults(role)
        return _spec_from(flag, "flag", fb_adapter, fb_effort)

    if role == "recheck":
        _, fb_adapter, fb_effort = _role_defaults("recheck")
        env_val = _env(_ENV_VARS["recheck"], env)
        if env_val:
            return _spec_from(env_val, f"env:{_ENV_VARS['recheck']}", fb_adapter, fb_effort)
        cfg_hit = _config("recheck", project_cfg, global_cfg)
        if cfg_hit:
            return _spec_from(cfg_hit[0], cfg_hit[1], fb_adapter, fb_effort)
        if review_resolved is not None:
            return ResolvedModel(
                role=role,
                model=review_resolved.model,
                adapter=review_resolved.adapter,
                effort=review_resolved.effort,
                source="fallback:review",
            )
        # recheck resolved before review somehow -- fall through to review's own chain
        return resolve("review", env=env, project_cfg=project_cfg, global_cfg=global_cfg)

    if role in ("bugbot", "final_audit"):
        # dev-ralf-renewal-claude.md §3.7: these fall back to the
        # `compliance` role's OWN slot (env var, then config file) -- never to
        # compliance's fully-resolved value. A bare `--compliance` flag (with no
        # COMPLIANCE_MODEL env var or models.compliance config entry) does NOT
        # propagate here, and compliance's own DEFAULT does not either. Only
        # `recheck` inherits a sibling role's fully-resolved outcome
        # ("first-pass reviewers"); bugbot/final_audit consult compliance's raw slot.
        _, fb_adapter, fb_effort = _role_defaults(role)
        env_val = _env(_ENV_VARS[role], env)
        if env_val:
            return _spec_from(env_val, f"env:{_ENV_VARS[role]}", fb_adapter, fb_effort)
        cfg_hit = _config(role, project_cfg, global_cfg)
        if cfg_hit:
            return _spec_from(cfg_hit[0], cfg_hit[1], fb_adapter, fb_effort)
        compliance_env = _env(_ENV_VARS["compliance"], env)
        if compliance_env:
            return _spec_from(compliance_env, f"env:{_ENV_VARS['compliance']} (via compliance fallback)", fb_adapter, fb_effort)
        compliance_cfg_hit = _config("compliance", project_cfg, global_cfg)
        if compliance_cfg_hit:
            value, source = compliance_cfg_hit
            return _spec_from(value, f"{source} (via compliance fallback)", fb_adapter, fb_effort)
        fb_model, _, _ = _role_defaults(role)
        return ResolvedModel(role=role, model=fb_model, adapter=fb_adapter, effort=fb_effort, source="default")

    # dev / review / compliance / dev_escalation: flag -> own env var -> project cfg -> global cfg -> default
    fb_model, fb_adapter, fb_effort = _role_defaults(role)
    env_val = _env(_ENV_VARS[role], env)
    if env_val:
        return _spec_from(env_val, f"env:{_ENV_VARS[role]}", fb_adapter, fb_effort)
    cfg_hit = _config(role, project_cfg, global_cfg)
    if cfg_hit:
        return _spec_from(cfg_hit[0], cfg_hit[1], fb_adapter, fb_effort)
    return ResolvedModel(role=role, model=fb_model, adapter=fb_adapter, effort=fb_effort, source="default")


def resolve_review_list(
    flags: list[str] | None,
    *,
    env: dict[str, str] | None = None,
    project_cfg: dict | None = None,
    global_cfg: dict | None = None,
) -> list[ResolvedModel]:
    """One `ResolvedModel` per `--review` flag given (repeatable, dev-ralf's
    own multi-reviewer convention -- `--review` is the only role flag
    dev-ralf itself allows more than once).

    `flags` empty/None falls through to the normal single-reviewer chain
    (`resolve("review", ...)`: env var -> project cfg -> global cfg ->
    default), returned as a one-element list -- so a plain `--review`
    invocation (or none at all) still dispatches exactly one reviewer,
    unchanged from before this function existed.
    """
    env = env if env is not None else dict(os.environ)
    project_cfg = project_cfg if project_cfg is not None else {}
    global_cfg = global_cfg if global_cfg is not None else {}
    if not flags:
        return [resolve("review", env=env, project_cfg=project_cfg, global_cfg=global_cfg)]
    return [
        resolve("review", flag=f, env=env, project_cfg=project_cfg, global_cfg=global_cfg)
        for f in flags
    ]


def resolve_all(
    *,
    flags: dict[str, str] | None = None,
    review_flags: list[str] | None = None,
    env: dict[str, str] | None = None,
    workdir: str | Path | None = None,
    load_config_files: bool = True,
) -> dict:
    """Resolve every role in the correct dependency order (review first).

    When `load_config_files` is True (default), actually reads
    `~/.reasona/reasona.yaml` and `<workdir>/.reasona/reasona.yaml` from disk
    (`workdir` defaults to `Path.cwd()`, matching
    `plan_compile.compile_to_bernstein_plan()`'s own convention -- see
    docs/ARCHITECTURE.md §0.1). Pass `load_config_files=False` (or explicit
    empty dicts via `resolve()` directly) to keep resolution
    filesystem-free, e.g. in tests.

    `review_flags` is the full repeatable `--review` list (see
    `resolve_review_list`); `flags["review"]` (if present) is still used as
    a single-value fallback when `review_flags` is empty, so a caller that
    only has the single-value `flags` dict (e.g. `compile-plan`, which never
    dispatches multiple reviewers) keeps working unchanged. The result's
    `"review"` key is always the FIRST resolved reviewer -- the one every
    other call site in this project already reads (`pr_cycle.py`'s
    non-review roles, `bernstein_config.py`'s role_model_policy sync) -- and
    `"review_all"` is the full ordered list (always >= 1 element).
    `"review_ocr_requested"` is True when ANY resolved reviewer carried the
    `,ocr` marker (see `ResolvedModel.ocr`) -- the OCR co-reviewer is
    dispatched once per review cycle, not once per marked reviewer.
    """
    flags = flags or {}
    env = env if env is not None else dict(os.environ)

    if load_config_files:
        project_cfg = config_file.load_project(workdir if workdir is not None else Path.cwd())
        global_cfg = config_file.load_global()
    else:
        project_cfg, global_cfg = {}, {}

    if review_flags:
        reviewers = resolve_review_list(review_flags, env=env, project_cfg=project_cfg, global_cfg=global_cfg)
    elif flags.get("review"):
        reviewers = [resolve("review", flag=flags["review"], env=env, project_cfg=project_cfg, global_cfg=global_cfg)]
    else:
        reviewers = resolve_review_list(None, env=env, project_cfg=project_cfg, global_cfg=global_cfg)
    review = reviewers[0]

    return {
        "dev": resolve("dev", flag=flags.get("dev"), env=env, project_cfg=project_cfg, global_cfg=global_cfg),
        "review": review,
        "review_all": reviewers,
        "review_ocr_requested": any(r.ocr for r in reviewers),
        "recheck": resolve(
            "recheck", flag=flags.get("recheck"), env=env,
            project_cfg=project_cfg, global_cfg=global_cfg, review_resolved=review,
        ),
        "compliance": resolve("compliance", flag=flags.get("compliance"), env=env, project_cfg=project_cfg, global_cfg=global_cfg),
        "bugbot": resolve("bugbot", flag=flags.get("bugbot"), env=env, project_cfg=project_cfg, global_cfg=global_cfg),
        "final_audit": resolve(
            "final_audit", flag=flags.get("final_audit"), env=env,
            project_cfg=project_cfg, global_cfg=global_cfg,
        ),
        "dev_escalation": resolve(
            "dev_escalation", flag=flags.get("dev_escalation"), env=env,
            project_cfg=project_cfg, global_cfg=global_cfg,
        ),
    }


def write_resolved_config(resolved: dict[str, ResolvedModel], path: str | Path) -> None:
    """Persist model + adapter + effort + source for every role -- the CONDUCTOR-COLLAPSE audit trail."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({role: asdict(r) for role, r in resolved.items()}, indent=2, sort_keys=True)
    )
