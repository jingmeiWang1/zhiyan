"""Build (and rebuild) one document's semantic index.

The shape of a job: read the stored file -> pages -> chunks -> vectors -> rows.
Nothing here starts a thread or touches HTTP directly; `worker.py` decides WHEN,
`embed.py` owns the request, `store.py` owns the rows.

WHAT `model` MEANS IN THE INDEX ROW
    `document_index.model` is "provider:model" — the identity of the vector
    space these chunks were embedded into. Two different embedding models put
    the same sentence in unrelated places, and comparing across them does not
    fail: it returns confident, meaningless hits. Recording the identity is what
    turns that silent wrongness into "the model changed, rebuild" — see
    `worker.next_job`.
"""

from __future__ import annotations

from zhiyan.applications.library import file_path, get_document
from zhiyan.applications.reader import ReaderError, parse_pages
from zhiyan.core.models import embed_target
from zhiyan.rag import store
from zhiyan.rag.chunk import chunks_for
from zhiyan.rag.embed import EmbedError, embed_texts


def current_model(settings) -> str:
    """The identity of the vector space embeddings are built in, or "" when the
    feature is off."""
    target = embed_target(settings)
    return f"{target[0]}:{target[1]}" if target else ""


def _fail(conn, doc_id: str, model: str, message: str) -> dict:
    store.mark(conn, doc_id, model=model, state=store.ERROR, error=message)
    return {"ok": False, "state": store.ERROR, "error": message}


def index_document(settings, conn, home, doc_id: str) -> dict:
    """Index one document. Idempotent — every run replaces what was there.

    Does not raise for the failures a user can actually cause (a scanned PDF, an
    endpoint that is down): those are recorded on the row and reported, because
    the alternative is a background thread that dies at 3am and a page that
    still says "排队中".
    """
    model = current_model(settings)
    if not model:
        return {"ok": False, "state": store.QUEUED, "error": "语义检索未启用"}
    row = get_document(conn, doc_id)
    if row is None:
        return _fail(conn, doc_id, model, "文档不存在")

    try:
        pages = parse_pages(row["title"], file_path(conn, home, doc_id).read_bytes())
        chunks = chunks_for(row["kind"], pages)
    except (ReaderError, OSError) as exc:
        return _fail(conn, doc_id, model, str(exc))

    if not chunks:
        # A scanned PDF has no text layer. Saying "done, nothing to index" is
        # the honest answer; refusing it as an error is not, because the file is
        # perfectly fine — there is simply nothing in it to embed.
        store.mark(conn, doc_id, model=model, state=store.READY, done=0, total=0)
        return {"ok": True, "state": store.READY, "total": 0}

    # total goes in BEFORE the first request, so the page can say "0/54" instead
    # of showing nothing at all until the job is over.
    store.mark(conn, doc_id, model=model, state=store.RUNNING, done=0, total=len(chunks))

    def progress(done: int, total: int) -> None:
        store.progress(conn, doc_id, done, total)

    try:
        vectors = embed_texts(settings, [chunk.text for chunk in chunks],
                              on_progress=progress)
    except EmbedError as exc:
        return _fail(conn, doc_id, model, str(exc))

    store.replace_chunks(conn, doc_id, [
        (ordinal, chunk.page, chunk.page_end, chunk.text, store.normalize(vector))
        for ordinal, (chunk, vector) in enumerate(zip(chunks, vectors))])
    store.mark(conn, doc_id, model=model, state=store.READY,
               done=len(chunks), total=len(chunks))
    return {"ok": True, "state": store.READY, "total": len(chunks)}


def _describe(model: str, row: dict | None) -> dict:
    """One document's index, in the shape the page renders.

    `state` is "none" (never indexed) when there is no row at all, and `stale`
    means the vectors are ready but were built in a different vector space —
    the one case the page must not describe as "已索引".
    """
    return {
        "enabled": bool(model),
        "model": model,
        "state": (row or {}).get("state") or "none",
        "done": (row or {}).get("done", 0),
        "total": (row or {}).get("total", 0),
        "error": (row or {}).get("error", ""),
        "stale": bool(row and model and row["state"] in (store.READY, store.RUNNING)
                      and row["model"] != model),
    }


def index_info(settings, conn, doc_id: str) -> dict:
    """What the page needs to describe this document's index, in one dict."""
    return _describe(current_model(settings), store.state_of(conn, doc_id))


def decorate(settings, conn, documents: list[dict]) -> list[dict]:
    """Attach each document's index state to the library list.

    Two queries for the whole list, not two per row: the reader's library is
    drawn on every page load and on every upload, and a per-row status query
    would turn one round trip into N.
    """
    model = current_model(settings)
    states = store.all_states(conn)
    for document in documents:
        document["index"] = _describe(model, states.get(document["id"]))
    return documents
