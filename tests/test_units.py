"""Fast unit tests: extraction, chunking, search query handling. No servers needed."""
import pytest

from sfdocs.chunk import chunk_markdown
from sfdocs.extract import html_to_markdown
from sfdocs.search import Hit, _fts_query

import mock_site


def test_html_extraction_strips_chrome_and_keeps_code():
    md, title, canonical = html_to_markdown(mock_site.ATLAS_PAGE.replace("{base}", "https://x"))
    assert title == "Using Batch Apex"
    assert canonical == "https://x/docs/atlas.en-us.apexcode.meta/apexcode/apex_batch.htm"
    assert md.startswith("# Using Batch Apex")
    assert "## Batch Apex Governor Limits" in md
    assert "```apex" in md and "Database.getQueryLocator('SELECT Id FROM Account');" in md
    for noise in ("Site header", "Table of contents", "footer links", "API version 67.0"):
        assert noise not in md, noise


def test_js_shell_extracts_almost_nothing():
    md, _, _ = html_to_markdown(mock_site.JS_PAGE)
    assert "ReplayId" not in md            # content only exists after JavaScript runs
    assert len(md) < 300                   # -> below render.min_text_chars, so it gets queued for Playwright


def test_chunker_respects_headings_and_never_splits_code():
    code = "```apex\n" + "\n".join(f"System.debug({i});" for i in range(40)) + "\n```"
    md = "# Guide\n\n## Alpha\n\n" + ("alpha text. " * 50) + "\n\n" + code + "\n\n## Beta\n\nbeta text only."
    chunks = chunk_markdown(md, max_chars=900, overlap_chars=100)
    assert all(c.text.count("```") % 2 == 0 for c in chunks), "a code fence was split"
    beta = [c for c in chunks if c.heading.endswith("Beta")]
    assert beta and "alpha" not in beta[0].text, "overlap leaked across sections"
    assert any("System.debug(39);" in c.text for c in chunks)


def test_chunker_tracks_pdf_pages():
    md = "# Guide\n<!-- page 1 -->\n## One\nfirst page text here, long enough to keep.\n<!-- page 2 -->\n## Two\nsecond page text here, long enough to keep."
    chunks = chunk_markdown(md, 4000, 0)
    assert [(c.heading.split(" > ")[-1], c.page) for c in chunks] == [("One", 1), ("Two", 2)]


def test_fts_query_is_injection_safe():
    q = _fts_query('Database.executeBatch "scope" OR -- ) (*')
    assert q == '"Database" OR "executeBatch" OR "scope" OR "OR"'
    assert _fts_query("?!") is None


def test_citation_dedupes_title():
    h = Hit(1, 1, 0.1, "Using Batch Apex", "https://x", "Using Batch Apex > Limits", None, "text", "apexcode", "t")
    assert h.citation() == "Using Batch Apex > Limits - https://x"
    h2 = Hit(1, 1, 0.1, "REST API", "https://x/a.pdf", "REST API > Composite", 2, "text", None, "t")
    assert h2.citation() == "REST API > Composite (PDF page 2) - https://x/a.pdf"


def test_host_throttle_backs_off_then_defers():
    import asyncio
    import time

    from sfdocs.fetch import HostBlocked, HostThrottle

    t = HostThrottle(delay=0, max_block_s=150)
    t.blocked("h", 403)                       # first block: 60s pause
    assert t.paused_until["h"] - time.monotonic() > 55
    t.blocked("h", 403)                       # already paused: no double counting
    assert t.blocked_s["h"] == 60
    t.paused_until["h"] = 0
    t.blocked("h", 403)                       # second block doubles to 120s -> 180s total > 150
    assert "h" in t.gave_up
    with pytest.raises(HostBlocked):
        asyncio.run(t.wait("h"))
    t2 = HostThrottle(delay=0)
    t2.blocked("h", 429)
    t2.ok("h")                                # a success resets the back-off
    assert t2.backoff["h"] == 0 and t2.blocked_s["h"] == 0
