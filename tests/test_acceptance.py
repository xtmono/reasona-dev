import json

import pytest

from reasona_dev import acceptance, plan_compile
from reasona_dev.acceptance import AcceptanceCriterion, parse_criteria, run_all, run_criterion
from reasona_dev.plan_compile import PlanError, compile_to_bernstein_plan, parse_manifest_units, parse_plan_units

MANIFEST_PLAN = """\
---
plan: sample
index_family: arabic
pr_units:
  - index: 1
    title: "bootstrap config"
    type: feat
    depends_on: []
    files: [src/config.rs]
    acceptance:
      - id: AC-1-1
        cmd: "true"
        expect: exit0
      - id: AC-1-2
        cmd: "false"
        expect: exit_nonzero
  - index: 2
    title: "use config in server"
    type: feat
    depends_on: [1]
    files: [src/server.rs]
---

## PR 1: bootstrap config

- [ ] add config.rs

## PR 2: use config in server

- [ ] wire config into src/server.rs
"""


# --- manifest parsing -------------------------------------------------------

def test_manifest_is_authoritative_over_prose():
    units = parse_plan_units(MANIFEST_PLAN)
    assert [u.index for u in units] == ["1", "2"]
    assert units[0].files == ["src/config.rs"]
    assert units[0].unit_type == "feat"
    assert units[1].depends_on == ["1"]
    # prose body still reaches the dev agent as the step description
    assert "add config.rs" in units[0].section


def test_acceptance_criteria_are_parsed_from_the_manifest():
    units = parse_plan_units(MANIFEST_PLAN)
    assert [c.id for c in units[0].acceptance] == ["AC-1-1", "AC-1-2"]
    assert units[0].acceptance[1].expect == "exit_nonzero"
    assert units[1].acceptance == []


def test_plan_without_frontmatter_still_uses_the_prose_fallback():
    plan = "## PR 1: only prose\ntype: feat\ndepends_on: none\n\n- [ ] do it\n"
    units = parse_plan_units(plan)
    assert [u.index for u in units] == ["1"]
    assert units[0].acceptance == []


def test_manifest_entry_without_a_prose_section_is_an_error():
    plan = "---\npr_units:\n  - index: 9\n    title: orphan\n---\n\n## PR 1: other\n"
    _, errors = parse_manifest_units(plan)
    assert any("no matching '## PR 9:' section" in e for e in errors)


def test_malformed_frontmatter_degrades_to_prose_not_a_crash():
    plan = "---\nnot: [valid\n---\n\n## PR 1: still parses\n"
    units = parse_plan_units(plan)
    assert [u.index for u in units] == ["1"]


# --- criterion validation ---------------------------------------------------

@pytest.mark.parametrize("item,fragment", [
    ({"cmd": "true"}, "no id"),
    ({"id": "A", "cmd": ""}, "no cmd"),
    ({"id": "A", "cmd": "true", "expect": "maybe"}, "expect must be one of"),
    ({"id": "A", "cmd": "true", "expect": "stdout_matches"}, "requires a pattern"),
    ({"id": "A", "cmd": "true", "expect": "stdout_matches", "pattern": "["}, "not a valid regex"),
])
def test_malformed_criteria_are_reported(item, fragment):
    criteria, errors = parse_criteria([item])
    assert criteria == []
    assert any(fragment in e for e in errors)


def test_duplicate_id_is_rejected_because_it_is_the_join_key():
    criteria, errors = parse_criteria([
        {"id": "AC-1", "cmd": "true"},
        {"id": "AC-1", "cmd": "false"},
    ])
    assert [c.id for c in criteria] == ["AC-1"]
    assert any("duplicate acceptance id" in e for e in errors)


def test_all_errors_are_collected_not_just_the_first():
    _, errors = parse_criteria([{"cmd": "true"}, {"id": "B", "cmd": ""}])
    assert len(errors) == 2


# --- execution --------------------------------------------------------------

def test_exit0_expectation(tmp_path):
    r = run_criterion(AcceptanceCriterion(id="a", cmd="true"), tmp_path)
    assert r.passed and r.exit_code == 0


