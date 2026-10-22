"""SQLite manifest: every discovered URL, its pipeline state, and the text chunks.

Each pipeline stage records the content hash it last processed, so re-running any
stage only touches documents whose content changed. That makes the batch job
resumable after a crash and cheap to re-run on a schedule.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT UNIQUE NOT NULL,
    source          TEXT NOT NULL,
    kind            TEXT NOT NULL,            -- html | pdf
    guide           TEXT,                      -- guide / product label
    title           TEXT,
    canonical       TEXT,
    lastmod         TEXT,                      -- from sitemap
    render_mode     TEXT DEFAULT 'auto',       -- auto | always
    -- fetch
    fetched_at      REAL,
    http_status     INTEGER,
    etag            TEXT,
    last_modified   TEXT,
    content_hash    TEXT,
    raw_path        TEXT,
    needs_render    INTEGER DEFAULT 0,         -- 0 no, 1 queued, 2 rendered
    error           TEXT,
    attempts        INTEGER DEFAULT 0,
    skipped         INTEGER DEFAULT 0,         -- duplicates, empty pages, etc.
    -- downstream stages (each stores the content_hash it processed)
    extracted_hash  TEXT,
    text_path       TEXT,
    summary         TEXT,
    summary_hash    TEXT,
    indexed_hash    TEXT,
    updated_at      REAL
);
CREATE INDEX IF NOT EXISTS docs_source ON docs(source);
CREATE INDEX IF NOT EXISTS docs_canonical ON docs(canonical);

CREATE TABLE IF NOT EXISTS chunks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id    INTEGER NOT NULL REFERENCES docs(id),
    ord       INTEGER NOT NULL,
    kind      TEXT NOT NULL DEFAULT 'text',    -- text | summary
    heading   TEXT,
    page      INTEGER,
    context   TEXT,
    text      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id, ord);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title, heading, text,
    chunk_id UNINDEXED,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(data_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(Path(data_dir) / "manifest.sqlite3"), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def now() -> float:
    return time.time()


def upsert_discovered(conn, url, source, kind, guide=None, title=None, lastmod=None, render_mode="auto") -> bool:
    """Insert a newly discovered URL. If it exists and the sitemap lastmod changed,
    mark it for refetch. Returns True if new."""
    row = conn.execute("SELECT id, lastmod FROM docs WHERE url=?", (url,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO docs(url, source, kind, guide, title, lastmod, render_mode, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (url, source, kind, guide, title, lastmod, render_mode, now()),
        )
        return True
    if lastmod and row["lastmod"] and lastmod != row["lastmod"]:
        conn.execute("UPDATE docs SET lastmod=?, fetched_at=NULL, updated_at=? WHERE id=?", (lastmod, now(), row["id"]))
    elif lastmod and not row["lastmod"]:
        conn.execute("UPDATE docs SET lastmod=? WHERE id=?", (lastmod, row["id"]))
    if guide or title:
        conn.execute(
            "UPDATE docs SET guide=COALESCE(guide, ?), title=COALESCE(title, ?) WHERE id=?", (guide, title, row["id"])
        )
    return False


def delete_chunks_for_doc(conn, doc_id: int) -> list[int]:
    ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
    if ids:
        q = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({q})", ids)
        conn.execute(f"DELETE FROM chunks WHERE id IN ({q})", ids)
    return ids


def status(conn) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "discovered": q("SELECT COUNT(*) FROM docs"),
        "fetched": q("SELECT COUNT(*) FROM docs WHERE content_hash IS NOT NULL"),
        "fetch_errors": q("SELECT COUNT(*) FROM docs WHERE error IS NOT NULL AND content_hash IS NULL AND skipped=0"),
        "skipped": q("SELECT COUNT(*) FROM docs WHERE skipped=1"),
        "awaiting_render": q("SELECT COUNT(*) FROM docs WHERE needs_render=1"),
        "extracted": q("SELECT COUNT(*) FROM docs WHERE extracted_hash IS NOT NULL AND extracted_hash=content_hash"),
        "summarised": q("SELECT COUNT(*) FROM docs WHERE summary_hash IS NOT NULL AND summary_hash=content_hash"),
        "indexed": q("SELECT COUNT(*) FROM docs WHERE indexed_hash IS NOT NULL AND indexed_hash=content_hash"),
        "chunks": q("SELECT COUNT(*) FROM chunks"),
        "by_source": {r[0]: r[1] for r in conn.execute("SELECT source, COUNT(*) FROM docs GROUP BY source")},
    }
