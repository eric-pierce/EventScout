"""Local LLM via Ollama's native chat API with JSON-schema constrained output."""

from __future__ import annotations

import json

import httpx

from eventscout.config import OllamaConfig
from eventscout.llm.base import LLM, LLMError


class OllamaLLM(LLM):
    name = "ollama"
    supports_web_search = False

    def __init__(self, cfg: OllamaConfig):
        super().__init__()
        self.cfg = cfg
        self.client = httpx.Client(base_url=cfg.base_url.rstrip("/"), timeout=cfg.timeout_seconds)

    def json(self, *, system: str, user: str, schema: dict, task: str) -> dict:
        body = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user + "\n\nRespond with JSON only."},
            ],
            "stream": False,
            "format": schema,
            "think": self.cfg.think,
            "options": {"temperature": 0, "num_ctx": self.cfg.num_ctx},
        }
        try:
            resp = self.client.post("/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama unreachable at {self.cfg.base_url}: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"Ollama HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        self.usage.add(task=task, inp=data.get("prompt_eval_count", 0) or 0, out=data.get("eval_count", 0) or 0)
        if data.get("done_reason") == "length":
            raise LLMError("Ollama output truncated (raise ollama.num_ctx)")
        content = (data.get("message") or {}).get("content", "")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Ollama returned invalid JSON: {exc}: {content[:200]}") from exc
