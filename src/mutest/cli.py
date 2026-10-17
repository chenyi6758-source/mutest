"""Command line interface for mutest."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

from mutest import __version__
from mutest.config import load_config
from mutest.mutators import ALL_OPERATORS, OPERATORS_BY_CODE, generate_mutants
from mutest.runner import (
    ERROR,
    KILLED,
    SURVIVED,
    TIMEOUT,
    BaselineError,
    check_baseline,
    run_mutants,
)
from mutest.report import write_report


THRESHOLD_FAILURE = 4


def _percentage(value: str) -> float:
    """Parse a finite percentage-point value in the inclusive 0-100 range."""
    try:
        percentage = float(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("must be a number between 0 and 100") from e
    if not math.isfinite(percentage) or not 0 <= percentage <= 100:
        raise argparse.ArgumentTypeError("must be a number between 0 and 100")
    return percentage


def _threshold_failure_reasons(summary, fail_under: float | None) -> list[str]:
    """Return CI failure reasons for an explicitly requested score threshold."""
    if fail_under is None:
        return []
    reasons = []
    score_percentage = summary.score * 100
    if score_percentage < fail_under:
        reasons.append(
            f"mutation score {score_percentage:.1f}% is below --fail-under "
            f"{fail_under:g}%"
        )
    incomplete = []
    if summary.timeout:
        incomplete.append(f"{summary.timeout} timeout(s)")
    if summary.error:
        incomplete.append(f"{summary.error} error(s)")
    if incomplete:
        reasons.append(
            "mutation run has " + " and ".join(incomplete) +
            "; thresholded runs require every mutant to complete"
        )
    return reasons


def _is_test_file(path: Path) -> bool:
    name = path.name
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or "tests" in path.parts
    )


def collect_sources(src_dir: Path) -> list[Path]:
    files = sorted(
        p for p in src_dir.rglob("*.py")
        if not _is_test_file(p.relative_to(src_dir))
    )
    return files


def changed_files_since_git(src_dir: Path, base_ref: str | None = None) -> list[Path] | None:
    """Files changed for incremental mode. Returns None if git fails.

    Without an explicit base revision this preserves the local development
    behavior of comparing the working tree with ``HEAD``.  CI pull requests
    can pass a base revision to compare the committed range ``base...HEAD``.
    """
    revision = f"{base_ref}...HEAD" if base_ref else "HEAD"
    try:
        out = subprocess.run(
            ["git", "-C", str(src_dir), "diff", "--name-only", revision, "--"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return None
        names = {l.strip() for l in out.stdout.splitlines() if l.strip().endswith(".py")}
        return [src_dir / n for n in sorted(names) if (src_dir / n).exists()]
    except Exception:
        return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mutest",
        description=(
            "Mutation-testing-driven AI test generation. "
            "Measures your test suite with mutation score (not line coverage) "
            "and lets an LLM write tests that kill the survivors."
        ),
    )
    p.add_argument("--src", default="src", help="source directory (default: src)")
    p.add_argument("--tests", default="tests", help="test directory (default: tests)")
    p.add_argument("--model", default=None,
                   help="LLM for test generation, e.g. gpt-4o-mini (litellm format). "
                        "If omitted, only mutation scoring runs.")
    p.add_argument("--verify-model", default=None,
                   help="second LLM (different provider) to judge generated tests "
                        "— cross-model validation against self-review blindness.")
    p.add_argument("--top-n", type=int, default=10,
                   help="survived mutants to target with AI tests (default: 10)")
    p.add_argument("--workers", type=int, default=0,
                   help="parallel mutant runs (default: cpu_count - 1, capped at 8; "
                        "explicit values are honored)")
    p.add_argument("--timeout", type=int, default=120,
                   help="per-mutant pytest timeout in seconds (default: 120)")
    p.add_argument("--output", "-o", default="mutest-report.html",
                   help="HTML report path (default: mutest-report.html)")
    p.add_argument("--fail-under", type=_percentage, default=None, metavar="PERCENT",
                   help="fail if the final mutation score is below this percentage "
                        "(0-100); when set, timeout/error outcomes also fail")
    p.add_argument("--config", "-c", default=None, help="path to mutest.yaml")
    p.add_argument("--no-llm", action="store_true",
                   help="skip AI test generation even if --model is set")
    p.add_argument("--json", action="store_true",
                   help="print the score summary as JSON (for CI)")
    p.add_argument("--incremental", action="store_true",
                   help="only mutate files changed vs git HEAD")
    p.add_argument("--base-ref", default=None,
                   help="base git revision for --incremental (diffed as base...HEAD; "
                        "local fallback remains HEAD when omitted)")
    p.add_argument("--gen-file", default="test_mutest_gen.py",
                   help="name of the generated-tests file (default: test_mutest_gen.py)")
    p.add_argument("--list-operators", action="store_true",
                   help="list mutation operators and exit")
    p.add_argument("--version", action="store_true", help="print version and exit")
    return p


def _print_progress(done: int, total: int, last_status: str) -> None:
    bar_len = 30
    filled = int(bar_len * done / max(total, 1))
    bar = "#" * filled + "-" * (bar_len - filled)
    sys.stdout.write(f"\r[{bar}] {done}/{total} mutants ({last_status})")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.version:
        print(f"mutest {__version__}")
        return 0
    if args.list_operators:
        for op in ALL_OPERATORS:
            print(f"{op.code:10s} {op.title}")
        return 0

    cfg = load_config(args.config)
    src_dir = Path(args.src).resolve()
    tests_dir = Path(args.tests).resolve()
    if not src_dir.is_dir():
        print(f"error: --src '{src_dir}' is not a directory", file=sys.stderr)
        return 2
    if not tests_dir.is_dir():
        print(f"error: --tests '{tests_dir}' is not a directory", file=sys.stderr)
        return 2

    workers = args.workers or cfg.workers
    timeout = args.timeout if args.timeout != 120 else cfg.timeout
    model = args.model or cfg.model or os.environ.get("MUTEST_MODEL")
    verify_model = args.verify_model or cfg.verify_model
    top_n = args.top_n if args.top_n != 10 else cfg.top_n
    enabled = cfg.enabled_operators([op.code for op in ALL_OPERATORS])
    operators = [OPERATORS_BY_CODE[c] for c in enabled]

    # 1. collect mutants
    if args.incremental:
        changed = changed_files_since_git(src_dir, args.base_ref)
        if changed is None:
            print("warning: --incremental needs a git repo; falling back to all files",
                  file=sys.stderr)
            files = collect_sources(src_dir)
        else:
            files = [f for f in changed if not _is_test_file(f.relative_to(src_dir))]
            print(f"incremental mode: {len(files)} changed file(s)")
    else:
        files = collect_sources(src_dir)
    if not files:
        print("error: no source files found (test files are excluded)", file=sys.stderr)
        return 2

    mutants = []
    for f in files:
        rel = str(f.relative_to(src_dir))
        mutants.extend(generate_mutants(f.read_text(encoding="utf-8"), rel, operators))
    print(f"generated {len(mutants)} mutants from {len(files)} file(s) "
          f"({len(operators)} operators)")
    if not mutants:
        print("nothing to mutate — all operators produced no mutants.")
        return 0

    # 2. baseline must be green
    print("running baseline test suite...", flush=True)
    try:
        check_baseline(src_dir, tests_dir, timeout)
    except BaselineError as e:
        print(str(e), file=sys.stderr)
        return 3
    print("baseline green ✓")

    # 3. run mutants
    print(f"executing mutants ({workers or 'auto'} workers)...", flush=True)
    done = [0]

    def _cb(r):
        done[0] += 1
        _print_progress(done[0], len(mutants), r.status)

    t0 = time.time()
    results, summary = run_mutants(mutants, src_dir, tests_dir,
                                   workers=workers, timeout=timeout,
                                   progress_cb=_cb)
    print()  # end progress line
    by_id = {r.mutant.id: r for r in results}
    print(f"mutation score: {summary.score*100:.1f}% "
          f"({summary.killed} killed / {summary.survived} survived / "
          f"{summary.timeout} timeout / {summary.error} error / "
          f"{summary.total} total, {summary.elapsed_s:.0f}s)")

    # 4. AI test generation loop
    gen_info: dict = {}
    generated_tests: list[dict] = []
    if model and not args.no_llm:
        from mutest import llm as llm_mod

        survived = [r for r in results if r.status == SURVIVED][:top_n]
        print(f"AI test generation: targeting {len(survived)} survived mutant(s) "
              f"with {model}" + (f" (judge: {verify_model})" if verify_model else ""))
        provider = llm_mod.get_provider()
        gen_path = tests_dir / args.gen_file
        existing = "\n".join(
            (tests_dir / f).read_text(encoding="utf-8")
            for f in sorted(os.listdir(tests_dir))
            if f.startswith("test_") and f.endswith(".py") and f != args.gen_file
        )
        kept: list[tuple[str, str, str]] = []  # (name, code, mutant_id)
        for r in survived:
            m = r.mutant
            module_src = (src_dir / m.file).read_text(encoding="utf-8")
            try:
                gen = provider.generate_test(m, module_src, existing, model)
            except Exception as e:
                print(f"  !! LLM failed for {m.id}: {e}")
                continue
            if verify_model and not isinstance(provider, llm_mod.FakeProvider):
                ok, reason = provider.judge(m, gen.code, verify_model)
                if not ok:
                    print(f"  -- judge rejected test for {m.id}: {reason}")
                    continue
            kept.append((f"test_mutest_{m.id.replace(':','_').replace('/','_')}",
                         gen.code, m.id))
            print(f"  + generated test for {m.id} ({m.operator}: {m.description})")
        if kept:
            with open(gen_path, "w", encoding="utf-8") as f:
                f.write('"""Tests auto-generated by mutest — do not edit by hand."""\n\n')
                for name, code, _mid in kept:
                    f.write(code if code.rstrip().endswith("\n") else code + "\n")
                    f.write("\n")
            # empirical check 1: generated tests must pass on ORIGINAL code
            from mutest.runner import _Worker
            w = _Worker(src_dir, tests_dir)
            try:
                rc, out = w.run_pytest(
                    timeout,
                    target=f"{w._tests_arg}/{args.gen_file}",
                    sanitize_env=True,
                )
            finally:
                w.close()
            if rc != 0:
                print(f"warning: generated tests fail on original code — discarding file:\n{out[-800:]}",
                      file=sys.stderr)
                gen_path.unlink(missing_ok=True)
                kept = []
            else:
                # empirical check 2: re-run the targeted mutants with new tests
                print("re-running targeted mutants with generated tests...")
                results2, _ = run_mutants(
                    [by_id[mid].mutant for _, _, mid in kept],
                    src_dir, tests_dir, workers=workers, timeout=timeout,
                    sanitize_env=True)
                newly_killed = sum(1 for r in results2 if r.status == KILLED)
                for r in results2:
                    by_id[r.mutant.id] = r
                # recompute summary
                summary = _resummarize(by_id.values())
                for (name, code, mid), r in zip(kept, results2):
                    generated_tests.append({
                        "name": name, "code": code,
                        "target": mid, "model": model,
                        "status": _generated_test_status(r.status),
                    })
                gen_info = {
                    "delta": True,
                    "gen_model": model,
                    "verify_model": verify_model,
                    "gen_count": len(kept),
                    "rerun": len(results2),
                    "delta_killed": newly_killed,
                    "delta_before": round(_resummarize(results).score, 4),
                    "delta_after": round(summary.score, 4),
                }
                print(f"AI loop: {newly_killed}/{len(results2)} newly killed — "
                      f"score now {summary.score*100:.1f}%")

    # 5. report
    files_ctx = []
    per_file: dict[str, dict] = {}
    for r in by_id.values():
        d = per_file.setdefault(r.mutant.file, {
            "total": 0, "killed": 0, "survived": 0, "timeout": 0, "error": 0,
        })
        d["total"] += 1
        if r.status == KILLED:
            d["killed"] += 1
        elif r.status == SURVIVED:
            d["survived"] += 1
        elif r.status == TIMEOUT:
            d["timeout"] += 1
        else:
            d["error"] += 1
    for name in sorted(per_file):
        d = per_file[name]
        denom = d["total"]
        files_ctx.append({"name": name, **d,
                          "score": (d["killed"] / denom) if denom else 0.0})
    survived_ms = [
        {"id": r.mutant.id, "operator": r.mutant.operator,
         "description": r.mutant.description,
         "original_snippet": r.mutant.original_snippet,
         "mutated_snippet": r.mutant.mutated_snippet}
        for r in by_id.values() if r.status == SURVIVED
    ]
    unresolved_ms = [
        {"id": r.mutant.id, "operator": r.mutant.operator,
         "description": r.mutant.description,
         "original_snippet": r.mutant.original_snippet,
         "mutated_snippet": r.mutant.mutated_snippet,
         "status": r.status, "detail": r.detail}
        for r in by_id.values() if r.status in (TIMEOUT, ERROR)
    ]
    ctx = {
        "target": f"{src_dir}  (+ {tests_dir})",
        "version": __version__,
        "score": round(summary.score, 4),
        **summary.as_dict(),
        "files": files_ctx,
        "survived_mutants": survived_ms,
        "unresolved_mutants": unresolved_ms,
        "generated_tests": generated_tests,
        **gen_info,
    }
    write_report(args.output, ctx)
    print(f"report written to {args.output}")

    threshold_reasons = _threshold_failure_reasons(summary, args.fail_under)
    if threshold_reasons:
        print("FAIL: " + "; ".join(threshold_reasons), file=sys.stderr)

    if args.json:
        payload = {"version": __version__, **summary.as_dict(),
                   "output": args.output}
        print(json.dumps(payload))
    return THRESHOLD_FAILURE if threshold_reasons else 0


def _resummarize(results):
    from mutest.runner import RunSummary
    s = RunSummary(total=0)
    for r in results:
        s.total += 1
        if r.status == KILLED:
            s.killed += 1
        elif r.status == SURVIVED:
            s.survived += 1
        elif r.status == TIMEOUT:
            s.timeout += 1
        else:
            s.error += 1
    return s


def _generated_test_status(status: str) -> str:
    """Return an honest status for a generated-test mutant re-run."""
    if status == KILLED:
        return "killed ✓"
    if status == TIMEOUT:
        return "timeout"
    if status == ERROR:
        return "error"
    return "still survived"


if __name__ == "__main__":
    sys.exit(main())
