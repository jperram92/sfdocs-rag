"""Drive the MCP server over stdio exactly the way Claude Code does."""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _payload(result):
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


async def _session_calls(config):
    params = StdioServerParameters(command=sys.executable, args=["-m", "sfdocs.mcp_server"],
                                   env={**os.environ, "SFDOCS_CONFIG": str(config)})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            out = {"tools": sorted(t.name for t in (await s.list_tools()).tools)}
            out["search"] = _payload(await s.call_tool(
                "search_salesforce_docs", {"query": "Database.executeBatch maximum scope size", "k": 3}))
            out["filtered"] = _payload(await s.call_tool(
                "search_salesforce_docs", {"query": "API request limit", "guide": "api_rest", "k": 2}))
            pdf_url = out["filtered"]["results"][0]["url"]
            out["read"] = _payload(await s.call_tool(
                "read_salesforce_doc", {"url_or_doc_id": pdf_url, "max_chunks": 2}))
            out["read2"] = _payload(await s.call_tool(
                "read_salesforce_doc", {"url_or_doc_id": pdf_url, "start_chunk": out["read"]["next_start"]}))
            out["ask"] = _payload(await s.call_tool("ask_salesforce_docs", {"question": "How many batch jobs can be queued?"}))
            out["guides"] = _payload(await s.call_tool("list_salesforce_guides", {}))
            out["status"] = _payload(await s.call_tool("salesforce_docs_index_status", {}))
            out["missing"] = _payload(await s.call_tool("read_salesforce_doc", {"url_or_doc_id": "https://nope"}))
            return out


def test_mcp_tools_end_to_end(pipeline):
    out = asyncio.run(_session_calls(pipeline["config"]))
    assert out["tools"] == ["ask_salesforce_docs", "list_salesforce_guides", "read_salesforce_doc",
                            "salesforce_docs_index_status", "search_salesforce_docs"]
    top = out["search"]["results"][0]
    assert "apex_batch.htm" in top["url"] and "2,000 records" in top["text"]
    assert top["citation"].startswith("Using Batch Apex > ")
    assert out["filtered"]["results"][0]["url"].endswith("api_rest.pdf")
    assert out["read"]["total_chunks"] == 3 and out["read"]["next_start"] == 2
    assert "(page 3)" in out["read2"]["content"] and out["read2"]["next_start"] is None
    assert out["ask"]["answer"] and out["ask"]["sources"][0]["citation"]
    assert out["status"]["indexed"] >= 3
    assert "error" in out["missing"]
