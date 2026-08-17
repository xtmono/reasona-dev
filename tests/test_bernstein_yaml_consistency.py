"""bernstein.yaml's role_model_policy is a hand-maintained mirror of
reasona_dev.model_config's resolved `adapter` per role -- it drifted once
already (bugbot moved to claude:opus:high in ~/.reasona/config.yaml but the
committed bernstein.yaml still said "kilo" until this test was added). This
guards against that class of drift recurring silently.
"""

from pathlib import Path

import yaml

from reasona_dev.model_config import resolve_all

REPO_ROOT = Path(__file__).resolve().parent.parent

# Bernstein role name -> the model_config role that determines its adapter.
# Mirrors reasona_dev/plugin.py's _SPAWN_ROLE_TO_CONFIG_ROLES, but picks the
# single PRIMARY config role per Bernstein role (not "any of" -- this test
# checks bernstein.yaml matches one specific resolved adapter, not a set of
# acceptable ones).
_BERNSTEIN_ROLE_TO_PRIMARY_CONFIG_ROLE = {
    "backend": "dev",
    "reviewer": "review",
    "bugbot": "bugbot",
    "compliance": "verify",
}


def test_role_model_policy_providers_match_resolved_adapters():
    bernstein_yaml = yaml.safe_load((REPO_ROOT / "bernstein.yaml").read_text())
    policy = bernstein_yaml["role_model_policy"]

    resolved = resolve_all()  # real config layers -- this pairing is environment-specific by design

    for bernstein_role, config_role in _BERNSTEIN_ROLE_TO_PRIMARY_CONFIG_ROLE.items():
        assert bernstein_role in policy, f"bernstein.yaml role_model_policy is missing {bernstein_role!r}"
        expected_adapter = resolved[config_role].adapter
        actual_adapter = policy[bernstein_role]["provider"]
        assert actual_adapter == expected_adapter, (
            f"bernstein.yaml role_model_policy.{bernstein_role}.provider={actual_adapter!r} "
            f"but reasona_dev.model_config now resolves {config_role!r} to adapter={expected_adapter!r} "
            "-- update bernstein.yaml to match."
        )
