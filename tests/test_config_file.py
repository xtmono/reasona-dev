from reasona_dev import config_file
from reasona_dev.model_config import resolve


def test_model_for_extracts_role_from_dev_models_key():
    cfg = {"dev-models": {"dev": "opus", "review": "sonnet"}}
    assert config_file.model_for("dev", cfg) == "opus"
    assert config_file.model_for("recheck", cfg) is None


def test_model_for_ignores_plan_models_key():
    # dev-models/plan-models are namespaced siblings in the SAME file (a
    # future reasona-plan reads/writes plan-models here) -- model_for()
    # must never fall through to the other product's key.
    cfg = {"plan-models": {"dev": "should-not-be-read"}}
    assert config_file.model_for("dev", cfg) is None


def test_model_for_handles_missing_or_malformed_config():
    assert config_file.model_for("dev", {}) is None
    assert config_file.model_for("dev", {"dev-models": "not-a-dict"}) is None
    assert config_file.model_for("dev", {"dev-models": {"dev": ""}}) is None


def test_load_project_reads_dot_reasona_reasona_yaml(tmp_path):
    repo = tmp_path / "target-repo"
    (repo / ".reasona").mkdir(parents=True)
    (repo / ".reasona" / "reasona.yaml").write_text("dev-models:\n  dev: opus\n")

    cfg = config_file.load_project(repo)
    assert cfg == {"dev-models": {"dev": "opus"}}


def test_load_project_missing_file_returns_empty_dict(tmp_path):
    assert config_file.load_project(tmp_path / "nonexistent") == {}


def test_project_config_beats_global_config():
    project_cfg = {"dev-models": {"dev": "opus-project"}}
    global_cfg = {"dev-models": {"dev": "opus-global"}}
    r = resolve("dev", env={}, project_cfg=project_cfg, global_cfg=global_cfg)
    assert r.model == "opus-project"
    assert r.source == "config:project:dev"


def test_global_config_used_when_project_silent():
    r = resolve("dev", env={}, project_cfg={}, global_cfg={"dev-models": {"dev": "opus-global"}})
    assert r.model == "opus-global"
    assert r.source == "config:global:dev"


def test_env_var_beats_both_config_layers():
    r = resolve(
        "dev",
        env={"REASONA_DEV_DEV_MODEL": "from-env"},
        project_cfg={"dev-models": {"dev": "from-project"}},
        global_cfg={"dev-models": {"dev": "from-global"}},
    )
    assert r.model == "from-env"
    assert r.source == "env:REASONA_DEV_DEV_MODEL"


def test_config_beats_hardcoded_default():
    r = resolve("dev", env={}, project_cfg={}, global_cfg={"dev-models": {"dev": "opus-global"}})
    assert r.model != "sonnet"  # would be the hardcoded default without config


def test_bugbot_does_not_consult_compliances_config_slot():
    # No cross-role fallback anywhere (SKILL.md): a `dev-models.compliance`
    # config entry must never leak into bugbot's own resolution.
    project_cfg = {"dev-models": {"compliance": "sonnet-from-config"}}
    r = resolve("bugbot", env={}, project_cfg=project_cfg, global_cfg={})
    assert r.model == "deepseek-v4-pro"  # bugbot's OWN default, untouched
    assert r.source == "default"


def test_recheck_config_slot_beats_its_own_default():
    project_cfg = {"dev-models": {"recheck": "sonnet-recheck-config"}}
    r = resolve("recheck", env={}, project_cfg=project_cfg, global_cfg={})
    assert r.model == "sonnet-recheck-config"
    assert r.source == "config:project:recheck"


def test_flag_beats_config_file():
    r = resolve("dev", flag="haiku", project_cfg={"dev-models": {"dev": "opus"}}, env={})
    assert r.model == "haiku"
    assert r.source == "flag"
