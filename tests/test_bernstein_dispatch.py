import subprocess
from pathlib import Path

import yaml

from reasona_dev import bernstein_dispatch
from reasona_dev.bernstein_dispatch import (
    DEFAULT_ROLE_SCOPE,
    DispatchResult,
    role_dispatch_timeout,
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


def test_one_step_plan_carries_model_effort_and_scope(tmp_path):
    step = _plan(tmp_path)["stages"][0]["steps"][0]
    assert step["model"] == "opus"
    assert step["effort"] == "high"
    assert step["scope"] == DEFAULT_ROLE_SCOPE


def test_scope_is_how_the_turn_budget_travels(tmp_path):
    """`Task.max_turns` reaches `--max-turns` directly but is settable only
    over HTTP. `complexity` looks like the substitute and is NOT --
    `compute_max_turns()` has no production call site in Bernstein, verified
    live when `complexity: high` still died at 23 turns. What the claude
    adapter actually computes is
    `effort_base_turns[effort] * scope_multipliers[scope]`."""
    assert _plan(tmp_path, scope="small")["stages"][0]["steps"][0]["scope"] == "small"


def test_files_becomes_the_steps_files_field_for_bernstein_owned_files(tmp_path):
    step = _plan(tmp_path, files=["src/a.rs", "src/b.rs"])["stages"][0]["steps"][0]
    assert step["files"] == ["src/a.rs", "src/b.rs"]


def test_no_files_key_at_all_when_files_is_omitted_or_empty(tmp_path):
    assert "files" not in _plan(tmp_path)["stages"][0]["steps"][0]
    assert "files" not in _plan(tmp_path, files=[])["stages"][0]["steps"][0]


def test_role_dispatch_timeout_matches_dev_ralfs_own_split():
    """dispatch.md: "dev 3600s (60 min ...); all other roles (review/
    recheck/bugbot/compliance/final_audit) 900s (15 min)". reasona-dev's
    dev/fix dispatches go out under the Bernstein role string "backend"."""
    assert role_dispatch_timeout("backend") == 3600
    for role in ("reviewer", "ocr_reviewer", "bugbot", "compliance", "final_audit"):
        assert role_dispatch_timeout(role) == 900


def test_the_default_scope_is_the_widest_multiplier_available():
    """`large` doubles the effort-derived base -- the widest the adapter
    offers without a per-task override this path cannot set. A step with no
    scope gets the 1.5 default, which is what produced the 22-turn budget the
    live reviewer died inside."""
    from bernstein.core.defaults import COST

    assert DEFAULT_ROLE_SCOPE == "large"
    assert COST.scope_multipliers[DEFAULT_ROLE_SCOPE] == max(COST.scope_multipliers.values())


def test_turn_budget_follows_the_roles_configured_effort():
    """Turn budget and reasoning effort are both "how much work is this role
    allowed to do", so the coupling is intended: a role configured cheap gets
    fewer turns. What broke live was the missing scope multiplier, not this."""
    from bernstein.core.defaults import COST

    budget = {
        e: int(b * COST.scope_multipliers[DEFAULT_ROLE_SCOPE])
        for e, b in COST.effort_base_turns.items()
    }
    assert budget["low"] < budget["high"] < budget["max"]
    # the live death was at 23 turns; every effort tier now clears it except
    # the cheapest, which is a configuration choice rather than a defect
    assert budget["high"] >= 100


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

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bernstein_dispatch.subprocess, "run", fake_run)
    run_plan_file(tmp_path / "p.yaml", tmp_path, port=8099)

    seen["cmd"] = calls[0]
    assert calls[0][:3] == ["bernstein", "run", str(tmp_path / "p.yaml")]
    assert "--auto-approve" in calls[0]
    # --merge direct, not the default "pr" -- otherwise --auto-approve alone
    # still activates ApprovalGate(mode=PR): a throwaway bernstein/task-<id>
    # branch pushed to origin and a real GitHub PR opened, on top of the
    # direct merge, that nothing ever reads or cleans up.
    idx = calls[0].index("--merge")
    assert calls[0][idx + 1] == "direct"
    assert ["--port", "8099"] == calls[0][-2:]
    # ...and the leftovers are reaped before the next dispatch can collide
    assert calls[1][:2] == ["bernstein", "stop"]


def test_leftovers_are_reaped_even_when_the_run_fails(tmp_path, monkeypatch):
    """A timed-out or crashed run leaves the same detached server and
    watchdog behind as a clean one, and the NEXT dispatch is what breaks if
    they survive -- observed live as a 401 on port 8052 from a previous run's
    server holding a different token."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == "run":
            raise subprocess.TimeoutExpired(cmd="bernstein", timeout=1)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bernstein_dispatch.subprocess, "run", fake_run)
    r = run_plan_file(tmp_path / "p.yaml", tmp_path, timeout=1)

    assert not r.ok
    assert [c[1] for c in calls] == ["run", "stop"]
