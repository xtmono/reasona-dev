import subprocess

from reasona_dev import worktree


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "a.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo, check=True,
    )
    return repo


def test_unit_worktree_path_is_namespaced_by_plan_then_stage(tmp_path):
    assert worktree.unit_worktree_path(tmp_path, "my-plan", "pr-3") == (
        tmp_path / ".worktrees" / "my-plan" / "pr-3"
    )


def test_unit_branch_name_is_namespaced_the_same_way():
    assert worktree.unit_branch_name("my-plan", "pr-3") == "reasona/my-plan/pr-3"


def test_ensure_unit_worktree_creates_a_real_checkout(tmp_path):
    repo = _repo(tmp_path)
    path, branch = worktree.ensure_unit_worktree(repo, "plan", "pr-1", base="main")

    assert path == worktree.unit_worktree_path(repo, "plan", "pr-1")
    assert (path / "a.txt").is_file()
    assert branch == "reasona/plan/pr-1"

    out = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == branch


def test_ensure_unit_worktree_is_isolated_from_a_sibling_units_worktree(tmp_path):
    """The bug this whole module exists to fix: two units must never share
    a checkout, so a commit made in one never appears in the other."""
    repo = _repo(tmp_path)
    path1, _ = worktree.ensure_unit_worktree(repo, "plan", "pr-1", base="main")
    (path1 / "unit1.txt").write_text("only in unit 1\n")
    subprocess.run(["git", "add", "-A"], cwd=path1, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "unit 1 work"],
        cwd=path1, check=True,
    )

    path2, _ = worktree.ensure_unit_worktree(repo, "plan", "pr-2", base="main")
    assert not (path2 / "unit1.txt").exists()


def test_ensure_unit_worktree_reuses_an_existing_worktree_on_resume(tmp_path):
    repo = _repo(tmp_path)
    path, branch = worktree.ensure_unit_worktree(repo, "plan", "pr-1", base="main")
    (path / "mid-run.txt").write_text("work in progress\n")

    path2, branch2 = worktree.ensure_unit_worktree(repo, "plan", "pr-1", base="main")
    assert path2 == path and branch2 == branch
    assert (path2 / "mid-run.txt").is_file()  # not recreated -- in-progress work survives


def test_ensure_unit_worktree_copies_the_project_local_reasona_config(tmp_path):
    """The incident this closes: `bernstein_config.ensure_bernstein_yaml()`/
    `config_file.load_project()`/`prompt_profile.resolve_prompt()` all
    resolve relative to `workdir=<worktree>`, and `.reasona/` is gitignored
    everywhere, so a plain `git worktree add` never carries the
    project-local template/config/prompts across on its own."""
    repo = _repo(tmp_path)
    (repo / ".reasona" / "prompts" / "rust-dev").mkdir(parents=True)
    (repo / ".reasona" / "bernstein-template.yaml").write_text("role_model_policy:\n  backend:\n    provider: claude\n")
    (repo / ".reasona" / "reasona.yaml").write_text("dev-models:\n  dev: claude:sonnet:high\n")
    (repo / ".reasona" / "prompts" / "rust-dev" / "review.md").write_text("review this\n")

    path, _ = worktree.ensure_unit_worktree(repo, "plan", "pr-1", base="main")

    assert (path / ".reasona" / "bernstein-template.yaml").read_text() == "role_model_policy:\n  backend:\n    provider: claude\n"
    assert (path / ".reasona" / "reasona.yaml").read_text() == "dev-models:\n  dev: claude:sonnet:high\n"
    assert (path / ".reasona" / "prompts" / "rust-dev" / "review.md").read_text() == "review this\n"


def test_ensure_unit_worktree_skips_reasona_config_missing_at_the_source(tmp_path):
    """An operator relying purely on the global `~/.reasona/` layer has
    nothing project-local to copy -- that is a supported configuration,
    not something to fail or warn about."""
    repo = _repo(tmp_path)

    path, _ = worktree.ensure_unit_worktree(repo, "plan", "pr-1", base="main")

    assert not (path / ".reasona").exists()


