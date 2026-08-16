from reasona_dev.model_config import resolve, resolve_all


def test_default_when_nothing_set():
    r = resolve("dev", env={})
    assert r.value == "sonnet"
    assert r.source == "default"


def test_env_var_overrides_default():
    r = resolve("dev", env={"REASONA_DEV_DEV_MODEL": "opus"})
    assert r.value == "opus"
    assert r.source == "env:REASONA_DEV_DEV_MODEL"


def test_flag_overrides_env_var():
    r = resolve("dev", flag="haiku", env={"REASONA_DEV_DEV_MODEL": "opus"})
    assert r.value == "haiku"
    assert r.source == "flag"


def test_recheck_falls_back_to_resolved_review_not_hardcoded_default():
    review = resolve("review", env={"REASONA_DEV_REVIEW_MODEL": "custom-opus"})
    recheck = resolve("recheck", env={}, review_resolved=review)
    assert recheck.value == "custom-opus"
    assert recheck.source == "fallback:review"


def test_recheck_own_env_var_wins_over_review_fallback():
    review = resolve("review", env={})
    recheck = resolve(
        "recheck", env={"REASONA_DEV_RECHECK_MODEL": "sonnet-light"}, review_resolved=review
    )
    assert recheck.value == "sonnet-light"


def test_bugbot_falls_back_to_verify_env_var_itself():
    # dev-ralf-renewal-claude.md §3.7, verbatim: bugbot falls back to the
    # VERIFY_MODEL ENV VAR, not to verify's own resolved outcome.
    bugbot = resolve("bugbot", env={"REASONA_DEV_VERIFY_MODEL": "sonnet-strict"})
    assert bugbot.value == "sonnet-strict"
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
    assert bugbot.value == "deepseek-v4-pro"  # bugbot's OWN default, not verify's flag value
    assert bugbot.source == "default"


def test_bugbot_own_env_var_wins_over_verify_env_fallback():
    bugbot = resolve(
        "bugbot",
        env={
            "REASONA_DEV_VERIFY_MODEL": "sonnet-strict",
            "REASONA_DEV_BUGBOT_MODEL": "deepseek-v4-pro",
        },
    )
    assert bugbot.value == "deepseek-v4-pro"
    assert bugbot.source == "env:REASONA_DEV_BUGBOT_MODEL"


def test_bugbot_final_default_when_nothing_resolved():
    bugbot = resolve("bugbot", env={})
    assert bugbot.value == "deepseek-v4-pro"
    assert bugbot.source == "default"


def test_final_audit_falls_back_to_verify_env_var_not_verifys_default():
    fa = resolve("final_audit", env={})  # verify unresolved / no env var
    assert fa.value == "opus"  # final_audit's OWN default
    assert fa.source == "default"

    fa2 = resolve("final_audit", env={"REASONA_DEV_VERIFY_MODEL": "sonnet-v3"})
    assert fa2.value == "sonnet-v3"
    assert fa2.source.startswith("env:REASONA_DEV_VERIFY_MODEL")


def test_resolve_all_dependency_order_is_correct():
    resolved = resolve_all(env={"REASONA_DEV_VERIFY_MODEL": "sonnet-v2"})
    assert resolved["bugbot"].value == "sonnet-v2"
    assert resolved["final_audit"].value == "sonnet-v2"
    assert resolved["recheck"].value == resolved["review"].value


def test_resolve_all_flags_take_priority_everywhere():
    resolved = resolve_all(flags={"dev": "haiku", "bugbot": "custom"}, env={})
    assert resolved["dev"].value == "haiku"
    assert resolved["bugbot"].value == "custom"
    assert resolved["dev"].source == "flag"
