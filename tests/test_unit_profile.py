import pytest

from reasona_dev.prompt_profile import ProfileConflict, resolve_unit_profile

_CFG = {
    "dev-profile": "generic",
    "dev-profile-map": {
        "crates/**": "rust",
        "services/**/*.py": "python",
        "web/**": "typescript",
    },
}


def test_explicit_unit_profile_wins_outright():
    """A path map is a default; it never overrides the author stating the
    answer."""
    got = resolve_unit_profile(
        files=["crates/gen/build.rs"], unit_profile="rust-buildscript",
        unit_index="4", project_cfg=_CFG,
    )
    assert got == "rust-buildscript"


def test_path_map_resolves_when_unit_is_silent():
    got = resolve_unit_profile(files=["crates/flow/src/x.rs"], unit_index="3", project_cfg=_CFG)
    assert got == "rust"


def test_map_matches_a_top_level_file_under_a_double_star_glob():
    got = resolve_unit_profile(
        files=["web/index.ts"], unit_index="1",
        project_cfg={"dev-profile-map": {"**/*.ts": "typescript"}},
    )
    assert got == "typescript"


def test_unmatched_files_are_ignored_not_treated_as_the_default():
    """A Rust PR that also edits README.md is a Rust PR -- counting the
    README as 'generic' would manufacture a conflict from every doc change."""
    got = resolve_unit_profile(
        files=["crates/flow/src/x.rs", "README.md", "docs/design.md"],
        unit_index="3", project_cfg=_CFG,
    )
    assert got == "rust"


def test_no_match_at_all_falls_back_to_the_repo_default():
    got = resolve_unit_profile(files=["README.md"], unit_index="1", project_cfg=_CFG, fallback="generic")
    assert got == "generic"


def test_no_files_declared_falls_back():
    assert resolve_unit_profile(files=[], unit_index="1", project_cfg=_CFG) == "generic"


def test_project_map_beats_global_map_wholesale():
    got = resolve_unit_profile(
        files=["crates/x/lib.rs"], unit_index="1",
        project_cfg={"dev-profile-map": {"crates/**": "rust-strict"}},
        global_cfg={"dev-profile-map": {"crates/**": "rust"}},
    )
    assert got == "rust-strict"


def test_global_map_used_when_project_declares_none():
    got = resolve_unit_profile(
        files=["crates/x/lib.rs"], unit_index="1",
        project_cfg={}, global_cfg={"dev-profile-map": {"crates/**": "rust"}},
    )
    assert got == "rust"


def test_two_profiles_in_one_unit_is_refused():
    with pytest.raises(ProfileConflict) as exc:
        resolve_unit_profile(
            files=["crates/flow/src/x.rs", "services/api/ingest.py"],
            unit_index="3", project_cfg=_CFG,
        )
    msg = str(exc.value)
    assert "PR 3 spans 2 profiles" in msg
    # names which file pulled in which -- an unactionable conflict message
    # is the same as no message
    assert "crates/flow/src/x.rs -> rust" in msg
    assert "services/api/ingest.py -> python" in msg
    assert "split it into separate PR units" in msg


def test_explicit_profile_resolves_an_otherwise_conflicting_unit():
    """The stated escape hatch actually works."""
    got = resolve_unit_profile(
        files=["crates/flow/src/x.rs", "services/api/ingest.py"],
        unit_profile="polyglot", unit_index="3", project_cfg=_CFG,
    )
    assert got == "polyglot"


def test_first_matching_glob_wins_per_file():
    """Per-file resolution stops at the first match, so two globs covering
    the same file do not make it ambiguous with itself."""
    got = resolve_unit_profile(
        files=["crates/flow/src/x.rs"], unit_index="1",
        project_cfg={"dev-profile-map": {"crates/**": "rust", "crates/flow/**": "rust-flow"}},
    )
    assert got in ("rust", "rust-flow")


# --- compile-time validation ------------------------------------------------

_MIXED_PLAN = """\
---
plan: mixed
pr_units:
  - index: 3
    title: "spans two languages"
    files: [crates/flow/src/x.rs, services/api/ingest.py]
---

## PR 3: spans two languages

- [ ] do it
"""

_RESOLVED_PLAN = _MIXED_PLAN.replace(
    'files: [crates/flow/src/x.rs, services/api/ingest.py]',
    'files: [crates/flow/src/x.rs, services/api/ingest.py]\n    profile: polyglot',
)


def _repo_with_map(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".reasona").mkdir(parents=True)
    (repo / ".reasona" / "reasona.yaml").write_text(
        "dev-profile: generic\n"
        "dev-profile-map:\n"
        '  "crates/**": rust\n'
        '  "services/**/*.py": python\n'
    )
    return repo


def test_compile_refuses_a_unit_spanning_two_profiles(tmp_path):
    """A plan defect surfaces while the author still has the plan open, not
    an hour into a run."""
    from reasona_dev.plan_compile import PlanError, compile_to_bernstein_plan

    repo = _repo_with_map(tmp_path)
    with pytest.raises(PlanError) as exc:
        compile_to_bernstein_plan(
            _MIXED_PLAN, plan_name="s", description="d", workdir=repo,
            write_audit_trail=False, write_bernstein_yaml=False,
        )
    assert "spans 2 profiles" in str(exc.value)


def test_compile_accepts_the_same_unit_once_profile_is_stated(tmp_path):
    from reasona_dev.plan_compile import compile_to_bernstein_plan

    repo = _repo_with_map(tmp_path)
    plan = compile_to_bernstein_plan(
        _RESOLVED_PLAN, plan_name="s", description="d", workdir=repo,
        write_audit_trail=False, write_bernstein_yaml=False,
    )
    assert len(plan["stages"]) == 1


def test_manifest_profile_reaches_the_parsed_unit():
    from reasona_dev.plan_compile import parse_plan_units

    units = parse_plan_units(_RESOLVED_PLAN)
    assert units[0].profile == "polyglot"


def test_validation_can_be_disabled_deliberately(tmp_path):
    from reasona_dev.plan_compile import compile_to_bernstein_plan

    repo = _repo_with_map(tmp_path)
    plan = compile_to_bernstein_plan(
        _MIXED_PLAN, plan_name="s", description="d", workdir=repo,
        write_audit_trail=False, write_bernstein_yaml=False, validate_profiles=False,
    )
    assert len(plan["stages"]) == 1
