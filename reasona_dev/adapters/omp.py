"""OMP (Oh My Pi) CLI adapter -- ported from dev-ralf `reference/dispatch.md` ->
*External CLI* -> *omp* (added 2026-08-23, tas-dev-plugins commit `114e70b`, the
same commit that reverted `kilo` back to `opencode` after kilo turned out to
carry the identical silent-hang defect it was adopted to work around --
`plugins/dev/skills/dev-ralf/reference/rationale.md` -> *opencode <-> kilo*).

Confirmed absent from the installed Bernstein 3.15.1 adapter registry the same
way `ocr.py`'s own docstring confirmed for `ocr` (grepped `adapters/` and
`adapters/registry.py`, no `omp.py`, no other module wiring `"omp"`). Ported
here the same way `ocr` was: a `CLIAdapter` subclass registered under the
`bernstein.adapters` entry-point group (see `pyproject.toml`), so a role whose
`role_model_policy.provider` is `omp` is spawned through here without any
reasona-dev code touching the subprocess itself.

**Single-shot, never `--resume` -- deliberately diverges from dev-ralf's own
two-phase warmup+resume design.** dev-ralf's raw dispatch keeps ONE long-lived
`omp` session PER ROLE across every fix cycle of a PR (warmup once via
`--mode json` to capture the session id, `--resume "$session_id"` every cycle
after) -- a session-reuse optimization dev-ralf's own rationale.md documents
as real cost savings for review/final-audit roles. reasona-dev has nothing to
reuse: every `bernstein_dispatch.run_plan_file()` call IS its own fresh
`bernstein run` subprocess, spawned, executed, and reaped before the next one
starts (`run_plan_file()`'s own docstring: "the run spawns, executes, merges
and exits, so there is nothing to poll") -- there is no live session anywhere
in this project's architecture for `--resume` to attach to, on ANY adapter,
not just this one (`pr_cycle.py`'s own module docstring: "no CLI session to
`--resume`, so the artifact file is the only reliable handoff"). Each cycle's
FULL context is already reassembled into the prompt text itself -- the same
file-handoff convention every other role in this project already uses -- so
this adapter runs `omp` exactly once per spawn, with the complete prompt, and
never passes `--mode json` or `--resume`. This is the same simplification
`ocr.py`'s own `supports_session_continuation = False` already established
for a different CLI, for the same underlying reason.

**The transient `Working...` status line dispatch.md strips is not handled
here, on purpose.** dev-ralf's own raw dispatch greps it out of `$tmpfile`
before matching the `=== DEV-RALF DONE` marker line-by-line. reasona-dev never
does line-oriented matching on raw adapter output -- `pr_cycle.py`/`roles.py`
extract a specific `=== ... RESULT ===` block (or labeled `TITLE:`/`CHANGES:`
lines) out of the full text regardless of what surrounds it, so one extra
cosmetic status line is inert here and stripping it would be dead code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from bernstein.core.models import ModelConfig

#: Operator override, mirroring ocr.py's/agy.py's own BERNSTEIN_<X>_BINARY pattern.
BINARY_ENV_VAR: str = "BERNSTEIN_OMP_BINARY"
OMP_BINARY: str = "omp"


class OmpBinaryNotInstalledError(RuntimeError):
    """Raised when the `omp` binary cannot be resolved on PATH."""


def resolve_omp_binary(*, which: Any = None, env: dict[str, str] | None = None, strict: bool = False) -> str:
    """Same discovery cascade as `ocr.resolve_ocr_binary`/`agy.resolve_agy_binary`:
    env override wins, then PATH lookup, then (strict only) a named error
    instead of a bare FileNotFoundError.
    """
    source_env = env if env is not None else os.environ
    resolver = which if which is not None else shutil.which
    override = (source_env.get(BINARY_ENV_VAR) or "").strip()
    if override:
        if resolver(override) is None:
            raise OmpBinaryNotInstalledError(
                f"{BINARY_ENV_VAR}={override!r} but {override!r} is not on PATH."
            )
        return override
    if resolver(OMP_BINARY) is not None:
        return OMP_BINARY
    if strict:
        raise OmpBinaryNotInstalledError(
            f"The '{OMP_BINARY}' binary was not found on PATH. "
            f"Set {BINARY_ENV_VAR}=<path-or-name> to override discovery."
        )
    return OMP_BINARY


def build_omp_command(*, binary: str, prompt: str, model: str = "", effort: str = "") -> list[str]:
    """Pure command-construction function -- testable without spawning a process.

    dispatch.md's own cycle-invocation shape, minus `--resume "$session_id"`
    (see module docstring for why this adapter never sends one):
    `omp -p --auto-approve ${model:+--model "$model"} ${effort:+--thinking "$effort"} "$prompt"`.
    """
    cmd = [binary, "-p", "--auto-approve"]
    if model and model != "default":
        cmd.extend(["--model", model])
    if effort:
        cmd.extend(["--thinking", effort])
    cmd.append(prompt)
    return cmd


class OmpAdapter(CLIAdapter):
    """Spawn and monitor `omp` (Oh My Pi) CLI sessions -- one-shot, no `--resume`."""

    registry_name = "omp"
    provides = ("omp",)
    default_model = "default"
    # No cross-call session reuse in this project's architecture -- see module docstring.
    supports_session_continuation = False

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: "ModelConfig",
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        self.refuse_multimodal_if_needed(multimodal_context)
        self.enforce_network_policy()

        binary = resolve_omp_binary(strict=False)
        cmd = build_omp_command(
            binary=binary,
            prompt=prompt,
            model=getattr(model_config, "model", "") or "",
            effort=getattr(model_config, "effort", "") or "",
        )

        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=getattr(model_config, "model", "") or "",
        )

        env = build_filtered_env()
        preexec_fn = self._get_preexec_fn()
        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    preexec_fn=preexec_fn,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(f"{OMP_BINARY!r} not found in PATH") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing {OMP_BINARY!r}: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "OMP (Oh My Pi)"
