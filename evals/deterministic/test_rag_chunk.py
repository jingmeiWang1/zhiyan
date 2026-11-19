"""OFFLINE checks for the chunker — the part of RAG that decides what can be found.

No network, no database, no model: `chunk.py` is pure, so its boundary rules can
be pinned exactly. That matters more here than anywhere else in the feature,
because a chunking mistake does not throw — it just quietly stops matching.

The properties worth protecting, in the order they bite:

  * **Nothing is lost.** A splitter that drops or invents characters shifts
    every page number computed from an offset.
  * **A page number is true.** A chunk's stated page range has to contain the
    text it claims, or a citation sends the reader somewhere the passage is not.
  * **Sizes are bounded.** A chunk longer than the embedding window is truncated
    by the model *silently*, which looks like a model that simply forgot.
  * **Cuts happen at sentence boundaries** — unless the text has no punctuation,
    which is the only case that gets sawn in half.
"""

from __future__ import annotations

from itertools import pairwise

from zhiyan.rag import chunk as C


def page_window(pages: list[str], first: int, last: int) -> str:
    """The pages named by a citation, as one string — built the way the document
    itself is laid out, so a chunk that STARTS on a page break can still be
    found. Joining with a single newline here made a boundary-spanning chunk
    look like a lie when it was not."""
    return "".join(page + "\n\n" for page in pages[first - 1:last])


def test_a_markdown_heading_path_becomes_the_chunk_address():
    """Structure first: the heading path is the document's own idea of where a
    topic starts and ends, and it is what a short section has instead of length."""
    text = ("# 长上下文\n\n" + "甲" * 300 + "\n\n"
            "## 检索\n\n" + "乙" * 300 + "\n\n"
            "## 缓存\n\n" + "丙" * 300)
    chunks = C.chunks_for("markdown", [text])

    assert [c.heading for c in chunks] == ["长上下文", "长上下文 › 检索", "长上下文 › 缓存"], \
        [c.heading for c in chunks]
    # The path goes INSIDE the indexed text, not just beside it — otherwise the
    # embedding never sees it and "缓存那一节讲了什么" has nothing to match.
    assert chunks[2].text.startswith("长上下文 › 缓存")
    assert "丙" * 300 in chunks[2].text


def test_a_long_section_is_split_but_keeps_its_address():
    body = "。".join(f"第{i}个句子讲的是某个具体的事情" for i in range(120))
    chunks = C.chunks_for("markdown", [f"# 大节\n\n{body}"])

    assert len(chunks) > 1, "a section far over the ceiling has to be cut"
    assert all(c.heading == "大节" for c in chunks), "every piece keeps its 地址"
    assert all(len(c.text) <= C.CHUNK_MAX + len("大节") + 2 for c in chunks), \
        sorted(len(c.text) for c in chunks)


def test_a_page_number_is_true_for_the_text_it_labels():
    """The check that would have caught a whole-paper-reported-as-page-1 bug:
    every chunk's first characters have to be findable in the pages it names."""
    pages = [f"Page {n} of 8. " + f"word{n} " * 320 for n in range(1, 9)]
    chunks = C.chunks_for("pdf", pages)

    assert chunks, "eight full pages should produce chunks"
    for item in chunks:
        window = page_window(pages, item.page, item.page_end)
        assert item.text[:30] in window, (item.page, item.page_end, item.text[:60])
    pages_seen = [c.page for c in chunks]
    assert pages_seen == sorted(pages_seen), "页码必须单调，否则引用会在文档里来回跳"
    assert {c.page for c in chunks} == set(range(1, 9)), "每一页都要被覆盖到"


def test_a_chunk_that_straddles_a_page_break_names_both_pages():
    """A paragraph broken by a page break should stay whole — and then saying
    only the first page would send the reader to a page the passage leaves."""
    pages = ["甲" * 900 + "跨页的句子从这里开始……", "……并在这里结束。" + "乙" * 900]
    chunks = C.chunks_for("pdf", pages)

    straddling = [c for c in chunks if c.page != c.page_end]
    assert straddling, "这一份就是为跨页造的"
    assert all((c.page, c.page_end) == (1, 2) for c in straddling)


def test_a_run_of_text_with_no_separators_is_still_bounded():
    """The one place a sentence can be cut in half: no punctuation, no spaces,
    no newlines. It must not become one enormous chunk the embedder truncates
    without saying so."""
    chunks = C.chunks_for("text", ["z" * 5000])

    assert len(chunks) >= 3
    assert all(len(c.text) <= C.CHUNK_MAX for c in chunks)


def test_short_junk_is_not_worth_a_vector():
    """A page number and a running header still get a vector, and that vector
    still gets to occupy a top-k slot. Better to have no row at all."""
    chunks = C.chunks_for("text", ["1706.03762v7  [cs.CL]  15 Oct 2017"])
    assert chunks == []

    # …and a document made only of whitespace is not an error, just empty.
    assert C.chunks_for("markdown", ["\n\n\n   \n"]) == []


def test_neighbouring_chunks_overlap_so_a_seam_is_findable_from_both_sides():
    body = "\n\n".join(f"这是第 {i} 段，里面有足够多的字来撑起一个块。" * 6 for i in range(40))
    chunks = C.chunks_for("text", [body])

    assert len(chunks) > 2
    for before, after in pairwise(chunks):
        tail = before.text[-C.OVERLAP:]
        assert tail[:20] in after.text or after.text.startswith(tail[:20]), \
            "相邻两块之间要有重叠，否则跨接缝的那句话两边都搜不到"


def test_every_character_of_the_document_survives_chunking():
    """Offsets are how a chunk knows its page, so the splitter must not drop or
    invent a single character on the way through."""
    text = "# 标题\n\n第一段。\n第二行\n\n第二段，有，很多，逗号。\n\n" + "尾" * 400
    units = C._split_units(text, C.CHUNK_MAX, C.SEPARATORS)
    assert "".join(piece for _, piece in units) == text
    assert [offset for offset, _ in units] == sorted(offset for offset, _ in units)

    merged = C._merge(units, C.CHUNK_TARGET, C.OVERLAP)
    assert merged
    assert merged[0][0] == 0, "第一块必须从文档开头算起"
    assert [offset for offset, _ in merged] == sorted(offset for offset, _ in merged)


def test_an_empty_document_produces_nothing_rather_than_raising():
    assert C.chunks_for("pdf", []) == []
    assert C.chunks_for("pdf", ["", "", ""]) == []
    assert C.chunks_for("text", [""]) == []
