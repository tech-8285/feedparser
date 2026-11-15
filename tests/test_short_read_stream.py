"""Integration tests for feeds read from non-rewindable, short-read streams.

A short-read stream is a binary stream whose ``read(size)`` may return fewer
than ``size`` bytes (for example, an unbuffered socket). feedparser must still
run the whole encoding detection and parsing pipeline on such a stream and
produce the exact same result as reading the same bytes in one go.

These tests deliberately put the byte order mark, the HTTP charset, and the
XML declaration in conflict, and they exercise both the strict XML parser and
the loose fallback. Every test compares the short-read result against a
full-read baseline built from the identical bytes, and asserts real content
(entry count, first title, body, and bozo marker) rather than merely that no
exception was raised.
"""

from __future__ import annotations

import codecs
import io
import typing

import pytest

import feedparser
import feedparser.encodings

# Irregular, deliberately small chunk sizes. Rotating through them makes chunk
# boundaries land at different offsets, including in the middle of multi-byte
# characters and across BOMs.
ROTATING_CHUNK_SIZES = [1, 2, 3, 1, 4, 2, 5, 1, 3, 2, 1, 7, 2, 1]

CHUNK_SIZES = pytest.mark.parametrize(
    "chunk_size",
    [1, 2, 3, 7, 64],
    ids=lambda value: f"chunk={value}",
)


class ShortReadStream(io.BytesIO):
    """A seekable byte stream that never returns more than a few bytes/read.

    It stays seekable so that ``_open_resource()`` passes it straight through
    instead of buffering it, yet every positive read is capped. This models a
    seekable source that still performs short reads.
    """

    def __init__(self, data: bytes, chunk_size: int | None = None):
        super().__init__(data)
        self._chunk_size = chunk_size
        self._index = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            return super().read()
        if self._chunk_size is None:
            limit = ROTATING_CHUNK_SIZES[
                self._index % len(ROTATING_CHUNK_SIZES)
            ]
            self._index += 1
        else:
            limit = self._chunk_size
        return super().read(min(size, limit))


class RotatingShortReadStream(ShortReadStream):
    """A short-read stream using the rotating chunk sizes."""

    def __init__(self, data: bytes):
        super().__init__(data, chunk_size=None)


class NonSeekableShortReadStream:
    """A non-seekable short-read stream (no seek/tell).

    ``_open_resource()`` must buffer such a stream into memory before the
    encoding pipeline ever sees it.
    """

    def __init__(self, data: bytes):
        self._stream = RotatingShortReadStream(data)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def seekable(self) -> bool:
        return False


def _exception_name(result: feedparser.FeedParserDict) -> str | None:
    exception = result.get("bozo_exception")
    return type(exception).__name__ if exception is not None else None


def _signature(result: feedparser.FeedParserDict) -> tuple[typing.Any, ...]:
    """The comparable outcome of parsing a feed.

    Two parses of the identical bytes must share this signature regardless of
    how the underlying stream chunks its reads.
    """

    entries = result["entries"]
    first = entries[0] if entries else feedparser.FeedParserDict()
    return (
        result["encoding"],
        bool(result["bozo"]),
        _exception_name(result),
        len(entries),
        first.get("title"),
        first.get("description"),
        first.get("link"),
    )


