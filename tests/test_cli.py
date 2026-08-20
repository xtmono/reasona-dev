import json

import pytest
import yaml

from reasona_dev import bernstein_config
from reasona_dev.cli import main


@pytest.fixture(autouse=True)
def _no_real_global_bernstein_yaml(tmp_path, monkeypatch):
    # compile-plan now also bootstraps <workdir>/bernstein.yaml from
    # ~/.reasona/bernstein-template.yaml (reasona_dev.bernstein_config) -- point it
    # at a path that doesn't exist so these tests never touch the real
    # machine's global template.
    monkeypatch.setattr(bernstein_config, "GLOBAL_BERNSTEIN_YAML", tmp_path / "unused-global.yaml")


def test_compile_plan_dev_flag_reaches_generated_model(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("## PR 1: bootstrap\ntype: feat\ndepends_on: none\n\n- [ ] x\n")
    out = tmp_path / "plan.yaml"
    workdir = tmp_path / "target-repo"
    workdir.mkdir()

    rc = main(
        [
            "compile-plan", str(plan), "-o", str(out),
            "--workdir", str(workdir), "--dev", "opus",
        ]
    )
    assert rc == 0

    compiled = yaml.safe_load(out.read_text())
    assert compiled["stages"][0]["steps"][0]["model"] == "opus"

    audit = json.loads((workdir / ".reasona" / "model_config.json").read_text())
    assert audit["dev"]["source"] == "flag"
    assert audit["dev"]["model"] == "opus"
    assert audit["dev"]["adapter"] == "claude"
    assert compiled["cli"] == "claude"


def test_compile_plan_bugbot_flag_reaches_role_model_policy_sync(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("## PR 1: bootstrap\ntype: feat\ndepends_on: none\n\n- [ ] x\n")
    out = tmp_path / "plan.yaml"
    workdir = tmp_path / "target-repo"
    workdir.mkdir()
    (workdir / "bernstein.yaml").write_text(
        "goal: test\nrole_model_policy:\n  bugbot:\n    provider: kilo\n"
    )

    rc = main(
        [
            "compile-plan", str(plan), "-o", str(out),
            "--workdir", str(workdir), "--bugbot", "codex:o1:max",
        ]
    )
    assert rc == 0

    text = (workdir / "bernstein.yaml").read_text()
    assert "bugbot:\n    provider: codex" in text


def test_compile_plan_without_flag_uses_default(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("## PR 1: bootstrap\ntype: feat\ndepends_on: none\n\n- [ ] x\n")
    out = tmp_path / "plan.yaml"
    workdir = tmp_path / "target-repo"
    workdir.mkdir()

    rc = main(["compile-plan", str(plan), "-o", str(out), "--workdir", str(workdir)])
    assert rc == 0

    compiled = yaml.safe_load(out.read_text())
    assert compiled["stages"][0]["steps"][0]["model"] == "sonnet"


# --- run-plan dispatches cycle-0 itself --------------------------------------

def _plan(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("## PR 1: bootstrap\ntype: feat\ndepends_on: none\n\n- [ ] x\n")
    workdir = tmp_path / "target-repo"
    workdir.mkdir()
    return plan, workdir


def _shipped_result():
    from reasona_dev.orchestrate import PlanRunResult, UnitOutcome

    return PlanRunResult(outcomes=[UnitOutcome(stage_name="pr-1", profile="generic", status="shipped", reason="ok")])


def test_run_plan_defaults_to_no_ship_and_no_merge(tmp_path, monkeypatch):
    """PR creation and merge are opt-in -- opening a real PR and
    squash-merging it are outward-facing, hard-to-undo actions, so nothing
    reaches gh by default."""
    from reasona_dev import orchestrate

    plan, workdir = _plan(tmp_path)
    seen = {}

    def _fake_run_plan(**kw):
        seen["ship"] = kw["ship"]
        seen["merge"] = kw["merge"]
        return _shipped_result()

    monkeypatch.setattr(orchestrate, "run_plan", _fake_run_plan)

    rc = main(["run-plan", str(plan), "--workdir", str(workdir)])
    assert rc == 0
    assert seen == {"ship": False, "merge": False}


def test_run_plan_ship_opts_in_and_stops_at_the_open_pr(tmp_path, monkeypatch):
    from reasona_dev import orchestrate

    plan, workdir = _plan(tmp_path)
    seen = {}

    def _fake_run_plan(**kw):
        seen["ship"] = kw["ship"]
        seen["merge"] = kw["merge"]
        return _shipped_result()

    monkeypatch.setattr(orchestrate, "run_plan", _fake_run_plan)

    rc = main(["run-plan", str(plan), "--workdir", str(workdir), "--ship"])
    assert rc == 0
    assert seen == {"ship": True, "merge": False}


def test_run_plan_merge_implies_ship(tmp_path, monkeypatch):
    from reasona_dev import orchestrate

    plan, workdir = _plan(tmp_path)
    seen = {}

    def _fake_run_plan(**kw):
        seen["ship"] = kw["ship"]
        seen["merge"] = kw["merge"]
        return _shipped_result()

    monkeypatch.setattr(orchestrate, "run_plan", _fake_run_plan)

    # --merge alone (no --ship) must still ship, since merge requires it
    rc = main(["run-plan", str(plan), "--workdir", str(workdir), "--merge"])
    assert rc == 0
    assert seen == {"ship": True, "merge": True}


def test_run_plan_from_pr_reaches_orchestrate(tmp_path, monkeypatch):
    from reasona_dev import orchestrate

    plan, workdir = _plan(tmp_path)
    seen = {}

    def _fake_run_plan(**kw):
        seen["from_pr"] = kw["from_pr"]
        return _shipped_result()

    monkeypatch.setattr(orchestrate, "run_plan", _fake_run_plan)

    rc = main(["run-plan", str(plan), "--workdir", str(workdir), "--from-pr", "2"])
    assert rc == 0
    assert seen == {"from_pr": "2"}


def test_run_plan_without_from_pr_passes_none(tmp_path, monkeypatch):
    from reasona_dev import orchestrate

    plan, workdir = _plan(tmp_path)
    seen = {}

    def _fake_run_plan(**kw):
        seen["from_pr"] = kw["from_pr"]
        return _shipped_result()

    monkeypatch.setattr(orchestrate, "run_plan", _fake_run_plan)

    rc = main(["run-plan", str(plan), "--workdir", str(workdir)])
    assert rc == 0


def test_run_plan_gh_review_max_wait_defaults_to_900(tmp_path, monkeypatch):
    from reasona_dev import orchestrate

    plan, workdir = _plan(tmp_path)
    seen = {}

    def _fake_run_plan(**kw):
        seen["gh_review_max_wait_seconds"] = kw["gh_review_max_wait_seconds"]
        return _shipped_result()

    monkeypatch.setattr(orchestrate, "run_plan", _fake_run_plan)

    rc = main(["run-plan", str(plan), "--workdir", str(workdir)])
    assert rc == 0
    assert seen == {"gh_review_max_wait_seconds": 900}


def test_run_plan_gh_review_max_wait_flag_reaches_orchestrate(tmp_path, monkeypatch):
    from reasona_dev import orchestrate

    plan, workdir = _plan(tmp_path)
    seen = {}

    def _fake_run_plan(**kw):
        seen["gh_review_max_wait_seconds"] = kw["gh_review_max_wait_seconds"]
        return _shipped_result()

    monkeypatch.setattr(orchestrate, "run_plan", _fake_run_plan)

    rc = main(["run-plan", str(plan), "--workdir", str(workdir), "--gh-review-max-wait", "120"])
    assert rc == 0
    assert seen == {"gh_review_max_wait_seconds": 120}


def test_run_plan_review_flag_given_once_is_a_single_reviewer(tmp_path, monkeypatch):
    from reasona_dev import orchestrate

    plan, workdir = _plan(tmp_path)
    seen = {}

    def _fake_run_plan(**kw):
        seen["resolved"] = kw["resolved"]
        return _shipped_result()

    monkeypatch.setattr(orchestrate, "run_plan", _fake_run_plan)

    rc = main(["run-plan", str(plan), "--workdir", str(workdir), "--review", "opus"])
    assert rc == 0
    assert [r.model for r in seen["resolved"]["review_all"]] == ["opus"]


def test_run_plan_review_flag_repeated_resolves_multiple_reviewers(tmp_path, monkeypatch):
    from reasona_dev import orchestrate

    plan, workdir = _plan(tmp_path)
    seen = {}

    def _fake_run_plan(**kw):
        seen["resolved"] = kw["resolved"]
        return _shipped_result()

    monkeypatch.setattr(orchestrate, "run_plan", _fake_run_plan)

    rc = main([
        "run-plan", str(plan), "--workdir", str(workdir),
        "--review", "claude:opus:high", "--review", "codex:o1:max,ocr",
    ])
    assert rc == 0
    reviewers = seen["resolved"]["review_all"]
    assert [r.model for r in reviewers] == ["opus", "o1"]
    assert [r.adapter for r in reviewers] == ["claude", "codex"]
    assert seen["resolved"]["review"].model == "opus"  # first stays the single-value representative
    assert seen["resolved"]["review_ocr_requested"] is True


def test_run_plan_skip_dev_reaches_orchestrate(tmp_path, monkeypatch):
    """Cycle-0 dispatch (per unit, ledger-checked) now lives entirely in
    `orchestrate.run_plan()` -- `cli.py` only threads `--skip-dev` through
    as a plain kwarg."""
    from reasona_dev import orchestrate

    plan, workdir = _plan(tmp_path)
    seen = {}

    def _fake_run_plan(**kw):
        seen["skip_dev"] = kw["skip_dev"]
        return _shipped_result()

    monkeypatch.setattr(orchestrate, "run_plan", _fake_run_plan)

    rc = main(["run-plan", str(plan), "--workdir", str(workdir), "--skip-dev"])
    assert rc == 0
    assert seen == {"skip_dev": True}


def test_run_plan_dev_flag_and_policy_flags_reach_orchestrate(tmp_path, monkeypatch):
    from reasona_dev import orchestrate

    plan, workdir = _plan(tmp_path)
    seen = {}

    def _fake_run_plan(**kw):
        seen["dev_flag"] = kw["dev_flag"]
        seen["policy_flags"] = kw["policy_flags"]
        return _shipped_result()

    monkeypatch.setattr(orchestrate, "run_plan", _fake_run_plan)

    rc = main(["run-plan", str(plan), "--workdir", str(workdir), "--dev", "opus", "--bugbot", "codex:o1:max"])
    assert rc == 0
    assert seen["dev_flag"] == "opus"
    assert seen["policy_flags"] == {"dev": "opus", "bugbot": "codex:o1:max"}


# --- ledger-based resume ------------------------------------------------------

def test_run_plan_passes_resume_true_by_default(tmp_path, monkeypatch):
    from reasona_dev import orchestrate

    plan, workdir = _plan(tmp_path)
    seen = {}

    def _fake_run_plan(**kw):
        seen["resume"] = kw["resume"]
        return _shipped_result()

    monkeypatch.setattr(orchestrate, "run_plan", _fake_run_plan)

    rc = main(["run-plan", str(plan), "--workdir", str(workdir)])
    assert rc == 0
    assert seen == {"resume": True}


def test_run_plan_restart_clears_the_ledger_and_passes_resume_false(tmp_path, monkeypatch):
    from reasona_dev import ledger, orchestrate

    plan, workdir = _plan(tmp_path)
    ledger.mark_dev_dispatched(workdir, "plan", "pr-1")
    ledger.mark_unit_terminal(workdir, "plan", "pr-1", status="shipped", reason="an earlier run")
    seen = {}

    def _fake_run_plan(**kw):
        seen["resume"] = kw["resume"]
        return _shipped_result()

    monkeypatch.setattr(orchestrate, "run_plan", _fake_run_plan)

    rc = main(["run-plan", str(plan), "--workdir", str(workdir), "--restart"])
    assert rc == 0
    assert seen == {"resume": False}
    assert ledger.unit_status(workdir, "plan", "pr-1") is None
    assert ledger.dev_already_dispatched(workdir, "plan", "pr-1") is False


# --- workdir resolution ------------------------------------------------------

def _args(**kw):
    import argparse
    return argparse.Namespace(**kw)


def test_absent_workdir_resolves_to_an_absolute_cwd(tmp_path, monkeypatch):
    """`--workdir` omitted means the current directory, but ABSOLUTE.

    A relative workdir propagates into the path handed to an agent, and an
    agent runs inside a per-task git worktree where a relative path resolves
    against THAT tree -- observed live as an agent writing its report into
    its own worktree and dying on `error_max_turns` while the driver recorded
    ERROR.
    """
    from reasona_dev.cli import _workdir

    monkeypatch.chdir(tmp_path)
    got = _workdir(_args(workdir=None))
    assert got.is_absolute()
    assert got == tmp_path.resolve()


def test_a_relative_workdir_is_resolved(tmp_path, monkeypatch):
    from reasona_dev.cli import _workdir

    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)
    assert _workdir(_args(workdir="sub")) == (tmp_path / "sub").resolve()


def test_an_absolute_workdir_is_preserved(tmp_path):
    from reasona_dev.cli import _workdir

    assert _workdir(_args(workdir=str(tmp_path))) == tmp_path.resolve()


def test_every_subcommand_that_takes_workdir_uses_the_same_helper():
    """The two used to disagree -- `compile-plan` passed None through to a
    `Path.cwd()` default while the others substituted the literal ".". One
    entry point is what keeps a NEW caller from rediscovering the rule."""
    import inspect

    from reasona_dev import cli

    for name in ("_cmd_compile_plan", "_cmd_acceptance", "_cmd_prompts",
                 "_cmd_run_plan", "_cmd_ship_gate", "_cmd_cycles_report"):
        src = inspect.getsource(getattr(cli, name))
        assert "_workdir(args)" in src, f"{name} does not resolve its workdir"
        assert 'args.workdir or "."' not in src, f"{name} still uses the raw fallback"
