"""Retrieval: find the passages that mean what the question means.

WHY THIS IS ONLY RETRIEVAL
    It used to also generate the answer, from a prompt that said "只根据这些段落
    回答、每条标 [第 N 页]、没有就说没有". That whole job now belongs to the
    agent: `semantic_search` is a TOOL it decides to call, and the model that
    writes the reply is the same one that chose to look. Keeping a second,
    server-side answer path would mean two places deciding how to cite a page,
    and two different answers to the same question depending on which door you
    came in through.

WHY CITATIONS STILL MATTER
    A retrieval hit nobody can check is a hit taken on faith. Chunks carry the
    page range they came from, the tool hands that to the model, and the reader
    turns 第 N 页 in the reply into something clickable — which is the entire
    reason a PDF is indexed by character offset in the first place.
"""

from __future__ import annotations

import sys

from knowme.core.models import rerank_target
from knowme.rag.embed import embed_one
from knowme.rag.rerank import RerankError, rerank_scores
from knowme.rag.store import search as vector_search

# Two stages when a reranker is configured: vector search recalls RECALL
# candidates, the reranker sorts them, the best SEARCH_K are returned. Without
# one, this is one stage and RECALL is never asked for.
RECALL = 30
SEARCH_K = 6       # hits returned per query
SNIPPET = 200


def label(hit: dict, ordinal: int) -> str:
    """How a hit is named in a citation and in the list under the answer."""
    if hit.get("page"):
        return (f"第 {hit['page']} 页" if hit.get("page_end") in (None, hit["page"])
                else f"第 {hit['page']}–{hit['page_end']} 页")
    return f"第 {ordinal} 段"


def snippet(text: str, limit: int = SNIPPET) -> str:
    """One line for a hit list. Whitespace is collapsed first — a PDF chunk is
    full of newlines that would otherwise eat the whole budget."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _titles(conn, doc_ids: list[str]) -> dict[str, str]:
    if not doc_ids:
        return {}
    marks = ",".join("?" for _ in doc_ids)
    try:
        rows = conn.execute(f"SELECT id, title FROM documents WHERE id IN ({marks})",
                            doc_ids).fetchall()
    except Exception:  # noqa: BLE001 - the title is decoration, never a blocker
        return {}
    return {row["id"]: row["title"] for row in rows}


def semantic_search(settings, conn, question: str, *, doc_id: str | None = None,
                    k: int = SEARCH_K) -> list[dict]:
    """The chunks closest in meaning to `question`, best first.

    `doc_id=None` searches the whole library — the same function serves the
    panel next to an open document and the agent tool that has no document in
    mind.

    Two stages when a reranker is configured: recall RECALL, rerank, return the
    best `k`. Every hit carries `reranked` so the caller knows whether `score`
    is a cross-encoder relevance or a cosine — two different scales that must
    not be labelled the same way.
    """
    question = (question or "").strip()
    if not question:
        return []
    reranking = rerank_target(settings) is not None
    hits = vector_search(conn, embed_one(settings, question),
                         k=RECALL if reranking else k, doc_id=doc_id)

    if reranking and hits:
        try:
            scores = rerank_scores(settings, question, [hit["text"] for hit in hits])
        except RerankError as exc:
            # Deliberate: a reranker that is down must not take semantic search
            # down with it — vector order is still a usable answer. But it is
            # never silent: the reason goes to stderr and `reranked` stays
            # False, so the tool labels the scores honestly.
            print(f"[knowme] 重排没生效，退回向量顺序：{exc}", file=sys.stderr)
        else:
            for hit, score in zip(hits, scores):
                hit["score"] = score
                hit["reranked"] = True
            hits.sort(key=lambda hit: hit["score"], reverse=True)

    titles = _titles(conn, sorted({hit["doc_id"] for hit in hits}))
    for ordinal, hit in enumerate(hits[:k], start=1):
        hit.setdefault("reranked", False)
        hit["label"] = label(hit, ordinal)
        hit["snippet"] = snippet(hit["text"])
        hit["title"] = titles.get(hit["doc_id"], "")
    return hits[:k]
