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
        PLAN, plan_name="sample", description="test plan", audit_trail_path=None
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
    plan = compile_to_bernstein_plan(
        PLAN, plan_name="sample", description="test plan", audit_trail_path=None
    )
    assert plan["stages"][0]["steps"][0]["model"] == "sonnet"


def test_explicit_dev_model_overrides_default():
    from reasona_dev.model_config import ResolvedModel

    plan = compile_to_bernstein_plan(
        PLAN,
        plan_name="sample",
        description="test plan",
        dev_model=ResolvedModel("dev", "opus", "flag"),
        audit_trail_path=None,
    )
    assert plan["stages"][0]["steps"][0]["model"] == "opus"


def test_no_pr_markers_falls_back_to_single_unit():
    units = parse_plan_units("just prose, no PR headings")
    assert len(units) == 1
    assert units[0].index == "1"