def assert_matches_baseline(
    data: bytes,
    *,
    headers: dict[str, str] | None = None,
    stream_factory: typing.Callable[[bytes], typing.IO[bytes]]
    | None = None,
    **parse_kwargs: typing.Any,
) -> None:
    """Parse *data* from a short-read stream and from a full-read baseline.

    Assert that both runs detect the same encoding and produce the same
    entries, first title/body, and bozo marker, and that the encoding layer
    reconstructs identical text (catching a duplicated XML declaration).

    ``stream_factory`` builds a fresh stream for each parse, since a stream is
    consumed by reading. It defaults to the rotating short-read stream.
    """

    headers = headers or {}
    if stream_factory is None:
        stream_factory = RotatingShortReadStream

    baseline = feedparser.parse(
        io.BytesIO(data), response_headers=headers, **parse_kwargs
    )
    short_read = feedparser.parse(
        stream_factory(data), response_headers=headers, **parse_kwargs
    )

    assert _signature(short_read) == _signature(baseline), (
        f"short-read result differs from the full-read baseline\n"
        f"baseline: {_signature(baseline)!r}\n"
        f"short:    {_signature(short_read)!r}"
    )

    # Also verify the encoding layer directly: the reconstructed text must be
    # byte-for-byte identical, which specifically guards against a duplicated
    # XML declaration introduced by the prefix-consumption bookkeeping. This
    # only applies to seekable streams; a non-seekable stream is buffered into
    # a BytesIO by _open_resource() and never reaches this layer directly.
    sample = stream_factory(data)
    if not sample.seekable():
        return

    def reconstruct(source: typing.IO[bytes]) -> str:
        result: dict[str, typing.Any] = {"headers": dict(headers)}
        factory = feedparser.encodings.convert_file_to_utf8(
            headers, source, result, True
        )
        return factory.get_text_file().read()

    assert reconstruct(stream_factory(data)) == reconstruct(io.BytesIO(data))


# A well-formed feed whose real on-disk encoding is UTF-8, while the XML
# declaration claims windows-1252. It contains Japanese and emoji text so that
# multi-byte characters appear throughout.
WELL_FORMED_TEMPLATE = """<?xml version="1.0" encoding="windows-1252"?>
<rss version="2.0">
  <channel>
    <title>Tëst 日本語 feed 😀</title>
    <item>
      <title>Fïrst entry 日本一 😛</title>
      <description>Body with émojis 🤯 and 日本語 text</description>
    </item>
    <item>
      <title>Second entry</title>
      <description>Second body 日本二</description>
    </item>
  </channel>
</rss>
"""

# An ill-formed variant: the unescaped "&" makes the strict XML parser fail,
# forcing feedparser to recover with the loose parser.
ILL_FORMED_TEMPLATE = """<?xml version="1.0" encoding="windows-1252"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Fïrst &amp; broken 日本一 😛</title>
      <link>http://example.com/feed?a=1&b=2</link>
      <description>Body with 🤯 emoji 日本語</description>
    </item>
    <item>
      <title>Second entry</title>
      <description>Second body 日本二</description>
    </item>
  </channel>
</rss>
"""


def _utf8_with_bom(template: str) -> bytes:
    return codecs.BOM_UTF8 + template.encode("utf-8")


def test_short_read_stream_is_seekable_and_passthrough():
    # The stream must stay seekable so _open_resource() does not buffer it;
    # otherwise these tests would not exercise the streaming code path.
    stream = RotatingShortReadStream(b"abcdef")
    assert stream.seekable()
    assert feedparser.api._open_resource(stream, {}) is stream
    # Reads are genuinely short: a large request returns a small chunk.
    assert len(stream.read(1024)) <= max(ROTATING_CHUNK_SIZES)


def test_non_seekable_stream_is_buffered():
    # The non-seekable variant must be buffered, exercising the other branch.
    stream = NonSeekableShortReadStream(b"abcdef")
    opened = feedparser.api._open_resource(stream, {})
    assert opened is not stream
    assert opened.read() == b"abcdef"


@CHUNK_SIZES
def test_strict_success_with_three_way_encoding_conflict(chunk_size):
    # BOM (utf-8) vs XML declaration (windows-1252) vs HTTP charset (ascii).
    # The document is well-formed, so the strict parser must succeed.
    data = _utf8_with_bom(WELL_FORMED_TEMPLATE)
    headers = {"content-type": "application/xml; charset=ascii"}
    assert_matches_baseline(
        data,
        headers=headers,
        stream_factory=lambda d: ShortReadStream(d, chunk_size),
    )

    baseline = feedparser.parse(io.BytesIO(data), response_headers=headers)
    short_read = feedparser.parse(
        ShortReadStream(data, chunk_size), response_headers=headers
    )
    # The strict parser wins: no SAX parse failure, two entries, real content.
    assert _exception_name(baseline) != "SAXParseException"
    assert _exception_name(short_read) != "SAXParseException"
    assert len(short_read.entries) == 2
    # The three-way conflict resolves identically under short reads and a
    # full read (including the mojibake the wrong charset produces).
    assert short_read.entries[0].title == baseline.entries[0].title
    assert short_read.entries[0].description == baseline.entries[0].description


