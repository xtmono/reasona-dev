"""Starts one persistent Bernstein task server + orchestrator for a whole
PR cycle, and dispatches each role as an HTTP `POST /tasks` instead of a
fresh `bernstein run <plan> --auto-approve` subprocess per role.

**Why this replaces the old per-role `bernstein run` subprocess.** The
original `pr_cycle.run_role()` shelled out to `bernstein run <plan>
--auto-approve` once PER ROLE PER CYCLE -- each call bootstraps a brand new
task server + orchestrator + worktree setup from scratch, then tears it all
down again once its one-step plan finishes. A `run_pr_cycle()` call can
dispatch a dozen or more roles (review/bugbot/compliance/dev-fix, repeated
across up to 8 cycles); paying that bootstrap cost every single time is
pure overhead. `bernstein start`/`run` both "detach the task server as a
background process and return" (confirmed: `bernstein start --help`'s own
description, and `server_launch.py`'s actual behavior) -- so one process,
started once, can serve every role dispatch in the cycle over its HTTP API,
and only pays the bootstrap cost once.

**What does NOT change: the file-handoff convention.** `POST /tasks`'s
`result_summary` is Bernstein's own one-line auto-completion note (e.g.
`"Auto-completed: agent backend-<id> made git commits on branch (no
signals to verify)"` -- confirmed live), never the agent's actual
free-form report. `pr_cycle.py` needs the agent's REAL structured output
(the markdown report + `RESULT: ...` contract line `finding_adapter.py`
parses) -- so the task `description` still ends with the exact same
"write your entire output to `<raw_output_path>` as your final action"
instruction `_write_role_plan()` used to bake into the plan step, and the
task's own `completion_signals` (`test_passes`: `test -s <raw_output_path>`)
is what the orchestrator uses to know the agent is actually done -- not
just that some git commits landed.

**What DOES change, for the better:** `model`/`effort`/`cli` (adapter) and
`completion_signals` are first-class fields on Bernstein's own `POST
/tasks` body (`core/server/server_models.py::TaskCreate`, confirmed
directly against the installed package) -- so no `plan.yaml` file needs to
be written and read back at all; the same per-dispatch overrides
`_write_role_plan()` used to express as a one-step plan now go straight in
the request body.

**Auth.** `BERNSTEIN_AUTH_TOKEN` is set explicitly (a fresh random token
per server, generated here) before the server subprocess is spawned, so
this module -- not Bernstein's own auto-generated-and-only-logged fallback
-- is the one thing that has to know it, and can supply it as
`Authorization: Bearer <token>` on every request.

**Not yet live-verified.** The individual HTTP calls this module makes
(`POST /tasks`, `GET /tasks/{id}`, `GET /health`) were each confirmed live
against a real running Bernstein server in an earlier session (see
`docs/ARCHITECTURE.md` §3.5.3/§3.5.4 for the paid verification log) -- but
this module's OWN orchestration of them (`start_server()` polling
`/health` until ready, `run_pr_cycle()`'s single persistent server serving
many sequential role dispatches, `stop_server()`'s shutdown) has not itself
been run end-to-end against a live paid server yet. Treat this module the
same way `pr_cycle.run_role()`'s original subprocess call was treated
before its own live pass: the individually-verified primitives are trusted,
their new composition is not, until it is.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
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
    process: subprocess.Popen
    base_url: str
    token: str


def start_server(workdir: str | Path, *, port: int = 8052, startup_timeout: float = 60.0) -> ServerHandle:
    """Launch `bernstein start --port <port>` in `workdir`, wait for
    `GET /health` to answer, and return a handle for `dispatch_task()`/
    `poll_task()`/`stop_server()`.

    `bernstein start` itself detaches the actual server+spawner as a
    background process and returns almost immediately -- the `subprocess.
    Popen` here is that quick-returning parent, not the long-lived server;
    it is kept only so `stop_server()` has something to reap if the
    detached process ever needs a harder kill than `bernstein stop`.
    """
    workdir = Path(workdir)
    token = secrets.token_urlsafe(24)
    full_env = os.environ.copy()
    full_env["BERNSTEIN_AUTH_TOKEN"] = token

    process = subprocess.Popen(
        ["bernstein", "start", "--port", str(port)],
        cwd=workdir,
        env=full_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    base_url = f"http://127.0.0.1:{port}"
    handle = ServerHandle(process=process, base_url=base_url, token=token)
    deadline = time.monotonic() + startup_timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as resp:
                if resp.status == 200:
                    return handle
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            last_error = exc
        time.sleep(1.0)
    raise RuntimeError(f"bernstein server on {base_url} did not become healthy within {startup_timeout}s") from last_error


def stop_server(handle: ServerHandle, *, workdir: str | Path) -> None:
    """`bernstein stop` (graceful -- agents save work first) against the
    same `workdir`/port the server was started with, then reap the
    launcher subprocess this module's own `Popen` tracks.
    """
    subprocess.run(["bernstein", "stop"], cwd=Path(workdir), check=False)
    handle.process.wait(timeout=30)


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
    approval_required: bool = False,
) -> str:
    """`POST /tasks` for one role dispatch. Returns the new task's id.

    `completion_signals` mirrors what `_write_role_plan()` used to put in
    the plan step: the orchestrator considers this task done only once
    `raw_output_path` is non-empty, the same file the description instructs
    the agent to write its full output to as its last action.

    `approval_required` is Bernstein's own per-task human gate
    (`TaskCreate.approval_required`, confirmed against the installed
    package's `core/server/server_models.py`): the task completes normally,
    then parks at `pending_approval` until a person acts, rather than
    taking effect immediately. `pr_cycle` sets it on the first PR unit of a
    plan only -- see its module docstring for why that one point.
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
    if approval_required:
        body["approval_required"] = True
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
) -> dict:
    """`GET /tasks/{id}` until `status` reaches a terminal value. Returns the
    final task JSON -- the caller (`pr_cycle.run_role()`) decides what a
    non-`done` terminal status means, this function only knows polling is
    over.

    **Approval handling.** A task that parks at `pending_approval` is
    waiting on a human, not on an agent, so the ordinary `timeout` must not
    apply to it -- 30 minutes is a reasonable bound on a model finishing
    work and a nonsensical one on a person noticing a review request. On
    first entering that state the deadline is extended to
    `approval_timeout` and `on_awaiting_approval(task)` is called once, so
    the caller can actually tell someone. Without this split an
    approval-gated task would fail with a bare `TimeoutError` that reads
    like a stuck agent.
    """
    deadline = time.monotonic() + timeout
    announced = False
    task = _request(handle, "GET", f"/tasks/{task_id}")
    while task.get("status") not in _TERMINAL_STATUSES:
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
