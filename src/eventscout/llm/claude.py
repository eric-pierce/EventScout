"""Claude implementation (Anthropic Python SDK)."""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from eventscout.config import ClaudeConfig, Home
from eventscout.llm.base import LLM, LLMError

log = logging.getLogger(__name__)

_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeLLM(LLM):
    name = "claude"
    supports_web_search = True

    def __init__(self, cfg: ClaudeConfig, home: Home):
        super().__init__()
        self.cfg = cfg
        self.home = home
        # Credentials resolve from ANTHROPIC_API_KEY (or an `ant auth login` profile).
        self.client = anthropic.Anthropic(max_retries=4)

    @staticmethod
    def _wrap(exc: Exception) -> LLMError:
        if isinstance(exc, anthropic.APIStatusError):
            return LLMError(f"Claude API error {exc.status_code}: {exc.message}")
        if isinstance(exc, anthropic.APIConnectionError):
            return LLMError(f"Claude connection error: {exc}")
        if isinstance(exc, TypeError) and "authentication" in str(exc):
            return LLMError("no Anthropic credentials: set ANTHROPIC_API_KEY")
        return LLMError(f"Claude error: {type(exc).__name__}: {exc}")

    def _common(self, effort: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": self.cfg.model, "output_config": {"effort": effort}}
        if self.cfg.fallbacks:
            kwargs["betas"] = [_FALLBACK_BETA]
            kwargs["fallbacks"] = self.cfg.fallbacks
        return kwargs

    def _record(self, resp: Any, task: str) -> None:
        u = resp.usage
        searches = 0
        stu = getattr(u, "server_tool_use", None)
        if stu is not None:
            searches = getattr(stu, "web_search_requests", 0) or 0
        self.usage.add(
            task=task,
            inp=u.input_tokens or 0,
            out=u.output_tokens or 0,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            searches=searches,
        )

    @staticmethod
    def _check(resp: Any) -> None:
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMError(f"Claude declined the request ({getattr(details, 'category', None)})")
        if resp.stop_reason == "max_tokens":
            raise LLMError("Claude response hit max_tokens (output truncated)")

    def json(self, *, system: str, user: str, schema: dict, task: str) -> dict:
        effort = {
            "extract": self.cfg.effort_extract,
            "score": self.cfg.effort_score,
            "discover": self.cfg.effort_discover,
        }.get(task, "medium")
        kwargs = self._common(effort)
        kwargs["output_config"]["format"] = {"type": "json_schema", "schema": schema}
        try:
            with self.client.beta.messages.stream(
                max_tokens=32000,
                # The system prompt is stable across calls, so cache it.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                **kwargs,
            ) as stream:
                resp = stream.get_final_message()
        except (anthropic.AnthropicError, TypeError) as exc:
            raise self._wrap(exc) from exc
        self._record(resp, task)
        self._check(resp)
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise LLMError("Claude returned no text block")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Claude returned invalid JSON: {exc}") from exc

    def research(self, *, system: str, user: str) -> str:
        tools = [{
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": self.cfg.max_web_searches,
            "user_location": {
                "type": "approximate",
                "city": self.home.city,
                "region": self.home.region,
                "country": self.home.country,
                "timezone": self.home.timezone,
            },
        }]
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        texts: list[str] = []
        found_urls: list[str] = []
        for _ in range(6):  # pause_turn continuations
            try:
                with self.client.beta.messages.stream(
                    max_tokens=32000,
                    system=system,
                    messages=messages,
                    tools=tools,
                    **self._common(self.cfg.effort_discover),
                ) as stream:
                    resp = stream.get_final_message()
            except (anthropic.AnthropicError, TypeError) as exc:
                raise self._wrap(exc) from exc
            self._record(resp, "discover")
            if resp.stop_reason == "refusal":
                self._check(resp)
            for block in resp.content:
                if block.type == "text":
                    texts.append(block.text)
                elif block.type == "web_search_tool_result" and isinstance(block.content, list):
                    found_urls.extend(r.url for r in block.content if getattr(r, "url", None))
            if resp.stop_reason == "pause_turn":
                messages = [{"role": "user", "content": user}, {"role": "assistant", "content": resp.content}]
                continue
            break
        report = "\n".join(texts)
        if found_urls:
            report += "\n\nAll URLs seen in search results:\n" + "\n".join(dict.fromkeys(found_urls))
        return report
