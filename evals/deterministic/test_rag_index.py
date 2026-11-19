"""OFFLINE checks for the vector store, the index job and the worker.

The embedding endpoint here is a REAL http server on an ephemeral port, not a
monkeypatched `urlopen`. That matters: the parts most likely to be wrong are the
ones a monkeypatch would happily agree with — the request body's shape, the
status code on failure, whether the response is parsed in the order it was sent.
A stub that speaks the protocol catches those; a fake function does not.

The vectors it returns are a hashed bag of words, so "similar text" really does
mean "similar vector" and ranking assertions test ranking rather than arithmetic.
"""

from __future__ import annotations

import json
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from zhiyan.applications import library
from zhiyan.core import custom_providers
from zhiyan.db import connect
from zhiyan.rag import index as index_mod
from zhiyan.rag import worker
from zhiyan.rag.embed import EmbedError, embed_texts
from zhiyan.rag.query import RECALL, semantic_search
from zhiyan.rag.rerank import DOC_CHARS, rerank_scores
from zhiyan.rag.store import ERROR, QUEUED, READY, all_states, pack, search, unpack

DIM = 64


def fake_vector(text: str) -> list[float]:
    """Hashed character n-grams — cosine similarity between these is meaningful.

    n-grams rather than words because 中文 has no spaces: splitting on whitespace
    makes a whole Chinese sentence a single token, so a Chinese query and a
    Chinese passage would share nothing and every score would be 0.0 — a test
    that passes while measuring nothing. crc32 rather than hash() so the buckets
    do not move between processes.
    """
    vector = [0.0] * DIM
    flat = " ".join(text.lower().split())
    for n in (2, 3):
        for i in range(len(flat) - n + 1):
            vector[zlib.crc32(flat[i:i + n].encode()) % DIM] += 1.0
    return vector


def last_candidate_wins(query, documents):
    """The reranker's opinion, and it is deliberately NOT the vector order: the
    candidate the vector search ranked LAST comes back FIRST.

    Two things a real reranker does that a lazy stub would not, and both matter.
    The rows come back SORTED BY SCORE rather than in the order they were sent,
    and the score is not the same number as the index. A stub that echoed the
    input in order would let a "place the score by list position" bug pass every
    test in this file.
    """
    return {"results": [{"index": i, "relevance_score": float(i)}
                        for i in reversed(range(len(documents)))]}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or "{}")
        self.server.requests.append(payload)          # the test reads this back
        self.server.authorization = self.headers.get("Authorization")
        if self.server.fail_with:
            body = json.dumps({"error": {"message": self.server.fail_with}}).encode()
            self.send_response(self.server.fail_with_status)
        elif self.path.endswith("/embeddings"):
            body = json.dumps({"data": [
                {"index": i, "embedding": fake_vector(text)}
                for i, text in enumerate(payload.get("input", []))]}).encode()
            self.send_response(200)
        elif self.path.endswith("/rerank"):
            self.server.rerank_requests.append(payload)
            if self.server.fail_rerank_with:
                body = json.dumps(
                    {"error": {"message": self.server.fail_rerank_with}}).encode()
                self.send_response(self.server.fail_rerank_status)
            else:
                body = json.dumps(self.server.rerank_fn(
                    payload.get("query", ""), payload.get("documents", []))).encode()
                self.send_response(200)
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def endpoint(tmp_path):
    """A live server with /v1/embeddings AND /v1/rerank, as two custom
    providers on the same base — which is how llama-server actually serves
    them, except llama.cpp needs one process per mode."""
    import os

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests, server.authorization = [], None
    server.fail_with, server.fail_with_status = "", 500
    server.rerank_requests, server.rerank_fn = [], last_candidate_wins
    server.fail_rerank_with, server.fail_rerank_status = "", 500
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v1"
    custom_providers.register(tmp_path, {
        "id": "fake_embed", "label": "fake", "kind": "openai",
        "base_url": base, "model": "fake-embed", "small_model": "fake-embed"})
    custom_providers.register(tmp_path, {
        "id": "fake_rerank", "label": "fake rerank", "kind": "openai",
        "base_url": base, "model": "fake-rerank"})
    # provider_key() reads these env vars for a custom provider; without one
    # client_for would refuse to build a client at all.
    os.environ["ZHIYAN_CUSTOM_FAKE_EMBED_API_KEY"] = "test-key"
    os.environ["ZHIYAN_CUSTOM_FAKE_RERANK_API_KEY"] = "test-key"
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        os.environ.pop("ZHIYAN_CUSTOM_FAKE_EMBED_API_KEY", None)
        os.environ.pop("ZHIYAN_CUSTOM_FAKE_RERANK_API_KEY", None)


