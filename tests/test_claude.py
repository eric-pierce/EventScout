"""Exercises ClaudeLLM through the real Anthropic SDK against a local mock server (no API key needed)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from eventscout.config import ClaudeConfig, Home
from eventscout.llm.base import LLMError
from eventscout.llm.claude import ClaudeLLM


def _sse(text: str, stop_reason: str = "end_turn") -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 50, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                           "usage": {"output_tokens": 20}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(body)}\n\n" for name, body in events).encode()


@pytest.fixture
def mock_api(monkeypatch):
    captured: dict = {}
    reply = {"text": json.dumps({"results": [{"id": 1, "score": 88, "reason": "jazz", "tags": ["jazz"]}]}),
             "stop": "end_turn"}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            captured["path"] = self.path
            captured["headers"] = dict(self.headers)
            captured["body"] = json.loads(body)
            payload = _sse(reply["text"], reply["stop"])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    yield captured, reply
    server.shutdown()


def test_json_request_shape(mock_api):
    captured, _ = mock_api
    llm = ClaudeLLM(ClaudeConfig(), Home())
    schema = {"type": "object", "properties": {"results": {"type": "array"}}, "required": ["results"],
              "additionalProperties": False}
    out = llm.json(system="sys prompt", user="rate these", schema=schema, task="score")

    assert out["results"][0]["score"] == 88
    body = captured["body"]
    assert captured["path"].startswith("/v1/messages")
    assert body["model"] == "claude-opus-5"
    assert body["stream"] is True
    assert body["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": schema}}
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in captured["headers"].get("anthropic-beta", "")
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert llm.usage.input_tokens == 50 and llm.usage.output_tokens == 20


def test_truncated_output_raises(mock_api):
    _, reply = mock_api
    reply.update(text='{"results": [', stop="max_tokens")
    llm = ClaudeLLM(ClaudeConfig(fallbacks=None), Home())
    with pytest.raises(LLMError, match="max_tokens"):
        llm.json(system="s", user="u", schema={"type": "object"}, task="extract")


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    llm = ClaudeLLM(ClaudeConfig(), Home())
    with pytest.raises(LLMError):
        llm.json(system="s", user="u", schema={"type": "object"}, task="extract")


def test_ollama_request_shape():
    import httpx

    from eventscout.config import OllamaConfig
    from eventscout.llm.ollama import OllamaLLM

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": '{"events": []}'}, "done_reason": "stop",
                                         "prompt_eval_count": 12, "eval_count": 3})

    llm = OllamaLLM(OllamaConfig(base_url="http://ollama:11434", model="qwen3:14b"))
    llm.client = httpx.Client(base_url="http://ollama:11434", transport=httpx.MockTransport(handler))
    schema = {"type": "object"}
    assert llm.json(system="s", user="u", schema=schema, task="extract") == {"events": []}
    assert seen["url"] == "http://ollama:11434/api/chat"
    assert seen["body"]["format"] == schema and seen["body"]["stream"] is False and seen["body"]["model"] == "qwen3:14b"
    assert llm.usage.input_tokens == 12
