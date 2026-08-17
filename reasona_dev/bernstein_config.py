"""Bootstraps a target repo's `.bernstein/bernstein.yaml` from a local or
global template.

Bernstein's own seed/config loader (`cli/helpers.py:find_seed_file()`)
checks, in order, `.bernstein/bernstein.yaml` / `.bernstein/bernstein.yml`
THEN the repo-root `bernstein.yaml` / `bernstein.yml` -- both cwd-relative,
no `~/.bernstein/` or other home-directory fallback in that function at
all. `bernstein run` DOES accept an explicit `--seed PATH` override (so it
alone could point straight at some other file), but bare `bernstein` ("run
from bernstein.yaml or backlog") and `bernstein doctor` have no such flag
-- they always call `find_seed_file()` with no override available. A
handful of OTHER, unrelated subsystems (`core/agents/warm_pool.py`,
`core/routes/embedding.py`, `core/protocols/mcp/mcp_composition.py`, a
couple of TUI settings modules) DO fall back to `~/.bernstein/
bernstein.yaml`, but each only reads its own narrow section from it (e.g.
warm_pool.py reads only the `warm_pool:` key) -- never the
`role_model_policy`/`model_fallback`/`approval`/`worktree_setup` config
`bernstein run` actually needs. Net result: a real, project-local
`.bernstein/bernstein.yaml` (or repo-root `bernstein.yaml`) has to exist for
every ad-hoc `bernstein` invocation to work, `--seed` or not -- so this
module always produces one, at the `.bernstein/` location specifically
(the one `find_seed_file()` checks FIRST, and a tidier place for it than
cluttering the repo root).

What CAN be global is reasona-dev's own copy of it, and -- mirroring
`reasona_dev.config_file`'s exact two-layer cascade for `reasona.yaml`
-- it too has a project-local layer above the global one:

    <workdir>/.reasona/bernstein.yaml   project-local template (checked first)
    ~/.reasona/bernstein.yaml           global template (GLOBAL_BERNSTEIN_YAML)

`ensure_bernstein_yaml()` copies whichever one wins into a target repo's
`<workdir>/.bernstein/bernstein.yaml` the first time reasona-dev compiles a
plan against that repo -- and ONLY if that repo doesn't already have a
seed file Bernstein would find on its own (checked at BOTH real locations,
`.bernstein/bernstein.yaml` and the legacy repo-root `bernstein.yaml`), so
a repo that already has either kept from before this module existed is
left untouched. This project's own repo uses the `.bernstein/` layout for
itself too (see README) -- `.bernstein/bernstein.yaml` is committed
directly, the plain "already has one" case above, not the template
cascade. The local/global template cascade exists for every OTHER target
repo reasona-dev bootstraps.

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
    """Copy a template into `workdir/.bernstein/bernstein.yaml` if the target
    repo has no seed file `find_seed_file()` would discover on its own.

    Existing-file check covers BOTH real locations Bernstein itself reads
    (`.bernstein/bernstein.yaml` first, then the legacy repo-root
    `bernstein.yaml`) -- a repo already using either convention is left
    completely untouched, never given a second, redundant copy.

    Source priority when neither exists: `<workdir>/.reasona/bernstein.yaml`
    (project-local template) -> `GLOBAL_BERNSTEIN_YAML`
    (`~/.reasona/bernstein.yaml`, global template) -- the same
    local-beats-global order `reasona_dev.config_file` uses for
    `reasona.yaml`.

    Returns the path now in place (whichever already existed, or the
    freshly-copied `.bernstein/bernstein.yaml`), or `None` if nothing was
    available to copy from either (the caller still proceeds; `bernstein
    run` will surface its own "no seed file found" error if this is never
    resolved).
    """
    workdir = Path(workdir)
    dot_bernstein_target = workdir / ".bernstein" / "bernstein.yaml"
    legacy_root_target = workdir / "bernstein.yaml"
    if dot_bernstein_target.exists():
        return dot_bernstein_target
    if legacy_root_target.exists():
        return legacy_root_target

    local_template = workdir / ".reasona" / "bernstein.yaml"
    for source in (local_template, GLOBAL_BERNSTEIN_YAML):
        if source.is_file():
            dot_bernstein_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, dot_bernstein_target)
            return dot_bernstein_target
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
    `~/.reasona/reasona.yaml` used to require a matching hand-edit here;
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