def make_settings(home, provider="fake_embed", model="fake-embed", rerank=""):
    """`rerank` is a model id, and empty means the reranker is OFF.

    Both halves are set explicitly rather than left to `Settings()` defaults: the
    maintainer's own .env is loaded at import time, so "not configured" has to be
    stated here or this suite would measure their machine.
    """
    from zhiyan.config import Settings

    settings = Settings()
    settings.home = home
    settings.embed_provider, settings.embed_model = provider, model
    settings.rerank_provider, settings.rerank_model = (
        ("fake_rerank", rerank) if rerank else ("", ""))
    return settings


@pytest.fixture
def rag(tmp_path, endpoint):
    home = tmp_path / "home"
    home.mkdir()
    conn = connect(home)
    settings = make_settings(home)
    return settings, conn, home


def save(conn, home, name, text, *, kind=None):
    """Put a document in the library the way an upload would."""
    doc = library.save_document(conn, home, name=name, raw=text.encode(),
                                text="\n\n".join(text.split("\n\n")), source=name)
    if kind:
        conn.execute("UPDATE documents SET kind=? WHERE id=?", (kind, doc["id"]))
        conn.commit()
    return doc


# --- the store ---------------------------------------------------------------


def test_vectors_survive_the_round_trip_through_a_blob():
    vector = [0.5, -1.25, 3.0, 0.0]
    assert unpack(pack(vector), 4) == pytest.approx(vector)


def test_search_ranks_by_cosine_and_honours_the_document_filter(tmp_path):
    conn = connect(tmp_path)
    a = library.save_document(conn, tmp_path, name="a.md", raw=b"a", text="x")
    b = library.save_document(conn, tmp_path, name="b.md", raw=b"b", text="x")
    from zhiyan.rag.store import normalize, replace_chunks

    replace_chunks(conn, a["id"], [
        (0, None, None, "光合作用", normalize(fake_vector("photosynthesis plants"))),
        (1, None, None, "别的", normalize(fake_vector("unrelated words here"))),
    ])
    replace_chunks(conn, b["id"], [
        (0, None, None, "另一份", normalize(fake_vector("photosynthesis again"))),
    ])

    everything = search(conn, fake_vector("photosynthesis"), k=5)
    assert len(everything) == 3
    assert everything[0]["score"] >= everything[-1]["score"]

    only_a = search(conn, fake_vector("photosynthesis"), k=5, doc_id=a["id"])
    assert [hit["doc_id"] for hit in only_a] == [a["id"], a["id"]]
    assert only_a[0]["text"] == "光合作用"


def test_a_vector_of_the_wrong_width_is_skipped_not_misread(tmp_path):
    """This only happens when the embedding model changed. Unpacking it at the
    wrong stride would produce a number, and the number would be nonsense."""
    conn = connect(tmp_path)
    doc = library.save_document(conn, tmp_path, name="a.md", raw=b"a", text="x")
    from zhiyan.rag.store import replace_chunks

    replace_chunks(conn, doc["id"], [(0, None, None, "旧模型建的", [1.0, 0.0, 0.0])])
    assert search(conn, [1.0, 0.0], k=5) == []


# --- the embedding call ------------------------------------------------------


def test_the_request_body_is_the_openai_embeddings_shape(rag, endpoint):
    settings, _, _ = rag
    vectors = embed_texts(settings, ["a b", "c d"])

    assert len(vectors) == 2 and len(vectors[0]) == DIM
    assert endpoint.requests[-1] == {"model": "fake-embed", "input": ["a b", "c d"]}
    assert endpoint.authorization == "Bearer test-key"


def test_requests_are_batched_and_progress_is_counted_where_the_work_is(rag):
    settings, _, _ = rag
    seen: list[tuple[int, int]] = []
    embed_texts(settings, [f"text {i}" for i in range(40)],
                on_progress=lambda done, total: seen.append((done, total)))
    assert seen == [(16, 40), (32, 40), (40, 40)]


