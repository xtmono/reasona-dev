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


