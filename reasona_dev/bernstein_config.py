"""Bootstraps a target repo's `bernstein.yaml` from a local or global
template, and keeps its `role_model_policy` in sync.

Bernstein disagrees with itself about where the seed file lives:
`find_seed_file()` checks `.bernstein/bernstein.yaml` first, while `bernstein
run`'s orchestrator bypasses that function entirely and re-derives a root-only
`workdir/bernstein.yaml`. A repo satisfying only the first spawns zero agents,
silently -- re-confirmed live: with no root file the orchestrator logs
`resolved seed_path=<workdir>/bernstein.yaml (exists=False)` then
`FATAL: no adapter configured`.

So the real file lives at `.bernstein/bernstein.yaml` and the repo root gets a
relative symlink to it -- one file, both lookups satisfied.

**The root symlink must NOT be tracked by git.** Committing it makes git
materialize it inside every per-task agent worktree, where Bernstein's
worktree isolation check rejects it outright:

    Worktree isolation violation: Symlink 'bernstein.yaml' points into
    parent repo mutable state
    Cannot create workspace for agent backend-<id>

which is again zero agents, just from the other direction. Bernstein's own
worktree exclude list already carries `/bernstein.yaml` alongside `/.sdd/`,
`/.env` and `/CLAUDE.md`, so an untracked root file is the arrangement it
expects. `ensure_bernstein_yaml()` therefore also adds `bernstein.yaml` to the
target repo's `.gitignore`.

Because it is untracked, a fresh clone has `.bernstein/bernstein.yaml` and no
root link -- so the link is ensured on EVERY call, not only when bootstrapping
a repo that has neither. An early return on "`.bernstein/` already exists"
would leave every cloned repo one FATAL away from its first run.

Template cascade (mirrors `reasona_dev.config_file`'s two layers for
`reasona.yaml`). Named `bernstein-template.yaml` so it reads as the source
copied FROM, not a second copy of the real file:

    <workdir>/.reasona/bernstein-template.yaml   project-local (checked first)
    ~/.reasona/bernstein-template.yaml           global (GLOBAL_BERNSTEIN_YAML)

A repo that already has a seed file at either real location keeps its content
untouched; only the root link and the ignore entry are ensured.

`sync_role_model_policy()` addresses a separate, ongoing concern: a
`role_model_policy` already in place can drift from what
`reasona_dev.model_config` now resolves. See its own docstring. Note that this
block doubles as the task server's ROLE WHITELIST -- `POST /tasks` returns 400
for any role absent from it (verified live), so every role reasona-dev
dispatches has to appear there.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from reasona_dev.model_config import BERNSTEIN_ROLE_TO_PRIMARY_CONFIG_ROLE, ResolvedModel

GLOBAL_BERNSTEIN_YAML = Path.home() / ".reasona" / "bernstein-template.yaml"


def _ensure_root_link(workdir: Path) -> None:
    """Guarantee an UNTRACKED root `bernstein.yaml` -> `.bernstein/bernstein.yaml`.

    Idempotent and non-destructive: a real file already at the root is left
    alone (that repo predates this convention and already works), and an
    existing correct symlink is left alone. Only a missing or wrongly-aimed
    link is (re)created.
    """
    root = workdir / "bernstein.yaml"
    target = Path(".bernstein") / "bernstein.yaml"
    if not (workdir / target).is_file():
        return
    if root.is_symlink():
        if root.readlink() != target:
            root.unlink()
            root.symlink_to(target)
    elif not root.exists():
        root.symlink_to(target)
    _ensure_gitignored(workdir, "bernstein.yaml")


def _ensure_gitignored(workdir: Path, entry: str) -> None:
    """Append `entry` to the repo's `.gitignore` if it is not already there.

    Tracking the root link breaks agent spawning entirely (see module
    docstring), so keeping it ignored is part of making it work -- not a
    tidiness nicety.
    """
    path = workdir / ".gitignore"
    try:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        if any(line.strip() == entry for line in existing.splitlines()):
            return
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        path.write_text(
            existing + prefix
            + "# Untracked on purpose: a COMMITTED bernstein.yaml symlink is\n"
            + "# materialized into every agent worktree, where Bernstein's\n"
            + "# isolation check rejects it and no agent can spawn.\n"
            + f"{entry}\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def ensure_bernstein_yaml(workdir: str | Path) -> Path | None:
    """Ensure a usable seed layout, and return the real file's path.

    Three cases, in order:

    1. `.bernstein/bernstein.yaml` exists -> content untouched; the root
       symlink and the ignore entry are still ensured, because an untracked
       link does not survive a clone (see module docstring).
    2. Only a root `bernstein.yaml` exists (a repo predating this
       convention) -> left completely alone. It already satisfies the
       orchestrator, and converting it would rewrite a file this module did
       not create.
    3. Neither exists -> copy a template into `.bernstein/bernstein.yaml`
       and create the root link.

    Source priority when bootstrapping: `<workdir>/.reasona/
    bernstein-template.yaml` then `GLOBAL_BERNSTEIN_YAML` -- the same
    local-beats-global order `reasona_dev.config_file` uses.

    Returns the real file now in place, or `None` if nothing was available
    to copy from (the caller proceeds; `bernstein run` surfaces its own
    error if this is never resolved).
    """
    workdir = Path(workdir)
    dot_bernstein_target = workdir / ".bernstein" / "bernstein.yaml"
    root_target = workdir / "bernstein.yaml"

    if dot_bernstein_target.is_file():
        _ensure_root_link(workdir)
        return dot_bernstein_target
    if root_target.is_file() and not root_target.is_symlink():
        return root_target

    local_template = workdir / ".reasona" / "bernstein-template.yaml"
    for source in (local_template, GLOBAL_BERNSTEIN_YAML):
        if source.is_file():
            dot_bernstein_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, dot_bernstein_target)
            _ensure_root_link(workdir)
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
