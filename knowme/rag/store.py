"""Where the vectors live: SQLite, packed as float32, compared by brute force.

WHY THERE IS NO VECTOR DATABASE HERE
    A few thousand chunks is the whole scale this app will see (the library is a
    handful of papers). An index — HNSW, ivfflat, whatever — buys sub-linear
    search at the cost of a dependency, a build step, and a second place the
    data lives. A full scan over 700 chunks is a tenth of a second in plain
    Python. When that stops being true is when this comment becomes wrong, and
    that is the point of writing the number down.

WHY VECTORS ARE STORED NORMALIZED
    Cosine similarity of two normalized vectors is just their dot product, so
    normalizing once at write time turns every later query into half the work
    (no per-candidate norm to recompute). Every vector that goes in or comes out
    for comparison passes through `normalize`, so the two sides cannot disagree
    about whether length matters. `dim` is stored per row: a model with a
    different width must not be read as if it were this one.

The tables are created lazily rather than in `db.py`'s SCHEMA, on purpose —
same rule as `library._ensure_table` and `tools/knowledge._ensure_table`, so an
existing workspace needs no migration and a workspace that never turns semantic
search on never grows these tables at all.
"""

from __future__ import annotations

import math
import sqlite3
import struct
from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS document_index (
    doc_id  TEXT PRIMARY KEY,
    model   TEXT NOT NULL,
    state   TEXT NOT NULL,
    done    INTEGER DEFAULT 0,
    total   INTEGER DEFAULT 0,
    error   TEXT    DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_chunks (
    doc_id   TEXT NOT NULL,
    ordinal  INTEGER NOT NULL,
    page     INTEGER,
    page_end INTEGER,
    text     TEXT NOT NULL,
    dim      INTEGER NOT NULL,
    vector   BLOB NOT NULL,
    PRIMARY KEY (doc_id, ordinal)
);
"""

QUEUED, RUNNING, READY, ERROR = "queued", "running", "ready", "error"


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize(vector: list[float]) -> list[float]:
    """Unit length, so cosine = dot product. A zero vector stays zero."""
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm:
        return list(vector)
    return [v / norm for v in vector]


def pack(vector: list[float]) -> bytes:
    """float32, little-endian, explicitly — the byte order a reader expects must
    not depend on the machine that wrote it."""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"<{dim}f", blob))


# --- the index's own bookkeeping --------------------------------------------


def mark(conn: sqlite3.Connection, doc_id: str, *, model: str, state: str,
         done: int = 0, total: int = 0, error: str = "") -> None:
    ensure_tables(conn)
    conn.execute(
        "INSERT INTO document_index (doc_id, model, state, done, total, error, updated_at)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(doc_id) DO UPDATE SET model=excluded.model, state=excluded.state,"
        " done=excluded.done, total=excluded.total, error=excluded.error,"
        " updated_at=excluded.updated_at",
        (doc_id, model, state, done, total, error, _now()))
    conn.commit()


def progress(conn: sqlite3.Connection, doc_id: str, done: int, total: int) -> None:
    """Tick the counter without touching anything else — this runs while a
    document is being indexed, and rewriting the whole row would clear an error
    or reset the state a concurrent reader is looking at."""
    conn.execute("UPDATE document_index SET done=?, total=?, updated_at=? WHERE doc_id=?",
                 (done, total, _now(), doc_id))
    conn.commit()


def state_of(conn: sqlite3.Connection, doc_id: str) -> dict | None:
    ensure_tables(conn)
    row = conn.execute("SELECT * FROM document_index WHERE doc_id=?", (doc_id,)).fetchone()
    return dict(row) if row else None


def all_states(conn: sqlite3.Connection) -> dict[str, dict]:
    """Every document's index state, keyed by doc_id — one query for the whole
    dashboard payload rather than one per row in the library list."""
    ensure_tables(conn)
    return {row["doc_id"]: dict(row)
            for row in conn.execute("SELECT * FROM document_index")}


# --- chunks -----------------------------------------------------------------


def replace_chunks(conn: sqlite3.Connection, doc_id: str,
                   rows: list[tuple[int, int | None, int | None, str, list[float]]]) -> None:
    """Swap in a document's chunks atomically. `rows` are
    (ordinal, page, page_end, text, normalized vector)."""
    ensure_tables(conn)
    with conn:  # one transaction: a half-written index is worse than none
        conn.execute("DELETE FROM document_chunks WHERE doc_id=?", (doc_id,))
        conn.executemany(
            "INSERT INTO document_chunks (doc_id, ordinal, page, page_end, text, dim, vector)"
            " VALUES (?,?,?,?,?,?,?)",
            [(doc_id, ordinal, page, page_end, text, len(vector), pack(vector))
             for ordinal, page, page_end, text, vector in rows])


def forget(conn: sqlite3.Connection, doc_id: str) -> None:
    """Drop a document's vectors and its index row. Called when the document
    itself is deleted — leaving vectors behind means search keeps finding a
    document that no longer exists."""
    ensure_tables(conn)
    with conn:
        conn.execute("DELETE FROM document_chunks WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM document_index WHERE doc_id=?", (doc_id,))


def counts(conn: sqlite3.Connection, doc_id: str) -> int:
    ensure_tables(conn)
    row = conn.execute("SELECT COUNT(*) FROM document_chunks WHERE doc_id=?",
                       (doc_id,)).fetchone()
    return int(row[0])


def search(conn: sqlite3.Connection, vector: list[float], *, k: int = 8,
           doc_id: str | None = None) -> list[dict]:
    """The k most similar chunks, best first. `doc_id=None` searches everything.

    A chunk whose stored width does not match the query's is skipped rather than
    unpacked at the wrong stride: that only happens when the embedding model
    changed, and reading it as if it were the same shape would produce confident
    nonsense.
    """
    ensure_tables(conn)
    query = normalize(vector)
    sql = ("SELECT doc_id, ordinal, page, page_end, text, dim, vector"
           " FROM document_chunks")
    params: tuple = ()
    if doc_id:
        sql += " WHERE doc_id=?"
        params = (doc_id,)
    scored: list[dict] = []
    for row in conn.execute(sql, params):
        if row["dim"] != len(query):
            continue
        stored = unpack(row["vector"], row["dim"])
        score = sum(a * b for a, b in zip(query, stored))
        scored.append({"doc_id": row["doc_id"], "ordinal": row["ordinal"],
                       "page": row["page"], "page_end": row["page_end"],
                       "text": row["text"], "score": score})
    scored.sort(key=lambda hit: hit["score"], reverse=True)
    return scored[:k]
