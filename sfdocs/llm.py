"""Thin client for LM Studio (or any OpenAI-compatible server): embeddings + chat."""
from __future__ import annotations

import logging
import re
import time

from openai import OpenAI

log = logging.getLogger("sfdocs.llm")


class LocalLLM:
    def __init__(self, cfg):
        c = cfg["llm"]
        self.c = c
        self.client = OpenAI(base_url=c["base_url"], api_key=c.get("api_key") or "lm-studio",
                             timeout=float(c.get("timeout_s", 300)))

    # ------------------------------------------------------------ embeddings
    def _embed(self, texts: list[str]) -> list[list[float]]:
        for attempt in range(5):
            try:
                resp = self.client.embeddings.create(model=self.c["embed_model"], input=texts)
                return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
            except Exception as e:  # noqa: BLE001
                if attempt == 4:
                    raise
                log.warning("embedding call failed (%s); retrying", e)
                time.sleep(2 ** attempt)
        return []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        pre = self.c.get("embed_doc_prefix") or ""
        cap = int(self.c.get("max_embed_chars") or 6000)
        bs = int(self.c.get("embed_batch_size") or 64)
        out: list[list[float]] = []
        for i in range(0, len(texts), bs):
            out += self._embed([pre + t[:cap] for t in texts[i:i + bs]])
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._embed([(self.c.get("embed_query_prefix") or "") + text])[0]

    # ------------------------------------------------------------ chat
    def chat(self, system: str, user: str, max_tokens: int = 600, temperature: float = 0.1) -> str:
        for attempt in range(4):
            try:
                resp = self.client.chat.completions.create(
                    model=self.c["chat_model"],
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                text = resp.choices[0].message.content or ""
                # Reasoning models (qwen3, deepseek-r1...) may emit <think> blocks; drop them.
                return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
            except Exception as e:  # noqa: BLE001
                if attempt == 3:
                    raise
                log.warning("chat call failed (%s); retrying", e)
                time.sleep(2 ** attempt)
        return ""

    def health(self) -> dict:
        models = [m.id for m in self.client.models.list().data]
        return {
            "base_url": self.c["base_url"],
            "models_loaded": models,
            "chat_model_ok": self.c["chat_model"] in models,
            "embed_model_ok": self.c["embed_model"] in models,
        }
