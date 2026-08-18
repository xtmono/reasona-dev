"""OCR reviewer adapter -- the one dev-ralf CLI genuinely absent from Bernstein.

Confirmed absent: `grep -i ocr` over the entire installed Bernstein 3.15.1
`adapters/` tree and `adapters/registry.py` returns nothing (unlike `agy`,
which IS a native Bernstein adapter -- "Antigravity CLI", binary name `agy`,
same flag shape dev-ralf's dispatch.md already documents; see
docs/ARCHITECTURE.md §3.4 -- no adapter needed there).

Ported from dev-ralf `reference/dispatch.md` -> *External CLI* -> *ocr*:

    ocr review --repo "$worktree_path" --from origin/main --to HEAD \
        --format json --audience agent --timeout $((T / 60)) \
        ${model:+--model "$model"} ${background:+--background "$background"}

Key properties this adapter must preserve (dispatch.md is explicit about
all three):

- **Stateless.** ocr re-diffs `origin/main..HEAD` fresh every invocation --
  no `$prompt`, no session, no `--resume`. The `prompt`/`session_id`
  parameters `CLIAdapter.spawn()` requires are accepted (interface
  contract) but not passed to the `ocr` binary.
- **`--timeout` is OCR's own per-file budget in MINUTES**, independent of
  the outer process timeout (seconds). Left unset, a large file can hit
  ocr's internal 10-minute default and silently come back
  `"classification":"timeout"` even with outer budget left. This adapter
  always derives it from `timeout_seconds // 60` so the two budgets agree.
- **stdout is one JSON object** (`{status, comments[], failed[]}`), no PTY,
  no stream-json framing -- reasona_dev.finding_adapter.parse_ocr_result
  consumes it directly.

**No reasona_dev module calls this, by design.** It is registered under the
`bernstein.adapters` entry-point group (see pyproject.toml), so the consumer
is Bernstein's own adapter registry, not this package: a role whose
`role_model_policy.provider` is `ocr` is spawned through here without any
reasona-dev code being involved. Grepping for a caller inside `reasona_dev/`
finds nothing, and that is the correct state -- not dead code.

What IS unbuilt is the reviewer that would use it. `.reasona/reasona.yaml`
carries `review: claude:opus:high,ocr`, whose `,ocr` extra was meant to run
OCR as an ADDITIONAL reviewer beside the primary one, merging both verdicts
through `finding_adapter.merge()`. Nothing dispatches that second reviewer
yet, so the extra is parsed and ignored. `parse_ocr_result` is the other half
already in place: it maps OCR's `failed[]` to INCONCLUSIVE rather than to a
synthetic finding, which is the one INCONCLUSIVE producer this project has.
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

#: Operator override, mirroring agy.py's BERNSTEIN_AGY_BINARY pattern.
BINARY_ENV_VAR: str = "BERNSTEIN_OCR_BINARY"
OCR_BINARY: str = "ocr"


class OcrBinaryNotInstalledError(RuntimeError):
    """Raised when the `ocr` binary cannot be resolved on PATH."""


def resolve_ocr_binary(*, which: Any = None, env: dict[str, str] | None = None, strict: bool = False) -> str:
    """Same discovery cascade as `agy.resolve_agy_binary`: env override wins,
    then PATH lookup, then (strict only) a named error instead of a bare
    FileNotFoundError.
    """
    source_env = env if env is not None else os.environ
    resolver = which if which is not None else shutil.which
    override = (source_env.get(BINARY_ENV_VAR) or "").strip()
    if override:
        if resolver(override) is None:
            raise OcrBinaryNotInstalledError(
                f"{BINARY_ENV_VAR}={override!r} but {override!r} is not on PATH."
            )
        return override
    if resolver(OCR_BINARY) is not None:
        return OCR_BINARY
    if strict:
        raise OcrBinaryNotInstalledError(
            f"The '{OCR_BINARY}' binary was not found on PATH. "
            f"Set {BINARY_ENV_VAR}=<path-or-name> to override discovery."
        )
    return OCR_BINARY


def build_ocr_command(
    *,
    binary: str,
    workdir: Path,
    timeout_seconds: int,
    model: str = "",
    background: str = "",
) -> list[str]:
    """Pure command-construction function -- testable without spawning a process."""
    timeout_minutes = max(1, timeout_seconds // 60)
    cmd = [
        binary, "review",
        "--repo", str(workdir),
        "--from", "origin/main",
        "--to", "HEAD",
        "--format", "json",
        "--audience", "agent",
        "--timeout", str(timeout_minutes),
    ]
    if model and model != "default":
        cmd.extend(["--model", model])
    if background:
        cmd.extend(["--background", background])
    return cmd


class OcrAdapter(CLIAdapter):
    """Spawn and monitor `ocr review` -- stateless diff-scanning reviewer."""

    registry_name = "ocr"
    provides = ("ocr",)
    default_model = "default"
    # ocr has no session/resume concept -- every cycle is a fresh diff scan
    # (dispatch.md: "no warmup, no conversation to resume").
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
        # ocr takes no prompt -- it re-diffs the worktree itself. `prompt`
        # and `system_addendum` are accepted only to satisfy the interface.
        self.refuse_multimodal_if_needed(multimodal_context)

        binary = resolve_ocr_binary(strict=False)
        cmd = build_ocr_command(
            binary=binary,
            workdir=workdir,
            timeout_seconds=timeout_seconds,
            model=getattr(model_config, "model", "") or "",
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
                raise RuntimeError(f"{OCR_BINARY!r} not found in PATH") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing {OCR_BINARY!r}: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "OCR (diff-scanning reviewer)"
