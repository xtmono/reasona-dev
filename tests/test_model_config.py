from reasona_dev.model_config import resolve, resolve_all, resolve_review_list


def test_default_when_nothing_set():
    r = resolve("dev", env={})
    assert r.model == "sonnet"
    assert r.adapter == "claude"
    assert r.effort == "high"
    assert r.source == "default"


def test_env_var_overrides_default_bare_model_only():
    r = resolve("dev", env={"REASONA_DEV_DEV_MODEL": "opus"})
    assert r.model == "opus"
    assert r.adapter == "claude"  # bare string keeps the role's default adapter
    assert r.effort == "high"  # ...and default effort
    assert r.source == "env:REASONA_DEV_DEV_MODEL"


def test_env_var_composite_overrides_adapter_and_effort_too():
    # dev-ralf's own DEV_RALF_DEV_MODEL=claude:sonnet:high shape -- every
    # layer (flag/env/config) must parse this identically, not just a bare
    # model name.
    r = resolve("dev", env={"REASONA_DEV_DEV_MODEL": "codex:o1:max"})
    assert r.model == "o1"
    assert r.adapter == "codex"
    assert r.effort == "max"


def test_composite_with_trailing_ocr_marker_is_captured_on_ocr_field():
    r = resolve("review", env={"REASONA_DEV_REVIEW_MODEL": "claude:opus:high,ocr"})
    assert r.model == "opus"
    assert r.adapter == "claude"
    assert r.effort == "high"
    assert r.ocr is True


def test_composite_without_ocr_marker_leaves_ocr_false():
    r = resolve("review", env={"REASONA_DEV_REVIEW_MODEL": "claude:opus:high"})
    assert r.ocr is False


def test_flag_overrides_env_var():
    r = resolve("dev", flag="haiku", env={"REASONA_DEV_DEV_MODEL": "opus"})
    assert r.model == "haiku"
    assert r.source == "flag"


def test_recheck_resolves_via_its_own_default_never_reviews():
    # SKILL.md: no cross-role fallback anywhere -- recheck's default is its
    # OWN claude:sonnet:high, independent of whatever review resolved to.
    resolve("review", env={"REASONA_DEV_REVIEW_MODEL": "claude:custom-opus:max"})
    recheck = resolve("recheck", env={})
    assert recheck.model == "sonnet"
    assert recheck.adapter == "claude"
    assert recheck.effort == "high"
    assert recheck.source == "default"


def test_recheck_own_env_var_wins_over_its_own_default():
    recheck = resolve("recheck", env={"REASONA_DEV_RECHECK_MODEL": "sonnet-light"})
    assert recheck.model == "sonnet-light"
    assert recheck.source == "env:REASONA_DEV_RECHECK_MODEL"


def test_bugbot_does_not_consult_compliances_env_var():
    # SKILL.md: "no cross-role fallback anywhere in this table" -- a
    # COMPLIANCE_MODEL env var must never leak into bugbot's resolution.
    bugbot = resolve("bugbot", env={"REASONA_DEV_COMPLIANCE_MODEL": "sonnet-strict"})
    assert bugbot.model == "deepseek-v4-pro"  # bugbot's OWN default, untouched
    assert bugbot.source == "default"


def test_bugbot_does_not_inherit_compliances_own_flag_either():
    # A bare `--compliance` flag or compliance resolving via ITS OWN default
    # must NOT propagate to bugbot -- every role's resolution reads only its
    # own flag/env var/config, never a sibling role's.
    compliance = resolve("compliance", flag="whatever-compliance-flag-picked", env={})
    assert compliance.source == "flag"  # compliance itself resolved via its flag
    bugbot = resolve("bugbot", env={})  # no REASONA_DEV_COMPLIANCE_MODEL set
    assert bugbot.model == "deepseek-v4-pro"  # bugbot's OWN default, not compliance's flag value
    assert bugbot.adapter == "kilo"
    assert bugbot.source == "default"


def test_bugbot_own_env_var_wins_over_its_own_default():
    bugbot = resolve("bugbot", env={"REASONA_DEV_BUGBOT_MODEL": "deepseek-v4-pro"})
    assert bugbot.model == "deepseek-v4-pro"
    assert bugbot.source == "env:REASONA_DEV_BUGBOT_MODEL"


