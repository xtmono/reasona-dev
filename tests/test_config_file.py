from reasona_dev import config_file
from reasona_dev.model_config import resolve


def test_model_for_extracts_role_from_models_key():
    cfg = {"models": {"dev": "opus", "review": "sonnet"}}
    assert config_file.model_for("dev", cfg) == "opus"
    assert config_file.model_for("recheck", cfg) is None


def test_model_for_handles_missing_or_malformed_config():
    assert config_file.model_for("dev", {}) is None
    assert config_file.model_for("dev", {"models": "not-a-dict"}) is None
    assert config_file.model_for("dev", {"models": {"dev": ""}}) is None


def test_load_project_reads_dot_reasona_config_yaml(tmp_path):
    repo = tmp_path / "target-repo"
    (repo / ".reasona").mkdir(parents=True)
    (repo / ".reasona" / "config.yaml").write_text("models:\n  dev: opus\n")

    cfg = config_file.load_project(repo)
    assert cfg == {"models": {"dev": "opus"}}


def test_load_project_missing_file_returns_empty_dict(tmp_path):
    assert config_file.load_project(tmp_path / "nonexistent") == {}


def test_project_config_beats_global_config():
    project_cfg = {"models": {"dev": "opus-project"}}
    global_cfg = {"models": {"dev": "opus-global"}}
    r = resolve("dev", env={}, project_cfg=project_cfg, global_cfg=global_cfg)
    assert r.value == "opus-project"
    assert r.source == "config:project:dev"


def test_global_config_used_when_project_silent():
    r = resolve("dev", env={}, project_cfg={}, global_cfg={"models": {"dev": "opus-global"}})
    assert r.value == "opus-global"
    assert r.source == "config:global:dev"


def test_env_var_beats_both_config_layers():
    r = resolve(
        "dev",
        env={"REASONA_DEV_DEV_MODEL": "from-env"},
        project_cfg={"models": {"dev": "from-project"}},
        global_cfg={"models": {"dev": "from-global"}},
    )
    assert r.value == "from-env"
    assert r.source == "env:REASONA_DEV_DEV_MODEL"


def test_config_beats_hardcoded_default():
    r = resolve("dev", env={}, project_cfg={}, global_cfg={"models": {"dev": "opus-global"}})
    assert r.value != "sonnet"  # would be the hardcoded default without config


def test_bugbot_falls_back_to_verify_config_slot_not_verifys_resolved_value():
    # Same asymmetry as the env-var chain: bugbot must consult verify's OWN
    # config slot, never verify's fully-resolved outcome (which could have
    # come from a --verify flag that must NOT propagate to bugbot).
    project_cfg = {"models": {"verify": "sonnet-from-config"}}
    r = resolve("bugbot", env={}, project_cfg=project_cfg, global_cfg={})
    assert r.value == "sonnet-from-config"
    assert "via verify fallback" in r.source


def test_recheck_config_slot_beats_review_fallback():
    review = resolve("review", env={}, project_cfg={}, global_cfg={})
    project_cfg = {"models": {"recheck": "sonnet-recheck-config"}}
    r = resolve("recheck", env={}, project_cfg=project_cfg, global_cfg={}, review_resolved=review)
    assert r.value == "sonnet-recheck-config"
    assert r.source == "config:project:recheck"


def test_flag_beats_config_file():
    r = resolve("dev", flag="haiku", project_cfg={"models": {"dev": "opus"}}, env={})
    assert r.value == "haiku"
    assert r.source == "flag"
