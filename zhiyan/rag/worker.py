"""The one background thread that builds indexes. Everything else is a function.

WHY THERE IS NO HOOK IN `library.save_document`
    The obvious design is "when a document is saved, push it onto a queue". That
    means `applications/library.py` importing this package, and it means a
    document saved before the user configured embeddings would never be queued —
    invisible until they wonder why semantic search finds nothing in the papers
    they already had.

    So there is no queue to push onto. `next_job` ASKS which document still
    needs work, every second, and the answer is a LEFT JOIN: no index row yet,
    or queued, or a run that was interrupted, or one built with a model that is
    no longer the configured one. Upload, URL fetch, the agent's own
    `fetch_document` tool — all of them are covered without any of them knowing
    this module exists, and the documents that were already in the library get
    indexed the first time the feature is switched on.

WHY `tick` IS A PLAIN FUNCTION
    A thread is the one thing in this package a test cannot drive. `tick` does
    exactly one unit of work and returns; `start` is the only thing that loops.
    Tests call `tick` directly and never race anything.
"""

from __future__ import annotations

import sqlite3
import sys
import threading

from zhiyan.db import connect
from zhiyan.rag import store
from zhiyan.rag.index import current_model, index_document

INTERVAL = 1.0

_lock = threading.Lock()
_started = False
_stop = threading.Event()

# One query, and it is the whole queue. Ordered by when the document arrived so
# a fresh upload is not stuck behind a rebuild of everything older.
#
# 'running' counts as work to do: a row is only left in that state by a process
# that died mid-job, because a completed run always moves it to 'ready' or
# 'error'. Picking it up again is what makes a restart recover instead of
# leaving a document stuck at "索引中…" forever.
_NEXT_JOB = """
SELECT d.id FROM documents d
LEFT JOIN document_index i ON i.doc_id = d.id
WHERE i.doc_id IS NULL
   OR i.state IN ('queued', 'running')
   OR (i.state = 'ready' AND i.model <> ?)
ORDER BY d.created_at
LIMIT 1
"""


def next_job(conn: sqlite3.Connection, model: str) -> str | None:
    """The next document that needs indexing, or None when there is nothing to do."""
    # The queue query NAMES document_index, and that table is created lazily
    # like every other feature table in this project. Without this line the
    # query fails with "no such table" on a workspace that has never been
    # indexed — which the handler below dutifully reads as "nothing to do", and
    # the worker then does nothing at all, forever, in silence.
    store.ensure_tables(conn)
    try:
        row = conn.execute(_NEXT_JOB, (model,)).fetchone()
    except sqlite3.OperationalError:
        # …and THIS is the one that really means it: the library has never held
        # a document, so `documents` does not exist yet.
        return None
    return row["id"] if row else None


def tick(settings, conn, home) -> str | None:
    """Index at most one document. Returns its id, or None if there was nothing.

    A no-op when the embed role is unset: no tables are created, no rows are
    written, and a workspace that never turns semantic search on is byte-for-byte
    what it was before this feature existed.
    """
    model = current_model(settings)
    if not model:
        return None
    doc_id = next_job(conn, model)
    if doc_id is None:
        return None
    index_document(settings, conn, home, doc_id)
    return doc_id


def _loop(settings, home, interval: float = INTERVAL) -> None:
    conn = connect(home)
    while not _stop.is_set():
        worked = False
        try:
            worked = tick(settings, conn, home) is not None
        except Exception as exc:  # noqa: BLE001 - the thread must outlive any one job
            # index_document records the failures a user can cause. Anything
            # reaching here is a bug, and it must not take indexing down for
            # every later document.
            print(f"[zhiyan] 建索引失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        if not worked:
            _stop.wait(interval)


def start(settings, home) -> None:
    """Begin indexing in the background. Idempotent; the web server calls it once."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, args=(settings, home), daemon=True,
                     name="zhiyan-index").start()


def enqueue(conn: sqlite3.Connection, doc_id: str) -> None:
    """Mark a document as needing (re)indexing. Not required for the worker to
    find it — `next_job` would — but it is how a *rebuild* is requested."""
    store.mark(conn, doc_id, model="", state=store.QUEUED)
