"""Command line entry point.

    sfdocs run-all            # the full batch job: discover -> fetch -> extract -> render -> condense -> index
    sfdocs discover|fetch|extract|render|condense|index
    sfdocs search "how many batch apex jobs can be queued"
    sfdocs status | doctor
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from . import db
from .config import load_config


def _setup_logging(verbose: bool, data_dir):
    handlers = [logging.StreamHandler(sys.stderr), logging.FileHandler(data_dir / "sfdocs.log", encoding="utf-8")]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", handlers=handlers)
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv=None):
    p = argparse.ArgumentParser(prog="sfdocs", description="Salesforce documentation RAG pipeline")
    p.add_argument("--config", help="path to config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("discover", help="find every doc URL from sitemaps / PDF index")
    s.add_argument("--source", action="append", help="only these source names")
    s = sub.add_parser("fetch", help="download pending docs")
    s.add_argument("--refresh", action="store_true", help="re-check every doc (conditional GET)")
    s.add_argument("--limit", type=int, default=0)
    for name, hlp in (("extract", "HTML/PDF -> markdown"), ("render", "headless-browser JS pages"),
                      ("condense", "LLM summaries"), ("index", "chunk + embed + store")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("--limit", type=int, default=0)
        if name == "index":
            s.add_argument("--rebuild", action="store_true", help="drop and rebuild the whole index")
    s = sub.add_parser("run-all", help="full batch job (resumable)")
    s.add_argument("--source", action="append")
    s.add_argument("--refresh", action="store_true")
    s.add_argument("--skip-condense", action="store_true")
    s.add_argument("--limit", type=int, default=0, help="cap docs per stage (trial runs)")
    s = sub.add_parser("search", help="query the index from the terminal")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=5)
    s.add_argument("--guide")
    s.add_argument("--mode", default="hybrid", choices=["hybrid", "keyword", "vector"])
    s.add_argument("--json", action="store_true")
    sub.add_parser("status", help="pipeline progress")
    sub.add_parser("doctor", help="check LM Studio models and config")
    sub.add_parser("serve-mcp", help="run the MCP server on stdio")

    a = p.parse_args(argv)
    cfg = load_config(a.config)
    if getattr(a, "source", None):
        # --source narrows every stage, not just discovery (fetch only pulls enabled sources).
        for s in cfg["sources"]:
            s["enabled"] = s.get("enabled", True) and s["name"] in a.source
    if a.cmd == "serve-mcp":
        from .mcp_server import main as mcp_main
        return mcp_main()
    _setup_logging(a.verbose, cfg.data_dir)
    log = logging.getLogger("sfdocs")
    conn = db.connect(cfg.data_dir)

    if a.cmd == "discover":
        from .discover import discover
        print(json.dumps(discover(cfg, conn, a.source), indent=2))
    elif a.cmd == "fetch":
        from .fetch import fetch
        print(json.dumps(fetch(cfg, conn, a.refresh, a.limit), indent=2))
    elif a.cmd == "extract":
        from .extract import extract
        print(json.dumps(extract(cfg, conn, a.limit), indent=2))
    elif a.cmd == "render":
        from .fetch import render
        print(json.dumps(render(cfg, conn, a.limit), indent=2))
    elif a.cmd == "condense":
        from .condense import condense
        print(json.dumps(condense(cfg, conn, a.limit), indent=2))
    elif a.cmd == "index":
        from .index import index
        print(json.dumps(index(cfg, conn, a.limit, a.rebuild), indent=2))
    elif a.cmd == "run-all":
        from .condense import condense
        from .discover import discover
        from .extract import extract
        from .fetch import fetch, render
        from .index import index
        t0 = time.time()
        report = {}
        if a.limit:
            cfg["crawl"]["max_docs"] = a.limit
        steps = [
            ("discover", lambda: discover(cfg, conn, a.source)),
            ("fetch", lambda: fetch(cfg, conn, a.refresh, a.limit)),
            ("extract", lambda: extract(cfg, conn, a.limit)),
            ("render", lambda: render(cfg, conn, a.limit)),
            ("extract_rendered", lambda: extract(cfg, conn, a.limit)),
        ]
        if not a.skip_condense:
            steps.append(("condense", lambda: condense(cfg, conn, a.limit)))
        steps.append(("index", lambda: index(cfg, conn, a.limit)))
        for name, fn in steps:
            log.info("==== %s ====", name)
            try:
                report[name] = fn()
            except Exception as e:  # noqa: BLE001 - keep going; later runs resume
                log.exception("stage %s failed", name)
                report[name] = {"error": str(e)}
        report["status"] = db.status(conn)
        report["elapsed_min"] = round((time.time() - t0) / 60, 1)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_run', ?)", (json.dumps(report),))
        conn.commit()
        print(json.dumps(report, indent=2))
    elif a.cmd == "search":
        from .search import Searcher, hits_to_dicts
        hits = Searcher(cfg, conn).search(a.query, k=a.k, guide=a.guide, mode=a.mode)
        if a.json:
            print(json.dumps(hits_to_dicts(hits), indent=2))
        else:
            for i, h in enumerate(hits, 1):
                print(f"\n[{i}] {h.citation()}  (score {h.score})\n{'-' * 80}\n{h.text[:1200]}")
            if not hits:
                print("No results.")
    elif a.cmd == "status":
        print(json.dumps(db.status(conn), indent=2))
    elif a.cmd == "doctor":
        from .llm import LocalLLM
        try:
            info = LocalLLM(cfg).health()
        except Exception as e:  # noqa: BLE001
            info = {"error": f"Cannot reach {cfg['llm']['base_url']}: {e}. Is the LM Studio server running?"}
        info["config"] = str(cfg.path)
        info["data_dir"] = str(cfg.data_dir)
        print(json.dumps(info, indent=2))
        if info.get("models_loaded") and not (info["chat_model_ok"] and info["embed_model_ok"]):
            print("\nSet llm.chat_model / llm.embed_model in config.yaml to identifiers listed above.")


if __name__ == "__main__":
    main()
