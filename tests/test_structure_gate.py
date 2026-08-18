import subprocess

import pytest

from reasona_dev import structure_gate


def _repo(tmp_path, files: dict[str, str], cfg: str | None = None):
    """A real git repo -- structure_gate reads `git ls-files`, so a plain
    directory would look empty and every check would vacuously pass.
    """
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    if cfg is not None:
        (tmp_path / ".reasona").mkdir(exist_ok=True)
        (tmp_path / ".reasona" / "reasona.yaml").write_text(cfg)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp_path, check=True,
    )
    return tmp_path


def test_absent_config_disables_the_gate(tmp_path):
    """Enforcement is opt-in per repo -- a repo with no block must not
    inherit someone else's limits."""
    repo = _repo(tmp_path, {"a.rs": "x\n" * 5000})
    assert structure_gate.evaluate(repo) == []


def test_max_file_lines_catches_the_oversized_file(tmp_path):
    repo = _repo(
        tmp_path,
        {"big.rs": "x\n" * 50, "small.rs": "x\n" * 5},
        cfg="structure-gate:\n  max_file_lines:\n    limit: 20\n    include: ['*.rs']\n",
    )
    violations = structure_gate.evaluate(repo)
    assert [v.path for v in violations] == ["big.rs"]
    assert violations[0].actual == 50
    assert violations[0].limit == 20


def test_include_glob_scopes_the_check(tmp_path):
    repo = _repo(
        tmp_path,
        {"big.md": "x\n" * 50},
        cfg="structure-gate:\n  max_file_lines:\n    limit: 20\n    include: ['*.rs']\n",
    )
    assert structure_gate.evaluate(repo) == []


def test_waiver_with_reason_suppresses_a_violation(tmp_path):
    repo = _repo(
        tmp_path,
        {"big.rs": "x\n" * 50},
        cfg=(
            "structure-gate:\n"
            "  max_file_lines:\n    limit: 20\n    include: ['*.rs']\n"
            "  waivers:\n"
            "    - check: max_file_lines\n      path: big.rs\n      reason: split scheduled in PR 24\n"
        ),
    )
    assert structure_gate.evaluate(repo) == []


def test_waiver_without_reason_is_rejected(tmp_path):
    """A waiver is a recorded decision, not a silent threshold bump."""
    repo = _repo(
        tmp_path,
        {"big.rs": "x\n" * 50},
        cfg=(
            "structure-gate:\n"
            "  max_file_lines:\n    limit: 20\n    include: ['*.rs']\n"
            "  waivers:\n    - check: max_file_lines\n      path: big.rs\n"
        ),
    )
    assert len(structure_gate.evaluate(repo)) == 1


def test_duplicate_block_reports_the_copy_not_the_original(tmp_path):
    block = "\n".join(f"let v{i} = compute({i});" for i in range(6))
    repo = _repo(
        tmp_path,
        {"orig.rs": block + "\n", "copy.rs": block + "\n"},
        cfg="structure-gate:\n  duplicate_block:\n    window: 5\n    include: ['*.rs']\n",
    )
    violations = structure_gate.evaluate(repo)
    assert len(violations) == 1
    assert violations[0].path.startswith("orig.rs") or violations[0].path.startswith("copy.rs")
    # exactly one of the pair is blamed, never both
    assert violations[0].check == "duplicate_block"


def test_duplicate_block_ignores_comment_and_blank_differences(tmp_path):
    body = "\n".join(f"let v{i} = compute({i});" for i in range(6))
    repo = _repo(
        tmp_path,
        {"a.rs": body + "\n", "b.rs": "// a comment\n\n" + body + "\n"},
        cfg="structure-gate:\n  duplicate_block:\n    window: 5\n    include: ['*.rs']\n",
    )
    assert len(structure_gate.evaluate(repo)) == 1


def test_forbidden_dependency_flags_the_declared_pattern(tmp_path):
    repo = _repo(
        tmp_path,
        {"crates/domain/src/x.rs": "use crate::infra::db;\nfn f() {}\n"},
        cfg=(
            "structure-gate:\n"
            "  forbidden_dependency:\n"
            "    - name: domain must not import infra\n"
            "      in: ['crates/domain/**']\n"
            "      pattern: '^\\s*use\\s+crate::infra::'\n"
        ),
    )
    violations = structure_gate.evaluate(repo)
    assert len(violations) == 1
    assert violations[0].detail == "domain must not import infra"
    assert violations[0].path == "crates/domain/src/x.rs:1"


def test_forbidden_dependency_allows_the_same_import_elsewhere(tmp_path):
    repo = _repo(
        tmp_path,
        {"crates/app/src/x.rs": "use crate::infra::db;\n"},
        cfg=(
            "structure-gate:\n"
            "  forbidden_dependency:\n"
            "    - name: domain must not import infra\n"
            "      in: ['crates/domain/**']\n"
            "      pattern: '^\\s*use\\s+crate::infra::'\n"
        ),
    )
    assert structure_gate.evaluate(repo) == []


def test_cli_exit_codes_match_gate_check_convention(tmp_path):
    clean = _repo(tmp_path / "clean", {"a.rs": "x\n"}, cfg="structure-gate:\n  max_file_lines:\n    limit: 20\n")
    assert structure_gate.main([str(clean)]) == 0

    dirty = _repo(tmp_path / "dirty", {"a.rs": "x\n" * 50}, cfg="structure-gate:\n  max_file_lines:\n    limit: 20\n")
    assert structure_gate.main([str(dirty)]) == 1


@pytest.mark.parametrize("bad_cfg", [
    "structure-gate:\n  max_file_lines:\n    limit: not-a-number\n",
    "structure-gate:\n  duplicate_block:\n    window: 1\n",
    "structure-gate:\n  forbidden_dependency:\n    - name: x\n      in: ['*']\n      pattern: '['\n",
])
def test_malformed_check_config_is_skipped_not_crashed(tmp_path, bad_cfg):
    """A broken threshold must not fail every PR in the repo."""
    repo = _repo(tmp_path, {"a.rs": "x\n" * 50}, cfg=bad_cfg)
    assert structure_gate.evaluate(repo) == []
