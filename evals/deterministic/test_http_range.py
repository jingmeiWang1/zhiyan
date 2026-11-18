"""OFFLINE checks for the Range arithmetic behind serving a PDF lazily.

Why this case table is worth its length: every row here is a request pdf.js
really sends, or one a browser ends up sending by accident. The three answers
are all load-bearing —

  * a pair      → 206 with exactly those bytes,
  * None        → 200 with the whole file (what this endpoint did before Range
                  support existed, so it can never be worse than that),
  * RangeError  → 416.

The one that hurts if it is wrong is ``None``: turning an unsatisfiable request
into a 200 means the client downloads the entire file to answer a question about
one byte, which is the exact behaviour this feature exists to remove.
"""

from __future__ import annotations

import pytest

from knowme.ops.web.http_range import RangeError, parse_range

SIZE = 1000


def test_no_header_is_not_a_range():
    assert parse_range(None, SIZE) is None
    assert parse_range("", SIZE) is None
    assert parse_range("   ", SIZE) is None


def test_another_unit_or_a_multipart_request_is_ignored():
    """RFC 9110 lets a server ignore what it does not implement, and the
    fallback (the whole file) is always a correct answer to a Range request."""
    assert parse_range("items=0-1", SIZE) is None
    assert parse_range("bytes=0-99,200-299", SIZE) is None


def test_a_plain_range_is_inclusive():
    assert parse_range("bytes=0-99", SIZE) == (0, 99)
    assert parse_range("bytes=0-0", SIZE) == (0, 0)


def test_an_open_ended_range_runs_to_the_last_byte():
    assert parse_range("bytes=500-", SIZE) == (500, 999)


def test_a_suffix_range_counts_back_from_the_end():
    """'bytes=-500' is how a PDF viewer asks for the trailer before it knows
    where the trailer is. Getting this wrong breaks the FIRST request."""
    assert parse_range("bytes=-500", SIZE) == (500, 999)
    assert parse_range("bytes=-99999", SIZE) == (0, 999)   # more than the file


def test_an_end_past_the_file_is_clamped_not_refused():
    """Clients routinely ask for a fixed chunk size and overrun on the last
    one; the answer is the bytes that exist, not an error."""
    assert parse_range("bytes=900-5000", SIZE) == (900, 999)


def test_a_start_past_the_end_is_unsatisfiable_not_the_whole_file():
    """The important one. Answering 200 here is how one byte becomes 20 MB."""
    with pytest.raises(RangeError):
        parse_range("bytes=999999-", SIZE)
    with pytest.raises(RangeError):
        parse_range("bytes=1000-1005", SIZE)


def test_a_suffix_range_on_an_empty_file_is_unsatisfiable():
    with pytest.raises(RangeError):
        parse_range("bytes=-100", 0)


def test_malformed_specs_fall_back_to_the_whole_file():
    assert parse_range("bytes=abc-def", SIZE) is None
    assert parse_range("bytes=100", SIZE) is None          # no dash
    assert parse_range("bytes=500-100", SIZE) is None      # backwards
    assert parse_range("bytes=-", SIZE) is None            # neither end
    assert parse_range("bytes=1x0-", SIZE) is None


def test_the_header_is_case_insensitive_and_tolerates_spaces():
    """Header names and units are case-insensitive per RFC, and proxies have
    been known to add whitespace around the spec."""
    assert parse_range("BYTES=0-9", SIZE) == (0, 9)
    assert parse_range("bytes= 0-9 ", SIZE) == (0, 9)
