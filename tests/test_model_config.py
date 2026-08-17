from reasona_dev.model_config import resolve, resolve_all


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


def test_composite_with_trailing_ocr_marker_strips_it():
    r = resolve("review", env={"REASONA_DEV_REVIEW_MODEL": "claude:opus:high,ocr"})
    assert r.model == "opus"
    assert r.adapter == "claude"
    assert r.effort == "high"


def test_flag_overrides_env_var():
    r = resolve("dev", flag="haiku", env={"REASONA_DEV_DEV_MODEL": "opus"})
    assert r.model == "haiku"
    assert r.source == "flag"


def test_recheck_falls_back_to_resolved_review_not_hardcoded_default():
    review = resolve("review", env={"REASONA_DEV_REVIEW_MODEL": "claude:custom-opus:max"})
    recheck = resolve("recheck", env={}, review_resolved=review)
    assert recheck.model == "custom-opus"
    assert recheck.adapter == "claude"
    assert recheck.effort == "max"
    assert recheck.source == "fallback:review"


def test_recheck_own_env_var_wins_over_review_fallback():
    review = resolve("review", env={})
    recheck = resolve(
        "recheck", env={"REASONA_DEV_RECHECK_MODEL": "sonnet-light"}, review_resolved=review
    )
    assert recheck.model == "sonnet-light"


def test_bugbot_falls_back_to_verify_env_var_itself():
    # dev-ralf-renewal-claude.md §3.7, verbatim: bugbot falls back to the
    # VERIFY_MODEL ENV VAR, not to verify's own resolved outcome.
    bugbot = resolve("bugbot", env={"REASONA_DEV_VERIFY_MODEL": "sonnet-strict"})
    assert bugbot.model == "sonnet-strict"
    assert bugbot.source.startswith("env:REASONA_DEV_VERIFY_MODEL")


def test_bugbot_does_not_inherit_verifys_own_default():
    # A bare `--verify` flag or verify resolving via ITS OWN default must
    # NOT propagate to bugbot -- only the VERIFY_MODEL env var does. This
    # was a real bug in the first draft (bugbot inherited verify's fully
    # resolved value, including verify's default, via a `verify_resolved`
    # parameter that has since been removed).
    verify = resolve("verify", flag="whatever-verify-flag-picked", env={})
    assert verify.source == "flag"  # verify itself resolved via its flag
    bugbot = resolve("bugbot", env={})  # no REASONA_DEV_VERIFY_MODEL set
    assert bugbot.model == "deepseek-v4-pro"  # bugbot's OWN default, not verify's flag value
    assert bugbot.adapter == "kilo"
    assert bugbot.source == "default"


def test_bugbot_own_env_var_wins_over_verify_env_fallback():
    bugbot = resolve(
        "bugbot",
        env={
            "REASONA_DEV_VERIFY_MODEL": "sonnet-strict",
            "REASONA_DEV_BUGBOT_MODEL": "deepseek-v4-pro",
        },
    )
    assert bugbot.model == "deepseek-v4-pro"
    assert bugbot.source == "env:REASONA_DEV_BUGBOT_MODEL"


def test_bugbot_final_default_when_nothing_resolved():
    bugbot = resolve("bugbot", env={})
    assert bugbot.model == "deepseek-v4-pro"
    assert bugbot.adapter == "kilo"
    assert bugbot.source == "default"


def test_final_audit_falls_back_to_verify_env_var_not_verifys_default():
    fa = resolve("final_audit", env={})  # verify unresolved / no env var
    assert fa.model == "opus"  # final_audit's OWN default
    assert fa.source == "default"

    fa2 = resolve("final_audit", env={"REASONA_DEV_VERIFY_MODEL": "sonnet-v3"})
    assert fa2.model == "sonnet-v3"
    assert fa2.source.startswith("env:REASONA_DEV_VERIFY_MODEL")


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


def test_resolve_all_dependency_order_is_correct():
    resolved = resolve_all(load_config_files=False, env={"REASONA_DEV_VERIFY_MODEL": "sonnet-v2"})
    assert resolved["bugbot"].model == "sonnet-v2"
    assert resolved["final_audit"].model == "sonnet-v2"
    assert resolved["recheck"].model == resolved["review"].model
    assert "dev_escalation" in resolved


def test_resolve_all_flags_take_priority_everywhere():
    resolved = resolve_all(load_config_files=False, flags={"dev": "haiku", "bugbot": "custom"}, env={})
    assert resolved["dev"].model == "haiku"
    assert resolved["bugbot"].model == "custom"
    assert resolved["dev"].source == "flag"
