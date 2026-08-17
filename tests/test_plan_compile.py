from pathlib import Path

from reasona_dev.plan_compile import compile_to_bernstein_plan, parse_plan_units

PLAN = """\
# Sample plan

## PR 1: bootstrap config
type: feat
depends_on: none

- [ ] add config.rs

## PR 2: use config in server
type: feat
depends_on: 1

- [ ] wire config into src/server.rs
"""


def test_parses_two_units_with_dependency():
    units = parse_plan_units(PLAN)
    assert [u.index for u in units] == ["1", "2"]
    assert units[1].depends_on == ["1"]


def test_compiles_to_valid_stage_shape():
    plan = compile_to_bernstein_plan(
        PLAN, plan_name="sample", description="test plan", write_audit_trail=False
    )
    assert plan["name"] == "sample"
    assert len(plan["stages"]) == 2
    assert plan["stages"][0]["name"] == "pr-1"
    assert "depends_on" not in plan["stages"][0]
    assert plan["stages"][1]["depends_on"] == ["pr-1"]
    for stage in plan["stages"]:
        assert len(stage["steps"]) == 1
        step = stage["steps"][0]
        assert "title" in step
        assert step["completion_signals"][0]["type"] == "test_passes"
        assert "gate_check" in step["completion_signals"][0]["command"]


def test_dev_model_defaults_to_resolved_sonnet():
    from reasona_dev.model_config import ResolvedModel

    plan = compile_to_bernstein_plan(
        PLAN,
        plan_name="sample",
        description="test plan",
        dev_model=ResolvedModel("dev", "sonnet", "claude", "high", "default"),
        write_audit_trail=False,
    )
    assert plan["stages"][0]["steps"][0]["model"] == "sonnet"
    assert plan["stages"][0]["steps"][0]["effort"] == "high"
    assert plan["cli"] == "claude"


def test_explicit_dev_model_overrides_default():
    from reasona_dev.model_config import ResolvedModel

    plan = compile_to_bernstein_plan(
        PLAN,
        plan_name="sample",
        description="test plan",
        dev_model=ResolvedModel("dev", "opus", "claude", "high", "flag"),
        write_audit_trail=False,
    )
    assert plan["stages"][0]["steps"][0]["model"] == "opus"
    assert plan["stages"][0]["steps"][0]["effort"] == "high"
    assert plan["cli"] == "claude"


def test_no_pr_markers_falls_back_to_single_unit():
    units = parse_plan_units("just prose, no PR headings")
    assert len(units) == 1
    assert units[0].index == "1"


def test_audit_trail_anchors_to_workdir_not_caller_cwd(tmp_path):
    # reasona-dev has no "home repo" once deployed (installed like
    # `bernstein` itself) -- the only stable anchor is the TARGET repo
    # (`workdir`), never wherever the compile step happens to be invoked
    # from. This must hold regardless of the actual process CWD.
    target_repo = tmp_path / "some-target-repo"
    target_repo.mkdir()

    compile_to_bernstein_plan(
        PLAN, plan_name="sample", description="test plan", workdir=target_repo
    )

    expected = target_repo / ".reasona" / "model_config.json"
    assert expected.exists()
    assert not (Path.cwd() / ".reasona").exists()


def test_audit_trail_disabled_writes_nothing(tmp_path):
    target_repo = tmp_path / "another-repo"
    target_repo.mkdir()

    compile_to_bernstein_plan(
        PLAN,
        plan_name="sample",
        description="test plan",
        workdir=target_repo,
        write_audit_trail=False,
    )

    assert not (target_repo / ".reasona").exists()
