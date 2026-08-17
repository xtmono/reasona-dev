import json

import pytest

from reasona_dev import config_file, plugin


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """Isolate resolve_all()'s two filesystem inputs so on_agent_spawned's
    expectation lookup never touches the real machine's ~/.reasona or the
    test runner's own cwd.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_file, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent-global.yaml")
    for var in (
        "REASONA_DEV_DEV_MODEL",
        "REASONA_DEV_REVIEW_MODEL",
        "REASONA_DEV_RECHECK_MODEL",
        "REASONA_DEV_BUGBOT_MODEL",
        "REASONA_DEV_VERIFY_MODEL",
        "REASONA_DEV_FINAL_AUDIT_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_expected_models_maps_bernstein_role_to_config_role():
    assert plugin._expected_models("backend") == {"sonnet"}  # dev default
    assert plugin._expected_models("bugbot") == {"deepseek-v4-pro"}
    assert plugin._expected_models("compliance") == {"sonnet"}  # verify default


def test_expected_models_reviewer_accepts_both_review_and_recheck():
    # recheck falls back to review's resolved value when neither has its
    # own env/config override, so both slots collapse to "opus" here --
    # exercise that both are still accepted, not just one.
    assert plugin._expected_models("reviewer") == {"opus"}


def test_expected_models_unknown_role_returns_empty_set():
    assert plugin._expected_models("ocr_reviewer") == set()
    assert plugin._expected_models("some-other-role") == set()


def test_on_agent_spawned_matching_model_is_silent(tmp_path):
    gate = plugin.ReasonaGatePlugin()
    gate.on_agent_spawned(session_id="s1", role="backend", model="sonnet")
    assert not (tmp_path / ".reasona" / "model_divergence.jsonl").exists()


def test_on_agent_spawned_divergent_model_is_recorded(tmp_path):
    gate = plugin.ReasonaGatePlugin()
    gate.on_agent_spawned(session_id="s1", role="backend", model="opus")

    log_path = tmp_path / ".reasona" / "model_divergence.jsonl"
    assert log_path.exists()
    record = json.loads(log_path.read_text().strip())
    assert record == {
        "session_id": "s1",
        "role": "backend",
        "expected_models": ["sonnet"],
        "actual_model": "opus",
    }


def test_on_agent_spawned_unknown_role_never_writes(tmp_path):
    gate = plugin.ReasonaGatePlugin()
    gate.on_agent_spawned(session_id="s1", role="ocr_reviewer", model="anything")
    assert not (tmp_path / ".reasona" / "model_divergence.jsonl").exists()


def test_on_agent_spawned_appends_multiple_records(tmp_path):
    gate = plugin.ReasonaGatePlugin()
    gate.on_agent_spawned(session_id="s1", role="backend", model="opus")
    gate.on_agent_spawned(session_id="s2", role="bugbot", model="opus")

    lines = (tmp_path / ".reasona" / "model_divergence.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["role"] == "bugbot"
