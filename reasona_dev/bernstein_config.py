"""Bootstraps a target repo's `bernstein.yaml` from a local or global template.

Bernstein's own seed/config loader (`cli/helpers.py:find_seed_file()`) only
ever looks for a PROJECT-LOCAL `bernstein.yaml` / `.bernstein/bernstein.yaml`
in the invoking cwd -- confirmed by reading it directly: no `~/.bernstein/`
or other home-directory fallback exists in that function. `bernstein run`
DOES accept an explicit `--seed PATH` override (so it alone could point
straight at some other file), but bare `bernstein` ("run from bernstein.yaml
or backlog") and `bernstein doctor` have no such flag -- they always call
`find_seed_file()` with no override available. A handful of OTHER,
unrelated subsystems (`core/agents/warm_pool.py`, `core/routes/
embedding.py`, `core/protocols/mcp/mcp_composition.py`, a couple of TUI
settings modules) DO fall back to `~/.bernstein/bernstein.yaml`, but each
only reads its own narrow section from it (e.g. warm_pool.py reads only the
`warm_pool:` key) -- never the `role_model_policy`/`model_fallback`/
`approval`/`worktree_setup` config `bernstein run` actually needs. Net
result: a real, project-local `<workdir>/bernstein.yaml` (or
`.bernstein/bernstein.yaml`) has to exist for every ad-hoc `bernstein`
invocation to work, `--seed` or not -- so this module always produces one.

What CAN be global is reasona-dev's own copy of it, and -- mirroring
`reasona_dev.config_file`'s exact two-layer cascade for `config.yaml` -- it
too has a project-local layer above the global one:

    <workdir>/.reasona/bernstein.yaml   project-local template (checked first)
    ~/.reasona/bernstein.yaml           global template (GLOBAL_BERNSTEIN_YAML)

`ensure_bernstein_yaml()` copies whichever one wins into a target repo's
`<workdir>/bernstein.yaml` the first time reasona-dev compiles a plan
against that repo -- and ONLY if that repo doesn't already have one, so a
repo that wants its own customized `bernstein.yaml` keeps it untouched.
This project's own repo does NOT use either template layer for itself --
its `bernstein.yaml` is committed directly at the repo root (see README),
exactly the "already has one, so leave it alone" case above. The
local/global template cascade exists for every OTHER target repo reasona-dev
bootstraps.

`sync_role_model_policy()` handles a different, ongoing concern: even a
`bernstein.yaml` that's already in place can have a `role_model_policy`
that has drifted out of sync with what `reasona_dev.model_config` now
resolves (this happened for real -- see its docstring). It patches just
the `provider:` values in place, every `compile-plan` run, leaving
everything else in the file (including its comments) untouched.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from reasona_dev.model_config import BERNSTEIN_ROLE_TO_PRIMARY_CONFIG_ROLE, ResolvedModel

GLOBAL_BERNSTEIN_YAML = Path.home() / ".reasona" / "bernstein.yaml"


def ensure_bernstein_yaml(workdir: str | Path) -> Path | None:
    """Copy a template into `workdir/bernstein.yaml` if missing.

    Source priority: leave an existing `<workdir>/bernstein.yaml` untouched
    -> `<workdir>/.reasona/bernstein.yaml` (project-local template) ->
    `GLOBAL_BERNSTEIN_YAML` (`~/.reasona/bernstein.yaml`, global template) --
    the same local-beats-global order `reasona_dev.config_file` uses for
    `config.yaml`.

    Returns the path now in place (whether it was already there or just
    copied), or `None` if the target has no `bernstein.yaml` AND neither
    template exists to copy from (nothing was available -- the caller still
    proceeds; `bernstein run` will surface its own "no seed file found"
    error if this is never resolved).
    """
    workdir = Path(workdir)
    target = workdir / "bernstein.yaml"
    if target.exists():
        return target

    local_template = workdir / ".reasona" / "bernstein.yaml"
    for source in (local_template, GLOBAL_BERNSTEIN_YAML):
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, target)
            return target
    return None


def sync_role_model_policy(bernstein_yaml_path: str | Path, resolved: dict[str, ResolvedModel]) -> bool:
    """Patch `role_model_policy.<role>.provider` in `bernstein_yaml_path`
    in place to match `resolved`'s adapters -- text-level surgery, not a
    YAML re-serialize, so every comment in the file (and this project's
    `bernstein.yaml` carries a lot of them, e.g. the whole CREDIT-BURN
    writeup) survives byte-for-byte untouched.

    This is what closes the manual half of the gap
    `tests/test_bernstein_yaml_consistency.py` could previously only
    detect, not fix -- e.g. reasona-dev's own bugbot moving from the
    kilo/deepseek-v4-pro default to claude:opus:high in
    `~/.reasona/config.yaml` used to require a matching hand-edit here;
    now the next `compile-plan` run does it.

    Only rewrites a role's `provider:` value if that role is BOTH in
    `BERNSTEIN_ROLE_TO_PRIMARY_CONFIG_ROLE` AND already present in the
    file's `role_model_policy` block -- never adds or removes a role entry
    (declaring which roles exist is a structural decision for a human, not
    something this function invents). A `bernstein.yaml` with no
    `role_model_policy` block at all, or one shaped differently than this
    project's own template, is left completely untouched (the regex simply
    finds nothing to replace) -- this only helps files that already follow
    the shape `ensure_bernstein_yaml()` bootstraps.

    Returns True if the file was actually rewritten (something differed),
    False if it already matched (idempotent -- a repeat `compile-plan` run
    against an unchanged config produces no diff).
    """
    path = Path(bernstein_yaml_path)
    if not path.is_file():
        return False

    text = path.read_text()
    original = text
    for bernstein_role, config_role in BERNSTEIN_ROLE_TO_PRIMARY_CONFIG_ROLE.items():
        if config_role not in resolved:
            continue
        desired = resolved[config_role].adapter
        pattern = re.compile(
            rf"(^  {re.escape(bernstein_role)}:\n(?:    #.*\n)*    provider: )(\S+)",
            re.MULTILINE,
        )
        text = pattern.sub(lambda m: m.group(1) + desired, text, count=1)

    if text != original:
        path.write_text(text)
        return True
    return False
