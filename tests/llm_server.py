"""A fake LLM endpoint that speaks the documented wire formats of Ollama, OpenAI and Anthropic.

It validates what a real server would (auth headers, required fields) and records every request,
so tests can assert on exactly what Astronomo sent.  It proves the client is self-consistent
with the *documented* APIs; it does not replace running against a real Ollama (see
``test_ollama_live.py``).
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeLLMServer:
    def __init__(self, reply: Callable[[str], str] | str = "The candidate looks reliable.", *, api_key: str | None = "sk-test",
                 port: int = 0):
        self._reply = reply if callable(reply) else (lambda _prompt, text=reply: text)
        self.api_key = api_key  # required Bearer / x-api-key for the OpenAI & Anthropic routes
        self.requests: list[dict] = []
        self.models = ["llama3.1:latest", "qwen2.5-coder:7b"]
        self.failures: list[tuple[int, dict]] = []  # popped first, one per request
        self.ollama_prompt_tokens: int | None = None  # force a reported prompt size
        self.ollama_omit_counts = False
        self.finish_reason_length = False
        self._server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # -- lifecycle ------------------------------------------------------------ #
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> "FakeLLMServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "FakeLLMServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- inspection ----------------------------------------------------------- #
    def chat_requests(self) -> list[dict]:
        return [r for r in self.requests if r["method"] == "POST"]

    def prompts(self) -> list[str]:
        """The user-message text of every chat request, whatever the provider."""
        return [r["prompt"] for r in self.chat_requests()]

    # -- protocol ------------------------------------------------------------- #
    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _send(self, status: int, payload: dict) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                outer.requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers), "body": None, "prompt": ""})
                if self.path == "/api/tags":
                    self._send(200, {"models": [{"name": m} for m in outer.models]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                headers = {k.lower(): v for k, v in self.headers.items()}
                prompt, system = outer._extract(self.path, body)
                outer.requests.append(
                    {"method": "POST", "path": self.path, "headers": headers, "body": body, "prompt": prompt, "system": system}
                )
                if outer.failures:
                    status, payload = outer.failures.pop(0)
                    return self._send(status, payload)
                if self.path == "/api/chat":
                    return self._ollama(body, prompt, system)
                if self.path == "/v1/chat/completions":
                    if outer.api_key and headers.get("authorization") != f"Bearer {outer.api_key}":
                        return self._send(401, {"error": {"message": "Incorrect API key provided"}})
                    return self._openai(body, prompt, system)
                if self.path == "/v1/messages":
                    if outer.api_key and headers.get("x-api-key") != outer.api_key:
                        return self._send(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
                    if "anthropic-version" not in headers or "max_tokens" not in body:
                        return self._send(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "missing required field"}})
                    return self._anthropic(body, prompt, system)
                self._send(404, {"error": "unknown route"})

            def _ollama(self, body, prompt, system):
                if body["model"] not in outer.models and f"{body['model']}:latest" not in outer.models:
                    return self._send(404, {"error": f"model '{body['model']}' not found, try pulling it first"})
                text = outer._reply(prompt)
                out = {"model": body["model"], "message": {"role": "assistant", "content": text}, "done": True,
                       "done_reason": "length" if outer.finish_reason_length else "stop"}
                if not outer.ollama_omit_counts:
                    out["prompt_eval_count"] = outer.ollama_prompt_tokens or (len(prompt) + len(system)) // 4
                    out["eval_count"] = len(text) // 4 + 1
                self._send(200, out)

            def _openai(self, body, prompt, system):
                text = outer._reply(prompt)
                self._send(200, {
                    "choices": [{"message": {"role": "assistant", "content": text},
                                 "finish_reason": "length" if outer.finish_reason_length else "stop"}],
                    "usage": {"prompt_tokens": (len(prompt) + len(system)) // 4, "completion_tokens": len(text) // 4 + 1},
                })

            def _anthropic(self, body, prompt, system):
                text = outer._reply(prompt)
                self._send(200, {
                    "type": "message", "role": "assistant", "content": [{"type": "text", "text": text}],
                    "stop_reason": "max_tokens" if outer.finish_reason_length else "end_turn",
                    "usage": {"input_tokens": (len(prompt) + len(system)) // 4, "output_tokens": len(text) // 4 + 1},
                })

        return Handler

    @staticmethod
    def _extract(path: str, body: dict) -> tuple[str, str]:
        if path == "/v1/messages":
            return body["messages"][-1]["content"], body.get("system", "")
        msgs = body.get("messages") or []
        system = next((m["content"] for m in msgs if m["role"] == "system"), "")
        prompt = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        return prompt, system