def test_ensure_unit_worktree_resyncs_reasona_config_on_resume_too(tmp_path):
    """A resumed run against an already-existing worktree must pick up
    whatever the top-level repo's config currently is, not a stale copy
    from when the worktree was first created."""
    repo = _repo(tmp_path)
    (repo / ".reasona").mkdir()
    (repo / ".reasona" / "bernstein-template.yaml").write_text("goal: v1\n")

    path, _ = worktree.ensure_unit_worktree(repo, "plan", "pr-1", base="main")
    assert (path / ".reasona" / "bernstein-template.yaml").read_text() == "goal: v1\n"

    (repo / ".reasona" / "bernstein-template.yaml").write_text("goal: v2\n")
    path2, _ = worktree.ensure_unit_worktree(repo, "plan", "pr-1", base="main")

    assert path2 == path
    assert (path / ".reasona" / "bernstein-template.yaml").read_text() == "goal: v2\n"


def test_remove_unit_worktree_deletes_the_checkout_and_its_current_branch(tmp_path):
    repo = _repo(tmp_path)
    path, branch = worktree.ensure_unit_worktree(repo, "plan", "pr-1", base="main")
    assert path.is_dir()

    worktree.remove_unit_worktree(repo, "plan", "pr-1")

    assert not path.is_dir()
    branches = subprocess.run(
        ["git", "branch", "--list", branch], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert branches.strip() == ""


def test_remove_unit_worktree_follows_a_rename_before_deleting(tmp_path):
    """`gh_pr.py` renames the unit branch to `issue/<N>-<slug>` before this
    is ever called -- cleanup must delete whatever the branch is named NOW,
    not the original `reasona/<plan>/<stage>` name."""
    repo = _repo(tmp_path)
    path, original_branch = worktree.ensure_unit_worktree(repo, "plan", "pr-1", base="main")
    subprocess.run(["git", "branch", "-m", "issue/42-do-the-thing"], cwd=path, check=True)

    worktree.remove_unit_worktree(repo, "plan", "pr-1")

    assert not path.is_dir()
    for name in (original_branch, "issue/42-do-the-thing"):
        branches = subprocess.run(
            ["git", "branch", "--list", name], cwd=repo, capture_output=True, text=True, check=True,
        ).stdout
        assert branches.strip() == "", f"{name} should have been deleted"


def test_remove_unit_worktree_on_a_never_created_worktree_does_not_raise(tmp_path):
    repo = _repo(tmp_path)
    worktree.remove_unit_worktree(repo, "plan", "pr-1")  # nothing to remove -- must not raise


def test_remove_unit_worktree_reaps_leftover_processes_before_removal(tmp_path, monkeypatch):
    """B-6-3: worker.md's post-merge cleanup kills anything still running
    inside the worktree BEFORE `git worktree remove`, needed once a local
    CI command (`ci_gate.py`) can leave a build/test child process
    behind. The pattern must carry a trailing `/` -- a bare path also
    matches a sibling worktree that shares it as a prefix."""
    repo = _repo(tmp_path)
    path, _branch = worktree.ensure_unit_worktree(repo, "plan", "pr-1", base="main")

    calls = []
    from reasona_dev import _shell

    orig_run = _shell.run

    def spy(cmd, workdir, *, timeout=300):
        calls.append(cmd)
        return orig_run(cmd, workdir, timeout=timeout)

    monkeypatch.setattr(worktree._shell, "run", spy)
    worktree.remove_unit_worktree(repo, "plan", "pr-1")

    pkill_calls = [c for c in calls if c[0] == "pkill"]
    assert len(pkill_calls) == 1
    assert pkill_calls[0] == ["pkill", "-9", "-f", f"{path}/"]
    # pkill happened BEFORE the worktree removal, not after
    remove_idx = next(i for i, c in enumerate(calls) if c[:2] == ["git", "worktree"])
    pkill_idx = next(i for i, c in enumerate(calls) if c[0] == "pkill")
    assert pkill_idx < remove_idx
