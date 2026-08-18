"""Runs one Bernstein instance for a whole plan and dispatches each role as
an HTTP `POST /tasks`.

**The instance is a central task server plus a long-lived worker.** Bernstein
offers three execution modes and only the third is a daemon that keeps
claiming externally-posted tasks:

    bernstein run <plan>          batch: spawn, execute, merge, EXIT
    python -m ...orchestrator     the batch engine's claim loop -- self-stops
                                  on quiescence BY DESIGN
    bernstein serve + worker      central node + executor that "blocks until
                                  SIGINT/SIGTERM"

Both of the first two were tried here and both failed live, in ways that only
a real run shows: `bernstein start` (a seed bootstrap, not a bare server) left
`/health` reporting `spawner: {pid: null}` while every dispatch sat unclaimed,
and the raw orchestrator logged `Quiescence confirmed after 2.0s settle window
- self-stopping` the moment the review stage drained, stranding the entire
scan stage. See docs/ARCHITECTURE.md §3.8.1.

The worker mode is also the one that keeps remote execution open: it takes
`--server URL --token`, so the executor need not run on the machine posting
the tasks.

**What does NOT change: the file-handoff convention.** `result_summary` is
Bernstein's own one-line auto-completion note, never the agent's report, so
the task `description` still ends with "write your entire output to
`<raw_output_path>` as your final action" and that file is what
`pr_cycle` parses.

**The file is also the completion indicator, not the task status.** Bernstein
3.15.1 raises `TypeError: Object of type AgentLogSummary is not JSON
serializable` on the path that auto-completes a task whose agent has died,
leaving finished work parked at `claimed` forever -- observed live. Waiting on
the artifact is not a workaround layered over the contract; it IS the
contract, since the report was never carried by the status in the first place.
See `poll_task`.

**What the HTTP body carries.** `model`/`effort`/`cli` and
`completion_signals` are first-class fields on `TaskCreate`
(`core/server/server_models.py`), verified live, so no `plan.yaml` file is
written or read back for a role dispatch.

**Auth.** `BERNSTEIN_AUTH_TOKEN` is generated here and given to both
processes, so this module -- rather than Bernstein's auto-generated,
only-logged fallback -- is the one thing that knows it.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# TaskStatus values (bernstein.core.tasks.models.TaskStatus) that mean the
# orchestrator will never touch this task again -- polling stops here.
_TERMINAL_STATUSES = frozenset({
    "done", "closed", "failed", "cancelled", "orphaned", "abandoned", "refused", "blocked_by_abandon",
})

# `pending_approval` is NOT terminal -- Bernstein's own definition is
# "Completed; awaiting human approval before taking effect", so the task
# still moves on its own once a person acts. It needs its own handling
# rather than living in either set: treating it as terminal would report an
# unapproved task as finished, and treating it as an ordinary in-flight
# status would silently burn the normal poll timeout waiting for a human
# who was never told they were needed.
_AWAITING_APPROVAL = "pending_approval"


@dataclass
class ServerHandle:
    """The two processes a usable Bernstein instance needs, plus its token.

    Two, not one: `serve` is the task server and the orchestrator is what
    actually claims tasks and spawns agents. See `start_server()`.
    """

    process: subprocess.Popen  # the central task server (`bernstein serve`)
    base_url: str
    token: str
    spawner: subprocess.Popen | None = None  # the executor (`bernstein worker`)


# Roles the worker is told to claim. Must match what `pr_cycle` dispatches
# AND what the seed's `role_model_policy` whitelists -- a role missing from
# either side is a task nobody executes.
WORKER_ROLES = "backend,reviewer,bugbot,compliance,final_audit"


def start_server(
    workdir: str | Path,
    *,
    port: int = 8052,
    startup_timeout: float = 60.0,
    slots: int = 2,
) -> ServerHandle:
    """Bring up a central task server and a long-lived worker on `port`.

    **Which of Bernstein's three execution modes this is, and why.** Bernstein
    offers three, and only one of them is a daemon that keeps claiming
    externally-posted tasks:

        bernstein run <plan>                 batch: spawn, execute, merge, EXIT
        python -m ...orchestration.orchestrator
                                             the batch engine's claim loop --
                                             self-stops on quiescence BY DESIGN
        bernstein serve + bernstein worker   central node + executor that
                                             "blocks until SIGINT/SIGTERM"

    This module used to launch `bernstein start`, which is neither -- it
    bootstraps a run FROM the seed's own `goal:`. A live run showed the
    result exactly: the server came up, `POST /tasks` succeeded, and
    `/health` reported `spawner: {pid: null}`, so every dispatch sat
    unclaimed until its poll timeout.

    Replacing it with the raw orchestrator module got tasks executing but
    surfaced the next layer of the same mistake: that process is the BATCH
    engine's loop, and it logged `Quiescence confirmed after 2.0s settle
    window - self-stopping` the moment the review stage drained. Every task
    posted after that -- the whole scan stage -- had nobody to claim it.
    That is not a Bernstein limitation; it is the batch engine behaving as
    documented while being used as a daemon.

    `bernstein worker` is the mode built for this shape, and it is also the
    one that makes remote execution possible later: a worker takes
    `--server URL --token`, so the executor does not have to live on the
    machine that posts the tasks.
    """
    workdir = Path(workdir)
    token = secrets.token_urlsafe(24)
    full_env = os.environ.copy()
    full_env["BERNSTEIN_AUTH_TOKEN"] = token

    server = subprocess.Popen(
        ["bernstein", "serve", "--port", str(port)],
        cwd=workdir, env=full_env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + startup_timeout
    last_error: Exception | None = None
    healthy = False
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as resp:
                if resp.status == 200:
                    healthy = True
                    break
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            last_error = exc
        time.sleep(1.0)
    if not healthy:
        server.terminate()
        raise RuntimeError(
            f"bernstein task server on {base_url} did not become healthy within {startup_timeout}s"
        ) from last_error

    # Started only after the server answers: a worker's first act is to
    # register with the central node, and pointing it at a dead port just
    # produces connection-error noise while it retries.
    spawner = subprocess.Popen(
        ["bernstein", "worker", "--server", base_url, "--token", token,
         "--roles", WORKER_ROLES, "--slots", str(slots)],
        cwd=workdir, env=full_env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return ServerHandle(process=server, base_url=base_url, token=token, spawner=spawner)


def stop_server(handle: ServerHandle, *, workdir: str | Path) -> None:
    """Stop the worker first, then the task server.

    That order matters: a worker whose central node disappears mid-poll logs
    a burst of connection failures and may abandon work whose state it can no
    longer report. Both are terminated directly rather than through
    `bernstein stop`, which acts on pid files only a `bernstein run` writes.

    SIGTERM is the worker's documented clean-exit signal ("Blocks until
    SIGINT/SIGTERM"), so `terminate()` is the intended shutdown, not a kill.
    """
    for proc in (handle.spawner, handle.process):
        if proc is None:
            continue
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _request(handle: ServerHandle, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{handle.base_url}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {handle.token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def dispatch_task(
    handle: ServerHandle,
    *,
    role: str,
    title: str,
    description: str,
    model: str,
    effort: str,
    cli: str,
    raw_output_path: Path,
    max_turns: int | None = None,
) -> str:
    """`POST /tasks` for one role dispatch. Returns the new task's id.

    `completion_signals` mirrors what `_write_role_plan()` used to put in
    the plan step: the orchestrator considers this task done only once
    `raw_output_path` is non-empty, the same file the description instructs
    the agent to write its full output to as its last action.

    `max_turns` raises the agent's turn budget for this dispatch
    (`TaskCreate.max_turns`). It matters most for review-type roles: the
    review prompt asks the agent to enumerate every checklist item and every
    named file/symbol, grep the diff for secrets, AND then write a complete
    report as its final action. Observed live on a three-unit repo, the
    default budget ran out during exploration -- `[RESULT]
    subtype=error_max_turns turns=23` -- so the agent never reached the write
    step and the role came back ERROR with nothing to say about why. Left
    unset, Bernstein/the adapter picks its own default.
    """
    body = {
        "title": title,
        "description": description,
        "role": role,
        "model": model,
        "effort": effort,
        "cli": cli,
        "completion_signals": [{"type": "test_passes", "value": f"test -s {raw_output_path}"}],
    }
    if max_turns is not None:
        body["max_turns"] = max_turns
    response = _request(handle, "POST", "/tasks", body)
    return response["id"]


def poll_task(
    handle: ServerHandle,
    task_id: str,
    *,
    poll_interval: float = 5.0,
    timeout: float = 1800.0,
    approval_timeout: float = 86400.0,
    on_awaiting_approval=None,
    output_path: Path | None = None,
) -> dict:
    """`GET /tasks/{id}` until the task is finished. Returns the final task
    JSON -- the caller decides what a non-`done` status means.

    **`output_path` is the primary completion indicator, not the status.**
    Bernstein 3.15.1 has a bug on the path that auto-completes a task whose
    agent has died: `handle_orphaned_task` builds its completion payload
    from `collect_completion_data()`, which contains an `AgentLogSummary`,
    and the `POST /tasks/{id}/complete` then raises
    `TypeError: Object of type AgentLogSummary is not JSON serializable`.
    The task stays `claimed` forever. Observed live: a reviewer agent ran,
    wrote its full report, exited -- and the dispatch would have blocked for
    the entire 30-minute timeout waiting for a status that could never
    arrive.

    Waiting on the artifact instead is not a workaround bolted on top of the
    contract; it IS the contract. The agent's report was never carried by
    `result_summary` (see this module's header), so the file appearing is
    what "the role finished" has always meant here. The status is a
    secondary signal, and where the two disagree the file is the one with
    the actual deliverable behind it.

    A size that is unchanged across two consecutive polls is what counts as
    written -- the prompt instructs the agent to write it as its final
    action, and the settle check keeps a partially-flushed file from being
    parsed as a truncated report.

    **Approval handling.** A task parked at `pending_approval` waits on a
    person, not an agent, so the ordinary `timeout` must not apply: 30
    minutes is a reasonable bound on a model and a nonsensical one on a
    human. On first entering that state the deadline extends to
    `approval_timeout` and `on_awaiting_approval(task)` fires once.
    """
    deadline = time.monotonic() + timeout
    announced = False
    last_size: int | None = None

    def _settled() -> bool:
        nonlocal last_size
        if output_path is None or not output_path.is_file():
            last_size = None
            return False
        size = output_path.stat().st_size
        if size == 0:
            last_size = None
            return False
        stable = last_size == size
        last_size = size
        return stable

    task = _request(handle, "GET", f"/tasks/{task_id}")
    while task.get("status") not in _TERMINAL_STATUSES:
        if _settled():
            return task
        if task.get("status") == _AWAITING_APPROVAL and not announced:
            announced = True
            deadline = time.monotonic() + approval_timeout
            if on_awaiting_approval is not None:
                on_awaiting_approval(task)
        if time.monotonic() >= deadline:
            waiting_on = "human approval" if announced else "a terminal status"
            raise TimeoutError(
                f"task {task_id} did not reach {waiting_on} in time (last status: {task.get('status')!r})"
            )
        time.sleep(poll_interval)
        task = _request(handle, "GET", f"/tasks/{task_id}")
    return task
