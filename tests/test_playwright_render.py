"""Real headless-browser tests: JavaScript-only pages and shadow-DOM pages must be rendered by
Playwright, extracted, indexed and searchable.

Skipped when no Chromium can be launched, unless SFDOCS_REQUIRE_BROWSER=1 (Docker/CI), where
a missing browser fails the run instead.
"""
import asyncio

import pytest
from conftest import doc, doc_text

from sfdocs import db as sfdb
from sfdocs.config import load_config
from sfdocs.search import Searcher

import mock_site


@pytest.fixture(autouse=True)
def _need_browser(browser_ok):
    if not browser_ok:
        pytest.skip("no launchable Chromium (install with: playwright install chromium)")


def test_js_only_page_is_rendered_and_indexed(pipeline):
    c = pipeline["conn"]
    d = doc(c, "events/guide/replay.html")
    assert d["needs_render"] == 2, "page was not rendered by Playwright"
    assert d["indexed_hash"] == d["content_hash"]
    assert d["title"] == "Platform Event Replay"
    text = doc_text(c, "events/guide/replay.html")
    assert "stored in the event bus for 72 hours" in text
    assert mock_site.SHELL_LEFTOVER not in text


def test_shadow_dom_help_page_is_rendered_and_indexed(pipeline):
    c = pipeline["conn"]
    d = doc(c, "help/mfa.html")
    assert d["render_mode"] == "always" and d["needs_render"] == 2
    assert d["title"] == "Set Up Multi-Factor Authentication"
    text = doc_text(c, "help/mfa.html")
    assert "Salesforce Authenticator" in text and "nebula-mfa-shadow" in text


def test_rendered_content_is_searchable(pipeline):
    cfg = load_config(str(pipeline["config"]))
    s = Searcher(cfg, sfdb.connect(cfg.data_dir))
    hits = s.search("how long are high-volume platform event messages retained for replay", k=3)
    assert any("replay.html" in h.url for h in hits)
    hits = s.search("nebula-mfa-shadow", k=1, mode="keyword")
    assert hits and "help/mfa.html" in hits[0].url
    assert pipeline["report"]["status"]["awaiting_render"] == 0


def test_render_stage_directly(site, lm, tmp_path):
    """Exercise sfdocs.fetch.render() on its own against a fresh database."""
    from conftest import write_config
    from sfdocs.discover import discover
    from sfdocs.extract import extract
    from sfdocs.fetch import fetch, render

    cfg = load_config(str(write_config(tmp_path / "c.yaml", site["base"], lm, tmp_path / "data")))
    cfg["sources"] = [s for s in cfg["sources"] if s["name"] == "dev-docs"]
    conn = sfdb.connect(cfg.data_dir)
    discover(cfg, conn)
    fetch(cfg, conn)
    first = extract(cfg, conn)
    assert first["queued_render"] == 1                     # the JS page
    out = render(cfg, conn)
    assert out == {"rendered": 1}
    second = extract(cfg, conn)
    assert second["extracted"] == 1 and second["queued_render"] == 0


def test_browser_sees_what_plain_http_cannot(site):
    """Sanity check of the premise: raw HTML lacks the content, the rendered DOM has it."""
    import os

    import httpx
    from playwright.async_api import async_playwright

    url = f"{site['base']}/docs/platform/events/guide/replay.html"
    assert "ReplayId" not in httpx.get(url).text.split("<script>")[0]

    async def go():
        async with async_playwright() as p:
            kw = {"headless": True}
            if os.environ.get("SFDOCS_CHROMIUM_PATH"):
                kw["executable_path"] = os.environ["SFDOCS_CHROMIUM_PATH"]
            b = await p.chromium.launch(**kw)
            page = await b.new_page()
            await page.goto(url)
            await page.wait_for_selector("main h1", state="attached")
            txt = await page.text_content("main")
            await b.close()
            return txt

    assert "ReplayId" in asyncio.run(go())
