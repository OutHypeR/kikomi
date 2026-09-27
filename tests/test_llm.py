"""Checks the Anthropic request shape and stream parsing against a local fake server."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from kikomi.llm import AnthropicLLM

EVENTS = [
    ("message_start", {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
        "model": "claude-opus-5", "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi Ana! "}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Good to hear you."}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 8}}),
    ("message_stop", {"type": "message_stop"}),
]


@pytest.fixture
def fake_api(monkeypatch):
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["beta"] = self.headers.get("anthropic-beta")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for name, data in EVENTS:
                self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    yield seen
    server.shutdown()


@pytest.mark.parametrize("fallbacks", [True, False])
def test_stream_request_and_text(fake_api, fallbacks):
    llm = AnthropicLLM(model="claude-opus-5", effort="low", fallbacks=fallbacks)

    async def run():
        return [t async for t in llm.stream("You are Nova.", [{"role": "user", "content": "Ana: hi nova"}])]

    assert "".join(asyncio.run(run())) == "Hi Ana! Good to hear you."
    body = fake_api["body"]
    assert body["model"] == "claude-opus-5" and body["stream"] is True
    assert body["output_config"] == {"effort": "low"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["messages"] == [{"role": "user", "content": "Ana: hi nova"}]
    if fallbacks:
        assert body["fallbacks"] == "default"
        assert "server-side-fallback-2026-07-01" in (fake_api["beta"] or "")
    else:
        assert "fallbacks" not in body
