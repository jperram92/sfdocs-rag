"""Stage 3 - turn raw HTML / PDF into clean Markdown with headings, code and page markers."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

from . import db
from .fetch import read_raw, url_key

log = logging.getLogger("sfdocs.extract")

NOISE_SELECTORS = [
    "script", "style", "noscript", "svg", "iframe", "form", "button",
    "header", "footer", "nav", "aside",
    "[role=navigation]", "[role=banner]", "[role=contentinfo]", "[aria-hidden=true]",
    ".breadcrumb", ".breadcrumbs", ".toc", ".sidebar", ".feedback", ".cookie", "#onetrust-consent-sdk",
    "doc-xml-content > .version-picker", "dx-header", "dx-footer", "dx-sidebar", "doc-header",
    "dx-toc", "dx-feedback", "dx-scroll-to-top", "dx-version-picker", "dx-dropdown",
]
# Candidates for the main content container, most specific first.
MAIN_SELECTORS = [
    "doc-content", "#doc-content", ".doc-content", "doc-xml-content", "div.body", "#maincontent ~ *",
    "main article", "article", "main", "[role=main]", "#sfdocs-shadow", "body",
]


class _MD(MarkdownConverter):
    def convert_pre(self, el, text, *args, **kwargs):  # keep code verbatim
        code = el.get_text("\n")
        lang = ""
        cls = " ".join(el.get("class", []) + (el.code.get("class", []) if el.find("code") else []))
        m = re.search(r"(?:language|lang)-(\w+)", cls)
        if m:
            lang = m.group(1)
        return f"\n```{lang}\n{code.strip()}\n```\n"


def _md(soup_el) -> str:
    return _MD(heading_style="ATX", bullets="-", strip=["img", "a"]).convert_soup(soup_el)


def _clean_md(md: str) -> str:
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    return md.strip()


def html_to_markdown(html: str) -> tuple[str, str | None, str | None]:
    """Returns (markdown, title, canonical_url)."""
    soup = BeautifulSoup(html, "lxml")
    canonical = None
    link = soup.find("link", rel="canonical")
    if link and link.get("href"):
        canonical = link["href"].strip()
    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.split("|")[0].strip()

    for sel in NOISE_SELECTORS:
        for el in soup.select(sel):
            el.decompose()

    best, best_len = None, 0
    for sel in MAIN_SELECTORS:
        try:
            els = soup.select(sel)
        except Exception:  # noqa: BLE001 - odd selectors on odd pages
            continue
        for el in els:
            n = len(el.get_text(" ", strip=True))
            # Prefer the first specific container that has real content.
            if n > 200 and sel != "body":
                best, best_len = el, n
                break
            if n > best_len:
                best, best_len = el, n
        if best is not None and best_len > 200 and sel != "body":
            break
    if best is None:
        return "", title, canonical

    h1 = best.find("h1") or soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(" ", strip=True)
    md = _clean_md(_md(best))
    # Drop leftover version-switcher lines such as "Summer '26 (API version 67.0)".
    md = "\n".join(l for l in md.splitlines() if not re.fullmatch(r"\s*\[?(Spring|Summer|Winter) ['’]\d\d \(API version [\d.]+\)\]?.*", l))
    if title and not md.lstrip().startswith("#"):
        md = f"# {title}\n\n{md}"
    return md, title, canonical


def pdf_to_markdown(path: Path) -> tuple[str, str | None]:
    try:
        import pymupdf as fitz
    except ImportError:  # older releases
        import fitz

    parts = []
    title = None
    with fitz.open(path) as doc:
        meta_title = (doc.metadata or {}).get("title")
        if meta_title:
            title = meta_title.strip()
        toc = doc.get_toc(simple=True)  # [level, title, page]
        by_page: dict[int, list[tuple[int, str]]] = {}
        for level, t, page in toc:
            by_page.setdefault(page, []).append((level, t))
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text")
            text = re.sub(r"[ \t]+\n", "\n", text)
            heads = "".join(f"\n{'#' * min(6, lvl + 1)} {t.strip()}\n" for lvl, t in by_page.get(i, []))
            parts.append(f"\n<!-- page {i} -->\n{heads}\n{text.strip()}\n")
    md = "".join(parts)
    if title:
        md = f"# {title}\n{md}"
    return _clean_md(md), title


def text_path_for(data_dir: Path, url: str) -> Path:
    key = url_key(url)
    sub = data_dir / "text" / key[:2]
    sub.mkdir(parents=True, exist_ok=True)
    return sub / f"{key}.md"


def extract(cfg, conn, limit=0) -> dict:
    min_chars = int(cfg["render"]["min_text_chars"])
    render_on = bool(cfg["render"].get("enabled", True))
    sql = """SELECT id, url, kind, raw_path, content_hash, needs_render, render_mode, canonical FROM docs
             WHERE content_hash IS NOT NULL AND skipped=0 AND needs_render != 1
               AND (extracted_hash IS NULL OR extracted_hash != content_hash) ORDER BY id"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    counts = {"extracted": 0, "queued_render": 0, "duplicate": 0, "empty": 0, "error": 0}
    for i, row in enumerate(rows, 1):
        try:
            raw = Path(row["raw_path"])
            if row["kind"] == "pdf":
                md, title = pdf_to_markdown(raw)
                canonical = None
            else:
                if render_on and row["render_mode"] == "always" and row["needs_render"] == 0:
                    conn.execute("UPDATE docs SET needs_render=1 WHERE id=?", (row["id"],))
                    counts["queued_render"] += 1
                    continue
                md, title, canonical = html_to_markdown(read_raw(raw).decode("utf-8", errors="replace"))
                body_len = len(re.sub(r"\s+", " ", md))
                if body_len < min_chars and render_on and row["needs_render"] == 0:
                    conn.execute("UPDATE docs SET needs_render=1 WHERE id=?", (row["id"],))
                    counts["queued_render"] += 1
                    continue
            if len(md.strip()) < 80:
                conn.execute("UPDATE docs SET skipped=1, error='empty after extraction' WHERE id=?", (row["id"],))
                counts["empty"] += 1
                continue
            if canonical and canonical != row["url"]:
                dup = conn.execute(
                    "SELECT id FROM docs WHERE (url=? OR canonical=?) AND id<? AND skipped=0 LIMIT 1",
                    (canonical, canonical, row["id"]),
                ).fetchone()
                if dup:
                    conn.execute("UPDATE docs SET skipped=1, canonical=?, error='duplicate of canonical' WHERE id=?",
                                 (canonical, row["id"]))
                    counts["duplicate"] += 1
                    continue
            out = text_path_for(cfg.data_dir, row["url"])
            out.write_text(md, encoding="utf-8")
            conn.execute(
                """UPDATE docs SET text_path=?, extracted_hash=?, title=COALESCE(?, title), canonical=?,
                   updated_at=? WHERE id=?""",
                (str(out), row["content_hash"], title, canonical, db.now(), row["id"]),
            )
            counts["extracted"] += 1
        except Exception as e:  # noqa: BLE001
            log.warning("extract failed for %s: %s", row["url"], e)
            conn.execute("UPDATE docs SET error=? WHERE id=?", (f"extract: {e}", row["id"]))
            counts["error"] += 1
        if i % 500 == 0:
            conn.commit()
            log.info("  extracted %d/%d %s", i, len(rows), counts)
    conn.commit()
    return counts
