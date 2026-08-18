import json

import pytest

from reasona_dev import bernstein_server
from reasona_dev.bernstein_server import ServerHandle, dispatch_task, poll_task


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status = 200

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _handle():
    return ServerHandle(process=None, base_url="http://127.0.0.1:8052", token="t")


def test_pending_approval_is_not_treated_as_terminal(monkeypatch):
    """Regression: `pending_approval` used to be absent from both the
    terminal set and any special handling, so an approval-gated task
    polled until the ordinary timeout and failed as if the agent hung."""
    statuses = ["in_progress", "pending_approval", "pending_approval", "done"]
    monkeypatch.setattr(bernstein_server.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        bernstein_server.urllib.request, "urlopen",
        lambda req, timeout=30: _FakeResponse({"id": "t1", "status": statuses.pop(0)}),
    )

    notified = []
    task = poll_task(
        _handle(), "t1", poll_interval=0.0,
        on_awaiting_approval=lambda t: notified.append(t["status"]),
    )

    assert task["status"] == "done"
    # announced exactly once, no matter how many polls it stays parked
    assert notified == ["pending_approval"]


def test_approval_wait_uses_the_long_deadline_not_the_agent_timeout(monkeypatch):
    """A person taking longer than the agent timeout must not read as a
    stuck agent."""
    clock = {"t": 0.0}
    monkeypatch.setattr(bernstein_server.time, "monotonic", lambda: clock["t"])

    def _sleep(_):
        clock["t"] += 100.0

    monkeypatch.setattr(bernstein_server.time, "sleep", _sleep)

    statuses = ["pending_approval"] * 5 + ["done"]
    monkeypatch.setattr(
        bernstein_server.urllib.request, "urlopen",
        lambda req, timeout=30: _FakeResponse({"id": "t1", "status": statuses.pop(0)}),
    )

    # agent timeout is 50s and the human takes 500s -- still succeeds
    task = poll_task(_handle(), "t1", poll_interval=0.0, timeout=50.0, approval_timeout=10_000.0)
    assert task["status"] == "done"


def test_ordinary_timeout_still_fires_for_a_stuck_agent(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(bernstein_server.time, "monotonic", lambda: clock["t"])

    def _sleep(_):
        clock["t"] += 100.0

    monkeypatch.setattr(bernstein_server.time, "sleep", _sleep)
    monkeypatch.setattr(
        bernstein_server.urllib.request, "urlopen",
        lambda req, timeout=30: _FakeResponse({"id": "t1", "status": "in_progress"}),
    )

    with pytest.raises(TimeoutError, match="terminal status"):
        poll_task(_handle(), "t1", poll_interval=0.0, timeout=50.0)


def test_max_turns_reaches_the_task_body(monkeypatch):
    """The review prompt writes its report as its LAST action, so a turn
    budget that runs out during exploration loses the whole result rather
    than truncating it. Observed live: `error_max_turns turns=23` with the
    analysis done and nothing written."""
    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["body"] = json.loads(req.data)
        return _FakeResponse({"id": "t1"})

    monkeypatch.setattr(bernstein_server.urllib.request, "urlopen", fake_urlopen)
    dispatch_task(
        _handle(), role="reviewer", title="t", description="d", model="haiku",
        effort="low", cli="claude", raw_output_path="/tmp/x", max_turns=60,
    )
    assert captured["body"]["max_turns"] == 60


def test_max_turns_is_omitted_when_unset(monkeypatch):
    """Left unset, Bernstein/the adapter keeps its own default."""
    captured = {}
    monkeypatch.setattr(
        bernstein_server.urllib.request, "urlopen",
        lambda req, timeout=30: (captured.__setitem__("body", json.loads(req.data)),
                                 _FakeResponse({"id": "t1"}))[1],
    )
    dispatch_task(
        _handle(), role="reviewer", title="t", description="d", model="haiku",
        effort="low", cli="claude", raw_output_path="/tmp/x",
    )
    assert "max_turns" not in captured["body"]
