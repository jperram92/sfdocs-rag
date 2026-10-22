# sfdocs-rag — the official Salesforce docs, local, as a tool for your agents

A batch pipeline that crawls the official Salesforce documentation (every developer guide page,
every downloadable PDF guide, and optionally Help articles), condenses it with a **local LLM in
LM Studio**, indexes it for retrieval, and exposes it to **Claude Code** (or any MCP client) as tools.

```
 sitemaps / robots.txt / PDF index
            │  discover
            ▼
   SQLite manifest (every URL + state) ──► fetch (async, polite, resumable, conditional GET)
            │                                   │ JS-only pages ─► render (Playwright, optional)
            ▼                                   ▼
        extract  HTML → Markdown (keeps headings + code), PDF → Markdown (TOC headings, page markers)
            ▼
        condense LM Studio chat model writes a dense summary per page/guide (+ optional per-chunk context)
            ▼
        index    heading-aware chunks → LM Studio embeddings → LanceDB vectors + SQLite FTS5 keywords
            ▼
   MCP server (stdio) ── hybrid search (vector + BM25, RRF fusion) ──► Claude Code
```

Everything runs on your machine. No cloud APIs, no vector-DB server to run (LanceDB and SQLite are files).

## What gets crawled

| Source (config name) | What it is | Default |
|---|---|---|
| `dev-atlas` | The classic developer guides on developer.salesforce.com — Apex, SOQL/SOSL, REST/SOAP/Bulk/Metadata/Tooling APIs, Object Reference, Visualforce, etc. English, latest release only. | on |
| `dev-docs` | The newer docs site — LWC, Agentforce, Data Cloud, Commerce, Marketing Cloud, Mobile SDK, Salesforce DX, Industries… | on |
| `pdf-guides` | Every `pdf_url` in Salesforce's docs index. A PDF is skipped when the same guide was already crawled page-by-page as HTML, so you don't index everything twice. | on |
| `help` | help.salesforce.com articles (admin/setup). JavaScript-rendered, so it needs Playwright, and it's the largest source. | **off** — enable after the dev docs are done |

Edit `include` / `exclude` regexes in `config.yaml` to narrow or widen any source (e.g. add older
releases by removing `(?!\d)` from the atlas pattern). Add any other sitemap as another source.

## 1. Set up LM Studio

1. In LM Studio, download two models:
   - a **chat model** for condensing — something like Qwen2.5-7B-Instruct or Llama-3.1-8B-Instruct (bigger = better summaries, slower)
   - an **embedding model** — `nomic-embed-text-v1.5` is the default here; `bge-m3` or `gte` models also work (set the two `embed_*_prefix` values in `config.yaml` to `""` for those)
2. Load both, open the **Developer** tab and **Start Server** (default `http://localhost:1234`).
3. Optional but worth it: raise the chat model's parallel/concurrent request slots and set `condense.concurrency` to match.

If LM Studio runs on another machine/VM, set `llm.base_url` (or `LMSTUDIO_BASE_URL`) to `http://<that-host>:1234/v1` and enable "Serve on local network" in LM Studio.

## 2. Install

Python 3.10+.

```bash
cd sfdocs-rag
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows: .venv\Scripts\activate
pip install -e .
# Only needed for JS-rendered pages (Help articles, any shell pages):
pip install -e ".[render]" && playwright install chromium
```

Then check the models are reachable and named correctly:

```bash
sfdocs doctor
```

If `chat_model_ok` / `embed_model_ok` are false, copy the exact ids it lists into `llm.chat_model` / `llm.embed_model` in `config.yaml`.

## 3. Run the batch job

Trial run first (a few hundred docs per stage) to sanity-check output quality:

```bash
sfdocs run-all --limit 200
sfdocs search "maximum scope size for Database.executeBatch"
```

Then the full run:

```bash
sfdocs run-all
```

It's **resumable** — stop it any time (Ctrl+C, reboot) and run it again; each stage only processes
what isn't done yet. Re-running later only re-downloads pages that changed (ETag / Last-Modified)
and only re-summarises / re-embeds those. Progress: `sfdocs status`. Log: `data/sfdocs.log`.

Stages can also run individually: `sfdocs discover | fetch | extract | render | condense | index`.
Skip summarisation for a faster first pass with `sfdocs run-all --skip-condense` (run `sfdocs condense && sfdocs index` later).

**How long?** Expect tens of thousands of HTML pages plus a few hundred PDFs for the developer docs alone.
Downloading is fast (well under a few hours at the polite defaults). The slow part is the local LLM:
summarising runs one call per page, so on a single consumer GPU with a 7–8B model plan on roughly a day
for the full set; embeddings are much quicker. `condense.chunk_context: true` (contextual retrieval —
better recall) multiplies the LLM work by the number of chunks, so leave it off unless you have the GPU time.

### Schedule it (keeps the index fresh across Salesforce releases)

- macOS/Linux: `scripts/run_batch.sh` from cron, e.g. weekly: `0 2 * * 0 /path/to/sfdocs-rag/scripts/run_batch.sh`
- Windows: `scripts/run_batch.ps1` from Task Scheduler (`powershell -File C:\path\to\sfdocs-rag\scripts\run_batch.ps1`)

## 4. Connect it to Claude Code

Register the MCP server once (user scope = available in every project). Use **absolute paths**:

