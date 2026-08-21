"""B-5: a local CI gate -- dev-ralf runs `$CI_FAST` after every dev fix
(reverting the commit on failure) and a full `make ci` once before
`/gh-pr` (worker.md -> *Ship via /gh-pr* §4). reasona-dev shipped with
neither: `acceptance.py`'s own module docstring names the gap directly --
"a plan that never writes an `acceptance:` block gets zero build/test
verification anywhere in reasona-dev's pipeline, silently." The only
backstop was GitHub's own CI, checked by `gh_review.py` -- after the PR is
already public, and after paying a full CI round trip for a defect a local
`cargo check`/`go build` would have caught in seconds.

**Configured, not assumed** -- `acceptance.py`'s own reason for not doing
this (reasona-dev cannot know a target repo's build command the way
dev-ralf's worker reads it from that repo's own `Makefile`) no longer holds
once the command is just another `.reasona/reasona.yaml` setting, the same
two-layer cascade every other role/model value already uses:

    ci:
      fast: "cargo check --workspace --all-targets"   # after every dev fix
      full: "make ci"                                  # once before /gh-pr

Unconfigured (no `ci:` key) -- both gates are a no-op, so upgrading with no
config change leaves every existing repo's behavior byte-for-byte
unchanged; this is opt-in, not a default that could break a target repo
whose fast/full commands are unknown or absent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

DEFAULT_FAST_TIMEOUT = 600
DEFAULT_FULL_TIMEOUT = 1800


def _run(command: str, workdir: Path, timeout: int) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(workdir),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except OSError as exc:
        return False, str(exc)
    tail = (proc.stdout + proc.stderr)[-2000:]
    return proc.returncode == 0, tail


def run_fast(workdir: Path, command: str | None, *, pre_fix_head: str | None, timeout: int = DEFAULT_FAST_TIMEOUT) -> tuple[bool, str]:
    """Run the fast CI check right after a dev fix commits. On failure,
    revert to `pre_fix_head` -- worker.md: "revert on failure". A fix that
    does not even compile must not survive into the next cycle's diff,
    where it would otherwise get re-reviewed (and likely re-reported) as
    if it were the reviewer's own finding rather than a broken fix attempt.

    A no-op `(True, "")` when `command` is `None` (the gate is not
    configured) -- callers do not need their own "is this configured"
    branch.
    """
    if not command:
        return True, ""
    ok, tail = _run(command, workdir, timeout)
    if not ok and pre_fix_head:
        subprocess.run(
            ["git", "-C", str(workdir), "reset", "--hard", pre_fix_head],
            capture_output=True, text=True, check=False,
        )
    return ok, tail


def run_full(workdir: Path, command: str | None, *, timeout: int = DEFAULT_FULL_TIMEOUT) -> tuple[bool, str]:
    """Run the full CI suite once, right before `/gh-pr` creates anything.
    No revert here -- this runs on a PR unit's accumulated commits, not one
    fix in isolation, so there is no single "pre" state reverting to would
    make correct; a failure here simply refuses to open the PR (matching
    worker.md's own §4: the gate blocks PR creation, it does not undo
    history). No-op `(True, "")` when unconfigured.
    """
    if not command:
        return True, ""
    return _run(command, workdir, timeout)
