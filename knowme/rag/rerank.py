"""A second pass over the candidates, scoring each one AGAINST the question.

WHY THERE IS A SECOND PASS AT ALL
    The vector search in `store.py` is a "two-tower" search: the question and
    the passage are encoded SEPARATELY, so neither encoder ever saw the other.
    That is what makes it fast enough to scan a whole library — and also why it
    sorts only approximately. The passage that actually answers the question is
    regularly sitting at rank 12, and only the top few are ever handed to the
    model.

    A reranker is a "cross-encoder": the question and the candidate go in
    TOGETHER, and the model outputs one relevance score. Far slower (one forward
    pass per candidate) and far better ordered. So the usual arrangement is two
    stages — vector search recalls a few dozen, the reranker picks the few. That
    is exactly what `query.semantic_search` now does.

WHY urllib AND NOT THE `openai` SDK
    Same reason as `embed.py`, which this file is modelled on: `/v1/rerank` is
    not a chat endpoint, and `OpenAICompatClient` only speaks `.messages`. The
    precedent for plain urllib against a non-chat endpoint is `ops/catalog.py`.
    It also means a test can point this at a real stub server and read the exact
    request body — which the tests do.

WHY FAILING HERE DOES NOT FAIL THE SEARCH
    If the reranker is down, the vector order is still a usable answer. So
    `query.semantic_search` catches RerankError, keeps the vector order, and
    marks the hits so the tool prints 相似度 instead of 相关度 — a cosine and a
    cross-encoder score are different scales and must not wear the same label.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from knowme.core.models import PROVIDERS, provider_key, rerank_target

TIMEOUT = 120.0

# How much of each candidate is sent, and this number came from measuring a real
# server rather than from taste.
#
# llama.cpp reranks one `query + document` sequence per candidate, and each of
# those sequences has to fit the server's physical batch (n_ubatch, 512 by
# default). Measured on bge-reranker-v2-m3: 600 Chinese chars ≈ 478 tokens per
# sequence and passes; 800 chars ≈ 633 and comes back as HTTP 500 "input is too
# large to process". So the ceiling is about 640 Chinese chars, and sending a
# whole chunk (up to CHUNK_MAX = 1600) would fail EVERY request on a default
# server — the reranker would look permanently broken rather than too small.
#
# 512 chars ≈ 410 tokens leaves room for a long question, and is also simply
# where a cross-encoder is normally run: judging "is this passage about the
# question" does not need the whole chunk, and a chunk states its topic first.
DOC_CHARS = 512

NOT_CONFIGURED = ("重排未启用：设置 KNOWME_RERANK_PROVIDER 和 KNOWME_RERANK_MODEL "
                  "（例如 local_rerank / bge-reranker-v2-m3）之后可用")


class RerankError(RuntimeError):
    """The reranker is unavailable, misconfigured, or the endpoint refused."""


def _endpoint(settings) -> tuple[str, str, str]:
    """(url, model, api_key) for the configured rerank role."""
    target = rerank_target(settings)
    if target is None:
        raise RerankError(NOT_CONFIGURED)
    name, model = target
    provider = PROVIDERS.get(name)
    if provider is None:
        raise RerankError(f"重排服务商 '{name}' 不存在"
                          f"（可选：{', '.join(sorted(PROVIDERS))}）")
    if provider.kind != "openai":
        raise RerankError(f"'{name}' 走的是 {provider.kind} 协议，"
                          "没有 OpenAI 兼容的 /rerank 端点")
    base = (provider.configured_base_url() or "").rstrip("/")
    if not base:
        raise RerankError(f"'{name}' 没有配置 base_url")
    return f"{base}/rerank", model, provider_key(name, settings)


def _post(url: str, model: str, key: str, query: str,
          documents: list[str]) -> list[float]:
    body = json.dumps({"model": model, "query": query,
                       "documents": documents}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "KnowMe"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RerankError(f"重排端点返回 HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise RerankError(f"连不上重排端点（{url}）：{exc.reason}") from exc
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise RerankError(f"重排端点响应异常：{exc}") from exc

    rows = data.get("results")
    # A response we cannot read must NOT become a list of zeros: zeros sort as
    # "everything is equally irrelevant", which looks like a working reranker
    # that hates all your documents. Say what came back instead.
    if not isinstance(rows, list):
        raise RerankError(f"重排端点没有返回 results：{str(data)[:200]}")

    # Score by the index the endpoint reports, not by position in the list. The
    # standard shape does not promise the order we sent (llama.cpp's own tests
    # sort the results themselves), and getting this wrong reorders the hits
    # against the wrong texts — silently, and in a way that looks plausible.
    scores = [0.0] * len(documents)
    placed = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        index = row.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if not 0 <= index < len(documents):
            continue          # an index we never sent — drop it, do not guess
        score = row.get("relevance_score", row.get("score"))
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            continue
        scores[index] = float(score)
        placed += 1
    if placed == 0:
        raise RerankError(f"重排端点一条都没对上：{str(data)[:200]}")
    return scores


def rerank_scores(settings, query: str, documents: list[str]) -> list[float]:
    """One relevance score per candidate, IN THE ORDER GIVEN.

    Raises RerankError — callers are expected to fall back to vector order
    rather than fail the search (see the module docstring).
    """
    if not documents:
        return []
    url, model, key = _endpoint(settings)
    trimmed = [(doc or "")[:DOC_CHARS] for doc in documents]
    return _post(url, model, key, query, trimmed)
