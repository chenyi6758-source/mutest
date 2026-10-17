"""Execute the test suite against each mutant, in parallel.

Design: one temp workspace per worker thread (a pristine copy of the source
and test trees plus allowlisted root-level pytest context). For each mutant
the worker overwrites the single mutated file, runs ``python -m pytest`` with
``-x`` (stop at first failure — we only need to know whether the suite detects
the change), then restores the file.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from mutest.mutators import Mutant

# statuses
KILLED = "killed"
SURVIVED = "survived"
TIMEOUT = "timeout"
ERROR = "error"

# A large machine should not create one Python process per CPU by default.
# Positive --workers values are passed through unchanged; this only limits the
# automatic value selected when workers is zero or negative.
AUTO_WORKER_LIMIT = 8


@dataclass
class MutantResult:
    mutant: Mutant
    status: str  # killed | survived | timeout | error
    duration_s: float = 0.0
    detail: str = ""  # tail of pytest output / error message


@dataclass
class RunSummary:
    total: int = 0
    killed: int = 0
    survived: int = 0
    timeout: int = 0
    error: int = 0
    elapsed_s: float = 0.0

    @property
    def score(self) -> float:
        """Mutation score = killed / total.

        A timeout or execution error means the mutant was not successfully
        classified as killed, so both remain in the denominator and lower the
        score rather than being counted as a success or silently excluded.
        """
        if self.total <= 0:
            return 0.0
        return self.killed / self.total

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "killed": self.killed,
            "survived": self.survived,
            "timeout": self.timeout,
            "error": self.error,
            "score": round(self.score, 4),
            "elapsed_s": round(self.elapsed_s, 1),
        }


class BaselineError(RuntimeError):
    """Raised when the test suite fails on the unmutated code."""


_SECRET_ENV_RE = re.compile(
    r"(?:^|_)(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIALS?|AUTH|"
    r"PRIVATE)(?:_|$)",
    re.IGNORECASE,
)


def sanitize_environment(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Copy an environment without common secret-like variable names.

    Mutant and generated-test subprocesses execute untrusted, model-produced
    code.  They still receive normal process configuration, but provider keys,
    tokens, passwords, and credential variables are deliberately withheld.
    """
    source = os.environ if environ is None else environ
    return {key: value for key, value in source.items()
            if not _SECRET_ENV_RE.search(key)}


