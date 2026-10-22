"""Stage 1 - discover every documentation URL from sitemaps, robots.txt and the PDF index."""
from __future__ import annotations

import gzip
import json
import logging
import re
import time
from urllib.parse import urljoin, urlparse

import httpx
from lxml import etree

from . import db
from .fetch import tls_verify

log = logging.getLogger("sfdocs.discover")

ATLAS_META_RE = re.compile(r"/docs/(atlas\.[a-z]{2}-[a-z]{2}\.(?:\d+\.\d+\.)?([A-Za-z0-9_]+)\.meta)/")


class _Client(httpx.Client):
    """Sync client with the crawler's pacing: a delay between requests, and on 403/429 (bot
    protection) a back-off from 60s doubling to 15 min, for up to max_block_s per request."""

    def __init__(self, *a, delay: float = 0, max_block_s: float = 0, **kw):
        super().__init__(*a, **kw)
        self.delay, self.max_block_s, self._last = delay, max_block_s, 0.0

    def paced_get(self, url: str) -> httpx.Response:
        gap = self.delay - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        try:
            return self.get(url)
        finally:
            self._last = time.monotonic()


def _client(cfg) -> _Client:
    c = cfg["crawl"]
    return _Client(
        headers={"User-Agent": c["user_agent"], "Accept-Encoding": "gzip, deflate"},
        timeout=c["timeout_s"],
        follow_redirects=True,
        verify=tls_verify(),
        delay=float(c.get("per_host_delay_s") or 0),
        max_block_s=float(c.get("max_block_minutes", 120)) * 60,
    )


def _get(client: _Client, url: str, retries: int = 3) -> bytes | None:
    attempt = blocked_s = 0
    backoff = 60.0
    while attempt < retries:
        try:
            r = client.paced_get(url)
            if r.status_code == 200:
                body = r.content
                if url.endswith(".gz") and body[:2] == b"\x1f\x8b":
                    body = gzip.decompress(body)
                return body
            log.warning("GET %s -> %s", url, r.status_code)
            if r.status_code in (403, 429) and blocked_s < client.max_block_s:
                wait = max(backoff, float(r.headers.get("Retry-After", 0) or 0))
                log.warning("  host is refusing requests; retrying in %.0fs", wait)
                time.sleep(wait)
                blocked_s += wait
                backoff = min(900.0, backoff * 2)
                continue  # a block isn't the URL's fault: doesn't use up a retry
            if r.status_code in (403, 404, 410, 429):
                return None
        except httpx.HTTPError as e:
            log.warning("GET %s failed (%s), attempt %d", url, e, attempt + 1)
        attempt += 1
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def walk_sitemap(client, url: str, seen: set[str] | None = None, depth: int = 0):
    """Yield (loc, lastmod) for every page URL reachable from a sitemap or sitemap index."""
    seen = seen if seen is not None else set()
    if url in seen or depth > 6:
        return
    seen.add(url)
    body = _get(client, url)
    if not body:
        return
    try:
        root = etree.fromstring(body, parser=etree.XMLParser(recover=True, huge_tree=True))
    except etree.XMLSyntaxError:
        log.warning("Not XML: %s", url)
        return
    if root is None:
        return
    kind = _local(root.tag)
    for node in root:
        name = _local(node.tag) if isinstance(node.tag, str) else ""
        if name not in ("sitemap", "url"):
            continue
        loc = lastmod = None
        for child in node:
            if not isinstance(child.tag, str):
                continue
            cname = _local(child.tag)
            if cname == "loc" and child.text:
                loc = child.text.strip()
            elif cname == "lastmod" and child.text:
                lastmod = child.text.strip()
        if not loc:
            continue
        if kind == "sitemapindex" or name == "sitemap":
            yield from walk_sitemap(client, loc, seen, depth + 1)
        else:
            yield loc, lastmod


def robots_sitemaps(client, host: str) -> list[str]:
    body = _get(client, urljoin(host.rstrip("/") + "/", "robots.txt"))
    if not body:
        return []
    out = []
    for line in body.decode("utf-8", "replace").splitlines():
        if line.lower().startswith("sitemap:"):
            out.append(line.split(":", 1)[1].strip())
    return out


