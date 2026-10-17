"""CLI tests, including end-to-end runs on the sample project."""

import json
import os
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from mutest.cli import (
    _generated_test_status,
    _threshold_failure_reasons,
    build_parser,
    changed_files_since_git,
    main,
)
from mutest.runner import ERROR, KILLED, SURVIVED, TIMEOUT

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample_calc"
SRC = Path(__file__).resolve().parent.parent / "src"


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_version(capsys):
    assert main(["--version"]) == 0
    assert "0.1.0" in capsys.readouterr().out


def test_list_operators(capsys):
    assert main(["--list-operators"]) == 0
    out = capsys.readouterr().out
    for code in ("AOR", "ROR", "LOR", "NOT", "NEGCOND",
                 "NUM", "STR", "RETNONE", "RETBOOL", "UNARY"):
        assert code in out


def test_bad_src_dir():
    assert main(["--src", "/nonexistent-xyz"]) == 2


def test_base_ref_is_used_for_committed_incremental_range(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    changed = src / "changed.py"
    changed.write_text("value = 1\n")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="changed.py\n")

    monkeypatch.setattr("mutest.cli.subprocess.run", fake_run)
    assert changed_files_since_git(src, "origin/main") == [changed]
    assert calls == [[
        "git", "-C", str(src), "diff", "--name-only", "origin/main...HEAD", "--",
    ]]


def test_base_ref_cli_option():
    args = build_parser().parse_args(["--incremental", "--base-ref", "abc123"])
    assert args.incremental is True
    assert args.base_ref == "abc123"


def test_fail_under_is_a_percentage_point_threshold():
    args = build_parser().parse_args(["--fail-under", "75.5"])
    assert args.fail_under == 75.5
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--fail-under", "100.1"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--fail-under", "-1"])


def test_fail_under_passes_and_fails_the_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PYTHONPATH", str(SRC))
    for threshold, expected_rc in (("70", 0), ("70.1", 4)):
        report = tmp_path / f"report-{threshold}.html"
        rc = main([
            "--src", str(SAMPLE), "--tests", str(SAMPLE), "--no-llm",
            "--workers", "2", "--timeout", "60", "--fail-under", threshold,
            "--output", str(report), "--json",
        ])
        assert rc == expected_rc
        assert report.exists()
    assert "below --fail-under 70.1%" in capsys.readouterr().err


def test_fail_under_requires_complete_mutant_outcomes():
    summary = SimpleNamespace(score=1.0, timeout=1, error=1)
    reasons = _threshold_failure_reasons(summary, 0)
    assert len(reasons) == 1
    assert "1 timeout(s) and 1 error(s)" in reasons[0]


def test_generated_test_status_does_not_call_unfinished_runs_killed():
    assert _generated_test_status(KILLED) == "killed ✓"
    assert _generated_test_status(SURVIVED) == "still survived"
    assert _generated_test_status(TIMEOUT) == "timeout"
    assert _generated_test_status(ERROR) == "error"


def test_e2e_no_llm(tmp_path, monkeypatch):
    """Full scoring run without the LLM step."""
    monkeypatch.setenv("PYTHONPATH", str(SRC))
    out = tmp_path / "report.html"
    rc = main(["--src", str(SAMPLE), "--tests", str(SAMPLE),
               "--no-llm", "--workers", "2", "--timeout", "60",
               "--output", str(out), "--json"])
    assert rc == 0
    assert out.exists()
    html = out.read_text()
    assert "mutation score" in html
    assert "calc.py" in html


def test_e2e_with_fake_llm(tmp_path, monkeypatch):
    """Full loop with the fake LLM: canned boundary test must kill the
    survived ROR mutant and raise the score."""
    monkeypatch.setenv("PYTHONPATH", str(SRC))
    monkeypatch.setenv("MUTEST_LLM_PROVIDER", "fake")
    # figure out the survived mutant id first
    from mutest.mutators import generate_mutants
    ms = generate_mutants((SAMPLE / "calc.py").read_text(), "calc.py")
    ror = [m for m in ms if m.operator == "ROR" and m.lineno == 14]
    assert ror, "expected a survived ROR mutant at line 14"
    canned = {
        ror[0].id: (
            "def test_mutest_is_adult_boundary():\n"
            "    from calc import is_adult\n"
            "    assert is_adult(18) is True\n"
        )
    }
    canned_path = tmp_path / "canned.json"
    canned_path.write_text(json.dumps(canned))
    monkeypatch.setenv("MUTEST_FAKE_TESTS_FILE", str(canned_path))

    # copy sample to tmp so the gen file doesn't pollute the repo example
    import shutil
    work = tmp_path / "proj"
    shutil.copytree(SAMPLE, work)
    out = tmp_path / "report.html"
    rc = main(["--src", str(work), "--tests", str(work),
               "--model", "fake-model", "--top-n", "30",
               "--workers", "2", "--timeout", "60",
               "--output", str(out)])
    assert rc == 0
    gen_file = work / "test_mutest_gen.py"
    assert gen_file.exists(), "generated test file should exist"
    html = out.read_text()
    assert "newly killed" in html
    # the boundary test must really kill the mutant: run it directly
    env = _env()
    env["PYTHONPATH"] = str(work) + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "test_mutest_gen.py", "-q",
         "-p", "no:cacheprovider"],
        cwd=work, env=env, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stdout[-500:] + r.stderr[-500:]
