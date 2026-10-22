"""LanceDB vector store (embedded, file-based - no server to run)."""
from __future__ import annotations

from pathlib import Path

import lancedb
import pyarrow as pa

TABLE = "chunks"


class VectorStore:
    def __init__(self, data_dir: Path):
        self.db = lancedb.connect(str(Path(data_dir) / "vectors.lance"))
        self._tbl = None

    def _names(self) -> list[str]:
        try:
            res = self.db.list_tables()
            names = getattr(res, "tables", res)
        except AttributeError:
            names = self.db.table_names()
        return list(names)

    @property
    def table(self):
        if self._tbl is None and TABLE in self._names():
            self._tbl = self.db.open_table(TABLE)
        return self._tbl

    def add(self, rows: list[dict]):
        """rows: {chunk_id:int, doc_id:int, vector:list[float]}"""
        if not rows:
            return
        dim = len(rows[0]["vector"])
        data = pa.table({
            "chunk_id": pa.array([r["chunk_id"] for r in rows], pa.int64()),
            "doc_id": pa.array([r["doc_id"] for r in rows], pa.int64()),
            "vector": pa.array([r["vector"] for r in rows], pa.list_(pa.float32(), dim)),
        })
        if self.table is None:
            self._tbl = self.db.create_table(TABLE, data=data)
        else:
            self.table.add(data)

    def delete_docs(self, doc_ids: list[int]):
        if self.table is None or not doc_ids:
            return
        for i in range(0, len(doc_ids), 500):
            ids = ",".join(str(int(d)) for d in doc_ids[i:i + 500])
            self.table.delete(f"doc_id IN ({ids})")

    def search(self, vector: list[float], k: int) -> list[tuple[int, float]]:
        if self.table is None:
            return []
        res = self.table.search(vector).metric("cosine").limit(k).to_list()
        return [(int(r["chunk_id"]), float(r.get("_distance", 0.0))) for r in res]

    def count(self) -> int:
        return 0 if self.table is None else self.table.count_rows()

    def maybe_build_ann_index(self, min_rows: int = 50_000):
        """For large corpora, an ANN index makes queries fast. Brute force is fine below ~50k rows."""
        t = self.table
        if t is None or t.count_rows() < min_rows:
            return False
        try:
            t.create_index(metric="cosine", vector_column_name="vector", replace=True)
            return True
        except Exception:  # noqa: BLE001 - older/newer lancedb signatures
            try:
                t.create_index(metric="cosine", replace=True)
                return True
            except Exception:  # noqa: BLE001
                return False
