"""Builds a small Salesforce-shaped documentation site on disk and serves it.

Covers every path the crawler has to handle:
  * sitemap index -> urlset, plus robots.txt discovery
  * a classic atlas page with nav/header noise, a version picker, code and a canonical link
  * a duplicate page whose canonical points at the first one
  * an old-version atlas URL that the include filter must drop
  * a server-rendered page on the newer docs site
  * a JavaScript-only page (content injected by script)          -> needs Playwright
  * a "Help" page whose content lives in a shadow DOM             -> needs Playwright, render: always
  * a 404 in the sitemap
  * a docs index JSON listing two PDFs: one for a guide already crawled as HTML (must be skipped)
"""
from __future__ import annotations

import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ATLAS_PAGE = """<html><head><title>Using Batch Apex | Apex Developer Guide | Salesforce Developers</title>
<link rel="canonical" href="{base}/docs/atlas.en-us.apexcode.meta/apexcode/apex_batch.htm"></head>
<body><header>Site header and global navigation</header>
<nav>Table of contents: Apex Basics, Triggers, Batch Apex, Queueable Apex, Scheduled Apex</nav>
<main><div class="body">
<h1>Using Batch Apex</h1>
<p>Summer '26 (API version 67.0)</p>
<p>To use batch Apex, write an Apex class that implements the Salesforce-provided interface Database.Batchable
and then invoke the class programmatically with Database.executeBatch.</p>
<h2>Batch Apex Governor Limits</h2>
<p>Up to 5 batch jobs can be queued or active concurrently. Up to 100 Holding batch jobs can be held in the
Apex flex queue. The maximum scope size for Database.executeBatch is 2,000 records; the default is 200.</p>
<pre><code class="language-apex">public class MyBatch implements Database.Batchable&lt;sObject&gt; {
    public Database.QueryLocator start(Database.BatchableContext bc) {
        return Database.getQueryLocator('SELECT Id FROM Account');
    }
}</code></pre>
<h2>Chaining Jobs</h2>
<p>Starting with API version 26.0 you can start another batch job from an existing batch job by calling
Database.executeBatch in the finish method of the job.</p>
</div></main><footer>Copyright Salesforce footer links</footer></body></html>"""

LWC_PAGE = """<html><head><title>Display Data in a Table | LWC Dev Guide</title></head><body><main><article>
<h1>Display Data in a Table with lightning-datatable</h1>
<p>The lightning-datatable component displays tabular data where each column can be displayed based on the data
type. Inline editing is enabled by setting editable: true on a column definition and handling the onsave event,
which returns draftValues.</p>
<p>Use the key-field attribute to associate each row with a unique identifier such as Id. Infinite loading is
supported through enable-infinite-loading and the onloadmore event.</p>
</article></main></body></html>"""

JS_PAGE = """<html><head><title>Platform Event Replay | Platform Events Developer Guide</title></head>
<body><div id="app">Loading...</div>
<script>
setTimeout(function () {
  document.getElementById('app').innerHTML =
    '<main><h1>Platform Event Replay</h1>' +
    '<p>Each published platform event message is assigned a ReplayId. Subscribers can use the ReplayId to ' +
    'resume a subscription from a specific point after a disconnection, using the Pub/Sub API or CometD.</p>' +
    '<h2>Event Retention</h2>' +
    '<p>High-volume platform event messages are stored in the event bus for 72 hours. Subscribers can retrieve ' +
    'stored messages within the retention window by passing a ReplayId; messages older than 72 hours are ' +
    'no longer available for replay. The marker phrase for tests is quasar-replay-window.</p></main>';
}, 300);
</script></body></html>"""

SHADOW_PAGE = """<html><head><title>Set Up Multi-Factor Authentication | Salesforce Help</title></head>
<body><help-article-shell></help-article-shell>
<script>
customElements.define('help-article-shell', class extends HTMLElement {
  constructor() {
    super();
    const root = this.attachShadow({mode: 'open'});
    root.innerHTML =
      '<article><h1>Set Up Multi-Factor Authentication</h1>' +
      '<p>Multi-factor authentication (MFA) requires users to provide two or more pieces of evidence when they ' +
      'log in. Admins enable MFA for direct logins by assigning the Multi-Factor Authentication for User ' +
      'Interface Logins permission through a permission set.</p>' +
      '<h2>Verification Methods</h2>' +
      '<p>Supported verification methods include the Salesforce Authenticator mobile app, third-party TOTP ' +
      'authenticator apps, and security keys such as Yubikeys. The marker phrase for tests is nebula-mfa-shadow.</p>' +
      '</article>';
  }
});
</script></body></html>"""