def test_exit_nonzero_expectation_makes_negative_tests_statable(tmp_path):
    """`expect: pass` alone cannot say "this input must be REJECTED", so the
    negative half of every plan's Tests section would silently degrade."""
    r = run_criterion(AcceptanceCriterion(id="a", cmd="false", expect="exit_nonzero"), tmp_path)
    assert r.passed

    wrong = run_criterion(AcceptanceCriterion(id="b", cmd="true", expect="exit_nonzero"), tmp_path)
    assert not wrong.passed


def test_stdout_matches_expectation(tmp_path):
    ok = run_criterion(
        AcceptanceCriterion(id="a", cmd="echo 'risk_model: strict'", expect="stdout_matches",
                            pattern=r"risk_model:\s*\w+"),
        tmp_path,
    )
    assert ok.passed

    miss = run_criterion(
        AcceptanceCriterion(id="b", cmd="echo nothing", expect="stdout_matches", pattern="risk_model"),
        tmp_path,
    )
    assert not miss.passed


def test_timeout_is_a_failure_never_an_unknown(tmp_path):
    r = run_criterion(AcceptanceCriterion(id="a", cmd="sleep 5", timeout_s=1), tmp_path)
    assert not r.passed
    assert r.exit_code is None
    assert "timed out" in r.error


def test_one_failure_fails_the_unit_no_partial_credit(tmp_path):
    report = run_all(
        [
            AcceptanceCriterion(id="a", cmd="true"),
            AcceptanceCriterion(id="b", cmd="false"),
            AcceptanceCriterion(id="c", cmd="true"),
        ],
        tmp_path,
    )
    assert not report.passed
    assert [r.id for r in report.failures] == ["b"]
    # all three still ran -- the author sees everything wrong in one pass
    assert len(report.results) == 3


def test_criteria_run_in_the_given_workdir(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    r = run_criterion(AcceptanceCriterion(id="a", cmd="test -f marker.txt"), tmp_path)
    assert r.passed


# --- compile-time wiring ----------------------------------------------------

def test_compile_writes_acceptance_file_only_for_units_that_declare_criteria(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    compile_to_bernstein_plan(
        MANIFEST_PLAN, plan_name="s", description="d", workdir=repo,
        write_bernstein_yaml=False,
    )
    written = plan_compile.acceptance_path(repo, "pr-1")
    assert json.loads(written.read_text())["criteria"][0]["id"] == "AC-1-1"
    assert not plan_compile.acceptance_path(repo, "pr-2").exists()


def test_removing_criteria_from_a_plan_removes_the_stale_gate_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    compile_to_bernstein_plan(
        MANIFEST_PLAN, plan_name="s", description="d", workdir=repo,
        write_bernstein_yaml=False,
    )
    assert plan_compile.acceptance_path(repo, "pr-1").exists()

    stripped = MANIFEST_PLAN.replace(
        """    acceptance:
      - id: AC-1-1
        cmd: "true"
        expect: exit0
      - id: AC-1-2
        cmd: "false"
        expect: exit_nonzero
""",
        "",
    )
    compile_to_bernstein_plan(
        stripped, plan_name="s", description="d", workdir=repo,
        write_bernstein_yaml=False,
    )
    assert not plan_compile.acceptance_path(repo, "pr-1").exists()


def test_strict_plan_refuses_a_manifest_with_defects(tmp_path):
    bad = MANIFEST_PLAN.replace("id: AC-1-1", "id: ")
    with pytest.raises(PlanError, match="defect"):
        compile_to_bernstein_plan(
            bad, plan_name="s", description="d", workdir=tmp_path,
            write_bernstein_yaml=False,
        )


# --- gate CLI ---------------------------------------------------------------

def test_cli_passes_when_every_criterion_holds(tmp_path):
    path = tmp_path / "acceptance-pr-1.json"
    path.write_text(json.dumps({"criteria": [{"id": "a", "cmd": "true", "expect": "exit0"}]}))
    assert acceptance.main([str(path), str(tmp_path)]) == 0


def test_cli_fails_on_a_failing_criterion(tmp_path):
    path = tmp_path / "acceptance-pr-1.json"
    path.write_text(json.dumps({"criteria": [{"id": "a", "cmd": "false", "expect": "exit0"}]}))
    assert acceptance.main([str(path), str(tmp_path)]) == 1


def test_cli_warns_but_passes_when_no_criteria_are_declared(tmp_path):
    """Refusing outright is the target, but flipping it today blocks every
    plan written before the field existed."""
    assert acceptance.main([str(tmp_path / "missing.json"), str(tmp_path)]) == 0
