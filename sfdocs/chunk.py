"""Heading-aware chunking that never splits inside a fenced code block."""
from __future__ import annotations

import re
from dataclasses import dataclass

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
PAGE_RE = re.compile(r"<!-- page (\d+) -->")


@dataclass
class Chunk:
    ord: int
    heading: str
    page: int | None
    text: str


def _blocks(md: str):
    """Yield (heading_path, page, block_text). Blocks are paragraphs or whole code fences."""
    path: list[str] = []
    page = None
    buf: list[str] = []
    in_code = False

    def flush():
        if buf:
            txt = "\n".join(buf).strip()
            buf.clear()
            if txt:
                return txt
        return None

    for line in md.splitlines():
        pm = PAGE_RE.fullmatch(line.strip())
        if pm and not in_code:
            t = flush()
            if t:
                yield " > ".join(path), page, t
            page = int(pm.group(1))
            continue
        if line.strip().startswith("```"):
            in_code = not in_code
            buf.append(line)
            if not in_code:
                t = flush()
                if t:
                    yield " > ".join(path), page, t
            continue
        if in_code:
            buf.append(line)
            continue
        hm = HEADING_RE.match(line)
        if hm:
            t = flush()
            if t:
                yield " > ".join(path), page, t
            level = len(hm.group(1))
            path[:] = path[: level - 1] + [hm.group(2).strip()]
            continue
        if not line.strip():
            t = flush()
            if t:
                yield " > ".join(path), page, t
            continue
        buf.append(line)
    t = flush()
    if t:
        yield " > ".join(path), page, t


def chunk_markdown(md: str, max_chars: int = 4000, overlap_chars: int = 400) -> list[Chunk]:
    chunks: list[Chunk] = []
    cur: list[str] = []
    cur_len = 0
    cur_head = ""
    cur_page = None

    def emit():
        nonlocal cur, cur_len
        if not cur:
            return
        body = "\n\n".join(cur).strip()
        if body:
            chunks.append(Chunk(len(chunks), cur_head, cur_page, body))
        # carry an overlap tail into the next chunk (same section only)
        tail = body[-overlap_chars:] if overlap_chars else ""
        cur = [tail] if tail and len(body) > overlap_chars else []
        cur_len = len(tail) if cur else 0

    for head, page, block in _blocks(md):
        if head != cur_head and cur:
            emit()
            cur, cur_len = [], 0  # no overlap across sections
        if not cur:
            cur_head, cur_page = head, page
        # oversized single block (huge table / code listing): hard split
        while len(block) > max_chars:
            if cur:
                emit()
                cur, cur_len = [], 0
                cur_head, cur_page = head, page
            cut = block.rfind("\n", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            chunks.append(Chunk(len(chunks), head, page, block[:cut].strip()))
            block = block[cut:]
        if cur_len + len(block) > max_chars and cur:
            emit()
            cur_head, cur_page = head, page
        cur.append(block)
        cur_len += len(block) + 2
    emit()
    return [c for c in chunks if c.text.strip()]