def test_bugbot_final_default_when_nothing_resolved():
    bugbot = resolve("bugbot", env={})
    assert bugbot.model == "deepseek-v4-pro"
    assert bugbot.adapter == "kilo"
    assert bugbot.source == "default"


def test_final_audit_does_not_consult_compliances_env_var():
    fa = resolve("final_audit", env={})  # its own default
    assert fa.model == "opus"
    assert fa.source == "default"

    fa2 = resolve("final_audit", env={"REASONA_DEV_COMPLIANCE_MODEL": "sonnet-v3"})
    assert fa2.model == "opus"  # unaffected -- final_audit has no cross-role fallback
    assert fa2.source == "default"


def test_dev_escalation_resolves_like_any_other_role():
    r = resolve("dev_escalation", env={})
    assert r.model == "opus"
    assert r.adapter == "claude"
    assert r.effort == "high"
    assert r.source == "default"

    r2 = resolve("dev_escalation", env={"REASONA_DEV_DEV_ESCALATION_MODEL": "codex:o1:max"})
    assert r2.model == "o1"
    assert r2.adapter == "codex"
    assert r2.effort == "max"


def test_resolve_all_resolves_every_role_independently():
    # No role's resolution depends on another's -- a COMPLIANCE_MODEL env
    # var affects ONLY compliance, never bugbot/final_audit/recheck.
    resolved = resolve_all(load_config_files=False, env={"REASONA_DEV_COMPLIANCE_MODEL": "sonnet-v2"})
    assert resolved["compliance"].model == "sonnet-v2"
    assert resolved["bugbot"].model == "deepseek-v4-pro"
    assert resolved["final_audit"].model == "opus"
    assert resolved["recheck"].model == "sonnet"
    assert "dev_escalation" in resolved


def test_resolve_all_flags_take_priority_everywhere():
    resolved = resolve_all(load_config_files=False, flags={"dev": "haiku", "bugbot": "custom"}, env={})
    assert resolved["dev"].model == "haiku"
    assert resolved["bugbot"].model == "custom"
    assert resolved["dev"].source == "flag"


def test_resolve_all_without_review_flags_has_one_reviewer():
    resolved = resolve_all(load_config_files=False, env={})
    assert len(resolved["review_all"]) == 1
    assert resolved["review_all"][0] is resolved["review"]
    assert resolved["review_ocr_requested"] is False


def test_resolve_all_multiple_review_flags_are_all_resolved_in_order():
    resolved = resolve_all(
        load_config_files=False, env={},
        review_flags=["claude:opus:high", "codex:o1:max"],
    )
    assert [r.model for r in resolved["review_all"]] == ["opus", "o1"]
    assert [r.adapter for r in resolved["review_all"]] == ["claude", "codex"]
    assert resolved["review"] is resolved["review_all"][0]  # first stays the single-value representative


def test_resolve_all_ocr_requested_true_if_any_review_flag_carries_the_marker():
    resolved = resolve_all(
        load_config_files=False, env={},
        review_flags=["claude:opus:high", "codex:o1:max,ocr"],
    )
    assert resolved["review_ocr_requested"] is True


def test_resolve_review_list_no_flags_falls_back_to_single_chain():
    reviewers = resolve_review_list(None, env={"REASONA_DEV_REVIEW_MODEL": "opus"})
    assert len(reviewers) == 1
    assert reviewers[0].model == "opus"
    assert reviewers[0].source == "env:REASONA_DEV_REVIEW_MODEL"


def test_resolve_review_list_multiple_flags_each_resolve_independently():
    reviewers = resolve_review_list(["opus", "codex:o1:max,ocr"], env={})
    assert reviewers[0].model == "opus"
    assert reviewers[0].adapter == "claude"  # bare flag keeps review's default adapter
    assert reviewers[0].ocr is False
    assert reviewers[1].model == "o1"
    assert reviewers[1].adapter == "codex"
    assert reviewers[1].ocr is True
