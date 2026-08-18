import subprocess
from pathlib import Path

import yaml

from reasona_dev import bernstein_dispatch
from reasona_dev.bernstein_dispatch import (
    DEFAULT_ROLE_COMPLEXITY,
    DispatchResult,
    run_plan_file,
    write_role_plan,
)


def _plan(tmp_path, **kw) -> dict:
    path = tmp_path / "p.yaml"
    write_role_plan(
        path=path, role=kw.pop("role", "reviewer"), title="t", description="d",
        model=kw.pop("model", "opus"), effort=kw.pop("effort", "high"),
        cli=kw.pop("cli", "claude"), **kw,
    )
    return yaml.safe_load(path.read_text())


def test_one_step_plan_carries_model_effort_and_complexity(tmp_path):
    step = _plan(tmp_path)["stages"][0]["steps"][0]
    assert step["model"] == "opus"
    assert step["effort"] == "high"
    assert step["complexity"] == DEFAULT_ROLE_COMPLEXITY


def test_complexity_is_how_the_turn_budget_travels(tmp_path):
    """`Task.max_turns` is reachable only over the HTTP API -- the plan-step
    schema has no such field. Bernstein derives the budget from `complexity`
    (low=20 / medium=40 / high=80 / critical=120), which is why the batch
    path keeps the control that fixed the live `error_max_turns` death."""
    assert _plan(tmp_path, complexity="critical")["stages"][0]["steps"][0]["complexity"] == "critical"


def test_adapter_goes_at_plan_level_because_the_step_schema_has_no_field(tmp_path):
    plan = _plan(tmp_path, cli="kilo")
    assert plan["cli"] == "kilo"
    assert "cli" not in plan["stages"][0]["steps"][0]


def test_no_completion_signals_are_emitted(tmp_path):
    """Signals are evaluated at the project root BEFORE the agent's branch
    merges, so they cannot see the artifact they would gate on. The driver
    checks the file itself after the run returns."""
    assert "completion_signals" not in _plan(tmp_path)["stages"][0]["steps"][0]


def test_the_plan_validates_against_bernstein(tmp_path):
    """The generated shape has to survive Bernstein's own validator, not just
    our expectations of it."""
    path = tmp_path / "p.yaml"
    write_role_plan(
        path=path, role="reviewer", title="t", description="d",
        model="haiku", effort="low", cli="claude",
    )
    proc = subprocess.run(
        ["bernstein", "plan", "validate", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Plan is valid" in proc.stdout


def test_a_missing_bernstein_binary_is_reported_not_raised(tmp_path, monkeypatch):
    """The caller decides what a failed dispatch means; for most roles the
    artifact's presence is the real verdict anyway."""
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(bernstein_dispatch.subprocess, "run", boom)
    r = run_plan_file(tmp_path / "p.yaml", tmp_path)
    assert not r.ok and "not found on PATH" in r.stderr_tail


def test_a_timeout_is_reported_with_its_bound(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="bernstein", timeout=7)

    monkeypatch.setattr(bernstein_dispatch.subprocess, "run", boom)
    r = run_plan_file(tmp_path / "p.yaml", tmp_path, timeout=7)
    assert not r.ok and "timed out after 7s" in r.stderr_tail


def test_a_successful_run_reports_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bernstein_dispatch.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "done", ""),
    )
    assert run_plan_file(tmp_path / "p.yaml", tmp_path).ok


def test_the_dispatch_is_synchronous(tmp_path, monkeypatch):
    """No polling, no server lifetime: `bernstein run` drives the agent to
    completion and exits, which is the whole reason this path has none of the
    server-mode defects."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bernstein_dispatch.subprocess, "run", fake_run)
    run_plan_file(tmp_path / "p.yaml", tmp_path, port=8099)

    assert seen["cmd"][:3] == ["bernstein", "run", str(tmp_path / "p.yaml")]
    assert "--auto-approve" in seen["cmd"]
    assert ["--port", "8099"] == seen["cmd"][-2:]