@CHUNK_SIZES
def test_character_encoding_override_marker_is_preserved(chunk_size):
    # An HTTP charset that cannot decode the document forces a
    # CharacterEncodingOverride; the same marker must appear under short reads.
    data = _utf8_with_bom(WELL_FORMED_TEMPLATE)
    headers = {"content-type": "application/xml; charset=ascii"}
    baseline = feedparser.parse(io.BytesIO(data), response_headers=headers)
    short_read = feedparser.parse(
        ShortReadStream(data, chunk_size), response_headers=headers
    )
    assert _exception_name(baseline) == "CharacterEncodingOverride"
    assert _exception_name(short_read) == "CharacterEncodingOverride"
    assert short_read.encoding == baseline.encoding


@CHUNK_SIZES
def test_loose_fallback_on_ill_formed_feed(chunk_size):
    # The unescaped "&" breaks the strict parser; the loose parser recovers.
    data = _utf8_with_bom(ILL_FORMED_TEMPLATE)
    headers = {"content-type": "application/xml; charset=ascii"}
    assert_matches_baseline(
        data,
        headers=headers,
        stream_factory=lambda d: ShortReadStream(d, chunk_size),
    )

    baseline = feedparser.parse(io.BytesIO(data), response_headers=headers)
    short_read = feedparser.parse(
        ShortReadStream(data, chunk_size), response_headers=headers
    )
    assert _exception_name(baseline) == "SAXParseException"
    assert _exception_name(short_read) == "SAXParseException"
    assert len(short_read.entries) == 2
    assert short_read.entries[0].title == baseline.entries[0].title
    assert short_read.entries[0].description == baseline.entries[0].description
    # The loose parser keeps the unescaped query string intact.
    assert short_read.entries[0].link == "http://example.com/feed?a=1&b=2"


@CHUNK_SIZES
def test_forced_loose_parser_matches_baseline(chunk_size, use_loose_parser):
    # With the strict XML parser disabled entirely, only the loose parser runs.
    data = _utf8_with_bom(ILL_FORMED_TEMPLATE)
    headers = {"content-type": "application/xml; charset=ascii"}
    assert_matches_baseline(
        data,
        headers=headers,
        stream_factory=lambda d: ShortReadStream(d, chunk_size),
    )

    baseline = feedparser.parse(io.BytesIO(data), response_headers=headers)
    short_read = feedparser.parse(
        ShortReadStream(data, chunk_size), response_headers=headers
    )
    assert _exception_name(baseline) == "CharacterEncodingOverride"
    assert _exception_name(short_read) == "CharacterEncodingOverride"
    assert short_read.entries[0].title == baseline.entries[0].title
    assert short_read.entries[0].description == baseline.entries[0].description


@CHUNK_SIZES
def test_bom_split_across_chunk_boundaries(chunk_size):
    # One-byte-ish reads split the 3-byte UTF-8 BOM across chunks, which used
    # to make feedparser miss the BOM and detect a different encoding.
    data = _utf8_with_bom(WELL_FORMED_TEMPLATE)
    assert_matches_baseline(
        data, stream_factory=lambda d: ShortReadStream(d, chunk_size)
    )

    baseline = feedparser.parse(io.BytesIO(data))
    short_read = feedparser.parse(ShortReadStream(data, chunk_size))
    assert short_read.encoding == baseline.encoding
    assert short_read.bozo == baseline.bozo
    assert short_read.entries[0].title == baseline.entries[0].title


@CHUNK_SIZES
def test_bomless_utf8_multibyte_feed(chunk_size):
    # No BOM: detection relies on the XML declaration and byte content, and
    # multi-byte characters straddle chunk boundaries from the very start.
    data = WELL_FORMED_TEMPLATE.encode("utf-8")
    assert_matches_baseline(
        data, stream_factory=lambda d: ShortReadStream(d, chunk_size)
    )

    baseline = feedparser.parse(io.BytesIO(data))
    short_read = feedparser.parse(ShortReadStream(data, chunk_size))
    assert short_read.encoding == baseline.encoding
    assert short_read.entries[0].title == baseline.entries[0].title
    assert short_read.entries[0].description == baseline.entries[0].description


