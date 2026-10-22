"""Hybrid retrieval: vector similarity (LanceDB) + BM25 keyword (SQLite FTS5), fused with RRF.

Keyword search matters a lot for Salesforce docs: exact API names such as
`Database.executeBatch`, `WITH SECURITY_ENFORCED` or `lightning-datatable`
are often missed by embeddings alone.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .llm import LocalLLM
from .store import VectorStore

RRF_K = 60


@dataclass
class Hit:
    chunk_id: int
    doc_id: int
    score: float
    title: str
    url: str
    heading: str
    page: int | None
    kind: str
    guide: str | None
    text: str

    def citation(self) -> str:
        loc = f" (PDF page {self.page})" if self.page else ""
        heading = self.heading
        if heading == self.title:
            heading = ""
        elif heading.startswith(self.title + " > "):
            heading = heading[len(self.title) + 3:]
        return f"{self.title} > {heading}{loc} - {self.url}" if heading else f"{self.title}{loc} - {self.url}"


def _fts_query(q: str) -> str | None:
    toks = re.findall(r"[A-Za-z0-9_]+", q)
    toks = [t for t in toks if len(t) > 1][:24]
    if not toks:
        return None
    return " OR ".join(f'"{t}"' for t in toks)


class Searcher:
    def __init__(self, cfg, conn):
        self.cfg = cfg
        self.conn = conn
        self.store = VectorStore(cfg.data_dir)
        self.llm = LocalLLM(cfg)

    def keyword(self, query: str, n: int) -> list[int]:
        fq = _fts_query(query)
        if not fq:
            return []
        rows = self.conn.execute(
            "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts, 4.0, 2.0, 1.0) LIMIT ?",
            (fq, n),
        ).fetchall()
        return [int(r[0]) for r in rows]

    def vector(self, query: str, n: int) -> list[int]:
        vec = self.llm.embed_query(query)
        return [cid for cid, _ in self.store.search(vec, n)]

    def search(self, query: str, k: int | None = None, guide: str | None = None,
               source: str | None = None, mode: str = "hybrid") -> list[Hit]:
        k = k or int(self.cfg["search"]["default_k"])
        n = max(int(self.cfg["search"]["candidates"]), k * 4)
        if guide or source:
            n *= 4  # filtering happens after retrieval, so pull more candidates
        ranked: dict[int, float] = {}
        lists = []
        if mode in ("hybrid", "vector"):
            try:
                lists.append(self.vector(query, n))
            except Exception:  # noqa: BLE001 - LM Studio down: degrade to keyword search
                if mode == "vector":
                    raise
        if mode in ("hybrid", "keyword"):
            lists.append(self.keyword(query, n))
        for lst in lists:
            for rank, cid in enumerate(lst):
                ranked[cid] = ranked.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
        if not ranked:
            return []
        ids = sorted(ranked, key=ranked.get, reverse=True)
        hits = self._hydrate(ids, ranked)
        if guide:
            g = guide.lower()
            hits = [h for h in hits if g in (h.guide or "").lower() or g in h.url.lower() or g in h.title.lower()]
        if source:
            srcs = {r[0] for r in self.conn.execute("SELECT id FROM docs WHERE source=?", (source,))}
            hits = [h for h in hits if h.doc_id in srcs]
        # at most 3 chunks from the same page, so results cover more ground
        per_doc: dict[int, int] = {}
        out = []
        for h in hits:
            if per_doc.get(h.doc_id, 0) >= 3:
                continue
            per_doc[h.doc_id] = per_doc.get(h.doc_id, 0) + 1
            out.append(h)
            if len(out) >= k:
                break
        return out

    def _hydrate(self, ids: list[int], scores: dict[int, float]) -> list[Hit]:
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"""SELECT c.id, c.doc_id, c.heading, c.page, c.kind, c.text, d.title, d.url, d.guide
                FROM chunks c JOIN docs d ON d.id=c.doc_id WHERE c.id IN ({q})""", ids
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        out = []
        for cid in ids:
            r = by_id.get(cid)
            if r is None:
                continue
            out.append(Hit(cid, r["doc_id"], round(scores[cid], 5), r["title"] or "", r["url"], r["heading"] or "",
                           r["page"], r["kind"], r["guide"], r["text"]))
        return out

    # ---------------------------------------------------------------- reading
    def read_doc(self, url_or_id: str, start: int = 0, max_chunks: int = 12) -> dict:
        if str(url_or_id).isdigit():
            d = self.conn.execute("SELECT * FROM docs WHERE id=?", (int(url_or_id),)).fetchone()
        else:
            d = self.conn.execute("SELECT * FROM docs WHERE url=? OR canonical=?", (url_or_id, url_or_id)).fetchone()
        if d is None:
            return {"error": f"Not in the index: {url_or_id}"}
        rows = self.conn.execute(
            "SELECT ord, heading, page, text FROM chunks WHERE doc_id=? AND kind='text' ORDER BY ord", (d["id"],)
        ).fetchall()
        sel = rows[start:start + max_chunks]
        return {
            "doc_id": d["id"],
            "title": d["title"],
            "url": d["url"],
            "summary": d["summary"],
            "total_chunks": len(rows),
            "returned": f"{start}-{start + len(sel) - 1}" if sel else "none",
            "next_start": start + len(sel) if start + len(sel) < len(rows) else None,
            "content": "\n\n".join(
                (f"## {r['heading']}" + (f"  (page {r['page']})" if r["page"] else "") + "\n" if r["heading"] else "")
                + r["text"] for r in sel
            ),
        }

    def list_guides(self, contains: str | None = None, limit: int = 200) -> list[dict]:
        sql = """SELECT guide, source, COUNT(*) AS pages, MIN(title) AS sample_title FROM docs
                 WHERE skipped=0 AND indexed_hash IS NOT NULL"""
        params: list = []
        if contains:
            sql += " AND (guide LIKE ? OR title LIKE ?)"
            params += [f"%{contains}%", f"%{contains}%"]
        sql += " GROUP BY guide, source ORDER BY pages DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params)]


def hits_to_dicts(hits: list[Hit], max_chars: int | None = None) -> list[dict]:
    out = []
    for h in hits:
        d = asdict(h)
        if max_chars and len(d["text"]) > max_chars:
            d["text"] = d["text"][:max_chars] + " ..."
        d["citation"] = h.citation()
        out.append(d)
    return out
