"""reasona-dev's own two-layer config cascade -- mirrors Bernstein's pattern
(`~/.bernstein/bernstein.yaml` -> `<workdir>/.bernstein/bernstein.yaml`) but
scoped to exactly two layers, as decided for this project:

    ~/.reasona/reasona.yaml           -- global (user-wide defaults)
    <workdir>/.reasona/reasona.yaml   -- project/local (overrides global)

The filename is `reasona.yaml`, not a generic `config.yaml` -- `.reasona/`
is the shared namespace this whole product family lives under, and this
one FILE is meant to be shared too: a future `reasona-plan` (the
`plan-ralf` successor) will read/write the same `reasona.yaml`, under its
own top-level key (`plan-models:`) next to this module's `dev-models:`.
Namespacing by KEY, not by a separate file per product, is deliberate --
see `model_for()`.

This is a SEPARATE cascade from Bernstein's own six-layer one
(docs/ARCHITECTURE.md §0.1) -- reasona-dev's config file only ever sets
`REASONA_DEV_*`-equivalent role->model defaults; it never touches
`bernstein.yaml` itself.

File format -- each value is either a bare model name or dev-ralf's own
`tool:model:effort` composite (`reasona_dev.model_config._split_composite`
parses either shape identically to a flag or env var):

    dev-models:
      dev: claude:sonnet:high
      review: claude:opus:high
      recheck: claude:sonnet:high
      bugbot: kilo:deepseek-v4-pro:high
      verify: claude:sonnet:high
      final_audit: claude:opus:high
      dev_escalation: claude:opus:high
    plan-models:
      ...   # reasona-plan's own future top-level key, same file, no collision

Any subset of roles may be present under `dev-models`; missing keys simply
fall through to the next layer in reasona_dev.model_config's priority
chain. reasona-dev never reads or writes `plan-models` -- that key exists
here purely as documentation of the shape this file is expected to grow
into.
"""

from __future__ import annotations

from pathlib import Path

import yaml

GLOBAL_CONFIG_PATH = Path.home() / ".reasona" / "reasona.yaml"


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
    """`<workdir>/.reasona/reasona.yaml` -- same `workdir` that
    `reasona_dev.plan_compile.compile_to_bernstein_plan()` anchors the audit
    trail to (docs/ARCHITECTURE.md §0.1), never reasona-dev's own install
    location.
    """
    return _load_yaml(Path(workdir) / ".reasona" / "reasona.yaml")


def model_for(role: str, cfg: dict) -> str | None:
    """Extract `dev-models.<role>` from a loaded config dict, or None if
    absent. Reads `dev-models` specifically (not `plan-models`, not a
    generic `models`) -- see module docstring for why this file is
    key-namespaced per product rather than split into one file each.
    """
    models = cfg.get("dev-models")
    if not isinstance(models, dict):
        return None
    val = models.get(role)
    return val.strip() if isinstance(val, str) and val.strip() else None
