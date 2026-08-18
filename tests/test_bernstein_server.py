import json
from pathlib import Path

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
    return ServerHandle(process=None, base_url="http://127.0.0.1:8052", token="test-token")


def test_dispatch_task_posts_role_model_effort_and_completion_signal(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data)
        return _FakeResponse({"id": "task-123"})

    monkeypatch.setattr(bernstein_server.urllib.request, "urlopen", fake_urlopen)

    task_id = dispatch_task(
        _handle(), role="reviewer", title="t", description="d",
        model="opus", effort="high", cli="claude",
        raw_output_path=Path("/tmp/x/reviewer-c1.raw.txt"),
    )

    assert task_id == "task-123"
    assert captured["url"] == "http://127.0.0.1:8052/tasks"
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["body"]["role"] == "reviewer"
    assert captured["body"]["model"] == "opus"
    assert captured["body"]["effort"] == "high"
    assert captured["body"]["cli"] == "claude"
    assert captured["body"]["completion_signals"] == [
        {"type": "test_passes", "value": "test -s /tmp/x/reviewer-c1.raw.txt"}
    ]


def test_poll_task_loops_until_terminal_status(monkeypatch):
    responses = [
        {"id": "t1", "status": "open"},
        {"id": "t1", "status": "claimed"},
        {"id": "t1", "status": "done", "result_summary": "ok"},
    ]
    sleeps = []
    monkeypatch.setattr(bernstein_server.time, "sleep", lambda s: sleeps.append(s))

    def fake_urlopen(req, timeout=30):
        return _FakeResponse(responses.pop(0))

    monkeypatch.setattr(bernstein_server.urllib.request, "urlopen", fake_urlopen)

    task = poll_task(_handle(), "t1", poll_interval=0.01)

    assert task["status"] == "done"
    assert len(sleeps) == 2  # slept between the two non-terminal polls


def test_poll_task_stops_on_failed_without_extra_polling(monkeypatch):
    def fake_urlopen(req, timeout=30):
        return _FakeResponse({"id": "t1", "status": "failed"})

    monkeypatch.setattr(bernstein_server.urllib.request, "urlopen", fake_urlopen)

    task = poll_task(_handle(), "t1", poll_interval=0.01)

    assert task["status"] == "failed"
