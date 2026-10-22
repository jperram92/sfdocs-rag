"""Stage 4 - condense documents with the local LLM.

* doc_summary   - a dense 3-6 sentence summary per page/guide. Indexed as its own
                  chunk, so broad questions ("how do I schedule Apex?") land on the
                  right page even when no single paragraph says it plainly.
* chunk_context - (optional, slow) Anthropic-style "contextual retrieval": a 1-2
                  sentence situating context is prepended to each chunk before embedding.
                  Done inside the index stage, see index.py.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import db
from .llm import LocalLLM

log = logging.getLogger("sfdocs.condense")

SUMMARY_SYSTEM = (
    "You condense official Salesforce documentation for a search index. "
    "Write a dense, factual summary of 3-6 sentences. Name the exact features, objects, "
    "classes, methods, API names, limits, editions, permissions and release-specific facts "
    "the page covers, so a developer can tell whether this page answers their question. "
    "No preamble, no marketing language, no speculation."
)

CONTEXT_SYSTEM = (
    "You situate a chunk of Salesforce documentation within its source document for search. "
    "Reply with 1-2 sentences that say which guide/page and topic the chunk belongs to and what it "
    "is about. Reply with the context only."
)


def summarise_text(llm: LocalLLM, title: str, url: str, md: str, max_chars: int) -> str:
    body = md if len(md) <= max_chars else md[: max_chars // 2] + "\n\n[...]\n\n" + md[-max_chars // 4:]
    user = f"Title: {title}\nURL: {url}\n\n---\n{body}\n---\n\nSummary:"
    return llm.chat(SUMMARY_SYSTEM, user, max_tokens=400)


def chunk_context(llm: LocalLLM, title: str, summary: str | None, chunk_text: str) -> str:
    user = (f"Document: {title}\nDocument summary: {summary or 'n/a'}\n\n<chunk>\n{chunk_text[:3000]}\n</chunk>\n\n"
            "Context:")
    return llm.chat(CONTEXT_SYSTEM, user, max_tokens=120)


def condense(cfg, conn, limit=0) -> dict:
    if not cfg["condense"].get("doc_summary", True):
        return {"summarised": 0, "disabled": True}
    sql = """SELECT id, url, title, text_path, content_hash FROM docs
             WHERE skipped=0 AND extracted_hash IS NOT NULL AND extracted_hash=content_hash
               AND (summary_hash IS NULL OR summary_hash != content_hash) ORDER BY id"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    if not rows:
        return {"summarised": 0}
    llm = LocalLLM(cfg)
    max_chars = int(cfg["condense"]["max_input_chars"])
    workers = max(1, int(cfg["condense"]["concurrency"]))
    log.info("Summarising %d documents with %s (%d parallel)", len(rows), cfg["llm"]["chat_model"], workers)
    done = errs = 0

    def job(row):
        md = Path(row["text_path"]).read_text("utf-8")
        return row, summarise_text(llm, row["title"] or "", row["url"], md, max_chars)

    with ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(job, r) for r in rows]
        for fut in as_completed(futs):
            try:
                row, summary = fut.result()
            except Exception as e:  # noqa: BLE001
                errs += 1
                log.warning("summary failed: %s", e)
                continue
            conn.execute(
                # Clearing indexed_hash makes the index stage re-embed this doc with its new summary.
                "UPDATE docs SET summary=?, summary_hash=?, indexed_hash=NULL, updated_at=? WHERE id=?",
                (summary, row["content_hash"], db.now(), row["id"]),
            )
            done += 1
            if done % 50 == 0:
                conn.commit()
                log.info("  summarised %d/%d", done, len(rows))
    conn.commit()
    return {"summarised": done, "errors": errs}
