"""LLM access for targeted test generation.

- :class:`LiteLLMProvider` — real provider via litellm (lazy import; only
  needed when ``--model`` is used). Never hard-code API keys: litellm reads
  the standard env vars (``OPENAI_API_KEY`` etc.).
- :class:`FakeProvider` — deterministic offline stand-in used by the test
  suite and by ``MUTEST_LLM_PROVIDER=fake``. Canned responses can be
  supplied per mutant id.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from mutest.mutators import Mutant


@dataclass
class GeneratedTest:
    code: str
    model: str
    target_mutant_id: str


def extract_python_code(text: str) -> str:
    """Pull the first ```python fenced block out of an LLM response."""
    m = re.search(r"```python\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip() + "\n"
    m = re.search(r"```\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip() + "\n"
    stripped = text.strip()
    if stripped.startswith("def test_") or stripped.startswith("import "):
        return stripped + "\n"
    return ""


def build_generation_prompt(
    mutant: Mutant, module_source: str, existing_tests: str
) -> list[dict]:
    src = module_source[:6000]
    tests = existing_tests[:4000]
    return [
        {
            "role": "system",
            "content": (
                "You are an expert Python test engineer. Write ONE pytest test "
                "function that kills the described mutant. Reply with ONLY a "
                "```python fenced code block, no explanations."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Module under test (file: {mutant.file}):\n```python\n{src}\n```\n\n"
                f"Mutant {mutant.id}:\n"
                f"- operator: {mutant.operator} ({mutant.title})\n"
                f"- change: {mutant.description} (line {mutant.lineno})\n"
                f"- original code:\n```python\n{mutant.original_snippet}\n```\n"
                f"- mutated code:\n```python\n{mutant.mutated_snippet}\n```\n\n"
                f"Existing tests (for style reference, do not duplicate):\n"
                f"```python\n{tests}\n```\n\n"
                "Write ONE pytest test function that PASSES on the original code "
                "and FAILS on the mutated code. Name it test_mutest_<something>. "
                "Import from the module the same way existing tests do."
            ),
        },
    ]


def build_judge_prompt(mutant: Mutant, test_code: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "You are a strict test reviewer. Answer with exactly one word: "
                "YES or NO, followed by one short sentence of reasoning."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Original code (line {mutant.lineno} of {mutant.file}):\n"
                f"```python\n{mutant.original_snippet}\n```\n"
                f"Mutated code:\n```python\n{mutant.mutated_snippet}\n```\n"
                f"Candidate test:\n```python\n{test_code}\n```\n"
                "Does this test PASS on the original code and FAIL on the mutated "
                "code? (A different model wrote the module; review it independently.)"
            ),
        },
    ]


class LiteLLMProvider:
    """Real LLM access through litellm. Import is lazy so mutest works
    without litellm installed until ``--model`` is actually used."""

    def __init__(self) -> None:
        try:
            import litellm  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "litellm is not installed. Install it with "
                "`pip install 'mutest[ai]'` or `pip install litellm`."
            ) from e

    def generate_test(
        self,
        mutant: Mutant,
        module_source: str,
        existing_tests: str,
        model: str,
    ) -> GeneratedTest:
        import litellm

        messages = build_generation_prompt(mutant, module_source, existing_tests)
        resp = litellm.completion(model=model, messages=messages, temperature=0.2)
        text = resp.choices[0].message.content or ""
        code = extract_python_code(text)
        if not code:
            raise RuntimeError(f"LLM returned no usable Python code for {mutant.id}")
        return GeneratedTest(code=code, model=model, target_mutant_id=mutant.id)

    def judge(self, mutant: Mutant, test_code: str, model: str) -> tuple[bool, str]:
        import litellm

        messages = build_judge_prompt(mutant, test_code)
        resp = litellm.completion(model=model, messages=messages, temperature=0)
        text = (resp.choices[0].message.content or "").strip()
        ok = text.upper().startswith("YES")
        return ok, text[:300]


class FakeProvider:
    """Deterministic offline provider for tests and demos.

    ``canned`` maps mutant id -> test code. Unknown ids get a template test
    that is clearly marked (it will usually fail the empirical check and be
    discarded, which is the honest behaviour).
    """

    def __init__(self, canned: dict[str, str] | None = None):
        self.canned = canned or {}

    @classmethod
    def from_env(cls) -> "FakeProvider":
        path = os.environ.get("MUTEST_FAKE_TESTS_FILE")
        canned: dict[str, str] = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                canned = json.load(f)
        return cls(canned)

    def generate_test(self, mutant, module_source, existing_tests, model):
        code = self.canned.get(mutant.id)
        if code is None:
            code = (
                f"# FAKE provider placeholder for {mutant.id} "
                f"({mutant.operator}: {mutant.description})\n"
                f"def test_mutest_{mutant.operator.lower()}_placeholder():\n"
                f"    assert True  # placeholder - will not kill anything\n"
            )
        return GeneratedTest(code=code, model="fake", target_mutant_id=mutant.id)

    def judge(self, mutant, test_code, model):
        return True, "fake provider always approves"


def get_provider() -> LiteLLMProvider | FakeProvider:
    """Select provider from MUTEST_LLM_PROVIDER (default: real litellm)."""
    if os.environ.get("MUTEST_LLM_PROVIDER") == "fake":
        return FakeProvider.from_env()
    return LiteLLMProvider()
