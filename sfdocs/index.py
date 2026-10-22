"""Stage 5 - chunk, (optionally) contextualise, embed and store everything for retrieval."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db
from .chunk import chunk_markdown
from .condense import chunk_context
from .llm import LocalLLM
from .store import VectorStore

log = logging.getLogger("sfdocs.index")

FLUSH_AT = 256  # chunks per embedding flush (spans many small pages)


def _embed_text(title: str, heading: str, context: str | None, text: str) -> str:
    head = " > ".join(x for x in (title, heading) if x)
    parts = [head] if head else []
    if context:
        parts.append(context)
    parts.append(text)
    return "\n".join(parts)


def index(cfg, conn, limit=0, rebuild=False) -> dict:
    store = VectorStore(cfg.data_dir)
    if rebuild:
        conn.execute("UPDATE docs SET indexed_hash=NULL")
        conn.execute("DELETE FROM chunks_fts")
        conn.execute("DELETE FROM chunks")
        conn.commit()
        if store.table is not None:
            store.db.drop_table("chunks")
            store._tbl = None

    sql = """SELECT id, url, title, text_path, summary, content_hash FROM docs
             WHERE skipped=0 AND extracted_hash IS NOT NULL AND extracted_hash=content_hash
               AND (indexed_hash IS NULL OR indexed_hash != content_hash) ORDER BY id"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    if not rows:
        return {"indexed_docs": 0}

    llm = LocalLLM(cfg)
    ccfg = cfg["chunk"]
    use_ctx = bool(cfg["condense"].get("chunk_context"))
    ctx_pool = ThreadPoolExecutor(max(1, int(cfg["condense"]["concurrency"]))) if use_ctx else None
    log.info("Indexing %d documents (chunk_context=%s)", len(rows), use_ctx)

    pending_vectors: list[tuple[int, int, str]] = []  # (chunk_id, doc_id, text_to_embed)
    pending_docs: list[tuple[int, str]] = []          # (doc_id, content_hash)
    totals = {"indexed_docs": 0, "chunks": 0}

    def flush():
        if not pending_vectors:
            for doc_id, h in pending_docs:
                conn.execute("UPDATE docs SET indexed_hash=? WHERE id=?", (h, doc_id))
            pending_docs.clear()
            conn.commit()
            return
        vecs = llm.embed_documents([t for _, _, t in pending_vectors])
        store.add([{"chunk_id": cid, "doc_id": did, "vector": v}
                   for (cid, did, _), v in zip(pending_vectors, vecs)])
        for doc_id, h in pending_docs:
            conn.execute("UPDATE docs SET indexed_hash=?, updated_at=? WHERE id=?", (h, db.now(), doc_id))
        conn.commit()
        totals["indexed_docs"] += len(pending_docs)
        pending_vectors.clear()
        pending_docs.clear()

    for i, row in enumerate(rows, 1):
        md = Path(row["text_path"]).read_text("utf-8")
        title = row["title"] or ""
        chunks = chunk_markdown(md, int(ccfg["max_chars"]), int(ccfg["overlap_chars"]))

        db.delete_chunks_for_doc(conn, row["id"])
        store.delete_docs([row["id"]])

        contexts = [None] * len(chunks)
        if use_ctx and chunks:
            contexts = list(ctx_pool.map(lambda c: chunk_context(llm, title, row["summary"], c.text), chunks))

        records = []
        if row["summary"]:
            records.append(("summary", 0, "Summary", None, None, row["summary"]))
        for c, ctx in zip(chunks, contexts):
            records.append(("text", c.ord + 1, c.heading, c.page, ctx, c.text))

        for kind, ord_, heading, page, ctx, text in records:
            cur = conn.execute(
                "INSERT INTO chunks(doc_id, ord, kind, heading, page, context, text) VALUES (?,?,?,?,?,?,?)",
                (row["id"], ord_, kind, heading, page, ctx, text),
            )
            cid = cur.lastrowid
            conn.execute("INSERT INTO chunks_fts(title, heading, text, chunk_id) VALUES (?,?,?,?)",
                         (title, heading or "", text, cid))
            pending_vectors.append((cid, row["id"], _embed_text(title, heading or "", ctx, text)))
        totals["chunks"] += len(records)
        pending_docs.append((row["id"], row["content_hash"]))

        if len(pending_vectors) >= FLUSH_AT:
            flush()
        if i % 200 == 0:
            log.info("  indexed %d/%d docs, %d chunks", i, len(rows), totals["chunks"])
    flush()
    if ctx_pool:
        ctx_pool.shutdown()
    if store.maybe_build_ann_index():
        log.info("Built ANN index over %d vectors", store.count())
    return totals