class _Worker:
    """One temp workspace; reused across mutants on the same thread."""

    def __init__(self, src_dir: Path, tests_dir: Path):
        self.root = Path(tempfile.mkdtemp(prefix="mutest-"))
        self.src = self.root / "src"
        self.tests = self.root / "tests"
        # NOTE: __pycache__ must not be copied. CPython records source mtime
        # in .pyc headers with only SECOND granularity, so a mutant written
        # within the same second as a previous write would be shadowed by a
        # stale .pyc and we'd execute the wrong mutant's code.
        no_cache = shutil.ignore_patterns("__pycache__")
        shutil.copytree(src_dir, self.src, ignore=no_cache)
        # src and tests may be the same directory
        if src_dir.resolve() == tests_dir.resolve():
            self.tests = self.src
        else:
            shutil.copytree(tests_dir, self.tests, ignore=no_cache)
        self._copy_project_context(src_dir, tests_dir)
        # remember pristine bytes of every file we may overwrite
        self._pristine: dict[str, bytes] = {}
        for base in {self.src, self.tests}:
            for p in base.rglob("*.py"):
                rel = str(p.relative_to(base))
                if rel not in self._pristine:
                    self._pristine[rel] = p.read_bytes()
        # pytest target relative to the workspace root ("tests", or "src"
        # when src and tests are the same directory)
        self._tests_arg = self.tests.relative_to(self.root).as_posix()

    def _copy_project_context(self, src_dir: Path, tests_dir: Path) -> None:
        """Copy only root-level pytest context for sibling source/test trees.

        A temporary workspace must not become a copy of the whole project:
        that could include secrets, build outputs, or unrelated application
        data.  These are the standard files pytest uses to discover project
        fixtures and configuration, so copy only the explicit allowlist when
        ``src`` and ``tests`` share a project root.
        """
        src_root = src_dir.resolve().parent
        tests_root = tests_dir.resolve().parent
        if src_root != tests_root:
            return
        for name in ("conftest.py", "pytest.ini", "pyproject.toml",
                     "setup.cfg", "tox.ini"):
            source = src_root / name
            if source.is_file():
                shutil.copy2(source, self.root / name)

    def _write_mutant(self, mutant: Mutant) -> None:
        target = self.src / mutant.file
        target.write_text(mutant.source, encoding="utf-8")

    def _restore(self, mutant: Mutant) -> None:
        target = self.src / mutant.file
        target.write_bytes(self._pristine[mutant.file])

    def run_pytest(self, timeout: int, extra_args: list[str] | None = None,
                   target: str | None = None,
                   sanitize_env: bool = False) -> tuple[int, str]:
        """Run pytest in the workspace. Returns (returncode, output tail).

        Secret filtering is opt-in so ordinary project tests retain their
        existing environment.  Generated-test validation enables it because
        that code came from an LLM.
        """
        cmd = [sys.executable, "-m", "pytest", target or self._tests_arg,
               "-x", "-q", "-p", "no:cacheprovider"]
        if extra_args:
            cmd.extend(extra_args)
        env = sanitize_environment() if sanitize_env else dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self.src), str(self.tests), env.get("PYTHONPATH", "")]
        )
        # Never write .pyc in workers: a .pyc records source mtime with only
        # second granularity, so back-to-back mutants could be shadowed by a
        # stale bytecode cache and execute the wrong mutant's code.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            proc = subprocess.run(
                cmd, cwd=self.root, env=env, capture_output=True,
                text=True, timeout=timeout,
            )
            out = (proc.stdout + proc.stderr)[-2000:]
            return proc.returncode, out
        except subprocess.TimeoutExpired as e:
            out = ""
            if e.stdout:
                out += e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else e.stdout
            return -1, out[-2000:]

    def run_mutant(self, mutant: Mutant, timeout: int,
                   sanitize_env: bool = False) -> MutantResult:
        import time

        t0 = time.time()
        try:
            self._write_mutant(mutant)
            rc, out = self.run_pytest(timeout, sanitize_env=sanitize_env)
        except Exception as e:  # workspace I/O problems etc.
            return MutantResult(mutant, ERROR, time.time() - t0, str(e)[:500])
        finally:
            try:
                self._restore(mutant)
            except Exception:
                pass
        dur = time.time() - t0
        if rc == -1:
            # Timeout: the suite did not complete, so this is not a kill.
            return MutantResult(mutant, TIMEOUT, dur, "pytest timed out")
        if rc == 0:
            return MutantResult(mutant, SURVIVED, dur, "")
        return MutantResult(mutant, KILLED, dur, out)

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def check_baseline(src_dir: Path, tests_dir: Path, timeout: int) -> str:
    """Run the suite on pristine code. Returns pytest output tail.

    Raises BaselineError if the suite is not green.
    """
    w = _Worker(src_dir, tests_dir)
    try:
        rc, out = w.run_pytest(timeout)
    finally:
        w.close()
    if rc != 0:
        raise BaselineError(
            "Baseline test suite is not green — mutest requires all tests "
            f"to pass on unmutated code.\n--- pytest output ---\n{out}"
        )
    return out


def run_mutants(
    mutants: list[Mutant],
    src_dir: Path,
    tests_dir: Path,
    workers: int = 0,
    timeout: int = 120,
    progress_cb=None,
    sanitize_env: bool = False,
) -> tuple[list[MutantResult], RunSummary]:
    """Run every mutant; return per-mutant results and the aggregate summary."""
    import time

    t0 = time.time()
    workers = _effective_worker_count(workers)
    results: list[MutantResult] = []
    # One workspace per OS thread, bound via thread-local storage.
    # (Task indices must NOT be used to pick a workspace: the pool hands
    # tasks to whichever thread is free, so two concurrent tasks could
    # otherwise share -- and clobber -- the same workspace.)
    tls = threading.local()
    created: list[_Worker] = []
    created_lock = threading.Lock()

    def _workspace() -> _Worker:
        w = getattr(tls, "worker", None)
        if w is None:
            w = _Worker(src_dir, tests_dir)
            tls.worker = w
            with created_lock:
                created.append(w)
        return w

    try:

        def _one(mutant: Mutant) -> MutantResult:
            r = _workspace().run_mutant(mutant, timeout, sanitize_env=sanitize_env)
            if progress_cb:
                progress_cb(r)
            return r

        with ThreadPoolExecutor(max_workers=workers) as pool:
            # map preserves order -> deterministic results
            results = list(pool.map(_one, mutants))
    finally:
        for w in created:
            w.close()

    summary = RunSummary(total=len(results), elapsed_s=time.time() - t0)
    for r in results:
        if r.status == KILLED:
            summary.killed += 1
        elif r.status == SURVIVED:
            summary.survived += 1
        elif r.status == TIMEOUT:
            summary.timeout += 1
        else:
            summary.error += 1
    return results, summary


def _automatic_worker_count() -> int:
    """Return the bounded worker count used when no explicit value is set."""
    return min(max(1, (os.cpu_count() or 2) - 1), AUTO_WORKER_LIMIT)


def _effective_worker_count(workers: int) -> int:
    """Resolve automatic workers while preserving every explicit positive value."""
    return workers if workers > 0 else _automatic_worker_count()
