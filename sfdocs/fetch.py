"""Stage 2 - download every discovered URL (HTML and PDF), politely and resumably.

Also contains the optional Playwright renderer for pages that are JavaScript shells.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import logging
import random
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import db

log = logging.getLogger("sfdocs.fetch")


def tls_verify():
    """Verify certificates against the OS trust store. resources.docs.salesforce.com (the PDFs)
    omits its intermediate certificate; the OS store completes the chain, certifi does not."""
    try:
        import ssl

        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return True


def url_key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()


def raw_path_for(data_dir: Path, url: str, kind: str) -> Path:
    key = url_key(url)
    sub = data_dir / "raw" / key[:2]
    sub.mkdir(parents=True, exist_ok=True)
    # HTML is stored gzipped: rendered pages are ~0.5 MB each and compress ~10x.
    return sub / f"{key}.{'pdf' if kind == 'pdf' else 'html.gz'}"


def write_raw(path: Path, body: bytes) -> None:
    path.write_bytes(gzip.compress(body, 6) if path.suffix == ".gz" else body)


def read_raw(path: Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == ".gz" else data


BLOCK_STATUSES = (403, 429)  # developer.salesforce.com's bot protection answers bursts with 403


class HostBlocked(Exception):
    pass


BLOCKED = "blocked"  # sentinel error from the renderer


class HostThrottle:
    """Per-host politeness delay, plus a host-wide pause when the host starts refusing us.

    A 403/429 pauses every request to that host for 60s, doubling up to 15 min while the block
    lasts; any success resets it. Once a host has been blocked for `max_block_s` in total, the
    remaining URLs for it are deferred (left pending, attempts untouched) for the next run."""

    def __init__(self, delay: float, max_block_s: float = 7200):
        self.delay = delay
        self.max_block_s = max_block_s
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.last: dict[str, float] = defaultdict(float)
        self.paused_until: dict[str, float] = defaultdict(float)
        self.backoff: dict[str, float] = defaultdict(float)
        self.blocked_s: dict[str, float] = defaultdict(float)
        self.gave_up: set[str] = set()

    async def wait(self, host: str):
        async with self.locks[host]:
            if host in self.gave_up:
                raise HostBlocked(host)
            pause = self.paused_until[host] - time.monotonic()
            if pause > 0:
                await asyncio.sleep(pause)
            gap = self.delay - (time.monotonic() - self.last[host])
            if gap > 0:
                await asyncio.sleep(gap)
            self.last[host] = time.monotonic()

    def blocked(self, host: str, status: int, retry_after: float = 0):
        if self.paused_until[host] > time.monotonic():
            return  # concurrent requests already triggered this pause
        self.backoff[host] = min(900, max(60, self.backoff[host] * 2, retry_after))
        self.blocked_s[host] += self.backoff[host]
        if self.blocked_s[host] > self.max_block_s:
            self.gave_up.add(host)
            log.warning("%s still refusing requests (HTTP %d) after %.0f min of back-off; deferring its "
                        "remaining URLs to the next run", host, status, self.blocked_s[host] / 60)
            return
        self.paused_until[host] = time.monotonic() + self.backoff[host]
        log.warning("%s answered HTTP %d - pausing it for %.0fs", host, status, self.backoff[host])

    def ok(self, host: str):
        self.backoff[host] = 0
        self.blocked_s[host] = 0


def _pending(conn, cfg, refresh: bool, limit: int):
    refresh_s = float(cfg["crawl"].get("refresh_after_days") or 0) * 86400
    cutoff = db.now() - refresh_s if refresh_s else 0
    # Rows from sources that have since been disabled stay in the manifest but aren't fetched.
    enabled = [s["name"] for s in cfg["sources"] if s.get("enabled", True)]
    sql = f"""SELECT id, url, kind, etag, last_modified, raw_path, content_hash FROM docs
             WHERE skipped=0 AND attempts < 6 AND source IN ({",".join("?" * len(enabled))})
               AND (fetched_at IS NULL"""
    params: list = list(enabled)
    if refresh:
        sql += " OR 1=1"
    elif refresh_s:
        sql += " OR fetched_at < ?"
        params.append(cutoff)
    sql += ") ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


async def _fetch_one(client, throttle, row, data_dir, retries):
    url = row["url"]
    headers = {}
    path = Path(row["raw_path"]) if row["raw_path"] else None
    if path and path.exists():
        if row["etag"]:
            headers["If-None-Match"] = row["etag"]
        if row["last_modified"]:
            headers["If-Modified-Since"] = row["last_modified"]
    host = urlparse(url).netloc
    last_err = None
    for attempt in range(retries):
        try:
            await throttle.wait(host)
        except HostBlocked:
            return {"status": -1, "error": "host blocked; deferred"}
        try:
            r = await client.get(url, headers=headers)
        except httpx.HTTPError as e:
            last_err = f"{type(e).__name__}: {e}"
            await asyncio.sleep(min(60, 2 ** attempt + random.random()))
            continue
        if r.status_code in BLOCK_STATUSES:
            last_err = f"HTTP {r.status_code}"
            throttle.blocked(host, r.status_code, float(r.headers.get("Retry-After", 0) or 0))
            continue
        throttle.ok(host)
        if r.status_code == 304:
            return {"status": 304}
        if r.status_code in (500, 502, 503, 504):
            wait = float(r.headers.get("Retry-After", 0) or 0) or min(120, 2 ** (attempt + 1))
            last_err = f"HTTP {r.status_code}"
            await asyncio.sleep(wait + random.random())
            continue
        if r.status_code != 200:
            return {"status": r.status_code, "error": f"HTTP {r.status_code}"}
        body = r.content
        ctype = r.headers.get("content-type", "")
        kind = "pdf" if ("pdf" in ctype or body[:4] == b"%PDF") else "html"
        out = raw_path_for(data_dir, url, kind)
        write_raw(out, body)
        return {
            "status": 200,
            "kind": kind,
            "path": str(out),
            "hash": hashlib.sha256(body).hexdigest(),
            "etag": r.headers.get("etag"),
            "last_modified": r.headers.get("last-modified"),
        }
    return {"status": 0, "error": last_err or "failed"}


async def _fetch_all(cfg, conn, refresh=False, limit=0):
    c = cfg["crawl"]
    rows = _pending(conn, cfg, refresh, limit or int(c.get("max_docs") or 0))
    if not rows:
        log.info("Nothing to fetch.")
        return {"fetched": 0}
    log.info("Fetching %d documents with concurrency %d", len(rows), c["concurrency"])
    throttle = HostThrottle(float(c["per_host_delay_s"]), float(c.get("max_block_minutes", 120)) * 60)
    sem = asyncio.Semaphore(int(c["concurrency"]))
    limits = httpx.Limits(max_connections=int(c["concurrency"]) * 2)
    counts = defaultdict(int)
    done = 0
    started = time.monotonic()

    async with httpx.AsyncClient(
        headers={"User-Agent": c["user_agent"]},
        timeout=float(c["timeout_s"]),
        follow_redirects=True,
        limits=limits,
        http2=False,
        verify=tls_verify(),
    ) as client:

        async def worker(row):
            async with sem:
                return row, await _fetch_one(client, throttle, row, cfg.data_dir, int(c["retries"]))

        tasks = [asyncio.create_task(worker(r)) for r in rows]
        for fut in asyncio.as_completed(tasks):
            row, res = await fut
            done += 1
            st = res["status"]
            if st == 200:
                changed = res["hash"] != row["content_hash"]
                conn.execute(
                    """UPDATE docs SET fetched_at=?, http_status=200, etag=?, last_modified=?, raw_path=?,
                       content_hash=?, kind=?, error=NULL, attempts=0,
                       needs_render=CASE WHEN ? THEN 0 ELSE needs_render END, updated_at=? WHERE id=?""",
                    (db.now(), res["etag"], res["last_modified"], res["path"], res["hash"], res["kind"],
                     changed, db.now(), row["id"]),
                )
                counts["changed" if changed else "unchanged"] += 1
            elif st == 304:
                conn.execute("UPDATE docs SET fetched_at=?, http_status=304, error=NULL WHERE id=?", (db.now(), row["id"]))
                counts["not_modified"] += 1
            elif st in (404, 410):
                conn.execute(
                    "UPDATE docs SET fetched_at=?, http_status=?, error=?, skipped=1 WHERE id=?",
                    (db.now(), st, res.get("error"), row["id"]),
                )
                counts["gone"] += 1
            elif st == -1:
                counts["deferred_host_blocked"] += 1  # left pending; the next run retries it
            else:
                conn.execute(
                    "UPDATE docs SET http_status=?, error=?, attempts=attempts+1 WHERE id=?",
                    (st, res.get("error"), row["id"]),
                )
                counts["error"] += 1
            if done % 200 == 0 or done == len(rows):
                conn.commit()
                rate = done / max(1e-6, time.monotonic() - started)
                log.info("  fetched %d/%d (%.1f/s) %s", done, len(rows), rate, dict(counts))
    conn.commit()
    return dict(counts)


def fetch(cfg, conn, refresh=False, limit=0) -> dict:
    return asyncio.run(_fetch_all(cfg, conn, refresh, limit))


# ---------------------------------------------------------------- rendering

async def _render_all(cfg, conn, limit=0):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        n = conn.execute("SELECT COUNT(*) FROM docs WHERE needs_render=1").fetchone()[0]
        if n:
            log.warning(
                "%d pages need a headless browser but Playwright is not installed. "
                "Run: pip install 'sfdocs[render]' && playwright install chromium", n,
            )
        return {"rendered": 0, "skipped_no_playwright": n}

    sql = "SELECT id, url, attempts FROM docs WHERE needs_render=1 AND skipped=0 ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    if not rows:
        return {"rendered": 0}
    log.info("Rendering %d JavaScript pages", len(rows))
    sem = asyncio.Semaphore(int(cfg["render"]["concurrency"]))
    c = cfg["crawl"]
    throttle = HostThrottle(float(c["per_host_delay_s"]), float(c.get("max_block_minutes", 120)) * 60)
    counts = defaultdict(int)
    async with async_playwright() as p:
        rc = cfg["render"]
        launch = {"headless": True, "args": list(rc.get("launch_args") or [])}
        if rc.get("executable_path"):
            launch["executable_path"] = rc["executable_path"]
        browser = await p.chromium.launch(**launch)
        ctx = await browser.new_context(user_agent=cfg["crawl"]["user_agent"])

        async def one(row):
            async with sem:
                host = urlparse(row["url"]).netloc
                for _ in range(int(c["retries"])):
                    try:
                        await throttle.wait(host)
                    except HostBlocked:
                        return row, None, BLOCKED
                    row_, html, err = await one_page(row)
                    if err is not BLOCKED:
                        throttle.ok(host)
                        return row_, html, err
                    throttle.blocked(host, 403)
                return row, None, BLOCKED

        async def one_page(row):
            page = await ctx.new_page()
            try:
                resp = await page.goto(row["url"], wait_until="networkidle",
                                       timeout=int(float(cfg["render"].get("timeout_s", 90)) * 1000))
                if resp is not None and resp.status in BLOCK_STATUSES:
                    return row, None, BLOCKED
                # Help/LWC pages often nest content in shadow DOM; give components a moment.
                await page.wait_for_timeout(1500)
                html = await page.evaluate(
                    """() => {
                      const walk = (root) => {
                        let out = '';
                        for (const el of root.querySelectorAll('*')) {
                          if (el.shadowRoot) out += walk(el.shadowRoot) + el.shadowRoot.innerHTML;
                        }
                        return out;
                      };
                      // Inside <body>: parsers drop anything after </html>.
                      const html = document.documentElement.outerHTML;
                      const extra = '<div id="sfdocs-shadow">' + walk(document) + '</div>';
                      const i = html.lastIndexOf('</body>');
                      return i < 0 ? html + extra : html.slice(0, i) + extra + html.slice(i);
                    }"""
                )
                return row, html, None
            except Exception as e:  # noqa: BLE001
                return row, None, str(e)
            finally:
                await page.close()

        for fut in asyncio.as_completed([asyncio.create_task(one(r)) for r in rows]):
            row, html, err = await fut
            if html:
                body = html.encode("utf-8")
                out = raw_path_for(cfg.data_dir, row["url"], "html")
                write_raw(out, body)
                conn.execute(
                    "UPDATE docs SET raw_path=?, content_hash=?, needs_render=2, error=NULL, updated_at=? WHERE id=?",
                    (str(out), hashlib.sha256(body).hexdigest(), db.now(), row["id"]),
                )
                counts["rendered"] += 1
            elif err is BLOCKED:
                counts["deferred_host_blocked"] += 1  # stays needs_render=1 for the next run
            else:
                # Retry on later runs; after 3 failures fall back to whatever the plain download had.
                conn.execute(
                    """UPDATE docs SET error=?, attempts=attempts+1,
                       needs_render=CASE WHEN attempts+1 >= 3 THEN 2 ELSE 1 END WHERE id=?""",
                    (f"render: {err}", row["id"]),
                )
                counts["error"] += 1
            if sum(counts.values()) % 50 == 0:
                conn.commit()
        await browser.close()
    conn.commit()
    return dict(counts)


def render(cfg, conn, limit=0) -> dict:
    if not cfg["render"].get("enabled", True):
        return {"rendered": 0, "disabled": True}
    return asyncio.run(_render_all(cfg, conn, limit))
