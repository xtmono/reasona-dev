import subprocess

from reasona_dev import ci_gate


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "a.txt").write_text("v1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo, check=True,
    )
    return repo


def _commit(repo, content):
    (repo / "a.txt").write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "fix"],
        cwd=repo, check=True,
    )


def _head(repo):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_default_timeouts_match_the_dev_ralf_parity_agreement():
    """docs/ARCHITECTURE.md §3.22 -- fast/full CI gate timeouts agreed with
    dev-ralf's own Bash-timeout guidance during a timeout-parity survey."""
    assert ci_gate.DEFAULT_FAST_TIMEOUT == 300
    assert ci_gate.DEFAULT_FULL_TIMEOUT == 1200


def test_run_fast_is_a_noop_when_unconfigured(tmp_path):
    repo = _repo(tmp_path)
    ok, tail = ci_gate.run_fast(repo, None, pre_fix_head=_head(repo))
    assert ok is True and tail == ""


def test_run_fast_passes_through_on_success(tmp_path):
    repo = _repo(tmp_path)
    pre = _head(repo)
    ok, _tail = ci_gate.run_fast(repo, "true", pre_fix_head=pre)
    assert ok is True


def test_run_fast_reverts_to_pre_fix_head_on_failure(tmp_path):
    repo = _repo(tmp_path)
    pre = _head(repo)
    _commit(repo, "broken\n")
    assert _head(repo) != pre

    ok, tail = ci_gate.run_fast(repo, "exit 1", pre_fix_head=pre)
    assert ok is False
    assert _head(repo) == pre  # reverted
    assert (repo / "a.txt").read_text() == "v1\n"


def test_run_fast_failure_output_is_captured(tmp_path):
    repo = _repo(tmp_path)
    pre = _head(repo)
    ok, tail = ci_gate.run_fast(repo, "echo compile-error-here && exit 1", pre_fix_head=pre)
    assert ok is False
    assert "compile-error-here" in tail


def test_run_fast_with_no_pre_fix_head_does_not_attempt_a_revert(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "broken\n")
    head_before = _head(repo)
    ok, _tail = ci_gate.run_fast(repo, "exit 1", pre_fix_head=None)
    assert ok is False
    assert _head(repo) == head_before  # nothing reverted -- no pre_fix_head given


def test_run_full_is_a_noop_when_unconfigured(tmp_path):
    repo = _repo(tmp_path)
    ok, tail = ci_gate.run_full(repo, None)
    assert ok is True and tail == ""


def test_run_full_reports_failure_without_reverting_anything(tmp_path):
    repo = _repo(tmp_path)
    pre = _head(repo)
    _commit(repo, "still here\n")
    after = _head(repo)

    ok, _tail = ci_gate.run_full(repo, "exit 1")
    assert ok is False
    assert _head(repo) == after  # run_full never reverts -- see its own docstring
    assert _head(repo) != pre