def test_chunk_boundary_beyond_the_encoding_prefix():
    # Build a feed larger than the encoding-detection prefix so that chunk
    # boundaries also fall inside multi-byte characters after the prefix.
    item = (
        "<item>"
        "<title>Item 日本語 😀 number {index}</title>"
        "<description>description 🤯 日本二 {index}</description>"
        "</item>"
    )
    items = "".join(item.format(index=i) for i in range(3000))
    text = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss version="2.0"><channel>'
        + items
        + "</channel></rss>"
    )
    data = text.encode("utf-8")
    assert len(data) > feedparser.encodings.CONVERT_FILE_PREFIX_LEN

    assert_matches_baseline(data, stream_factory=RotatingShortReadStream)

    baseline = feedparser.parse(io.BytesIO(data))
    short_read = feedparser.parse(RotatingShortReadStream(data))
    assert len(short_read.entries) == len(baseline.entries) == 3000
    assert short_read.entries[0].title == baseline.entries[0].title
    assert short_read.entries[-1].title == baseline.entries[-1].title
    assert short_read.entries[-1].description == (
        baseline.entries[-1].description
    )
    assert short_read.bozo == baseline.bozo


@pytest.mark.parametrize(
    ("bom", "encoding"),
    (
        (codecs.BOM_UTF16_BE, "utf-16be"),
        (codecs.BOM_UTF16_LE, "utf-16le"),
        (codecs.BOM_UTF32_BE, "utf-32be"),
        (codecs.BOM_UTF32_LE, "utf-32le"),
    ),
)
@CHUNK_SIZES
def test_non_ascii_compatible_bom_encodings(bom, encoding, chunk_size):
    # Non-ASCII-compatible encodings are sniffed purely from the leading BOM;
    # splitting that BOM across short reads must not defeat detection.
    text = (
        '<?xml version="1.0"?>\n'
        '<rss version="2.0"><channel>'
        "<item><title>Tëst 日本一 😛</title>"
        "<description>Body 🤯 日本語</description></item>"
        "</channel></rss>"
    )
    data = bom + text.encode(encoding)
    assert_matches_baseline(
        data, stream_factory=lambda d: ShortReadStream(d, chunk_size)
    )

    baseline = feedparser.parse(io.BytesIO(data))
    short_read = feedparser.parse(ShortReadStream(data, chunk_size))
    assert short_read.encoding == baseline.encoding == encoding
    assert short_read.bozo == baseline.bozo
    assert short_read.entries[0].title == baseline.entries[0].title
    assert short_read.entries[0].description == baseline.entries[0].description


def test_non_seekable_short_read_stream_matches_baseline():
    # A non-seekable stream is buffered by _open_resource(); the result must
    # still be identical to a full read, including under short reads.
    data = _utf8_with_bom(WELL_FORMED_TEMPLATE)
    headers = {"content-type": "application/xml; charset=ascii"}
    assert_matches_baseline(
        data,
        headers=headers,
        stream_factory=NonSeekableShortReadStream,
    )


@pytest.mark.parametrize("optimistic", [True, False])
def test_optimistic_encoding_detection_modes(optimistic):
    data = _utf8_with_bom(WELL_FORMED_TEMPLATE)
    headers = {"content-type": "application/xml; charset=ascii"}
    assert_matches_baseline(
        data,
        headers=headers,
        stream_factory=RotatingShortReadStream,
        optimistic_encoding_detection=optimistic,
    )


def test_reconstructed_text_has_single_xml_declaration():
    # Regression test for the duplicated XML declaration that short reads used
    # to introduce via the prefix-consumption fallback.
    data = _utf8_with_bom(WELL_FORMED_TEMPLATE)
    result: dict[str, typing.Any] = {"headers": {}}
    factory = feedparser.encodings.convert_file_to_utf8(
        {}, RotatingShortReadStream(data), result, True
    )
    text = factory.get_text_file().read()
    assert text.count("<?xml") == 1
