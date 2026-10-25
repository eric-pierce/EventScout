"""Provider-agnostic LLM interface.

Everything EventScout needs from an LLM is expressed as either:
  * `json()`     - one prompt in, one schema-conforming JSON object out
  * `research()` - open-ended web research returning prose with URLs (only providers with web search)
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class LLMError(Exception):
    pass


_COUNTERS = ("calls", "input_tokens", "output_tokens", "cache_read_tokens", "web_searches")


@dataclass
class Usage:
    """Running token counts, in total and per task (extract, score, discover, ...)."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    web_searches: int = 0
    by_task: dict[str, dict[str, int]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, *, task: str = "other", inp: int = 0, out: int = 0, cache_read: int = 0, searches: int = 0) -> None:
        with self._lock:
            row = self.by_task.setdefault(task, dict.fromkeys(_COUNTERS, 0))
            for name, n in zip(_COUNTERS, (1, inp, out, cache_read, searches)):
                setattr(self, name, getattr(self, name) + n)
                row[name] += n

    def as_dict(self) -> dict:
        with self._lock:
            return {**{name: getattr(self, name) for name in _COUNTERS},
                    "by_task": {task: dict(row) for task, row in self.by_task.items()}}


def usage_delta(after: dict, before: dict | None) -> dict:
    """Usage between two `Usage.as_dict()` snapshots (e.g. one run's share of a long-lived client)."""
    before = before or {}

    def diff(a: dict, b: dict) -> dict:
        return {name: max(0, a.get(name, 0) - b.get(name, 0)) for name in _COUNTERS}

    tasks = {task: diff(row, before.get("by_task", {}).get(task, {})) for task, row in after.get("by_task", {}).items()}
    return {**diff(after, before), "by_task": {t: row for t, row in tasks.items() if row["calls"]}}


class LLM(ABC):
    name: str = "llm"
    supports_web_search: bool = False

    def __init__(self) -> None:
        self.usage = Usage()

    @abstractmethod
    def json(self, *, system: str, user: str, schema: dict, task: str) -> dict:
        """Return a JSON object matching `schema`. `task` (extract|score|discover) tunes effort and labels
        the token counts."""

    def research(self, *, system: str, user: str) -> str:
        raise LLMError(f"{self.name} has no built-in web search")
