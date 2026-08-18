"""Bootstraps a target repo's `bernstein.yaml` from a local or global
template.

Bernstein's own seed/config loader (`cli/helpers.py:find_seed_file()`,
used by the top-level `bernstein run`/`doctor` CLI parsing) checks, in
order, `.bernstein/bernstein.yaml` THEN the repo-root `bernstein.yaml`.
`bernstein run` ALSO launches its orchestrator as a background subprocess
(`core/server/server_launch.py::_start_spawner`) that does NOT call
`find_seed_file()` at all -- when the seed path isn't explicitly propagated
to it (confirmed: happens with the exact invocation this project uses,
`bernstein run <plan> --auto-approve`, no `--from-plan` needed to trigger
it), it independently re-derives `workdir / "bernstein.yaml"`, a single
hardcoded ROOT-only path with no `.bernstein/` check whatsoever.

**Confirmed by an actual paid `bernstein run` against a real repo
(2026-08-18):** a repo with ONLY `.bernstein/bernstein.yaml` gets "FATAL:
no adapter configured" from that orchestrator subprocess, on repeat, until
the watchdog gives up after 5 restarts -- zero agents ever spawn, silently
(`_start_spawner`'s own docstring warns about exactly this class of bug).
So Bernstein's two lookup paths disagree: `find_seed_file()` wants
`.bernstein/` checked first; the orchestrator subprocess's fallback wants
root, exclusively.

**Fix: satisfy both by construction, not by picking a side.** The real
file is written to `.bernstein/bernstein.yaml` (what `find_seed_file()`
prefers), and `<workdir>/bernstein.yaml` is created as a **relative
symlink** pointing at it. A symlink is transparent to `Path.exists()` /
`Path.read_text()` -- confirmed live (no cost: a direct, non-spawning
orchestrator invocation) that the background subprocess's hardcoded
root-only lookup resolves the symlink and parses the seed correctly, no
different from a real file there. Both code paths now read the exact same
content from the exact same underlying file -- no duplicate source of
truth, no picking one location and letting the other silently break.

What CAN be global is reasona-dev's own copy of the template, and --
mirroring `reasona_dev.config_file`'s exact two-layer cascade for
`reasona.yaml` -- it too has a project-local layer above the global one.
Named `bernstein-template.yaml`, not `bernstein.yaml`, specifically so it
reads as "the source this gets COPIED from" rather than looking like a
second copy of the real, Bernstein-readable file:

    <workdir>/.reasona/bernstein-template.yaml   project-local template (checked first)
    ~/.reasona/bernstein-template.yaml           global template (GLOBAL_BERNSTEIN_YAML)

`ensure_bernstein_yaml()` copies whichever template wins into a target
repo's `<workdir>/.bernstein/bernstein.yaml` (with the root symlink) the
first time reasona-dev compiles a plan against that repo -- and ONLY if
that repo doesn't already have a seed file at either real location, so a
repo that already has its own `.bernstein/bernstein.yaml` OR root
`bernstein.yaml` (symlink or not) is left completely untouched.

This project's own repo commits BOTH: `.bernstein/bernstein.yaml` directly
(the plain "already has one" case above -- so its own `bernstein doctor`/
`bernstein run` invocations never touch the template cascade at all) AND
`.reasona/bernstein-template.yaml` (identical content, committed purely so
this repo's own template doubles as a real, checked-in example other
repos' project-local template can be copied from). This repo does NOT get
a root symlink -- it never runs `bernstein run` against itself for real
execution (only `doctor`/`plan validate`, neither of which spawns the
buggy subprocess), so it never needs one.

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