def test_an_endpoint_error_becomes_a_message_and_not_a_traceback(rag, endpoint):
    """The endpoint's own words have to survive into the exception — "model not
    found" is the message that tells the user to run `ollama pull`, and a bare
    "HTTP 404" tells them nothing."""
    settings, _, _ = rag
    endpoint.fail_with, endpoint.fail_with_status = "model not found", 404
    with pytest.raises(EmbedError, match="404") as raised:
        embed_texts(settings, ["hello"])
    assert "model not found" in str(raised.value)


def test_nothing_configured_is_a_sentence_not_an_exception(tmp_path):
    from zhiyan.config import Settings

    settings = Settings()
    settings.home = tmp_path
    settings.embed_provider = settings.embed_model = ""
    with pytest.raises(EmbedError, match="未启用") as raised:
        embed_texts(settings, ["hello"])
    # …and the message says what to set, not just that something is missing.
    assert "ZHIYAN_EMBED_MODEL" in str(raised.value)


# --- indexing ----------------------------------------------------------------


def test_indexing_a_document_makes_it_searchable_by_meaning(rag):
    settings, conn, home = rag
    doc = save(conn, home, "paper.md",
               "# 第一章\n\n" + "光合作用把光能变成化学能。" * 30
               + "\n\n# 第二章\n\n" + "路由器把数据包转发到下一跳。" * 30)

    out = index_mod.index_document(settings, conn, home, doc["id"])
    assert out["ok"] and out["state"] == READY and out["total"] > 0

    hits = semantic_search(settings, conn, "光合作用", doc_id=doc["id"], k=3)
    assert hits, "索引完之后应该能按意思找到"
    assert hits[0]["score"] > hits[-1]["score"]
    # Every hit has to carry what the UI prints: which page, which document,
    # and a one-line snippet.
    assert hits[0]["label"] and hits[0]["snippet"] and hits[0]["title"] == "paper"


def test_reindexing_replaces_rather_than_doubles(rag):
    settings, conn, home = rag
    doc = save(conn, home, "paper.md", "# 一\n\n" + "重复内容。" * 60)

    index_mod.index_document(settings, conn, home, doc["id"])
    first = index_mod.index_info(settings, conn, doc["id"])["total"]
    index_mod.index_document(settings, conn, home, doc["id"])
    assert index_mod.index_info(settings, conn, doc["id"])["total"] == first
    assert len(set(all_states(conn))) == 1


def test_a_document_with_no_text_is_indexed_as_empty_and_says_so(rag):
    """A scanned PDF is not an error — there is simply nothing in it to embed,
    and the page must say that instead of showing a failure. This is what a
    real scan looks like to us: a valid PDF whose pages extract to nothing."""
    pypdf = pytest.importorskip("pypdf")
    import io

    settings, conn, home = rag
    writer = pypdf.PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    doc = library.parse_and_save(conn, home, name="scan.pdf", raw=buffer.getvalue())

    out = index_mod.index_document(settings, conn, home, doc["id"])
    assert out == {"ok": True, "state": READY, "total": 0}
    assert index_mod.index_info(settings, conn, doc["id"])["total"] == 0


def test_a_failed_job_is_recorded_in_words_not_swallowed(rag, endpoint):
    settings, conn, home = rag
    doc = save(conn, home, "paper.md", "# 一\n\n" + "内容。" * 60)
    endpoint.fail_with, endpoint.fail_with_status = "ollama: model not found", 404

    out = index_mod.index_document(settings, conn, home, doc["id"])
    assert out["ok"] is False and out["state"] == ERROR
    assert "model not found" in out["error"]
    assert index_mod.index_info(settings, conn, doc["id"])["error"]


# --- the worker --------------------------------------------------------------


def test_the_worker_does_nothing_at_all_when_rag_is_off(tmp_path):
    """The rule the whole feature rests on: no embed role configured means a
    workspace is byte-for-byte what it was before this existed."""
    home = tmp_path / "home"
    home.mkdir()
    conn = connect(home)
    save(conn, home, "paper.md", "# 一\n\n" + "内容。" * 60)

    settings = make_settings(home, provider="", model="")
    assert worker.tick(settings, conn, home) is None
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "document_index" not in tables and "document_chunks" not in tables


