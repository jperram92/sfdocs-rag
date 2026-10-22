from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent))
import fake_lmstudio  # noqa: E402
import mock_site  # noqa: E402

# Deployment settings (e.g. SFDOCS_DATA_DIR=/data in the Docker image) must not leak into the
# hermetic tests. They are kept aside so the live tests can still use your LM Studio address.
DEPLOY_ENV = {k: os.environ.pop(k) for k in
              ("SFDOCS_CONFIG", "SFDOCS_DATA_DIR", "LMSTUDIO_BASE_URL", "SFDOCS_CHAT_MODEL", "SFDOCS_EMBED_MODEL")
              if k in os.environ}

# Local test servers must never go through a corporate/agent proxy.
for _k in ("NO_PROXY", "no_proxy"):
    os.environ[_k] = ",".join(filter(None, [os.environ.get(_k), "127.0.0.1", "localhost"]))


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="run tests against the real Salesforce docs site")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live") or os.environ.get("SFDOCS_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test: pass --live (or SFDOCS_LIVE=1) to hit developer.salesforce.com")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    mock_site.build(root, mock_site.make_pdf())
    srv, base = mock_site.serve(root)
    mock_site.write_dynamic(root, base)
    yield {"root": root, "base": base}
    srv.shutdown()


@pytest.fixture(scope="session")
def lm():
    srv, url = fake_lmstudio.start()
    yield url
    srv.shutdown()


def _browser_launch_error() -> str | None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return "playwright is not installed (pip install -e '.[render]')"

    async def go():
        async with async_playwright() as p:
            kw = {"headless": True}
            if os.environ.get("SFDOCS_CHROMIUM_PATH"):
                kw["executable_path"] = os.environ["SFDOCS_CHROMIUM_PATH"]
            b = await p.chromium.launch(**kw)
            await b.close()

    try:
        asyncio.run(go())
        return None
    except Exception as e:  # noqa: BLE001
        return f"cannot launch Chromium: {str(e).splitlines()[0]}"


@pytest.fixture(scope="session")
def browser_ok():
    """True when a headless Chromium can be launched. With SFDOCS_REQUIRE_BROWSER=1 a missing
    browser is a test failure instead of a skip (use this in CI / Docker)."""
    err = _browser_launch_error()
    if err and os.environ.get("SFDOCS_REQUIRE_BROWSER") == "1":
        pytest.fail(f"SFDOCS_REQUIRE_BROWSER=1 but {err}")
    return err is None


def write_config(path: Path, site_base: str, lm_url: str, data_dir: Path, **overrides) -> Path:
    cfg = {
        "data_dir": str(data_dir),
        "llm": {"base_url": lm_url},
        "sources": [
            {"name": "dev-atlas", "type": "sitemap", "urls": [f"{site_base}/sitemap-index.xml"],
             "include": [r"/docs/atlas\.en-us\.(?!\d)"]},
            {"name": "dev-docs", "type": "sitemap", "urls": [f"{site_base}/sitemap-index.xml"],
             "include": ["/docs/platform/"]},
            {"name": "pdf-guides", "type": "pdf_index", "urls": [f"{site_base}/index.json"],
             "skip_if_atlas_present": True},
            {"name": "help", "type": "robots", "hosts": [site_base], "include": ["language=en_US"],
             "render": "always"},
        ],
        "crawl": {"per_host_delay_s": 0, "retries": 2, "concurrency": 4},
        "render": {"concurrency": 2, "timeout_s": 30},
        "condense": {"chunk_context": True},
    }
    for k, v in overrides.items():
        cfg[k] = {**cfg.get(k, {}), **v} if isinstance(v, dict) else v
    path.write_text(yaml.safe_dump(cfg))
    return path


def run_cli(config: Path, *args, check=True) -> subprocess.CompletedProcess:
    env = {**os.environ, "SFDOCS_CONFIG": str(config)}
    p = subprocess.run([sys.executable, "-m", "sfdocs.cli", "--config", str(config), *args],
                       capture_output=True, text=True, env=env, timeout=600)
    if check and p.returncode != 0:
        raise AssertionError(f"sfdocs {' '.join(args)} failed:\n{p.stdout}\n{p.stderr}")
    return p


@pytest.fixture(scope="session")
def pipeline(tmp_path_factory, site, lm, browser_ok):
    """One full `sfdocs run-all` over the mock site. Shared by the pipeline, render and MCP tests."""
    work = tmp_path_factory.mktemp("run")
    config = write_config(work / "config.yaml", site["base"], lm, work / "data",
                          render={"enabled": browser_ok})
    out = run_cli(config, "run-all")
    report = json.loads(out.stdout)
    conn = sqlite3.connect(work / "data" / "manifest.sqlite3")
    conn.row_factory = sqlite3.Row
    yield {"config": config, "report": report, "conn": conn, "data": work / "data", "site": site,
           "browser": browser_ok}
    conn.close()


def doc(conn, url_part: str):
    return conn.execute("SELECT * FROM docs WHERE url LIKE ?", (f"%{url_part}%",)).fetchone()


def doc_text(conn, url_part: str) -> str:
    d = doc(conn, url_part)
    rows = conn.execute("SELECT text FROM chunks WHERE doc_id=? ORDER BY ord", (d["id"],)).fetchall()
    return "\n".join(r["text"] for r in rows)
