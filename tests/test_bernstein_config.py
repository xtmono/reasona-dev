from reasona_dev import bernstein_config
from reasona_dev.model_config import ResolvedModel

_SAMPLE_TEMPLATE = """\
# top-of-file comment, must survive
goal: test
cli: claude

role_model_policy:
  backend:
    provider: claude
  reviewer:
    provider: claude
  bugbot:
    # inline comment above a provider line must also survive
    provider: kilo
  compliance:
    provider: claude

approval: review
"""


def _resolved(**adapters: str) -> dict[str, ResolvedModel]:
    return {
        role: ResolvedModel(role=role, model="whatever", adapter=adapter, effort="high", source="test")
        for role, adapter in adapters.items()
    }


def test_bootstraps_dot_bernstein_and_symlinks_root(tmp_path, monkeypatch):
    # The real file lands at .bernstein/ (what find_seed_file() prefers);
    # root is a relative symlink to it, satisfying the background
    # orchestrator subprocess's hardcoded root-only fallback too (see
    # bernstein_config.py's module docstring).
    global_yaml = tmp_path / "global-bernstein.yaml"
    global_yaml.write_text("goal: test\ncli: claude\n")
    monkeypatch.setattr(bernstein_config, "GLOBAL_BERNSTEIN_YAML", global_yaml)

    workdir = tmp_path / "target-repo"
    workdir.mkdir()

    result = bernstein_config.ensure_bernstein_yaml(workdir)

    assert result == workdir / ".bernstein" / "bernstein.yaml"
    assert (workdir / ".bernstein" / "bernstein.yaml").read_text() == "goal: test\ncli: claude\n"
    assert (workdir / "bernstein.yaml").is_symlink()
    assert (workdir / "bernstein.yaml").read_text() == "goal: test\ncli: claude\n"
    assert not (workdir / "bernstein.yaml").readlink().is_absolute()


def test_existing_dot_bernstein_content_is_untouched_but_root_link_is_ensured(tmp_path, monkeypatch):
    """The root link is ensured on EVERY call, not only when bootstrapping.

    It is untracked (committing it breaks agent worktree creation), so a
    fresh clone has `.bernstein/` and no link -- and without the link the
    orchestrator re-derives a root-only path, finds nothing, and FATALs.
    An early return here would leave every cloned repo one FATAL from its
    first run.
    """
    global_yaml = tmp_path / "global-bernstein.yaml"
    global_yaml.write_text("goal: from-global\n")
    monkeypatch.setattr(bernstein_config, "GLOBAL_BERNSTEIN_YAML", global_yaml)

    workdir = tmp_path / "target-repo"
    (workdir / ".bernstein").mkdir(parents=True)
    (workdir / ".bernstein" / "bernstein.yaml").write_text("goal: repo-owns-this-already\n")

    result = bernstein_config.ensure_bernstein_yaml(workdir)

    assert result == workdir / ".bernstein" / "bernstein.yaml"
    # content never rewritten
    assert (workdir / ".bernstein" / "bernstein.yaml").read_text() == "goal: repo-owns-this-already\n"
    # ...but the link now exists, and is ignored so it is never committed
    assert (workdir / "bernstein.yaml").is_symlink()
    assert "bernstein.yaml" in (workdir / ".gitignore").read_text()


def test_never_overwrites_existing_root_file(tmp_path, monkeypatch):
    global_yaml = tmp_path / "global-bernstein.yaml"
    global_yaml.write_text("goal: from-global\n")
    monkeypatch.setattr(bernstein_config, "GLOBAL_BERNSTEIN_YAML", global_yaml)

    workdir = tmp_path / "target-repo"
    workdir.mkdir()
    (workdir / "bernstein.yaml").write_text("goal: repo-owns-this-already\n")

    result = bernstein_config.ensure_bernstein_yaml(workdir)

    assert result == workdir / "bernstein.yaml"
    assert (workdir / "bernstein.yaml").read_text() == "goal: repo-owns-this-already\n"
    assert not (workdir / ".bernstein").exists()


def test_project_local_template_beats_global_template(tmp_path, monkeypatch):
    global_yaml = tmp_path / "global-bernstein.yaml"
    global_yaml.write_text("goal: from-global\n")
    monkeypatch.setattr(bernstein_config, "GLOBAL_BERNSTEIN_YAML", global_yaml)

    workdir = tmp_path / "target-repo"
    (workdir / ".reasona").mkdir(parents=True)
    (workdir / ".reasona" / "bernstein-template.yaml").write_text("goal: from-project-local\n")

    result = bernstein_config.ensure_bernstein_yaml(workdir)

    assert result == workdir / ".bernstein" / "bernstein.yaml"
    assert (workdir / ".bernstein" / "bernstein.yaml").read_text() == "goal: from-project-local\n"
    assert (workdir / "bernstein.yaml").is_symlink()


def test_returns_none_when_nothing_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(bernstein_config, "GLOBAL_BERNSTEIN_YAML", tmp_path / "nonexistent.yaml")

    workdir = tmp_path / "target-repo"
    workdir.mkdir()

    assert bernstein_config.ensure_bernstein_yaml(workdir) is None
    assert not (workdir / ".bernstein" / "bernstein.yaml").exists()
    assert not (workdir / "bernstein.yaml").exists()


