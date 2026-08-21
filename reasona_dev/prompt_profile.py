"""Per-project review/bugbot/compliance prompt profiles.

dev-ralf's actual review/bugbot/compliance prompts (worker.md -> *Verify
cycles* / *Role I/O*) are project- and language-specific -- a Rust
monorepo's are Rust-aware (`agent/rust-rules.md`, cargo-specific bug
classes) and dispatch to that project's own skills (`ext-bugbot`,
`ext-review`). A different project
(a Python monorepo, a Go service) needs different prompts, not reasona-dev's
own hardcoded ones -- so these live as plain files under `.reasona/prompts/
<profile>/`, selected by NAME through the same flag > env var > project cfg
> global cfg > default chain as every other setting in this project.

Profile name resolution reads `dev-profile:` from the same `reasona.yaml`
`reasona_dev.config_file` already reads `dev-models:` from ("dev-profile"
for symmetry with "dev-models" -- both namespaced under this project's
`dev-` prefix in that shared, product-namespaced file; see config_file.py).

Prompt file resolution, once the profile name is known -- exactly two
layers, project beats global:

    <workdir>/.reasona/prompts/<profile>/<role>.md   project-local (a target repo's own profile)
    ~/.reasona/prompts/<profile>/<role>.md           global (an operator's shared profile across repos)

**No packaged layer.** Prompts used to also fall back to a copy shipped
inside the installed package, which made this the one setting in the
project resolved differently from every other one: `reasona.yaml`
(`config_file.py`) and the Bernstein seed template (`bernstein_config.py`)
are both exactly global-then-project with nothing underneath. A third,
invisible layer that lives inside a site-packages directory is also the
layer an operator cannot edit, cannot see in their repo, and does not know
is answering -- so a project that thought it had customized its review
prompt could silently be running a packaged one for any role file it
forgot to add.

The trade is explicit: a repo with neither layer present gets `None` and
`pr_cycle` aborts rather than reviewing against a prompt nobody in this
project chose. That is the same refusal `resolve_prompt` already makes for
an unknown profile name, applied consistently.

This repo commits its own `.reasona/prompts/rust-dev/` -- both because it
is what this repo actually runs on, and because it doubles as the
checked-in example an operator copies to `~/.reasona/prompts/rust-dev/` to
set up their global layer, exactly as `.reasona/bernstein-template.yaml`
does for the Bernstein seed.

`<role>` is one of `review`/`recheck`/`bugbot`/`compliance`/`final_audit`
in practice -- `dev` has no fixed prompt file of its own (dev-ralf-faithful:
its task content IS the plan document's own PR section, not a template).

**Per-PR-unit profiles, for repos with more than one language.** A single
repo-wide `dev-profile:` cannot express a monorepo whose Rust crates and
Python services need different review policies -- reviewing a Python
service against Rust-aware prompts is worse than having no profile at all,
because it produces confident findings from the wrong rulebook. So the
profile is resolved PER PR UNIT, from the `files:` that unit already
declares in the manifest (the same retrieval key `memory.select()` uses, so
it costs nothing new):

    pr_unit's own `profile:`     explicit, wins outright
    dev-profile-map: glob match  declared once per repo
    dev-profile:                 repo-wide default
    "rust-dev"                   built-in default name

    # <repo>/.reasona/reasona.yaml
    dev-profile: rust-dev
    dev-profile-map:
      "crates/**": rust
      "services/**/*.py": python

**A unit whose files map to two different profiles is refused, not
resolved.** Picking the most-specific glob or the majority language would
be deterministic and would also mean a unit spanning two languages gets
reviewed under one language's policy with nothing said about it. The other
half of the change goes unreviewed by any rulebook that applies to it,
silently -- the exact substitution failure this module already refuses for
an unknown profile name. The author either states `profile:` explicitly or
splits the unit, and splitting is what the 5-unit plan cap is pushing
toward anyway.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

DEFAULT_PROFILE = "rust-dev"


class ProfileConflict(ValueError):
    """One PR unit's files map to more than one profile.

    Carries the per-path mapping so the caller can name the conflict
    concretely -- "PR 3 spans two profiles" is only actionable if it also
    says which file pulled in which.
    """

    def __init__(self, unit_index: str, mapping: dict[str, str]) -> None:
        self.unit_index = unit_index
        self.mapping = mapping
        detail = "\n  ".join(f"{path} -> {profile}" for path, profile in sorted(mapping.items()))
        super().__init__(
            f"PR {unit_index} spans {len(set(mapping.values()))} profiles:\n  {detail}\n"
            "A single review policy cannot cover both. Either set `profile:` on "
            f"PR {unit_index} explicitly, or split it into separate PR units."
        )


def _profile_map(cfg: dict | None) -> dict[str, str]:
    raw = (cfg or {}).get("dev-profile-map")
    if not isinstance(raw, dict):
        return {}
    return {
        str(glob): str(name).strip()
        for glob, name in raw.items()
        if isinstance(name, str) and name.strip()
    }


def _match_glob(path: str, glob: str) -> bool:
    """`fnmatch` with the `**/` prefix people expect -- same reasoning as
    the glob helpers elsewhere in this project: `fnmatch`'s `*` already
    crosses `/`, so
    a literal `**/x` demands a separator and misses a top-level match.
    """
    if fnmatch.fnmatch(path, glob):
        return True
    if glob.startswith("**/") and fnmatch.fnmatch(path, glob[3:]):
        return True
    return False


def resolve_unit_profile(
    *,
    files: list[str],
    unit_profile: str | None = None,
    unit_index: str = "?",
    project_cfg: dict | None = None,
    global_cfg: dict | None = None,
    fallback: str | None = None,
) -> str:
    """The profile one PR unit is reviewed under.

    `unit_profile` (the manifest's own `profile:`) wins outright -- it is the
    author stating the answer, and a path map is a default, never an
    override of an explicit statement.

    Otherwise every declared file is matched against `dev-profile-map`
    (project layer beats global, whole-block). Files matching nothing are ignored rather than treated as the
    default profile: a PR that edits `crates/x/lib.rs` and `README.md` is a
    Rust PR, and counting the README as "rust-dev" would manufacture a
    conflict out of every doc change.

    Raises `ProfileConflict` when the matched files disagree.
    """
    if unit_profile and unit_profile.strip():
        return unit_profile.strip()

    mapping = _profile_map(project_cfg) or _profile_map(global_cfg)
    matched: dict[str, str] = {}
    for path in files or []:
        for glob, name in mapping.items():
            if _match_glob(path, glob):
                matched[path] = name
                break

    distinct = set(matched.values())
    if len(distinct) > 1:
        raise ProfileConflict(unit_index, matched)
    if distinct:
        return distinct.pop()
    return fallback or DEFAULT_PROFILE


def resolve_profile_name(
    *,
    flag: str | None = None,
    env: dict[str, str] | None = None,
    project_cfg: dict | None = None,
    global_cfg: dict | None = None,
) -> str:
    """flag > REASONA_DEV_PROFILE env var > project `dev-profile:` > global > "rust-dev".

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
    silent slide back to "rust-dev" (that would be exactly the kind of
    silent substitution CONDUCTOR-COLLAPSE guards against elsewhere in
    this project). Since the packaged layer is gone (see module docstring),
    a repo with neither layer present gets None for every role too, for the
    same reason: running against a prompt nobody in this project chose is
    the silent substitution, not the absence of one.
    """
    workdir = Path(workdir) if workdir is not None else Path.cwd()
    candidates = (
        workdir / ".reasona" / "prompts" / profile / f"{role}.md",
        Path.home() / ".reasona" / "prompts" / profile / f"{role}.md",
    )
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return None


def available_profiles(workdir: str | Path | None = None) -> dict[str, list[str]]:
    """profile name -> sorted role names, merged across both layers.

    A diagnostic for the failure this layering makes possible: `pr_cycle`
    aborting with "no review prompt for profile 'rust-dev'" says what is
    missing but not what IS present, and an operator whose global layer was
    never set up has no way to tell that apart from a typo'd profile name.
    Project entries shadow global ones per (profile, role), matching
    `resolve_prompt`'s own precedence.
    """
    workdir = Path(workdir) if workdir is not None else Path.cwd()
    found: dict[str, set[str]] = {}
    # Global first, then project -- later writes win, same as resolution.
    for root in (
        Path.home() / ".reasona" / "prompts",
        workdir / ".reasona" / "prompts",
    ):
        if not root.is_dir():
            continue
        for profile_dir in sorted(root.iterdir()):
            if not profile_dir.is_dir():
                continue
            roles = {p.stem for p in profile_dir.glob("*.md")}
            if roles:
                found.setdefault(profile_dir.name, set()).update(roles)
    return {name: sorted(roles) for name, roles in sorted(found.items())}
