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


def test_copies_global_template_to_repo_root_when_target_missing(tmp_path, monkeypatch):
    # Root, not .bernstein/ -- confirmed by a live `bernstein run` (see
    # bernstein_config.py's module docstring) that only the root location
    # works with the background orchestrator subprocess today.
    global_yaml = tmp_path / "global-bernstein.yaml"
    global_yaml.write_text("goal: test\ncli: claude\n")
    monkeypatch.setattr(bernstein_config, "GLOBAL_BERNSTEIN_YAML", global_yaml)

    workdir = tmp_path / "target-repo"
    workdir.mkdir()

    result = bernstein_config.ensure_bernstein_yaml(workdir)

    assert result == workdir / "bernstein.yaml"
    assert (workdir / "bernstein.yaml").read_text() == "goal: test\ncli: claude\n"
    assert not (workdir / ".bernstein").exists()


def test_never_overwrites_existing_dot_bernstein_file(tmp_path, monkeypatch):
    # A repo already using .bernstein/bernstein.yaml (e.g. this repo
    # itself) is left alone -- ensure_bernstein_yaml() never duplicates it
    # to root even though root is now the write target for FRESH bootstraps.
    global_yaml = tmp_path / "global-bernstein.yaml"
    global_yaml.write_text("goal: from-global\n")
    monkeypatch.setattr(bernstein_config, "GLOBAL_BERNSTEIN_YAML", global_yaml)

    workdir = tmp_path / "target-repo"
    (workdir / ".bernstein").mkdir(parents=True)
    (workdir / ".bernstein" / "bernstein.yaml").write_text("goal: repo-owns-this-already\n")

    result = bernstein_config.ensure_bernstein_yaml(workdir)

    assert result == workdir / ".bernstein" / "bernstein.yaml"
    assert (workdir / ".bernstein" / "bernstein.yaml").read_text() == "goal: repo-owns-this-already\n"
    assert not (workdir / "bernstein.yaml").exists()


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

    assert result == workdir / "bernstein.yaml"
    assert (workdir / "bernstein.yaml").read_text() == "goal: from-project-local\n"
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

    resolved = _resolved(dev="claude", review="claude", bugbot="claude", verify="claude")
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

    resolved = _resolved(dev="claude", review="claude", bugbot="kilo", verify="claude")
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