def test_the_worker_finds_documents_that_were_already_there(rag):
    """The reason there is no hook in save_document: the documents you had
    BEFORE you switched the feature on still get indexed."""
    settings, conn, home = rag
    doc = save(conn, home, "old.md", "# 一\n\n" + "早就存在的内容。" * 60)
    assert all_states(conn) == {}

    assert worker.tick(settings, conn, home) == doc["id"]
    assert index_mod.index_info(settings, conn, doc["id"])["state"] == READY


def test_changing_the_embedding_model_requeues_by_itself(rag):
    """Two models put the same sentence in unrelated places, and comparing
    across them does not fail — it returns confident nonsense. So a document
    built with a different model counts as work to do."""
    settings, conn, home = rag
    doc = save(conn, home, "paper.md", "# 一\n\n" + "内容。" * 60)
    worker.tick(settings, conn, home)
    assert index_mod.index_info(settings, conn, doc["id"])["stale"] is False

    settings.embed_model = "some-other-embed"
    info = index_mod.index_info(settings, conn, doc["id"])
    assert info["stale"] is True
    assert worker.tick(settings, conn, home) == doc["id"]
    assert index_mod.index_info(settings, conn, doc["id"])["stale"] is False


def test_an_error_is_not_retried_forever_but_a_rebuild_is(rag, endpoint):
    settings, conn, home = rag
    doc = save(conn, home, "paper.md", "# 一\n\n" + "内容。" * 60)
    endpoint.fail_with, endpoint.fail_with_status = "endpoint down", 503
    worker.tick(settings, conn, home)
    assert index_mod.index_info(settings, conn, doc["id"])["state"] == ERROR

    # Hammering a broken endpoint every second would be worse than waiting to be
    # asked, so the worker leaves it alone until something asks.
    assert worker.tick(settings, conn, home) is None

    endpoint.fail_with = ""
    worker.enqueue(conn, doc["id"])
    assert index_mod.index_info(settings, conn, doc["id"])["state"] == QUEUED
    assert worker.tick(settings, conn, home) == doc["id"]
    assert index_mod.index_info(settings, conn, doc["id"])["state"] == READY


def test_deleting_a_document_takes_its_vectors_with_it(rag):
    """Otherwise search keeps returning a hit whose document is gone, and
    clicking it opens nothing."""
    settings, conn, home = rag
    doc = save(conn, home, "paper.md", "# 一\n\n" + "内容。" * 60)
    worker.tick(settings, conn, home)
    assert search(conn, fake_vector("内容"), k=5)

    assert library.delete_document(conn, home, doc["id"]) is True
    assert search(conn, fake_vector("内容"), k=5) == []
    assert all_states(conn) == {}


def test_both_library_endpoints_carry_the_index_state(rag, monkeypatch):
    """The panel's header reads `currentDoc.index`, and the document it holds
    came from `open` — so BOTH requests have to carry it, not just the list.

    This is not a hypothetical: `open` raised NameError because the helper was
    imported inside the `list` branch, the route's catch-all turned that into a
    response with no `document` key at all, and the reader then refused to open
    *anything* — with no error on screen and nothing in the console.

    The settings are injected at the seam rather than through ZHIYAN_HOME: this
    route builds its own Settings and connection, and pointing it at the right
    home through the environment quietly stopped working once other test modules
    had run — the same call passed on its own and failed in the suite.
    """
    import zhiyan.ops.web.data as web_data
    from zhiyan.ops.web.data import library_action

    settings, conn, home = rag
    monkeypatch.setattr(web_data, "load_settings", lambda: settings)
    doc = save(conn, home, "paper.md", "# 一\n\n" + "内容。" * 60)

    opened = library_action({"action": "open", "doc_id": doc["id"]})
    assert opened["ok"], opened
    assert opened["document"]["id"] == doc["id"]
    assert opened["document"]["index"]["state"] == "none", "还没索引过"
    assert opened["document"]["index"]["enabled"] is True

    listed = library_action({"action": "list"})
    assert listed["documents"][0]["index"]["enabled"] is True

    # …and the state the worker writes shows up in both.
    worker.tick(settings, conn, home)
    assert library_action({"action": "open", "doc_id": doc["id"]})[
        "document"]["index"]["state"] == READY
    assert library_action({"action": "list"})["documents"][0]["index"]["total"] > 0


