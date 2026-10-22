"""A tiny stand-in for LM Studio's OpenAI-compatible server, for tests.

Embeddings are deterministic hashed bag-of-words vectors (so semantic search is
predictable); chat returns canned summaries / contexts / answers.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = 256
CHAT_MODEL = "qwen2.5-7b-instruct"
EMBED_MODEL = "text-embedding-nomic-embed-text-v1.5"


def embed(text: str) -> list[float]:
    v = [0.0] * DIM
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class _Handler(BaseHTTPRequestHandler):
    calls: dict = {"embeddings": 0, "chat": 0}

    def log_message(self, *a):
        pass

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send({"object": "list", "data": [{"id": CHAT_MODEL, "object": "model"},
                                               {"id": EMBED_MODEL, "object": "model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path.endswith("/embeddings"):
            _Handler.calls["embeddings"] += 1
            inp = body["input"]
            inp = [inp] if isinstance(inp, str) else inp
            self._send({"object": "list", "model": body["model"],
                        "data": [{"object": "embedding", "index": i, "embedding": embed(t)} for i, t in enumerate(inp)],
                        "usage": {"prompt_tokens": 1, "total_tokens": 1}})
            return
        _Handler.calls["chat"] += 1
        user = body["messages"][-1]["content"]
        if user.rstrip().endswith("Summary:"):
            m = re.search(r"Title: (.*)", user)
            # include a <think> block to prove reasoning output is stripped
            txt = f"<think>internal</think>Summary of {m.group(1) if m else 'document'}."
        elif user.rstrip().endswith("Context:"):
            m = re.search(r"Document: (.*)", user)
            txt = f"This chunk is from {m.group(1) if m else 'a document'}."
        else:
            txt = "Per [1], the documentation answers this."
        self._send({"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": txt}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


def start(host="127.0.0.1", port=0):
    srv = ThreadingHTTPServer((host, port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://{host}:{srv.server_address[1]}/v1"