def _filters(src):
    inc = [re.compile(p) for p in src.get("include") or []]
    exc = [re.compile(p) for p in src.get("exclude") or []]

    def ok(url: str) -> bool:
        if inc and not any(p.search(url) for p in inc):
            return False
        return not any(p.search(url) for p in exc)

    return ok


def _guide_from_url(url: str) -> str | None:
    m = ATLAS_META_RE.search(url)
    if m:
        return m.group(2)
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "docs":
        return "/".join(parts[1:3])
    return parts[0] if parts else None


def _kind(url: str) -> str:
    return "pdf" if urlparse(url).path.lower().endswith(".pdf") else "html"


def _walk_pdf_index(obj, out: list[dict]):
    """The docs index JSON is nested; collect every dict that carries a pdf_url."""
    if isinstance(obj, dict):
        if isinstance(obj.get("pdf_url"), str) and obj["pdf_url"].startswith("http"):
            out.append(obj)
        for v in obj.values():
            _walk_pdf_index(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_pdf_index(v, out)


def discover(cfg, conn, only: list[str] | None = None) -> dict:
    stats: dict[str, dict] = {}
    max_docs = int(cfg["crawl"].get("max_docs") or 0)
    with _client(cfg) as client:
        for src in cfg["sources"]:
            name = src["name"]
            if not src.get("enabled", True) or (only and name not in only):
                continue
            ok = _filters(src)
            new = seen = 0
            render_mode = "always" if src.get("render") == "always" else "auto"
            log.info("Discovering source %s (%s)", name, src["type"])

            if src["type"] in ("sitemap", "robots"):
                roots = list(src.get("urls") or [])
                for host in src.get("hosts") or []:
                    found = robots_sitemaps(client, host)
                    log.info("  robots.txt of %s lists %d sitemaps", host, len(found))
                    roots += found
                visited: set[str] = set()
                for root in roots:
                    for loc, lastmod in walk_sitemap(client, root, visited):
                        if not ok(loc):
                            continue
                        seen += 1
                        new += db.upsert_discovered(
                            conn, loc, name, _kind(loc), guide=_guide_from_url(loc),
                            lastmod=lastmod, render_mode=render_mode,
                        )
                        if seen % 2000 == 0:
                            conn.commit()
                            log.info("  %s: %d urls so far", name, seen)
                        if max_docs and seen >= max_docs:
                            break
                    if max_docs and seen >= max_docs:
                        break

            elif src["type"] == "pdf_index":
                atlas_guides = set()
                if src.get("skip_if_atlas_present", True):
                    # Only guides that will actually be crawled as HTML: enabled sources, not skipped.
                    enabled = [s["name"] for s in cfg["sources"] if s.get("enabled", True)]
                    marks = ",".join("?" * len(enabled))
                    for (url,) in conn.execute(
                            f"SELECT url FROM docs WHERE url LIKE '%/docs/atlas.%' AND skipped=0 "
                            f"AND source IN ({marks})", enabled):
                        m = ATLAS_META_RE.search(url)
                        if m:
                            atlas_guides.add(m.group(2))
                for idx_url in src.get("urls") or []:
                    body = _get(client, idx_url)
                    if not body:
                        continue
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError:
                        log.warning("Docs index is not JSON: %s", idx_url)
                        continue
                    entries: list[dict] = []
                    _walk_pdf_index(data, entries)
                    by_url = {}
                    for e in entries:
                        by_url.setdefault(e["pdf_url"], e)
                    for pdf_url, e in by_url.items():
                        if not ok(pdf_url):
                            continue
                        meta_id = str(e.get("_id") or e.get("id") or "")
                        m = re.search(r"atlas\.[a-z]{2}-[a-z]{2}\.(?:\d+\.\d+\.)?([A-Za-z0-9_]+)\.meta", meta_id)
                        if m and m.group(1) in atlas_guides:
                            continue  # same guide already crawled page-by-page as HTML
                        seen += 1
                        guide = e.get("deliverable") or (m.group(1) if m else None)
                        new += db.upsert_discovered(
                            conn, pdf_url, name, "pdf", guide=guide, title=e.get("title"),
                            lastmod=e.get("updated"),
                        )
                        if max_docs and seen >= max_docs:
                            break
            else:
                log.error("Unknown source type %r for %s", src["type"], name)
            conn.commit()
            stats[name] = {"matched": seen, "new": new}
            log.info("  %s: %d matched, %d new", name, seen, new)
    return stats
