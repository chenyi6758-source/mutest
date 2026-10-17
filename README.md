# 🧬 mutest

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![pytest](https://img.shields.io/badge/tested_with-pytest-yellow.svg)](tests/)

**Mutation-testing-driven AI test generation — the MIT-licensed successor to [qodo-cover](https://github.com/qodo-ai/qodo-cover) (deprecated June 2025).**

Line coverage is a vanity metric. `mutest` injects small bugs (mutants) into your code, runs your test suite against each one, and scores it: **mutation score = killed / total**. The mutants your suite *fails* to kill get targeted tests written by an LLM — cross-checked by a *different* model and verified by actually running them. Timeouts and execution errors remain in the total and are reported as not killed.

## ⚡ 30-second demo (real output)

```text
$ mutest --src examples/sample_calc --tests examples/sample_calc --no-llm
generated 20 mutants from 1 file(s) (10 operators)
running baseline test suite...
baseline green ✓
mutation score: 70.0% (14 killed / 6 survived / 20 total, 7s)
report written to mutest-report.html
```

Same project, two metrics:

| metric | score | what it means |
|---|---|---|
| line coverage | **93%** | looks great on the dashboard |
| **mutation score (mutest)** | **70%** | **30% of injected bugs go completely undetected** |

One survivor, for example — the boundary nobody tested:

```diff
# calc.py:14  (ROR: >= -> >)
-    return age >= 18
+    return age > 18     # is_adult(18) now wrongly returns False — suite says nothing
```

Full HTML report from this run: [`examples/sample_calc/report.html`](examples/sample_calc/report.html)

## 🚀 Quickstart

```bash
pip install mutest        # scoring only; keeps the installation lightweight
pip install "mutest[ai]"  # add the optional litellm dependency for --model
# Until v0.1.0 is on PyPI, use one of these instead:
pip install "mutest @ git+https://github.com/chenyi6758-source/mutest"
pip install "mutest[ai] @ git+https://github.com/chenyi6758-source/mutest"

cd your-python-project
mutest --src src --tests tests --no-llm        # scoring only, no API key needed
mutest --src src --tests tests --model gpt-4o-mini   # requires mutest[ai]
```

Requirements: Python 3.10+, a green `pytest` suite (mutest refuses to run on a red baseline). `litellm` is optional: install `mutest[ai]` only when using `--model` or `--verify-model`.

With `--model`, mutest takes the top survived mutants, asks the LLM for one targeted test each, writes them to `tests/test_mutest_gen.py`, then **empirically verifies**: the new tests must pass on your original code and fail on the mutant. Score goes up only when the tests *provably* kill.

## 🧠 How it works

1. **Mutate** — 10 AST operators (AOR/ROR/LOR/NOT/NEGCOND/NUM/STR/RETNONE/RETBOOL/UNARY) generate one mutant per site. Every mutant is re-parsed; no-ops and duplicates are dropped.
2. **Execute** — each mutant runs your full `pytest` suite in an isolated temp workspace, in parallel (`-x`: stop at first failure, we only need to know *whether* it's caught).
3. **Generate** — for the worst survivors, the LLM writes targeted tests (`--model`, any litellm provider; install `mutest[ai]`; keys use standard env vars and are never stored).
4. **Cross-validate** — optional `--verify-model` (a *different* provider) judges each generated test, then the empirical run confirms it: passes on original, fails on mutant.
5. **Report** — single-file HTML: score cards, per-file breakdown, every survivor with an original/mutated diff, generated tests.

## 🆚 mutest vs qodo-cover

| | mutest | qodo-cover |
|---|---|---|
| status | ✅ maintained | ❌ deprecated (June 2025) |
| license | **MIT** (enterprise-friendly) | AGPL-3.0 (banned by many SBOM policies) |
| quality metric | **mutation score** (killed/total) | line coverage |
| AI test loop | yes, with cross-model validation | yes |
| equivalent mutants | manual mark (auto-detection is P2) | n/a |

## ⚙️ Configuration

`mutest --help` lists everything. The knobs that matter:

```
--model gpt-4o-mini        LLM for test generation (any litellm model string)
--verify-model claude-haiku-4-5   second provider judges generated tests
--top-n 10                 survived mutants to target
--workers 4 / --timeout 120  (automatic workers are capped at 8; explicit values are honored)
--fail-under 75             fail CI below a final 75 percentage-point score
--incremental              only mutate files changed vs git HEAD (PR runs)
--base-ref BASE            PR base revision; compare BASE...HEAD with --incremental
--json                     print {"score": ...} for CI
--config mutest.yaml       operators on/off, defaults (see src/mutest/config.py)
```

Environment: `MUTEST_MODEL`, `MUTEST_LLM_PROVIDER=fake` (offline demos/tests).

## 🤖 GitHub Action

```yaml
- uses: chenyi6758-source/mutest@main  # pin to a full commit SHA in production
  with:
    src: src
    tests: tests
    # fail-under: "75"      # also fails when any mutant times out or errors
    incremental: "true"     # only changed files on PRs
    base-ref: ${{ github.event.pull_request.base.sha }}
```

See [`action.yml`](action.yml) and [`.github/workflows/mutest-example.yml`](.github/workflows/mutest-example.yml) — the example workflow comments the mutation score on every PR.

## ❓ FAQ

**My score is low. Is my code bad?**
Not necessarily — a low score means your *tests* don't detect behavior changes. That's exactly the information line coverage hides from you.

**Equivalent mutants (behavior-preserving changes) show as "survived". What now?**
Mark them: open the HTML report, confirm the mutant is truly equivalent, and add it to your exclusion list. Automatic equivalence detection is undecidable in general — it's on the P2 roadmap, not v0.1.

**It's slow on my repo.**
Use `--incremental` (changed files only), `--workers N`, and shrink the operator set in `mutest.yaml`. Mutation testing is inherently O(mutants × suite time); the parallel executor and `-x` early-exit keep it practical.

**Do the generated tests phone home?**
Generated tests execute locally and secret-like environment variables are removed before they run. This is not a full network sandbox, so treat generated code as untrusted and run it only in an appropriately isolated CI environment. The only network call mutest itself makes is the provider request you trigger with `--model`; provider keys are never written to the repo.

## 🗺️ Roadmap

- [x] v0.1: 10 AST operators, parallel runner, LLM loop, HTML report, GitHub Action
- [ ] P2: equivalent-mutant clustering, JS/TS support, flaky-test detection, Ollama/local models

## 🤝 Contributing

PRs welcome — especially new mutation operators and real-world score reports. Run `pytest` before pushing; it must be green.

---

If mutest caught a bug your coverage badge missed, give it a ⭐ — it keeps the project alive.

[中文版 README](README.zh-CN.md)
