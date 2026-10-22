"""MCP server exposing the Salesforce docs index to Claude Code (or any MCP client).

Register with Claude Code:
    claude mcp add salesforce-docs --scope user -- /path/to/.venv/bin/sfdocs-mcp
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP

from . import db
from .config import load_config
from .search import Searcher, hits_to_dicts

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout is the MCP channel

INSTRUCTIONS = """Local index of the official Salesforce documentation (developer guides, API references,
LWC, Apex, Agentforce, Data Cloud, PDFs and, if enabled, Help articles).
Use search_salesforce_docs first; it returns the most relevant passages with citations.
Use read_salesforce_doc to read a whole page/guide around a hit when you need more context.
Prefer these results over memory for Salesforce limits, API signatures, release behaviour and
setup steps, and cite the URLs you relied on."""

mcp = FastMCP("salesforce-docs", instructions=INSTRUCTIONS)
_state: dict = {}


def _searcher() -> Searcher:
    if "s" not in _state:
        cfg = load_config()
        _state["cfg"] = cfg
        _state["s"] = Searcher(cfg, db.connect(cfg.data_dir))
    return _state["s"]


@mcp.tool()
def search_salesforce_docs(query: str, k: int = 8, guide: Optional[str] = None,
                           mode: str = "hybrid") -> dict:
    """Search the official Salesforce documentation.

    Args:
        query: Natural-language question or exact API/class/method/field names
               (e.g. "Database.executeBatch scope size limit", "lightning-datatable inline editing").
        k: Number of passages to return (1-25).
        guide: Optional filter on guide/product, matched against guide id, URL and title
               (e.g. "apexcode", "lwc", "api_rest", "data-cloud", "agentforce").
        mode: "hybrid" (default), "keyword" (exact terms) or "vector" (semantic only).

    Returns passages with title, heading, url, PDF page and a ready-made citation.
    """
    k = max(1, min(25, int(k)))
    hits = _searcher().search(query, k=k, guide=guide, mode=mode)
    if not hits:
        return {"query": query, "results": [], "note": "No matches. Try different keywords or remove the guide filter."}
    return {"query": query, "results": hits_to_dicts(hits, max_chars=3500)}


@mcp.tool()
def read_salesforce_doc(url_or_doc_id: str, start_chunk: int = 0, max_chunks: int = 12) -> dict:
    """Read an indexed Salesforce documentation page or PDF guide in order, chunk by chunk.

    Args:
        url_or_doc_id: A URL or doc_id from search results.
        start_chunk: Index to start from (use next_start from the previous call to page forward).
        max_chunks: How many chunks to return (each is roughly 1,000 tokens).
    """
    return _searcher().read_doc(url_or_doc_id, int(start_chunk), max(1, min(40, int(max_chunks))))


@mcp.tool()
def list_salesforce_guides(contains: Optional[str] = None, limit: int = 100) -> list[dict]:
    """List the guides/products in the index with page counts. Useful for choosing a `guide` filter."""
    return _searcher().list_guides(contains, int(limit))


@mcp.tool()
def ask_salesforce_docs(question: str, k: int = 8, guide: Optional[str] = None) -> dict:
    """Retrieve passages and have the LOCAL LLM (LM Studio) draft a cited answer.

    Use search_salesforce_docs when you want to reason over the sources yourself;
    use this when a quick local first-pass answer is enough.
    """
    s = _searcher()
    hits = s.search(question, k=k, guide=guide)
    if not hits:
        return {"answer": "No relevant documentation found in the index.", "sources": []}
    ctx = "\n\n".join(f"[{i + 1}] {h.citation()}\n{h.text[:3000]}" for i, h in enumerate(hits))
    answer = s.llm.chat(
        "Answer strictly from the provided Salesforce documentation excerpts. Cite sources as [n]. "
        "If the excerpts do not contain the answer, say so plainly.",
        f"Question: {question}\n\nExcerpts:\n{ctx}\n\nAnswer:",
        max_tokens=900,
    )
    return {"answer": answer, "sources": [{"n": i + 1, "citation": h.citation()} for i, h in enumerate(hits)]}


@mcp.tool()
def salesforce_docs_index_status() -> dict:
    """Report how much of the documentation has been crawled and indexed."""
    s = _searcher()
    return db.status(s.conn)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
