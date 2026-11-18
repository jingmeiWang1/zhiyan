"""Cut a document into the pieces the semantic index stores.

Pure functions of a string: no network, no database, no settings. Chunk
boundaries decide what retrieval can and cannot find, so the rules have to be
testable exhaustively offline.

THE STRATEGY, IN THREE LAYERS
    1. Structure first — a Markdown heading path is the document's own idea of
       where a topic begins and ends, so it is split on before anything else,
       and every chunk carries its path ("长上下文 › 检索") as an address. The
       path is per-section, so it separates chunks rather than flattening them
       together the way a repeated document title would.
    2. Size second — anything still too long is cut **recursively**: paragraph
       → line → sentence → clause → word → character. A weaker separator is
       only reached when a stronger one cannot get the piece small enough, so a
       cut lands mid-sentence only for text with no punctuation at all.
    3. Fill third — the resulting units are packed back up to a target size with
       a little overlap, because a one-line paragraph carries too little context
       for its vector to mean anything alone.

WHY NOT ONE CHUNK PER PDF PAGE
    A page is a layout unit, not a topic: it can hold the tail of one section
    and the head of the next, and a paragraph broken by a page break gets sawn
    in half. So a PDF is split as text like anything else — and each chunk's
    character offsets are then mapped back to the pages it covers, which keeps
    the citation exact ("第 5–6 页") without letting the page dictate the topic
    boundary. The offsets are exact because the splitter never drops or invents
    a character.

WHY NOT SEMANTIC CHUNKING
    Embedding every sentence to find where the topic shifts means paying for a
    second full pass over the document at index time, and then the boundaries
    move whenever the embedding model changes. Structure + recursive splitting
    gets most of the way there for free, and — the part that matters here —
    keeps "which page was that?" answerable.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

# All sizes are characters, and the same numbers have to serve 中文 and English:
# ~800 characters is roughly 800 tokens of Chinese and roughly 200 tokens of
# English, which is a usable range on both sides of that asymmetry. bge-m3's
# window is 8192 tokens, so even the 1600-character ceiling is nowhere near
# being truncated — and a much larger chunk would average several topics into
# one vector, which is what makes retrieval miss.
CHUNK_TARGET = 800     # the packer fills up to here
CHUNK_MAX = 1600       # a single unit over this gets split further
CHUNK_MIN = 150        # a leftover shorter than this is not worth its own row
OVERLAP = 120          # repeated between neighbouring chunks (~15%)

# Strongest separator first. The English ones carry a trailing space so a cut
# cannot land inside "3.14" or "et al."; the Chinese ones do not need it because
# 中文 has no inter-word spaces to preserve.
SEPARATORS = [
    "\n\n", "\n",
    "。", "！", "？", "；",
    ". ", "! ", "? ", "; ",
    "，", ", ",
    " ",
    "",
]

# Below these, a piece is a page number or a running header rather than prose.
MIN_CHARS = 20
MIN_LETTERS = 25

_HEADING = re.compile(r"^(#{1,6})[ \t]*(.+?)[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    """One indexed piece.

    `page`/`page_end` are 1-based and span the range the chunk covers — a chunk
    can straddle a page break, and saying so is more honest than naming one
    page and letting the reader hunt for the rest. Both are None for documents
    with no pages at all.
    """
    text: str
    page: int | None = None
    page_end: int | None = None
    heading: str = ""


def has_content(text: str) -> bool:
    """Is this prose, or is it a page number / running header / table of digits?"""
    stripped = text.strip()
    if len(stripped) < MIN_CHARS:
        return False
    # `isalpha()` is True for CJK as well as for Latin letters, so one rule
    # covers 中文 and English.
    return sum(1 for ch in stripped if ch.isalpha()) >= MIN_LETTERS


def _reattach(parts: list[str], separator: str) -> list[str]:
    """`split()` throws the separator away; put it back, so the pieces still
    concatenate into the original text. Every offset downstream depends on
    nothing being lost or invented."""
    return [part + separator for part in parts[:-1]] + [parts[-1]]


def _split_units(text: str, limit: int, separators: list[str],
                 base: int = 0) -> list[tuple[int, str]]:
    """(offset, text) pieces no longer than `limit`, in document order.

    Recursive, in the sense that matters: try the strongest separator present,
    and re-split only the pieces that are STILL too long using the weaker ones
    that remain. Nothing is trimmed — blank pieces are kept so the offsets stay
    exactly contiguous with the original.
    """
    if len(text) <= limit:
        return [(base, text)]
    for index, separator in enumerate(separators):
        if not separator:
            break
        if separator not in text:
            continue
        units: list[tuple[int, str]] = []
        cursor = base
        for piece in _reattach(text.split(separator), separator):
            if len(piece) <= limit:
                units.append((cursor, piece))
            else:
                units.extend(_split_units(piece, limit, separators[index + 1:], cursor))
            cursor += len(piece)
        return units
    # Out of separators: a run of text with no punctuation, no spaces and no
    # newlines. This is the only place a "sentence" can be sawn in half.
    return [(base + offset, text[offset:offset + limit])
            for offset in range(0, len(text), limit)]


def _merge(units: list[tuple[int, str]], target: int,
           overlap: int) -> list[tuple[int, str]]:
    """Pack units up to `target`, carrying a tail of `overlap` into the next one.

    The overlap is what keeps a sentence that straddles a seam findable from
    either side. `start` is computed from the first unit's own offset minus
    whatever tail was carried in, so the returned offsets still point at the
    real position in the document.
    """
    merged: list[tuple[int, str]] = []
    buffered: list[str] = []
    size = 0
    start = 0
    carried = 0

    def flush() -> None:
        """Close the current chunk and carry its tail into the next one."""
        nonlocal buffered, size, carried
        body = "".join(buffered)
        merged.append((start, body))
        tail = body[-overlap:] if len(body) > overlap else ""
        buffered = [tail] if tail else []
        size = len(tail)
        carried = len(tail)

    for offset, unit in units:
        # "Am I at the start of a chunk?" — NOT "is the buffer empty?". After a
        # flush the buffer holds the carried tail, so it is non-empty while the
        # next chunk has already begun; testing the buffer instead of this flag
        # left `start` at 0 and every chunk in a 15-page paper was reported as
        # page 1.
        if not buffered:
            start = offset - carried
        elif size + len(unit) > target:
            # Close BEFORE adding, so a chunk never overshoots by one whole
            # unit. (Filling first and testing after is the other way round, and
            # it is how a 1600-character unit ends up in a 1720-character chunk.)
            flush()
            start = offset - carried
        buffered.append(unit)
        size += len(unit)
    rest = "".join(buffered)
    # A leftover that is nothing but the carried tail was already emitted; one
    # that adds only a sliver is not worth its own row.
    if rest.strip() and len(rest) - carried >= CHUNK_MIN:
        merged.append((start, rest))
    return merged


def _flatten(pages: list[str]) -> tuple[str, list[int]]:
    """The document as one string, plus where each page starts in it."""
    text = ""
    starts: list[int] = []
    for page in pages:
        starts.append(len(text))
        text += page + "\n\n"
    return text, starts


def _pages_for(starts: list[int], start: int, end: int) -> tuple[int, int]:
    """Which pages a chunk covers, from its character offsets."""
    first = bisect.bisect_right(starts, start)
    last = bisect.bisect_right(starts, end - 1) if end > start else first
    return max(1, first), max(1, last)


def _sections(text: str) -> list[tuple[int, int, str]]:
    """[(start, end, heading path)] — one span per heading, plus the preamble.

    A span ends where the next heading begins, so the spans are contiguous and
    cover the whole document. The preamble (whatever sits above the first
    heading) has no path and so falls back to plain packing.
    """
    marks: list[tuple[int, str]] = [(0, "")]
    stack: list[tuple[int, str]] = []
    for match in _HEADING.finditer(text):
        level = len(match.group(1))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, match.group(2).strip()))
        marks.append((match.start(), " › ".join(title for _, title in stack)))
    return [(start, marks[index + 1][0] if index + 1 < len(marks) else len(text), path)
            for index, (start, path) in enumerate(marks)]


def chunks_for(kind: str, pages: list[str]) -> list[Chunk]:
    """The front door: turn a document's pages into indexed chunks."""
    text, starts = _flatten(pages)
    # A heading is a HARD boundary, not a preference. Packing across one would
    # put the tail of "检索" and the head of "缓存" in the same chunk under the
    # first one's address — precisely the topic dilution that makes a chunk
    # unmatchable. So each section is packed on its own, with `base` carrying
    # the absolute offset so page attribution still works.
    spans = _sections(text) if kind == "markdown" else [(0, len(text), "")]
    chunks: list[Chunk] = []
    for start, end, heading in spans:
        # Units are cut to CHUNK_MAX - OVERLAP, not CHUNK_MAX: the packer adds an
        # overlap's worth on top, so this is what keeps the RESULT under the cap.
        for offset, body in _merge(_split_units(text[start:end], CHUNK_MAX - OVERLAP,
                                                SEPARATORS, base=start),
                                   CHUNK_TARGET, OVERLAP):
            page, page_end = ((_pages_for(starts, offset, offset + len(body)))
                              if kind == "pdf" else (None, None))
            # The heading goes in front of the body, not into it: the body is
            # what the size limits were measured against. It also happens to be
            # what gives a very short section enough context to be worth a row
            # at all — "## 缓存 / 用 KV cache。" is three words and a title.
            indexed = f"{heading}\n\n{body}" if heading and body.strip() else body
            if has_content(indexed):
                chunks.append(Chunk(text=indexed, page=page, page_end=page_end,
                                    heading=heading))
    return chunks
