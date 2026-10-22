"""Configuration loading. Environment variables override the YAML file:

SFDOCS_CONFIG      path to config.yaml
SFDOCS_DATA_DIR    data directory
LMSTUDIO_BASE_URL  OpenAI-compatible base URL (default http://localhost:1234/v1)
SFDOCS_CHAT_MODEL / SFDOCS_EMBED_MODEL  model identifiers
SFDOCS_CHROMIUM_PATH  use this Chromium/Chrome binary for rendering instead of Playwright's own
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "data_dir": "./data",
    "llm": {
        "base_url": "http://localhost:1234/v1",
        "api_key": "lm-studio",
        "chat_model": "qwen2.5-7b-instruct",
        "embed_model": "text-embedding-nomic-embed-text-v1.5",
        "embed_query_prefix": "search_query: ",
        "embed_doc_prefix": "search_document: ",
        "embed_batch_size": 64,
        "max_embed_chars": 6000,
        "timeout_s": 300,
    },
    "sources": [],
    "crawl": {
        # developer.salesforce.com 403s unrecognised agents; a known client token (httpx) gets through.
        "user_agent": "sfdocs-rag/0.1 (personal documentation index) python-httpx",
        "concurrency": 8,
        "per_host_delay_s": 0.25,
        "retries": 4,
        "timeout_s": 60,
        "max_docs": 0,
        "refresh_after_days": 14,
    },
    "render": {"enabled": True, "min_text_chars": 300, "concurrency": 3, "executable_path": None,
               "launch_args": [], "timeout_s": 90},
    "chunk": {"max_chars": 4000, "overlap_chars": 400},
    "condense": {
        "doc_summary": True,
        "chunk_context": False,
        "max_input_chars": 24000,
        "concurrency": 2,
    },
    "search": {"default_k": 8, "candidates": 40},
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config(dict):
    """dict with attribute-style access to top-level sections."""

    path: Path
    data_dir: Path

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as e:
            raise AttributeError(item) from e


def find_config(explicit: str | None = None) -> Path | None:
    candidates = [explicit, os.environ.get("SFDOCS_CONFIG")]
    candidates += [Path.cwd() / "config.yaml", Path(__file__).resolve().parent.parent / "config.yaml"]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c).resolve()
    return None


def load_config(explicit: str | None = None) -> Config:
    path = find_config(explicit)
    raw = {}
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    cfg = Config(_merge(DEFAULTS, raw))
    base = path.parent if path else Path.cwd()
    cfg.path = path or base / "config.yaml"

    if os.environ.get("LMSTUDIO_BASE_URL"):
        cfg["llm"]["base_url"] = os.environ["LMSTUDIO_BASE_URL"]
    if os.environ.get("SFDOCS_CHAT_MODEL"):
        cfg["llm"]["chat_model"] = os.environ["SFDOCS_CHAT_MODEL"]
    if os.environ.get("SFDOCS_EMBED_MODEL"):
        cfg["llm"]["embed_model"] = os.environ["SFDOCS_EMBED_MODEL"]

    if os.environ.get("SFDOCS_CHROMIUM_PATH"):
        cfg["render"]["executable_path"] = os.environ["SFDOCS_CHROMIUM_PATH"]

    data_dir = Path(os.environ.get("SFDOCS_DATA_DIR") or cfg["data_dir"])
    if not data_dir.is_absolute():
        data_dir = (base / data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("raw", "text"):
        (data_dir / sub).mkdir(exist_ok=True)
    cfg.data_dir = data_dir
    return cfg
