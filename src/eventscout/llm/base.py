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


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    web_searches: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, *, inp: int = 0, out: int = 0, cache_read: int = 0, searches: int = 0) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += inp
            self.output_tokens += out
            self.cache_read_tokens += cache_read
            self.web_searches += searches

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "web_searches": self.web_searches,
        }


class LLM(ABC):
    name: str = "llm"
    supports_web_search: bool = False

    def __init__(self) -> None:
        self.usage = Usage()

    @abstractmethod
    def json(self, *, system: str, user: str, schema: dict, task: str) -> dict:
        """Return a JSON object matching `schema`. `task` is one of extract|score|discover (tunes effort)."""

    def research(self, *, system: str, user: str) -> str:
        raise LLMError(f"{self.name} has no built-in web search")
