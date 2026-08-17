from reasona_dev.model_config import resolve_all
from reasona_dev.review_pipeline import render, render_bounded_recheck


def test_render_uses_resolved_models_not_hardcoded():
    resolved = resolve_all(load_config_files=False, env={"REASONA_DEV_REVIEW_MODEL": "opus-custom"})
    pipeline = render(resolved)
    reviewer = pipeline["stages"][0]["agents"][0]
    assert reviewer["model"] == "opus-custom"


def test_strategy_all_expresses_merge_rule():
    pipeline = render(resolve_all(load_config_files=False, env={}))
    for stage in pipeline["stages"]:
        assert stage["aggregator"]["strategy"] == "all"


def test_ocr_agent_has_no_model_slot():
    pipeline = render(resolve_all(load_config_files=False, env={}))
    ocr_agent = pipeline["stages"][0]["agents"][1]
    assert ocr_agent["adapter"] == "ocr"
    assert ocr_agent["model"] is None


def test_bugbot_and_compliance_use_resolved_values():
    resolved = resolve_all(load_config_files=False, env={"REASONA_DEV_VERIFY_MODEL": "sonnet-v9"})
    pipeline = render(resolved)
    scan_agents = {a["role"]: a["model"] for a in pipeline["stages"][1]["agents"]}
    assert scan_agents["compliance"] == "sonnet-v9"
    assert scan_agents["bugbot"] == "sonnet-v9"  # falls back to verify per model_config chain


def test_bounded_recheck_drops_scan_stage_and_uses_recheck_model():
    resolved = resolve_all(load_config_files=False, env={"REASONA_DEV_RECHECK_MODEL": "sonnet-light"})
    pipeline = render_bounded_recheck(resolved)
    assert len(pipeline["stages"]) == 1
    assert pipeline["stages"][0]["agents"][0]["model"] == "sonnet-light"
