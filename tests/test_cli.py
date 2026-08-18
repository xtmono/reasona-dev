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
