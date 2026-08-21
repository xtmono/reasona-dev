import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_PROMPTS = REPO_ROOT / ".reasona" / "prompts"


@pytest.fixture
def rust_dev_prompts(tmp_path):
    """Seed `<tmp_path>/.reasona/prompts/rust-dev/` from this repo's own copy.

    Needed because prompt resolution has exactly two layers (project, then
    global) with nothing packaged underneath -- so a bare `tmp_path` repo
    has no prompts at all and `run_pr_cycle` aborts. Any test exercising a
    cycle has to supply them, which is the intended behaviour, not a
    workaround: a repo that has not chosen its review prompts should not
    silently get someone else's.

    Copies the REAL files rather than writing stubs so assertions about
    prompt content (e.g. that a bounded recheck carries the confirm+
    regression contract from `recheck.md`) test the prompts actually
    shipped, not a fixture's paraphrase of them.
    """
    dest = tmp_path / ".reasona" / "prompts"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_PROMPTS, dest)
    return dest / "rust-dev"
