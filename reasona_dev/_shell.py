"""The one subprocess-run helper every git/gh-calling module in this
package shares -- `final_phase.py`, `worktree.py`, `gh_pr.py`,
`gh_review.py`, `gh_review_watch.py`. Extracted rather than left as
`final_phase.py`'s own private `_run()` once a second module needed the
exact same timeout/error-shape handling; duplicating it per module would
have meant four places to keep in sync on the next fix.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run(cmd: list[str], workdir: Path, *, timeout: int = 300) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=str(workdir), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(cmd)}: timed out after {timeout}s"
    except OSError as exc:
        return 1, "", str(exc)
    return p.returncode, p.stdout, p.stderr
