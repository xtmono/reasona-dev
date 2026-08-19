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
