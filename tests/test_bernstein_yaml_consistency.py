"""bernstein.yaml's role_model_policy is a hand-maintained mirror of
reasona_dev.model_config's resolved `adapter` per role -- it drifted once
already (bugbot moved to claude:opus:high in the model config but the
committed bernstein.yaml still said "kilo" until this test was added). This
guards against that class of drift recurring silently.

Reads the repo-root `bernstein.yaml` -- this repo commits it directly there
(not under `.reasona/`), because Bernstein's own config loader only ever
reads a root-level (or `.bernstein/`) file for bare `bernstein` / `bernstein
doctor` (no `--seed` override exists for those; only `bernstein run` has
one). `resolve_all(workdir=REPO_ROOT)` resolves against this repo's OWN
committed `.reasona/config.yaml` (project-local beats global), so this
test's outcome doesn't depend on what happens to be in the machine's
`~/.reasona/config.yaml`.
"""

from pathlib import Path

import yaml

from reasona_dev.model_config import BERNSTEIN_ROLE_TO_PRIMARY_CONFIG_ROLE, resolve_all

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_role_model_policy_providers_match_resolved_adapters():
    bernstein_yaml = yaml.safe_load((REPO_ROOT / "bernstein.yaml").read_text())
    policy = bernstein_yaml["role_model_policy"]

    resolved = resolve_all(workdir=REPO_ROOT)  # this repo's own committed .reasona/config.yaml

    for bernstein_role, config_role in BERNSTEIN_ROLE_TO_PRIMARY_CONFIG_ROLE.items():
        assert bernstein_role in policy, f"bernstein.yaml role_model_policy is missing {bernstein_role!r}"
        expected_adapter = resolved[config_role].adapter
        actual_adapter = policy[bernstein_role]["provider"]
        assert actual_adapter == expected_adapter, (
            f"bernstein.yaml role_model_policy.{bernstein_role}.provider={actual_adapter!r} "
            f"but reasona_dev.model_config now resolves {config_role!r} to adapter={expected_adapter!r} "
            "-- update bernstein.yaml to match."
        )