SHELL_LEFTOVER = "Loading..."


def build(root: Path, pdf_bytes: bytes) -> None:
    (root / "docs/atlas.en-us.apexcode.meta/apexcode").mkdir(parents=True, exist_ok=True)
    (root / "docs/platform/lwc/guide").mkdir(parents=True, exist_ok=True)
    (root / "docs/platform/events/guide").mkdir(parents=True, exist_ok=True)
    (root / "help").mkdir(parents=True, exist_ok=True)
    (root / "pdf").mkdir(parents=True, exist_ok=True)
    (root / "pdf/api_rest.pdf").write_bytes(pdf_bytes)
    (root / "docs/platform/lwc/guide/datatable.html").write_text(LWC_PAGE)
    (root / "docs/platform/events/guide/replay.html").write_text(JS_PAGE)
    (root / "help/mfa.html").write_text(SHADOW_PAGE)


def write_dynamic(root: Path, base: str) -> None:
    """Files that embed the server's address (written once the port is known)."""
    page = ATLAS_PAGE.replace("{base}", base)
    (root / "docs/atlas.en-us.apexcode.meta/apexcode/apex_batch.htm").write_text(page)
    (root / "docs/atlas.en-us.apexcode.meta/apexcode/apex_batch_alias.htm").write_text(page)
    (root / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap-index.xml\nSitemap: {base}/help-sitemap.xml\n")
    (root / "sitemap-index.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<sitemap><loc>{base}/sitemap-1.xml</loc></sitemap></sitemapindex>")
    urls = [
        ("docs/atlas.en-us.apexcode.meta/apexcode/apex_batch.htm", "2026-09-01"),
        ("docs/atlas.en-us.apexcode.meta/apexcode/apex_batch_alias.htm", None),
        ("docs/atlas.en-us.248.0.apexcode.meta/apexcode/apex_batch.htm", None),  # old version -> filtered
        ("docs/platform/lwc/guide/datatable.html", None),
        ("docs/platform/events/guide/replay.html", None),  # JS-only
        ("docs/platform/lwc/guide/removed.html", None),    # 404
    ]
    items = "".join(f"<url><loc>{base}/{u}</loc>{f'<lastmod>{m}</lastmod>' if m else ''}</url>" for u, m in urls)
    (root / "sitemap-1.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>')
    (root / "help-sitemap.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{base}/help/mfa.html?language=en_US</loc></url></urlset>")
    (root / "index.json").write_text(
        '{"content":['
        f'{{"id":"a","value":{{"_id":"atlas.en-us.apexcode.meta","title":"Apex Developer Guide","pdf_url":"{base}/pdf/apexcode.pdf","deliverable":"apexcode"}}}},'
        f'{{"id":"b","value":{{"_id":"atlas.en-us.api_rest.meta","title":"REST API Developer Guide","pdf_url":"{base}/pdf/api_rest.pdf","deliverable":"api_rest","updated":"2026-05-25"}}}}'
        "]}")


def make_pdf() -> bytes:
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    doc = fitz.open()
    pages = [
        "REST API Developer Guide\nIntroduction to REST API. REST API provides a simple\nweb services API for interacting with Salesforce.",
        "Composite Resources\nThe composite resource executes a series of REST API requests\nin a single call. You can have up to 25 subrequests in a single call.",
        "API Limits\nThe Limits resource lists information about limits in your org,\nsuch as DailyApiRequests. Each org has a rolling 24-hour API request limit.",
    ]
    for t in pages:
        doc.new_page().insert_text((50, 72), t, fontsize=10)
    doc.set_toc([[1, "Introduction", 1], [1, "Composite Resources", 2], [2, "API Limits", 3]])
    doc.set_metadata({"title": "REST API Developer Guide"})
    return doc.tobytes()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(root: Path, host="127.0.0.1"):
    srv = ThreadingHTTPServer((host, 0), functools.partial(_QuietHandler, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://{host}:{srv.server_address[1]}"