```bash
# macOS/Linux
claude mcp add salesforce-docs --scope user \
  --env SFDOCS_CONFIG=/abs/path/sfdocs-rag/config.yaml \
  -- /abs/path/sfdocs-rag/.venv/bin/sfdocs-mcp

# Windows (python -m: Windows can refuse to launch pip's sfdocs-mcp.exe, especially under OneDrive)
claude mcp add salesforce-docs --scope user --env SFDOCS_CONFIG=C:\path\sfdocs-rag\config.yaml -- C:\path\sfdocs-rag\.venv\Scripts\python.exe -m sfdocs.mcp_server
```

Check with `claude mcp list`, or `/mcp` inside Claude Code. The agent then has:

| Tool | Purpose |
|---|---|
| `search_salesforce_docs(query, k, guide, mode)` | Hybrid search; returns passages with title, section, URL, PDF page and a citation. `guide` filters (e.g. `apexcode`, `lwc`, `api_rest`). |
| `read_salesforce_doc(url_or_doc_id, start_chunk, max_chunks)` | Read a whole page/guide in order around a hit, paging with `next_start`. |
| `list_salesforce_guides(contains)` | What's in the index, with page counts. |
| `ask_salesforce_docs(question)` | Retrieval + a cited first-pass answer drafted by your **local** model. |
| `salesforce_docs_index_status()` | Crawl/index progress. |

Search still works (keyword-only) if LM Studio is down; vector search and `ask_` need it running.

To make Claude Code reach for it automatically, add `CLAUDE.md.snippet` to your project's `CLAUDE.md`
(or `~/.claude/CLAUDE.md` for all projects).

Any other MCP client (Claude Desktop, LM Studio's own MCP support, Cursor…) can use the same command:
`/abs/path/.venv/bin/sfdocs-mcp` with `SFDOCS_CONFIG` set.

## Testing

The suite builds a small Salesforce-shaped site (sitemap index, robots.txt, atlas pages with nav noise and
code, a duplicate canonical page, an old-version URL, a 404, a docs-index JSON with PDFs, a **JavaScript-only
page** and a **shadow-DOM "Help" page**) plus a fake LM Studio, then runs the real CLI end to end.

| File | What it proves |
|---|---|
| `tests/test_units.py` | extraction strips site chrome and keeps code; chunker never splits code or loses short sections; search queries are injection-safe |
| `tests/test_pipeline.py` | `run-all` discovers/filters correctly, skips 404s and duplicates, indexes HTML + PDF with page numbers, search ranks the right passage, re-runs are no-ops and `--refresh` re-indexes only changed pages |
| `tests/test_playwright_render.py` | a real headless Chromium renders JS-only and shadow-DOM pages, which are then extracted, indexed and found by search |
| `tests/test_mcp.py` | every MCP tool works over stdio, exactly as Claude Code calls it |
| `tests/test_live_salesforce.py` | opt-in (`--live`): crawls a small slice of the **real** sources and checks extraction quality; also indexes and searches it if LM Studio is up |

**In Docker (recommended — Chromium and its libraries come with the image):**

```bash
docker compose build
docker compose run --rm test          # hermetic suite, browser required (fails rather than skips without one)
docker compose run --rm live-test     # + live tests against developer.salesforce.com and your LM Studio
docker compose run --rm pipeline      # the real batch job; index lands in ./data
```

**Natively:**

```bash
pip install -e ".[test]" && playwright install chromium
pytest -v                    # browser tests skip if Chromium can't launch; set SFDOCS_REQUIRE_BROWSER=1 to make that a failure
pytest -v --live             # include live tests
```

To use your own Chrome/Chromium instead of Playwright's, set `SFDOCS_CHROMIUM_PATH` (or `render.executable_path`).

## Files

```
config.yaml            all settings (sources, LM Studio models, crawl politeness, chunking, condensing)
sfdocs/discover.py     sitemap / robots.txt / PDF-index discovery
sfdocs/fetch.py        async downloader + Playwright renderer
sfdocs/extract.py      HTML/PDF → Markdown
sfdocs/chunk.py        heading-aware chunker (never splits code blocks)
sfdocs/condense.py     LLM summaries / chunk context
sfdocs/index.py        embeddings → LanceDB, keywords → SQLite FTS5
sfdocs/search.py       hybrid retrieval (RRF)
sfdocs/mcp_server.py   MCP tools for agents
data/                  manifest.sqlite3, raw/, text/, vectors.lance/, sfdocs.log (created on first run)
```

## Notes

- **Be a good citizen.** The defaults crawl with 8 connections and a delay per host, and use conditional
  requests on re-runs. Keep it that way; this is for your own local reference index — check Salesforce's
  website terms if you plan anything beyond personal use.
- **Help articles:** the `help` source discovers URLs from help.salesforce.com's robots.txt sitemaps and
  renders each with a headless browser. Its URL patterns couldn't be verified during build, so do a
  `sfdocs run-all --source help --limit 50` trial and adjust its `include` regex if needed.
- **Sites change.** If Salesforce restructures its sitemaps or page templates, `sfdocs status` will show
  low extraction counts or many pages queued for render — adjust `sources` URLs or `MAIN_SELECTORS` in `extract.py`.
- To start over: delete `data/` (or `sfdocs index --rebuild` to re-embed only, e.g. after switching embedding model — **required** whenever you change `embed_model`).
