"""Opt-in tests against the REAL Salesforce documentation site.

    pytest --live -m live -v          (or SFDOCS_LIVE=1)

They crawl a small slice of each source and check that real pages extract into
substantial Markdown. If LM Studio is running at llm.base_url, they also index the
slice and run a real search. Run these after installing and again whenever results
look off - they catch Salesforce changing its sitemaps or page templates.
"""
import pytest

from sfdocs import db as sfdb
from sfdocs.config import load_config

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    from conftest import DEPLOY_ENV
    cfg = load_config(DEPLOY_ENV.get("SFDOCS_CONFIG"))  # project config.yaml: the real sources
    for key, env in (("base_url", "LMSTUDIO_BASE_URL"), ("chat_model", "SFDOCS_CHAT_MODEL"),
                     ("embed_model", "SFDOCS_EMBED_MODEL")):
        if DEPLOY_ENV.get(env):
            cfg["llm"][key] = DEPLOY_ENV[env]
    cfg.data_dir = tmp_path_factory.mktemp("live-data")
    for sub in ("raw", "text"):
        (cfg.data_dir / sub).mkdir()
    cfg["crawl"]["max_docs"] = 12
    cfg["crawl"]["per_host_delay_s"] = 0.5
    return cfg, sfdb.connect(cfg.data_dir)


def test_live_discover(live):
    from sfdocs.discover import discover
    cfg, conn = live
    names = [s["name"] for s in cfg["sources"] if s.get("enabled", True)]
    stats = discover(cfg, conn, names)
    for name in names:
        assert stats[name]["matched"] > 0, f"source {name} found no URLs - has the sitemap moved?"


def test_live_fetch_and_extract(live, browser_ok):
    from sfdocs.extract import extract
    from sfdocs.fetch import fetch, render
    cfg, conn = live
    cfg["render"]["enabled"] = browser_ok
    f = fetch(cfg, conn)
    assert f.get("changed", 0) > 0, f
    extract(cfg, conn)
    if browser_ok:
        render(cfg, conn)
        extract(cfg, conn)
    rows = conn.execute("SELECT url, text_path, kind FROM docs WHERE text_path IS NOT NULL").fetchall()
    assert rows, "nothing extracted"
    lengths = {r["url"]: len(open(r["text_path"], encoding="utf-8").read()) for r in rows}
    good = [u for u, n in lengths.items() if n > 400]
    assert len(good) >= 0.8 * len(lengths), f"too many thin extractions: {lengths}"
    if any(r["kind"] == "pdf" for r in rows):
        assert any(n > 20_000 for u, n in lengths.items() if u.endswith(".pdf")), "PDF extracted too little text"


def test_live_index_and_search(live):
    from sfdocs.llm import LocalLLM
    cfg, conn = live
    try:
        LocalLLM(cfg).health()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"LM Studio not reachable at {cfg['llm']['base_url']}: {e}")
    from sfdocs.index import index
    from sfdocs.search import Searcher
    res = index(cfg, conn)
    assert res["chunks"] > 0
    title = conn.execute("SELECT title FROM docs WHERE indexed_hash IS NOT NULL LIMIT 1").fetchone()[0]
    hits = Searcher(cfg, conn).search(title, k=3)
    assert hits and any(h.title == title for h in hits)