def test_reindex_puts_a_failed_document_back_in_the_queue(rag, monkeypatch):
    """The only way back from 索引失败. The worker deliberately never retries a
    failed row, so without this the document stays unsearchable for ever and
    there is nothing on screen that can change it."""
    import zhiyan.ops.web.data as web_data
    from zhiyan.ops.web.data import library_action
    from zhiyan.rag import store

    settings, conn, home = rag
    monkeypatch.setattr(web_data, "load_settings", lambda: settings)
    doc = save(conn, home, "paper.md", "# 一\n\n" + "内容。" * 60)
    store.mark(conn, doc["id"], model="fake_embed:fake-embed", state=ERROR,
               error="ollama 没开")
    assert worker.tick(settings, conn, home) is None, "失败的行不会自己重来"

    out = library_action({"action": "reindex", "doc_id": doc["id"]})
    assert out["ok"] and out["index"]["state"] == QUEUED

    assert library_action({"action": "reindex", "doc_id": "nope"})["ok"] is False
    assert worker.tick(settings, conn, home) == doc["id"]
    assert index_mod.index_info(settings, conn, doc["id"])["state"] == READY


def test_the_tool_tells_the_model_when_semantic_search_is_not_configured(tmp_path):
    from zhiyan.tools.documents import make_document_tools

    conn = connect(tmp_path)
    tools = make_document_tools(conn, tmp_path)          # no settings at all
    assert "未启用" in tools["semantic_search"].fn("随便问点什么")
    assert "semantic_search" in tools


def test_the_tool_returns_passages_with_their_document_and_page(rag):
    from zhiyan.tools.documents import make_document_tools

    settings, conn, home = rag
    doc = save(conn, home, "paper.md", "# 一\n\n" + "光合作用把光能变成化学能。" * 30)
    worker.tick(settings, conn, home)

    out = make_document_tools(conn, home, settings=settings)["semantic_search"].fn("光合作用")
    assert doc["id"] in out
    assert "相似度" in out
    assert "光合作用" in out

    # Nothing indexed anywhere: the honest answer is "nothing found", not an
    # empty string the model would read as "the library is uninteresting".
    elsewhere = home.parent / "empty"
    elsewhere.mkdir()
    nothing = make_document_tools(connect(elsewhere), home,
                                  settings=settings)["semantic_search"].fn("光合作用")
    assert "没有找到" in nothing


# --- the reranker -------------------------------------------------------------


TOPICS = ["光合作用把光能变成化学能储存在有机物里",
          "路由器根据路由表把数据包转发到下一跳",
          "酸面团依靠野生酵母和乳酸菌长时间发酵",
          "氢燃料电池排放的只有水没有二氧化碳",
          "潮汐能来自月球和太阳对海水的引力",
          "键值缓存把注意力计算的结果留在显存里"]

QUESTION = "光合作用把光能变成什么"


def fill_library(settings, conn, home):
    """Six documents, one chunk each — bodies are long enough to clear the
    chunker's has_content filter, which a one-line document would not."""
    docs = [save(conn, home, f"d{i}.md", "# 一\n\n" + topic * 30)
            for i, topic in enumerate(TOPICS)]
    for doc in docs:
        index_mod.index_document(settings, conn, home, doc["id"])
    return docs


def test_the_reranker_decides_the_order_and_each_hit_says_which_score_it_is(rag):
    settings, conn, home = rag
    fill_library(settings, conn, home)

    vector_order = semantic_search(settings, conn, QUESTION, k=6)
    assert len(vector_order) == 6
    assert all(hit["reranked"] is False for hit in vector_order)

    settings.rerank_provider, settings.rerank_model = "fake_rerank", "fake-rerank"
    reranked = semantic_search(settings, conn, QUESTION, k=6)

    # The stub says "the vector search's last candidate wins", so an implementation
    # that ignored the reranker would return the SAME order — never the reverse.
    assert [hit["doc_id"] for hit in reranked] == [hit["doc_id"] for hit in vector_order][::-1]
    assert all(hit["reranked"] is True for hit in reranked)
    assert reranked[0]["score"] == 5.0        # the score the stub gave index 5


