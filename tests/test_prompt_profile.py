from reasona_dev.prompt_profile import available_profiles, resolve_profile_name, resolve_prompt


def test_default_profile_when_nothing_set():
    assert resolve_profile_name(env={}) == "generic"


def test_project_cfg_beats_global_cfg():
    name = resolve_profile_name(
        env={}, project_cfg={"dev-profile": "rust"}, global_cfg={"dev-profile": "python"}
    )
    assert name == "rust"


def test_global_cfg_used_when_project_silent():
    name = resolve_profile_name(env={}, project_cfg={}, global_cfg={"dev-profile": "python"})
    assert name == "python"


def test_env_var_beats_both_config_layers():
    name = resolve_profile_name(
        env={"REASONA_DEV_PROFILE": "go"},
        project_cfg={"dev-profile": "rust"},
        global_cfg={"dev-profile": "python"},
    )
    assert name == "go"


def test_flag_beats_everything():
    name = resolve_profile_name(
        flag="ruby",
        env={"REASONA_DEV_PROFILE": "go"},
        project_cfg={"dev-profile": "rust"},
        global_cfg={"dev-profile": "python"},
    )
    assert name == "ruby"


def test_this_repos_own_committed_profile_is_readable(tmp_path, generic_prompts):
    """`.reasona/prompts/generic/` is a real, committed profile now -- not a
    copy inside the installed package."""
    text = resolve_prompt("review", profile="generic", workdir=tmp_path)
    assert text is not None
    assert "MUST_FIX:" in text
    assert "VERDICT: PASS or VERDICT: FAIL" in text


def test_project_local_beats_global(tmp_path, monkeypatch):
    fake_home = tmp_path / "fake-home"
    (fake_home / ".reasona" / "prompts" / "generic").mkdir(parents=True)
    (fake_home / ".reasona" / "prompts" / "generic" / "review.md").write_text("global\n")
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    workdir = tmp_path / "target-repo"
    prompt_dir = workdir / ".reasona" / "prompts" / "generic"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "review.md").write_text("project-local override\n")

    assert resolve_prompt("review", profile="generic", workdir=workdir) == "project-local override\n"


def test_global_used_when_project_has_no_file(tmp_path, monkeypatch):
    fake_home = tmp_path / "fake-home"
    (fake_home / ".reasona" / "prompts" / "rust").mkdir(parents=True)
    (fake_home / ".reasona" / "prompts" / "rust" / "bugbot.md").write_text("global rust bugbot\n")
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    workdir = tmp_path / "target-repo"
    workdir.mkdir()

    assert resolve_prompt("bugbot", profile="rust", workdir=workdir) == "global rust bugbot\n"


def test_per_role_shadowing_not_whole_profile_shadowing(tmp_path, monkeypatch):
    """A project overriding review.md must still inherit the global
    bugbot.md -- precedence is per FILE, not per profile directory."""
    fake_home = tmp_path / "fake-home"
    g = fake_home / ".reasona" / "prompts" / "generic"
    g.mkdir(parents=True)
    (g / "review.md").write_text("global review\n")
    (g / "bugbot.md").write_text("global bugbot\n")
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    workdir = tmp_path / "target-repo"
    local = workdir / ".reasona" / "prompts" / "generic"
    local.mkdir(parents=True)
    (local / "review.md").write_text("local review\n")

    assert resolve_prompt("review", profile="generic", workdir=workdir) == "local review\n"
    assert resolve_prompt("bugbot", profile="generic", workdir=workdir) == "global bugbot\n"


def test_no_layer_present_returns_none_rather_than_a_packaged_default(tmp_path, monkeypatch):
    """Regression guard for the layer that was removed: a repo that has
    chosen no prompts must get None, not one shipped inside site-packages
    that the operator cannot see or edit."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "empty-home")
    workdir = tmp_path / "bare-repo"
    workdir.mkdir()
    for role in ("review", "recheck", "bugbot", "compliance", "final_audit"):
        assert resolve_prompt(role, profile="generic", workdir=workdir) is None


def test_available_profiles_merges_both_layers(tmp_path, monkeypatch):
    fake_home = tmp_path / "fake-home"
    g = fake_home / ".reasona" / "prompts" / "generic"
    g.mkdir(parents=True)
    (g / "review.md").write_text("x\n")
    (g / "bugbot.md").write_text("x\n")
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    workdir = tmp_path / "target-repo"
    local = workdir / ".reasona" / "prompts" / "rust"
    local.mkdir(parents=True)
    (local / "review.md").write_text("x\n")

    found = available_profiles(workdir)
    assert found == {"generic": ["bugbot", "review"], "rust": ["review"]}


def test_available_profiles_is_empty_when_neither_layer_exists(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "empty-home")
    workdir = tmp_path / "bare"
    workdir.mkdir()
    assert available_profiles(workdir) == {}


def test_resolve_prompt_unknown_role_and_profile_returns_none(tmp_path):
    workdir = tmp_path / "target-repo"
    workdir.mkdir()
    assert resolve_prompt("nonexistent-role", profile="nonexistent-profile", workdir=workdir) is None


def test_resolve_prompt_never_falls_back_to_a_different_profile(tmp_path):
    # An operator naming a profile that doesn't exist must not silently
    # slide back to "generic" -- that would be a silent substitution.
    workdir = tmp_path / "target-repo"
    workdir.mkdir()
    assert resolve_prompt("review", profile="nonexistent-profile", workdir=workdir) is None