def test_sync_patches_only_changed_providers_and_preserves_comments(tmp_path):
    yaml_path = tmp_path / "bernstein.yaml"
    yaml_path.write_text(_SAMPLE_TEMPLATE)

    resolved = _resolved(dev="claude", review="claude", bugbot="claude", compliance="claude")
    changed = bernstein_config.sync_role_model_policy(yaml_path, resolved)

    assert changed is True
    text = yaml_path.read_text()
    assert "# top-of-file comment, must survive" in text
    assert "# inline comment above a provider line must also survive" in text
    assert "bugbot:\n    # inline comment above a provider line must also survive\n    provider: claude" in text
    # every other role was already "claude" and must be untouched
    assert "backend:\n    provider: claude" in text
    assert "reviewer:\n    provider: claude" in text
    assert "compliance:\n    provider: claude" in text


def test_sync_is_idempotent_when_nothing_differs(tmp_path):
    yaml_path = tmp_path / "bernstein.yaml"
    yaml_path.write_text(_SAMPLE_TEMPLATE)

    resolved = _resolved(dev="claude", review="claude", bugbot="kilo", compliance="claude")
    changed = bernstein_config.sync_role_model_policy(yaml_path, resolved)

    assert changed is False
    assert yaml_path.read_text() == _SAMPLE_TEMPLATE


def test_sync_never_adds_a_role_not_already_in_the_file(tmp_path):
    yaml_path = tmp_path / "bernstein.yaml"
    yaml_path.write_text("goal: no role_model_policy block at all\n")

    resolved = _resolved(dev="codex")
    changed = bernstein_config.sync_role_model_policy(yaml_path, resolved)

    assert changed is False
    assert yaml_path.read_text() == "goal: no role_model_policy block at all\n"


def test_sync_missing_file_is_a_safe_noop(tmp_path):
    assert bernstein_config.sync_role_model_policy(tmp_path / "nonexistent.yaml", _resolved(dev="claude")) is False


def test_root_link_is_gitignored_because_committing_it_breaks_worktrees(tmp_path, monkeypatch):
    """Live-verified: a COMMITTED symlink is materialized into every agent
    worktree, where Bernstein's isolation check rejects it and no agent can
    spawn at all."""
    global_yaml = tmp_path / "global-bernstein.yaml"
    global_yaml.write_text("goal: test\n")
    monkeypatch.setattr(bernstein_config, "GLOBAL_BERNSTEIN_YAML", global_yaml)

    workdir = tmp_path / "repo"
    workdir.mkdir()
    bernstein_config.ensure_bernstein_yaml(workdir)

    ignore = (workdir / ".gitignore").read_text()
    assert any(line.strip() == "bernstein.yaml" for line in ignore.splitlines())


def test_gitignore_entry_is_not_duplicated_across_calls(tmp_path, monkeypatch):
    global_yaml = tmp_path / "global-bernstein.yaml"
    global_yaml.write_text("goal: test\n")
    monkeypatch.setattr(bernstein_config, "GLOBAL_BERNSTEIN_YAML", global_yaml)

    workdir = tmp_path / "repo"
    workdir.mkdir()
    for _ in range(3):
        bernstein_config.ensure_bernstein_yaml(workdir)

    ignore = (workdir / ".gitignore").read_text()
    assert [l.strip() for l in ignore.splitlines()].count("bernstein.yaml") == 1


def test_existing_real_root_file_is_left_completely_alone(tmp_path, monkeypatch):
    """A repo predating this convention already satisfies the orchestrator;
    converting its real file to a symlink would rewrite something this
    module did not create."""
    global_yaml = tmp_path / "global-bernstein.yaml"
    global_yaml.write_text("goal: from-global\n")
    monkeypatch.setattr(bernstein_config, "GLOBAL_BERNSTEIN_YAML", global_yaml)

    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "bernstein.yaml").write_text("goal: legacy-root\n")

    result = bernstein_config.ensure_bernstein_yaml(workdir)

    assert result == workdir / "bernstein.yaml"
    assert not (workdir / "bernstein.yaml").is_symlink()
    assert (workdir / "bernstein.yaml").read_text() == "goal: legacy-root\n"


def test_wrongly_aimed_link_is_repaired(tmp_path, monkeypatch):
    global_yaml = tmp_path / "global-bernstein.yaml"
    global_yaml.write_text("goal: test\n")
    monkeypatch.setattr(bernstein_config, "GLOBAL_BERNSTEIN_YAML", global_yaml)

    workdir = tmp_path / "repo"
    (workdir / ".bernstein").mkdir(parents=True)
    (workdir / ".bernstein" / "bernstein.yaml").write_text("goal: real\n")
    (workdir / "bernstein.yaml").symlink_to("somewhere/else.yaml")

    bernstein_config.ensure_bernstein_yaml(workdir)

    from pathlib import Path as _P
    assert (workdir / "bernstein.yaml").readlink() == _P(".bernstein") / "bernstein.yaml"
    assert (workdir / "bernstein.yaml").read_text() == "goal: real\n"