def test_a_configured_reranker_recalls_thirty_and_hands_back_k(rag, endpoint):
    settings, conn, home = rag
    doc = save(conn, home, "big.md",
               "# 一\n\n" + "光合作用把光能变成化学能储存在有机物里。" * 4000)
    index_mod.index_document(settings, conn, home, doc["id"])
    assert index_mod.index_info(settings, conn, doc["id"])["total"] > RECALL

    settings.rerank_provider, settings.rerank_model = "fake_rerank", "fake-rerank"
    hits = semantic_search(settings, conn, "光合作用", k=6)

    assert len(hits) == 6
    sent = endpoint.rerank_requests[-1]
    # The decisive evidence that this really is two stages: the endpoint is asked
    # about RECALL candidates, while only k come back. Neither number is visible
    # from the return value alone.
    assert len(sent["documents"]) == RECALL
    assert sent["model"] == "fake-rerank" and sent["query"] == "光合作用"
    assert endpoint.authorization == "Bearer test-key"


def test_only_the_opening_of_each_candidate_is_sent(rag, endpoint):
    """The endpoint reads query+document as one string, so thirty whole chunks
    can run its context out. The opening is where a chunk states its topic."""
    settings, _, _ = rag
    settings.rerank_provider, settings.rerank_model = "fake_rerank", "fake-rerank"

    rerank_scores(settings, "问题", ["头" * 30 + "尾" * 5000])

    sent = endpoint.rerank_requests[-1]["documents"]
    assert len(sent) == 1
    assert len(sent[0]) == DOC_CHARS and sent[0].startswith("头" * 30)


def test_with_no_reranker_configured_nothing_is_recalled_or_scored(rag, endpoint):
    """The rule the whole feature rests on: rerank off is byte-for-byte what
    semantic search did before it existed. The endpoint is never even asked."""
    settings, conn, home = rag
    fill_library(settings, conn, home)

    hits = semantic_search(settings, conn, QUESTION, k=3)

    assert len(hits) == 3, "没有重排时不该被放大到 RECALL 条"
    assert all(hit["reranked"] is False for hit in hits)
    assert endpoint.rerank_requests == []


def test_a_broken_reranker_falls_back_to_vector_order_and_says_why(rag, endpoint, capsys):
    """A reranker that is down must not take semantic search down with it. But
    the fallback must not be silent either — the model is told 相似度, not 相关度,
    and the reason is on stderr."""
    settings, conn, home = rag
    fill_library(settings, conn, home)
    vector_order = semantic_search(settings, conn, QUESTION, k=6)

    settings.rerank_provider, settings.rerank_model = "fake_rerank", "fake-rerank"
    endpoint.fail_rerank_with, endpoint.fail_rerank_status = "server busy", 503
    hits = semantic_search(settings, conn, QUESTION, k=6)

    assert [hit["doc_id"] for hit in hits] == [hit["doc_id"] for hit in vector_order]
    assert len(hits) == 6, "重排挂了也要照旧返回 k 条"
    assert all(hit["reranked"] is False for hit in hits)
    stderr = capsys.readouterr().err
    assert "重排没生效" in stderr and "503" in stderr


def test_results_are_placed_by_the_index_they_report(rag, endpoint):
    """The standard response shape does not promise the order we sent. Trusting
    list order puts scores on the wrong texts — plausibly and silently.

    The assertion is on WHICH DOCUMENT each score ends up on, not on the list of
    scores: sorting makes a misplaced permutation produce the same sorted scores,
    so a score-only assertion passes whether the placement is right or wrong.
    """
    settings, conn, home = rag
    fill_library(settings, conn, home)
    vector_order = [hit["doc_id"] for hit in
                    semantic_search(settings, conn, QUESTION, k=6)]

    # Rows come back in a scrambled order, and the score is a fixed map rather
    # than the index — so no coincidence can make the two placements agree.
    scramble = [3, 0, 5, 1, 4, 2]
    score_of = {0: 0.2, 1: 0.9, 2: 0.4, 3: 0.1, 4: 0.7, 5: 0.5}

    def shuffled(query, documents):
        rows = [{"index": i, "relevance_score": score_of[i]} for i in scramble]
        rows.append({"index": 99, "relevance_score": 999.0})   # an index never sent
        return {"results": rows}

    settings.rerank_provider, settings.rerank_model = "fake_rerank", "fake-rerank"
    endpoint.rerank_fn = shuffled
    hits = semantic_search(settings, conn, QUESTION, k=6)

    best_first = sorted(score_of, key=lambda i: score_of[i], reverse=True)
    assert [hit["doc_id"] for hit in hits] == [vector_order[i] for i in best_first]
    # …and the row for an index we never sent is gone, not sitting at the top.
    assert 999.0 not in [hit["score"] for hit in hits]
