from reasona_dev.adapters.omp import build_omp_command


def test_command_shape_matches_dispatch_md():
    cmd = build_omp_command(binary="omp", prompt="do the thing")
    assert cmd[:3] == ["omp", "-p", "--auto-approve"]
    assert cmd[-1] == "do the thing"


def test_no_resume_or_mode_json_flag_present():
    # This adapter never resumes a session (module docstring) -- every
    # dispatch is single-shot, unlike dev-ralf's own warmup+--resume design.
    cmd = build_omp_command(binary="omp", prompt="x", model="gpt-5", effort="high")
    assert "--resume" not in cmd
    assert "--mode" not in cmd
    assert "json" not in cmd


def test_default_model_sentinel_omitted():
    cmd = build_omp_command(binary="omp", prompt="x", model="default")
    assert "--model" not in cmd


def test_explicit_model_included():
    cmd = build_omp_command(binary="omp", prompt="x", model="gpt-5-omp")
    i = cmd.index("--model")
    assert cmd[i + 1] == "gpt-5-omp"


def test_effort_uses_thinking_flag_not_effort():
    # dispatch.md's own effort table: omp's flag is --thinking, not
    # claude's --effort or codex's -c model_reasoning_effort.
    cmd = build_omp_command(binary="omp", prompt="x", effort="xhigh")
    i = cmd.index("--thinking")
    assert cmd[i + 1] == "xhigh"


def test_effort_omitted_when_blank():
    cmd = build_omp_command(binary="omp", prompt="x", effort="")
    assert "--thinking" not in cmd


def test_prompt_is_always_the_final_argument():
    # omp takes the prompt as a positional, after every flag -- confirms
    # flags never get appended after it by accident.
    cmd = build_omp_command(binary="omp", prompt="my prompt", model="m", effort="high")
    assert cmd[-1] == "my prompt"
