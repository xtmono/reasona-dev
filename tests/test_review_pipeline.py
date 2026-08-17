from reasona_dev.model_config import resolve_all
from reasona_dev.review_pipeline import render, render_bounded_recheck


def test_render_uses_resolved_models_not_hardcoded():
    resolved = resolve_all(load_config_files=False, env={"REASONA_DEV_REVIEW_MODEL": "opus-custom"})
    pipeline = render(resolved)
    reviewer = pipeline["stages"][0]["agents"][0]
    assert reviewer["model"] == "opus-custom"


def test_adapter_and_effort_come_from_resolved_spec_not_literals():
    # These used to be hardcoded literals ("claude"/"kilo", "high") in
    # review_pipeline.py, bypassing the priority chain entirely -- a
    # composite env var must now be able to move an agent off its default
    # adapter/effort, same as it can move its model.
    resolved = resolve_all(load_config_files=False, env={"REASONA_DEV_BUGBOT_MODEL": "codex:o1:max"})
    pipeline = render(resolved)
    bugbot = next(a for a in pipeline["stages"][1]["agents"] if a["role"] == "bugbot")
    assert bugbot["model"] == "o1"
    assert bugbot["adapter"] == "codex"
    assert bugbot["effort"] == "max"

    compliance = next(a for a in pipeline["stages"][1]["agents"] if a["role"] == "compliance")
    assert compliance["adapter"] == "claude"
    assert compliance["effort"] == "high"


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
    resolved = resolve_all(load_config_files=False, env={"REASONA_DEV_RECHECK_MODEL": "codex:o1:max"})
    pipeline = render_bounded_recheck(resolved)
    assert len(pipeline["stages"]) == 1
    reviewer = pipeline["stages"][0]["agents"][0]
    assert reviewer["model"] == "o1"
    assert reviewer["adapter"] == "codex"
    assert reviewer["effort"] == "max"
