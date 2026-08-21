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


def test_existing_dot_bernstein_content_is_regenerated_from_a_resolvable_template(tmp_path, monkeypatch):
    """`.bernstein/bernstein.yaml` is a DERIVED artifact: whenever a
    template resolves, the file is (re)written from it on every call, not
    only when bootstrapping a repo that has neither yet -- otherwise a
    role added to the template after a repo was first bootstrapped (the
    real incident this closes: `ocr_reviewer` added to the template long
    after a target repo's file was seeded) never reaches that repo, and
    Bernstein's task-create role allowlist silently stays stale."""
    global_yaml = tmp_path / "global-bernstein.yaml"
    global_yaml.write_text("goal: from-global\n")
    monkeypatch.setattr(bernstein_config, "GLOBAL_BERNSTEIN_YAML", global_yaml)

    workdir = tmp_path / "target-repo"
    (workdir / ".bernstein").mkdir(parents=True)
    (workdir / ".bernstein" / "bernstein.yaml").write_text("goal: stale-content\n")

    result = bernstein_config.ensure_bernstein_yaml(workdir)

    assert result == workdir / ".bernstein" / "bernstein.yaml"
    # regenerated from the (resolvable) template, not left as the stale content
    assert (workdir / ".bernstein" / "bernstein.yaml").read_text() == "goal: from-global\n"
    # ...and the link now exists, and is ignored so it is never committed
    assert (workdir / "bernstein.yaml").is_symlink()
    assert "bernstein.yaml" in (workdir / ".gitignore").read_text()


def test_dot_bernstein_content_is_left_alone_when_no_template_resolves(tmp_path, monkeypatch):
    """Nothing to regenerate FROM -- the pre-existing "leave it alone"
    behavior, now scoped to the case a template is genuinely unavailable
    (e.g. a hand-crafted file with no `bernstein-template.yaml` counterpart
    anywhere), rather than being the default for every repo."""
    monkeypatch.setattr(bernstein_config, "GLOBAL_BERNSTEIN_YAML", tmp_path / "nonexistent.yaml")

    workdir = tmp_path / "target-repo"
    (workdir / ".bernstein").mkdir(parents=True)
    (workdir / ".bernstein" / "bernstein.yaml").write_text("goal: repo-owns-this-already\n")

    result = bernstein_config.ensure_bernstein_yaml(workdir)

    assert result == workdir / ".bernstein" / "bernstein.yaml"
    assert (workdir / ".bernstein" / "bernstein.yaml").read_text() == "goal: repo-owns-this-already\n"
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


def test_a_role_added_to_the_template_after_bootstrap_reaches_an_already_materialized_repo(tmp_path, monkeypatch):
    """The exact incident this change closes: a target repo's
    `.bernstein/bernstein.yaml` was materialized before the template
    gained a new role, and never caught up on its own -- silently freezing
    Bernstein's task-create role allowlist at whatever existed the moment
    the repo was first bootstrapped."""
    global_yaml = tmp_path / "global-bernstein.yaml"
    global_yaml.write_text(
        "role_model_policy:\n"
        "  backend:\n"
        "    provider: claude\n"
    )
    monkeypatch.setattr(bernstein_config, "GLOBAL_BERNSTEIN_YAML", global_yaml)

    workdir = tmp_path / "target-repo"
    (workdir / ".bernstein").mkdir(parents=True)
    (workdir / ".bernstein" / "bernstein.yaml").write_text(
        "role_model_policy:\n"
        "  backend:\n"
        "    provider: claude\n"
    )

    # the template later gains a role the already-materialized repo never saw
    global_yaml.write_text(
        "role_model_policy:\n"
        "  backend:\n"
        "    provider: claude\n"
        "  ocr_reviewer:\n"
        "    provider: ocr\n"
    )

    bernstein_config.ensure_bernstein_yaml(workdir)

    assert "ocr_reviewer" in (workdir / ".bernstein" / "bernstein.yaml").read_text()


def test_a_real_legacy_root_file_is_never_superseded_by_a_resolvable_template(tmp_path, monkeypatch):
    """`find_seed_file()` checks `.bernstein/` first -- spawning a NEW
    `.bernstein/bernstein.yaml` next to an operator's own real root file
    would silently make Bernstein stop reading the file the operator
    actually maintains."""
    global_yaml = tmp_path / "global-bernstein.yaml"
    global_yaml.write_text("goal: from-global\n")
    monkeypatch.setattr(bernstein_config, "GLOBAL_BERNSTEIN_YAML", global_yaml)

    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "bernstein.yaml").write_text("goal: legacy-root\n")

    result = bernstein_config.ensure_bernstein_yaml(workdir)

    assert result == workdir / "bernstein.yaml"
    assert (workdir / "bernstein.yaml").read_text() == "goal: legacy-root\n"
    assert not (workdir / ".bernstein").exists()


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
    (workdir / ".bernstein" / "bernstein.yaml").write_text("goal: stale\n")
    (workdir / "bernstein.yaml").symlink_to("somewhere/else.yaml")

    bernstein_config.ensure_bernstein_yaml(workdir)

    from pathlib import Path as _P
    assert (workdir / "bernstein.yaml").readlink() == _P(".bernstein") / "bernstein.yaml"
    # a template resolves, so the target's content is also regenerated --
    # not just the link repaired
    assert (workdir / "bernstein.yaml").read_text() == "goal: test\n"
