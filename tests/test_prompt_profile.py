from reasona_dev.prompt_profile import resolve_profile_name, resolve_prompt


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


def test_resolve_prompt_reads_packaged_generic_review():
    text = resolve_prompt("review", profile="generic")
    assert text is not None
    assert "MUST_FIX:" in text
    assert "VERDICT: PASS or VERDICT: FAIL" in text


def test_resolve_prompt_project_local_beats_packaged(tmp_path):
    workdir = tmp_path / "target-repo"
    prompt_dir = workdir / ".reasona" / "prompts" / "generic"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "review.md").write_text("project-local override\n")

    text = resolve_prompt("review", profile="generic", workdir=workdir)
    assert text == "project-local override\n"


def test_resolve_prompt_global_beats_packaged_but_not_project_local(tmp_path, monkeypatch):
    fake_home = tmp_path / "fake-home"
    (fake_home / ".reasona" / "prompts" / "rust").mkdir(parents=True)
    (fake_home / ".reasona" / "prompts" / "rust" / "bugbot.md").write_text("global rust bugbot\n")
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    workdir = tmp_path / "target-repo"
    workdir.mkdir()

    text = resolve_prompt("bugbot", profile="rust", workdir=workdir)
    assert text == "global rust bugbot\n"


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
