import json

import yaml

from reasona_dev.cli import main


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


def test_render_review_bugbot_flag_reaches_pipeline(tmp_path):
    out = tmp_path / "review.yaml"
    workdir = tmp_path / "target-repo"
    workdir.mkdir()

    rc = main(
        [
            "render-review", "-o", str(out),
            "--workdir", str(workdir), "--bugbot", "custom-bugbot-model",
        ]
    )
    assert rc == 0

    pipeline = yaml.safe_load(out.read_text())
    bugbot_agent = next(a for a in pipeline["stages"][1]["agents"] if a["role"] == "bugbot")
    assert bugbot_agent["model"] == "custom-bugbot-model"


def test_render_review_bounded_flag_produces_single_stage(tmp_path):
    out = tmp_path / "review.yaml"
    workdir = tmp_path / "target-repo"
    workdir.mkdir()

    rc = main(["render-review", "-o", str(out), "--workdir", str(workdir), "--bounded"])
    assert rc == 0

    pipeline = yaml.safe_load(out.read_text())
    assert len(pipeline["stages"]) == 1


def test_final_audit_flag_uses_dashed_cli_name_but_role_key(tmp_path):
    out = tmp_path / "review.yaml"
    workdir = tmp_path / "target-repo"
    workdir.mkdir()

    # --final-audit is the flag name (matches dev-ralf's own flag naming),
    # but it does not appear on the initial-review pipeline's agents --
    # this just confirms the parser accepts it without error.
    rc = main(["render-review", "-o", str(out), "--workdir", str(workdir), "--final-audit", "opus"])
    assert rc == 0
