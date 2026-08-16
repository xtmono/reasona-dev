from pathlib import Path

from reasona_dev.adapters.ocr import build_ocr_command


def test_command_shape_matches_dispatch_md():
    cmd = build_ocr_command(binary="ocr", workdir=Path("/repo/worktree"), timeout_seconds=900)
    assert cmd[:2] == ["ocr", "review"]
    assert "--repo" in cmd and "/repo/worktree" in cmd
    assert "--from" in cmd and "origin/main" in cmd
    assert "--to" in cmd and "HEAD" in cmd
    assert "--format" in cmd and "json" in cmd
    assert "--audience" in cmd and "agent" in cmd


def test_timeout_converted_to_minutes_not_seconds():
    # dispatch.md: --timeout is OCR's own per-file budget in MINUTES,
    # derived from the outer run.py --timeout in SECONDS.
    cmd = build_ocr_command(binary="ocr", workdir=Path("/x"), timeout_seconds=900)
    i = cmd.index("--timeout")
    assert cmd[i + 1] == "15"  # 900s / 60 = 15min, not 900


def test_timeout_floor_is_one_minute():
    cmd = build_ocr_command(binary="ocr", workdir=Path("/x"), timeout_seconds=30)
    i = cmd.index("--timeout")
    assert cmd[i + 1] == "1"  # never 0


def test_no_prompt_or_session_flag_present():
    cmd = build_ocr_command(binary="ocr", workdir=Path("/x"), timeout_seconds=600)
    joined = " ".join(cmd)
    assert "--prompt" not in joined
    assert "--session" not in joined
    assert "--resume" not in joined


def test_default_model_sentinel_omitted():
    cmd = build_ocr_command(binary="ocr", workdir=Path("/x"), timeout_seconds=600, model="default")
    assert "--model" not in cmd


def test_explicit_model_included():
    cmd = build_ocr_command(binary="ocr", workdir=Path("/x"), timeout_seconds=600, model="gpt-5-ocr")
    i = cmd.index("--model")
    assert cmd[i + 1] == "gpt-5-ocr"
