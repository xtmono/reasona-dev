"""reasona-dev's own two-layer config cascade -- mirrors Bernstein's pattern
(`~/.bernstein/bernstein.yaml` -> `<workdir>/bernstein.yaml`) but scoped to
exactly two layers, as decided for this project:

    ~/.reasona/config.yaml           -- global (user-wide defaults)
    <workdir>/.reasona/config.yaml   -- project/local (overrides global)

This is a SEPARATE cascade from Bernstein's own six-layer one
(docs/ARCHITECTURE.md §0.1) -- reasona-dev's config file only ever sets
`REASONA_DEV_*`-equivalent role->model defaults; it never touches
`bernstein.yaml` itself.

File format -- each value is either a bare model name or dev-ralf's own
`tool:model:effort` composite (`reasona_dev.model_config._split_composite`
parses either shape identically to a flag or env var):

    models:
      dev: claude:sonnet:high
      review: claude:opus:high
      recheck: claude:sonnet:high
      bugbot: kilo:deepseek-v4-pro:high
      verify: claude:sonnet:high
      final_audit: claude:opus:high
      dev_escalation: claude:opus:high

Any subset of roles may be present; missing keys simply fall through to the
next layer in reasona_dev.model_config's priority chain.
"""

from __future__ import annotations

from pathlib import Path

import yaml

GLOBAL_CONFIG_PATH = Path.home() / ".reasona" / "config.yaml"


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def load_global() -> dict:
    return _load_yaml(GLOBAL_CONFIG_PATH)


def load_project(workdir: str | Path) -> dict:
    """`<workdir>/.reasona/config.yaml` -- same `workdir` that
    `reasona_dev.plan_compile.compile_to_bernstein_plan()` anchors the audit
    trail to (docs/ARCHITECTURE.md §0.1), never reasona-dev's own install
    location.
    """
    return _load_yaml(Path(workdir) / ".reasona" / "config.yaml")


def model_for(role: str, cfg: dict) -> str | None:
    """Extract `models.<role>` from a loaded config dict, or None if absent."""
    models = cfg.get("models")
    if not isinstance(models, dict):
        return None
    val = models.get(role)
    return val.strip() if isinstance(val, str) and val.strip() else None
