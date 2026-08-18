"""Bootstraps a target repo's `bernstein.yaml` from a local or global
template, and keeps its `role_model_policy` in sync.

Bernstein disagrees with itself about where the seed file lives:
`find_seed_file()` checks `.bernstein/bernstein.yaml` first, while `bernstein
run`'s background orchestrator subprocess bypasses that function entirely and
re-derives a root-only `workdir/bernstein.yaml`. A repo satisfying only the
first spawns zero agents, silently. So a fresh bootstrap writes the real file
to `.bernstein/bernstein.yaml` and creates the repo-root `bernstein.yaml` as a
relative symlink to it -- one file, both lookups satisfied. The investigation
behind this, including the live verification, is in docs/ARCHITECTURE.md
§3.5.3; it is not repeated here.

Template cascade (mirrors `reasona_dev.config_file`'s two layers for
`reasona.yaml`). Named `bernstein-template.yaml` so it reads as the source
copied FROM, not a second copy of the real file:

    <workdir>/.reasona/bernstein-template.yaml   project-local (checked first)
    ~/.reasona/bernstein-template.yaml           global (GLOBAL_BERNSTEIN_YAML)

A repo that already has a seed file at either real location is left
untouched. This repo itself is that case -- it commits
`.bernstein/bernstein.yaml` directly, and needs no root symlink because it
only ever runs `doctor`/`plan validate` against itself, neither of which
spawns the subprocess with the root-only lookup.

`sync_role_model_policy()` addresses a separate, ongoing concern: a
`role_model_policy` already in place can drift from what
`reasona_dev.model_config` now resolves. See its own docstring.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from reasona_dev.model_config import BERNSTEIN_ROLE_TO_PRIMARY_CONFIG_ROLE, ResolvedModel

GLOBAL_BERNSTEIN_YAML = Path.home() / ".reasona" / "bernstein-template.yaml"


def ensure_bernstein_yaml(workdir: str | Path) -> Path | None:
    """Bootstrap `<workdir>/.bernstein/bernstein.yaml` (+ a root symlink to
    it) if the target repo has no seed file at either real location yet.

    Existing-file check covers BOTH locations Bernstein's `find_seed_file()`
    recognizes (`.bernstein/bernstein.yaml` first, then repo-root
    `bernstein.yaml`) -- a repo already using either convention (as a real
    file OR a symlink) is left completely untouched, never given a second,
    redundant copy.

    Only when NEITHER exists: copies a template into
    `<workdir>/.bernstein/bernstein.yaml`, then creates `<workdir>/
    bernstein.yaml` as a RELATIVE symlink (`.bernstein/bernstein.yaml`) --
    relative so it survives being cloned/moved to a different absolute
    path. This satisfies both of Bernstein's disagreeing lookup paths (see
    module docstring) from one underlying file.

    Source priority when neither exists: `<workdir>/.reasona/
    bernstein-template.yaml` (project-local template) -> `GLOBAL_BERNSTEIN_YAML`
    (`~/.reasona/bernstein-template.yaml`, global template) -- the same
    local-beats-global order `reasona_dev.config_file` uses for
    `reasona.yaml`.

    Returns the path now in place (whichever already existed, or the
    freshly-bootstrapped `.bernstein/bernstein.yaml`), or `None` if nothing
    was available to copy from either (the caller still proceeds;
    `bernstein run` will surface its own "no seed file found" error if this
    is never resolved).
    """
    workdir = Path(workdir)
    dot_bernstein_target = workdir / ".bernstein" / "bernstein.yaml"
    root_target = workdir / "bernstein.yaml"
    if dot_bernstein_target.exists():
        return dot_bernstein_target
    if root_target.exists():
        return root_target

    local_template = workdir / ".reasona" / "bernstein-template.yaml"
    for source in (local_template, GLOBAL_BERNSTEIN_YAML):
        if source.is_file():
            dot_bernstein_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, dot_bernstein_target)
            root_target.symlink_to(Path(".bernstein") / "bernstein.yaml")
            return dot_bernstein_target
    return None


def sync_role_model_policy(bernstein_yaml_path: str | Path, resolved: dict[str, ResolvedModel]) -> bool:
    """Patch `role_model_policy.<role>.provider` in `bernstein_yaml_path`
    in place to match `resolved`'s adapters -- text-level surgery, not a
    YAML re-serialize, so every comment in the file (and this project's
    `bernstein.yaml` carries a lot of them, e.g. the whole CREDIT-BURN
    writeup) survives byte-for-byte untouched. Works the same whether
    `bernstein_yaml_path` is a real file or a symlink to one (`Path.
    read_text()`/`write_text()` follow symlinks transparently) -- editing
    through the root symlink `ensure_bernstein_yaml()` creates edits the
    same underlying `.bernstein/bernstein.yaml` either way.

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
