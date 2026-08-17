"""Per-project review/bugbot/compliance prompt profiles.

dev-ralf's actual review/bugbot/compliance prompts (worker.md -> *Verify
cycles* / *Role I/O*) are project- and language-specific -- a Rust monorepo's are
Rust-aware (`agent/rust-rules.md`, cargo-specific bug classes) and dispatch
to a target repo's own skills (`ext-bugbot`, `ext-review`). A different project
(a Python monorepo, a Go service) needs different prompts, not reasona-dev's
own hardcoded ones -- so these live as plain files under `.reasona/prompts/
<profile>/`, selected by NAME through the same flag > env var > project cfg
> global cfg > default chain as every other setting in this project.

Profile name resolution reads `dev-profile:` from the same `reasona.yaml`
`reasona_dev.config_file` already reads `dev-models:` from ("dev-profile"
for symmetry with "dev-models" -- both namespaced under this project's
`dev-` prefix in that shared, product-namespaced file; see config_file.py).

Prompt file resolution, once the profile name is known -- local beats
global beats packaged, same order `bernstein_config.py` uses:

    <workdir>/.reasona/prompts/<profile>/<role>.md   project-local (a target repo's own profile)
    ~/.reasona/prompts/<profile>/<role>.md           global (an operator's shared profile across repos)
    reasona_dev/prompts/<profile>/<role>.md          packaged with reasona-dev (only "generic" ships today)

`<role>` is one of `review`/`bugbot`/`compliance`/`final_audit` in
practice -- `dev` has no fixed prompt file of its own (dev-ralf-faithful:
its task content IS the plan document's own PR section, not a template).
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_PROFILE = "generic"
_PACKAGED_PROMPTS_DIR = Path(__file__).parent / "prompts"


def resolve_profile_name(
    *,
    flag: str | None = None,
    env: dict[str, str] | None = None,
    project_cfg: dict | None = None,
    global_cfg: dict | None = None,
) -> str:
    """flag > REASONA_DEV_PROFILE env var > project `dev-profile:` > global > "generic".

    `project_cfg`/`global_cfg` are the same loaded `reasona.yaml` dicts
    `reasona_dev.model_config.resolve()` takes -- callers that already
    loaded them once (e.g. `resolve_all`'s caller) pass the same dicts
    through instead of re-reading the file.
    """
    if flag and flag.strip():
        return flag.strip()

    env = env if env is not None else dict(os.environ)
    env_val = env.get("REASONA_DEV_PROFILE")
    if env_val and env_val.strip():
        return env_val.strip()

    project_cfg = project_cfg or {}
    project_val = project_cfg.get("dev-profile")
    if isinstance(project_val, str) and project_val.strip():
        return project_val.strip()

    global_cfg = global_cfg or {}
    global_val = global_cfg.get("dev-profile")
    if isinstance(global_val, str) and global_val.strip():
        return global_val.strip()

    return DEFAULT_PROFILE


def resolve_prompt(role: str, *, profile: str, workdir: str | Path | None = None) -> str | None:
    """Return `role`'s prompt text under `profile`, or None if no layer has it.

    None is a legitimate outcome, not an error -- a profile that doesn't
    define e.g. `final_audit.md` means that role runs without a fixed
    prompt override (caller decides the fallback, if any). This function
    never falls back to a DIFFERENT profile's files -- an operator who
    names a profile that doesn't exist gets None for every role, not a
    silent slide back to "generic" (that would be exactly the kind of
    silent substitution CONDUCTOR-COLLAPSE guards against elsewhere in
    this project).
    """
    workdir = Path(workdir) if workdir is not None else Path.cwd()
    candidates = (
        workdir / ".reasona" / "prompts" / profile / f"{role}.md",
        Path.home() / ".reasona" / "prompts" / profile / f"{role}.md",
        _PACKAGED_PROMPTS_DIR / profile / f"{role}.md",
    )
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return None
