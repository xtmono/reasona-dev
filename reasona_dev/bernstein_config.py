"""Regenerates a target repo's `bernstein.yaml` from a local or global
template on every run, and keeps its `role_model_policy` in sync.

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
    # Runtime artifacts must not be tracked either. An agent works in a git
    # worktree of the SAME repo, so a tracked `.reasona/runs/` is visible to
    # it -- observed live: an agent ran `git add .reasona/runs/.../x.raw.txt
    # && git commit`, and the merge-back then materialised the file in the
    # project root AFTER the driver had already looked for it and recorded
    # the role as ERROR.
    for entry in (".reasona/runs/", ".reasona/cycles.jsonl", ".reasona/memory/", ".sdd/"):
        _ensure_gitignored(workdir, entry)


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


def _resolve_template(workdir: Path) -> Path | None:
    """`<workdir>/.reasona/bernstein-template.yaml` then `GLOBAL_BERNSTEIN_YAML`
    -- the same local-beats-global order `reasona_dev.config_file` uses."""
    local_template = workdir / ".reasona" / "bernstein-template.yaml"
    for source in (local_template, GLOBAL_BERNSTEIN_YAML):
        if source.is_file():
            return source
    return None


def ensure_bernstein_yaml(workdir: str | Path) -> Path | None:
    """Ensure a usable seed layout, and return the real file's path.

    **`.bernstein/bernstein.yaml` is a DERIVED artifact, regenerated from
    its template on every call a template resolves for -- not a one-time
    seed.** An earlier version of this function copied the template only
    when NEITHER `.bernstein/bernstein.yaml` nor a root file existed yet,
    then left whatever it (or an operator) had produced there untouched
    forever. That made `role_model_policy` -- which doubles as Bernstein's
    task-create ROLE ALLOWLIST, `POST /tasks` returns HTTP 400 for a role
    absent from it -- silently freeze at whatever roles existed the moment
    a repo was first bootstrapped. A real incident: a target repo's
    `bernstein.yaml` was bootstrapped before this project's `,ocr` co-
    reviewer support existed; the template later gained an `ocr_reviewer`
    entry, but the already-materialized file never did, so every `review:
    ...,ocr` run there hard-blocked on "role/model unavailable" -- a
    template update that should have been a no-op operator experience
    instead required knowing to hand-diff two files nobody was told to
    compare. `plan.yaml` is already regenerated fresh on every role
    dispatch (`bernstein_dispatch.write_role_plan()`); this brings
    `bernstein.yaml` in line with that same "derived, not hand-maintained"
    treatment -- `sync_role_model_policy()` (called right after this
    function, unchanged) then layers the CURRENT run's resolved adapters on
    top, so a freshly-regenerated file is never one step behind either the
    template's role list OR `reasona_dev.model_config`'s resolved
    providers.

    **Project-local template wins, so per-repo customization (e.g.
    `worktree_setup.setup_command`, which this project's own template
    comments say to "override per repo") lives in `<workdir>/.reasona/
    bernstein-template.yaml`, not in a hand-edit of the materialized
    `.bernstein/bernstein.yaml`** -- a hand-edit there is silently
    discarded on the next call, same as a hand-edit of a compiled
    `plan.yaml` would be. A repo whose workflow runs more than one
    reasona-* tool against it (e.g. both reasona-dev and reasona-plan) also
    needs its OWN project-local template declaring the UNION of every role
    either tool creates -- each tool's own template only lists its own
    roles, and regeneration replaces the file's role list outright rather
    than merging it with whatever the other tool last wrote (a merge would
    have to guess which of two overlapping-but-different `cli:`/
    `model_fallback:`/`worktree_setup:` sections is authoritative, which
    isn't this function's decision to make; a shared repo's own template
    is the place that decision belongs).

    Four cases, in order:

    1. A real (non-symlink) root `bernstein.yaml` already exists and no
       `.bernstein/bernstein.yaml` does -> left completely alone,
       regardless of template availability. A repo predating the
       `.bernstein/` convention already satisfies the orchestrator; letting
       a template silently spawn a NEW `.bernstein/bernstein.yaml` next to
       it would supersede that file without telling anyone --
       `find_seed_file()` checks `.bernstein/` first, so the operator's own
       real file would stop being the one Bernstein actually reads.
    2. Otherwise, if a template resolves (project-local or global) ->
       `.bernstein/bernstein.yaml` is (re)written from it every call,
       whether or not a file already sits there; the root symlink and
       ignore entry are ensured as before.
    3. Otherwise (no template), `.bernstein/bernstein.yaml` already exists
       -> left untouched (nothing to regenerate FROM); the root symlink and
       ignore entry are still ensured.
    4. None of the above -> `None`; the caller proceeds and `bernstein run`
       surfaces its own error.

    Returns the real file now in place, or `None`.
    """
    workdir = Path(workdir)
    dot_bernstein_target = workdir / ".bernstein" / "bernstein.yaml"
    root_target = workdir / "bernstein.yaml"

    if root_target.is_file() and not root_target.is_symlink() and not dot_bernstein_target.is_file():
        return root_target

    template = _resolve_template(workdir)
    if template is not None:
        dot_bernstein_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(template, dot_bernstein_target)
        _ensure_root_link(workdir)
        return dot_bernstein_target

    if dot_bernstein_target.is_file():
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
