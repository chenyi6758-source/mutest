"""Optional YAML configuration: ``mutest.yaml``.

Example::

    operators:            # enable/disable operators (default: all on)
      AOR: true
      STR: false
    workers: 4            # parallel mutant executions (auto is capped at 8)
    timeout: 120          # per-mutant pytest timeout in seconds
    model: gpt-4o-mini    # default LLM for test generation
    verify_model: claude-haiku-4-5  # optional cross-model judge
    top_n: 10             # survived mutants to target with AI tests
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    operators: dict[str, bool] = field(default_factory=dict)
    workers: int = 0
    timeout: int = 120
    model: str | None = None
    verify_model: str | None = None
    top_n: int = 10

    def enabled_operators(self, all_codes: list[str]) -> list[str]:
        if not self.operators:
            return all_codes
        return [c for c in all_codes if self.operators.get(c, True)]


def load_config(path: str | None) -> Config:
    cfg = Config()
    if path is None:
        for candidate in ("mutest.yaml", "mutest.yml", ".mutest.yaml"):
            if os.path.exists(candidate):
                path = candidate
                break
    if not path or not os.path.exists(path):
        return cfg
    try:
        import yaml
    except ImportError:
        return cfg
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if isinstance(data.get("operators"), dict):
        cfg.operators = {str(k): bool(v) for k, v in data["operators"].items()}
    for key in ("workers", "timeout", "top_n"):
        if key in data:
            try:
                setattr(cfg, key, int(data[key]))
            except (TypeError, ValueError):
                pass
    for key in ("model", "verify_model"):
        if data.get(key):
            setattr(cfg, key, str(data[key]))
    return cfg
