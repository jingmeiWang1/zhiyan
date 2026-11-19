"""Byte ranges — the arithmetic half of serving a big file lazily.

WHY THIS EXISTS SEPARATELY FROM server.py
    It is the one part of HTTP range support that is pure: given a header and a
    file size, which bytes does the client want? No socket, no file handle, no
    handler state — so the whole case table can be tested offline, which is the
    same split ``library.text_window`` uses for agent reads.

WHY RANGES AT ALL
    pdf.js asks for the bytes it needs and no others: it reads the trailer at
    the END of the file first, then the cross-reference table, then a page at a
    time. A server that ignores ``Range`` makes it download the entire 20 MB
    before the first page appears — which is exactly how this project's reader
    behaved for its first year. The header it looks for is ``Accept-Ranges:
    bytes``; without that it never even asks.

THREE ANSWERS, NOT TWO
    ``None`` (ignore the header, send the whole file) and a pair are the obvious
    two. The third is ``RangeError``: the request is well-formed but asks for
    bytes that do not exist, and the answer to that is 416 — NOT the whole file.
    Replying 200 to ``bytes=999999-`` on a 100-byte file is how a client that
    wanted one byte ends up downloading the whole thing.
"""

from __future__ import annotations


class RangeError(ValueError):
    """A Range that cannot be satisfied — the caller answers 416, not 200."""


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The inclusive ``(start, end)`` a single range asks for, or None.

    None means "ignore this header and send the whole body", which RFC 9110
    explicitly allows for anything we do not want to implement: another unit
    than bytes, a multipart request (several ranges at once — pdf.js never sends
    one), or a spec we cannot parse. Being liberal here is safe because the
    fallback is the answer this endpoint gave before ranges existed.

    Only the *unsatisfiable* case is an error: it is the one where the liberal
    answer would be actively harmful.
    """
    if not header:
        return None
    text = header.strip()
    if not text.lower().startswith("bytes="):
        return None                                   # another unit: not ours
    spec = text[len("bytes="):].strip()
    if "," in spec:
        return None                                   # multipart: not ours
    if "-" not in spec:
        return None                                   # not a range at all
    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()

    if not first:
        # "bytes=-500": the LAST 500 bytes. How pdf.js finds a trailer whose
        # offset it does not know yet, so it is not a curiosity — it is the
        # first request a PDF viewer makes.
        if not last.isdigit():
            return None
        wanted = int(last)
        if not wanted or size == 0:
            raise RangeError(f"cannot read the last {wanted} bytes of {size}")
        start = max(0, size - wanted)
        return start, size - 1

    if not first.isdigit():
        return None
    start = int(first)
    if start >= size:
        # Past the end. "Give me everything from byte 999999" has no non-empty
        # answer, and answering with the whole file is the failure mode above.
        raise RangeError(f"byte {start} is past the end of {size} bytes")
    if not last:
        return start, size - 1                        # "bytes=500-"
    if not last.isdigit():
        return None
    end = int(last)
    if end < start:
        return None                                   # "bytes=500-100": nonsense
    return start, min(end, size - 1)                  # a too-large end is clamped
