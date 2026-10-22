"""End-to-end batch job over the mock site: discover -> fetch -> extract -> condense -> index -> search."""
import json
import time

from conftest import doc, doc_text, run_cli

from sfdocs.config import load_config
from sfdocs.search import Searcher
from sfdocs import db as sfdb


def test_discovery_filters_and_sources(pipeline):
    d = pipeline["report"]["discover"]
    assert d["dev-atlas"]["matched"] == 2          # old-version atlas URL dropped by include regex
    assert d["dev-docs"]["matched"] == 3
    assert d["help"]["matched"] == 1               # found via robots.txt -> help sitemap
    assert d["pdf-guides"]["matched"] == 1         # apexcode PDF skipped: guide already crawled as HTML
    c = pipeline["conn"]
    assert doc(c, "atlas.en-us.248.0") is None
    assert doc(c, "apexcode.pdf") is None and doc(c, "api_rest.pdf") is not None


def test_fetch_handles_404_and_duplicates(pipeline):
    c = pipeline["conn"]
    gone = doc(c, "removed.html")
    assert gone["http_status"] == 404 and gone["skipped"] == 1
    alias = doc(c, "apex_batch_alias.htm")
    assert alias["skipped"] == 1 and "duplicate" in alias["error"]


def test_html_and_pdf_are_indexed_with_summaries(pipeline):
    c = pipeline["conn"]
    for part in ("apex_batch.htm", "datatable.html", "api_rest.pdf"):
        d = doc(c, part)
        assert d["indexed_hash"] == d["content_hash"], part
        assert d["summary"].startswith("Summary of ") and "<think>" not in d["summary"]
    pdf_pages = {r[0] for r in c.execute(
        "SELECT page FROM chunks WHERE doc_id=? AND kind='text'", (doc(c, "api_rest.pdf")["id"],))}
    assert pdf_pages == {1, 2, 3}
    # chunk_context enabled in the test config -> every text chunk has an LLM context line
    assert c.execute("SELECT COUNT(*) FROM chunks WHERE kind='text' AND (context IS NULL OR context='')").fetchone()[0] == 0
    assert "Database.Batchable" in doc_text(c, "apex_batch.htm")


def test_hybrid_search_finds_the_right_passages(pipeline):
    cfg = load_config(str(pipeline["config"]))
    s = Searcher(cfg, sfdb.connect(cfg.data_dir))
    top = s.search("how many batch jobs can be queued or active concurrently", k=3)
    assert "apex_batch.htm" in top[0].url and "Governor Limits" in top[0].heading
    pdf = s.search("composite subrequests single call", k=2, guide="api_rest")
    assert pdf and pdf[0].url.endswith("api_rest.pdf") and pdf[0].page == 2
    kw = s.search("lightning-datatable draftValues", k=1, mode="keyword")
    assert "datatable.html" in kw[0].url


def test_rerun_is_a_noop_and_refresh_picks_up_changes(pipeline, tmp_path):
    cfg_path = pipeline["config"]
    again = json.loads(run_cli(cfg_path, "run-all").stdout)
    assert again["fetch"].get("changed", 0) == 0
    assert again["index"]["indexed_docs"] == 0

    page = pipeline["site"]["root"] / "docs/platform/lwc/guide/datatable.html"
    original = page.read_text()
    try:
        time.sleep(1.1)  # Last-Modified has 1-second resolution
        page.write_text(original.replace("key-field attribute", "key-field attribute (changed-zebra-marker)"))
        r = json.loads(run_cli(cfg_path, "run-all", "--refresh").stdout)
        assert r["fetch"]["changed"] == 1
        assert r["index"]["indexed_docs"] == 1
        cfg = load_config(str(cfg_path))
        hits = Searcher(cfg, sfdb.connect(cfg.data_dir)).search("changed-zebra-marker", k=1, mode="keyword")
        assert hits and "datatable.html" in hits[0].url
    finally:
        page.write_text(original)


def test_cli_search_and_status(pipeline):
    out = run_cli(pipeline["config"], "search", "Database.executeBatch scope size", "--json", "-k", "2").stdout
    hits = json.loads(out)
    assert hits and hits[0]["citation"].endswith("apex_batch.htm")
    status = json.loads(run_cli(pipeline["config"], "status").stdout)
    assert status["indexed"] >= 3 and status["chunks"] > 5
    doctor = json.loads(run_cli(pipeline["config"], "doctor").stdout)
    assert doctor["chat_model_ok"] and doctor["embed_model_ok"]
