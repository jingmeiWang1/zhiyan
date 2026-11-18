"""Text -> vectors, over whichever endpoint the user pointed the embed role at.

WHY urllib AND NOT THE `openai` SDK
    The SDK is already a dependency and `OpenAICompatClient` already knows how
    to build one — but it only exposes `.messages`, and widening it to carry
    `.embeddings` would put a second, differently-shaped API on the object every
    chat turn goes through. The precedent for a NON-chat endpoint already exists
    in this repo: `ops/catalog.py:_fetch_models` talks to `/v1/models` with plain
    urllib. This follows it. A test can point this at a local stub server and
    read the exact request body, which is what the tests here do.

WHY THE ROLE IS RESOLVED HERE AND NOT VIA `roles_for`
    `roles_for` builds CHAT clients. Embeddings are a different endpoint with a
    different response shape; the only thing they share with the other roles is
    how the (provider, model) pair is chosen — and that part is reused.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from knowme.core.models import PROVIDERS, embed_target, provider_key

# How many texts go in one request. Big enough that a 400-page paper is not 400
# round trips, small enough that a failure does not throw away much work.
BATCH = 16
TIMEOUT = 120.0

NOT_CONFIGURED = ("语义检索未启用：设置 KNOWME_EMBED_PROVIDER 和 KNOWME_EMBED_MODEL "
                  "（例如 local_ollama / bge-m3）之后可用")


class EmbedError(RuntimeError):
    """Embeddings are unavailable, misconfigured, or the endpoint refused."""


def _endpoint(settings) -> tuple[str, str, str]:
    """(url, model, api_key) for the configured embed role."""
    target = embed_target(settings)
    if target is None:
        raise EmbedError(NOT_CONFIGURED)
    name, model = target
    provider = PROVIDERS.get(name)
    if provider is None:
        raise EmbedError(f"embedding 服务商 '{name}' 不存在"
                         f"（可选：{', '.join(sorted(PROVIDERS))}）")
    if provider.kind != "openai":
        raise EmbedError(f"'{name}' 走的是 {provider.kind} 协议，"
                         "没有 OpenAI 兼容的 /embeddings 端点")
    base = (provider.configured_base_url() or "").rstrip("/")
    if not base:
        raise EmbedError(f"'{name}' 没有配置 base_url")
    return f"{base}/embeddings", model, provider_key(name, settings)


def _post(url: str, model: str, key: str, texts: list[str]) -> list[list[float]]:
    body = json.dumps({"model": model, "input": texts}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "KnowMe"}
    if key:
        # A local endpoint usually ignores this, but the header is free and a
        # hosted one requires it. `client_for` insists on a non-empty key even
        # for localhost, so in practice this is a placeholder — which is fine:
        # it is a credential to ollama, not a secret.
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise EmbedError(f"embedding 端点返回 HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise EmbedError(f"连不上 embedding 端点（{url}）：{exc.reason}") from exc
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise EmbedError(f"embedding 端点响应异常：{exc}") from exc

    rows = data.get("data")
    if not isinstance(rows, list):
        raise EmbedError(f"embedding 端点没有返回 data：{str(data)[:200]}")
    # Order matters and is not guaranteed to be the order we sent, so trust the
    # index the endpoint reports rather than the position in the list.
    rows = sorted(rows, key=lambda row: row.get("index", 0))
    vectors = [row.get("embedding") or [] for row in rows]
    if len(vectors) != len(texts):
        raise EmbedError(f"请求了 {len(texts)} 段，只回来 {len(vectors)} 个向量")
    widths = {len(vector) for vector in vectors}
    if len(widths) > 1:
        # Mixed widths inside one response cannot be compared to each other, and
        # storing them would make every later search silently skip rows.
        raise EmbedError(f"同一个批次里向量长度不一致：{sorted(widths)}")
    return [list(map(float, vector)) for vector in vectors]


def embed_texts(settings, texts: list[str], on_progress=None) -> list[list[float]]:
    """Vectors for `texts`, in the order given.

    `on_progress(done, total)` is called after each batch — the indexer uses it
    to write the counter the library page shows, so progress is counted where
    the work actually happens rather than guessed from outside.
    """
    texts = list(texts)
    if not texts:
        return []
    url, model, key = _endpoint(settings)
    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH):
        batch = texts[start:start + BATCH]
        vectors.extend(_post(url, model, key, batch))
        if on_progress is not None:
            on_progress(min(start + BATCH, len(texts)), len(texts))
    return vectors


def embed_one(settings, text: str) -> list[float]:
    """One query vector. Raises EmbedError, same as embed_texts."""
    return embed_texts(settings, [text])[0]
