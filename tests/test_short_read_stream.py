"""Integration tests for feeds read from short-read byte streams.

A *short read* is a read that returns fewer bytes than the caller asked for,
which binary streams are explicitly allowed to do (an unbuffered socket is the
classic example). feedparser's encoding detection reads a prefix of the feed,
assumes exactly that many bytes were consumed, and then seeks back to replay
the remainder. If a short read silently truncates the prefix, the byte order
mark (BOM) or XML declaration can be lost and a multi-byte character can be
split at the chunk boundary, so the feed is decoded with the wrong encoding.

These tests drive the whole public pipeline -- encoding detection, prefix
consumption and replay, strict XML parsing, and the loose fallback -- from
streams whose reads are deliberately fragmented. The BOM, the HTTP charset,
and the XML declaration are put in conflict so the resolved encoding is
observable, and every short-read parse is compared against a full read of the
identical bytes on encoding, bozo marker (including exception type), entry
count, first entry's title/body/link, and feed version. Concrete text is
asserted too, so "nothing raised" can never be mistaken for correct output.
"""

from __future__ import annotations

import codecs
import io
import typing

import pytest

import feedparser
import feedparser.encodings

# Irregular, deliberately small sizes. Cycling through them moves the read
# boundaries around, including into the middle of multi-byte characters and
# across the three-byte BOM.
ROTATING_CHUNK_SIZES = (1, 2, 3, 1, 4, 2, 5, 1, 3, 2, 1, 7, 2, 1)

CHUNK_SIZES = pytest.mark.parametrize(
    "chunk_size",
    (1, 2, 3, 7, 64),
    ids=lambda value: f"chunk={value}",
)


class ShortReadStream(io.BytesIO):
    """A seekable binary stream that caps every positive read.

    It stays seekable so that ``_open_resource()`` passes it through
    unchanged: the encoding pipeline really does consume it as a stream
    instead of seeing a fully buffered in-memory copy.
    """

    def __init__(self, data: bytes, chunk_size: int | None = None):
        super().__init__(data)
        self._chunk_size = chunk_size
        self._read_count = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            return super().read()
        if self._chunk_size is None:
            limit = ROTATING_CHUNK_SIZES[
                self._read_count % len(ROTATING_CHUNK_SIZES)
            ]
        else:
            limit = self._chunk_size
        self._read_count += 1
        return super().read(min(size, limit))


class RotatingShortReadStream(ShortReadStream):
    """A short-read stream whose chunk size varies from call to call."""

    def __init__(self, data: bytes):
        super().__init__(data, chunk_size=None)


class NonSeekableShortReadStream:
    """A non-rewindable short-read stream.

    There is no ``seek()``/``tell()``/``seekable()``, so ``_open_resource()``
    has to buffer the content. This proves the buffering path still yields the
    same parse when the source fragments its reads.
    """

    def __init__(self, data: bytes):
        self._stream = RotatingShortReadStream(data)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def seekable(self) -> bool:
        return False


def split_multibyte_character(data: bytes, chunk_size: int) -> list[str]:
    """Describe every multi-byte character a chunk boundary splits.

    Return human-readable descriptions like ``"U+1F600 at 0x... "`` for each
    character whose UTF-8 encoding has a chunk boundary strictly inside it.
    The list is empty when no character is split.
    """

    splits = []
    text = data.decode("utf-8", "surrogateescape")
    byte_offset = 0
    for character in text:
        encoded = character.encode("utf-8", "surrogateescape")
        if len(encoded) > 1:
            for inner in range(1, len(encoded)):
                if (byte_offset + inner) % chunk_size == 0:
                    splits.append(
                        f"U+{ord(character):04X} at byte {byte_offset}"
                    )
                    break
        byte_offset += len(encoded)
    return splits


def _bozo_exception_name(result: feedparser.FeedParserDict) -> str | None:
    exception = result.get("bozo_exception")
    return type(exception).__name__ if exception is not None else None


def _outcome(result: feedparser.FeedParserDict) -> tuple[typing.Any, ...]:
    """The observable result of parsing a feed, for baseline comparison."""

    entries = result["entries"]
    first = entries[0] if entries else feedparser.FeedParserDict()
    return (
        result["encoding"],
        bool(result["bozo"]),
        _bozo_exception_name(result),
        len(entries),
        first.get("title"),
        first.get("description"),
        first.get("link"),
        result.get("version"),
    )


def _first_entry(result: feedparser.FeedParserDict) -> feedparser.FeedParserDict:
    assert result["entries"], f"no entries parsed: {result!r}"
    return result["entries"][0]


def parse_both(
    data: bytes,
    *,
    headers: dict[str, str] | None = None,
    stream_factory: typing.Callable[[bytes], typing.IO[bytes]] = (
        RotatingShortReadStream
    ),
    **parse_kwargs: typing.Any,
) -> tuple[feedparser.FeedParserDict, feedparser.FeedParserDict]:
    """Parse *data* from one full read and from a short-read stream.

    Return ``(baseline, short_read)`` and require the two outcomes to be
    identical.
    """

    headers = dict(headers or {})
    baseline = feedparser.parse(
        io.BytesIO(data), response_headers=headers, **parse_kwargs
    )
    short_read = feedparser.parse(
        stream_factory(data), response_headers=headers, **parse_kwargs
    )

    assert _outcome(short_read) == _outcome(baseline), (
        "short-read parse differs from the full-read parse\n"
        f"baseline:   {_outcome(baseline)!r}\n"
        f"short read: {_outcome(short_read)!r}"
    )
    return baseline, short_read


# On-disk bytes are UTF-8 with a BOM, the XML declaration claims
# windows-1252, and the HTTP charset is varied per test. Multi-byte
# characters appear in the feed title and in both entry bodies, so chunk
# boundaries land inside them.
WELL_FORMED_XML = (
    '<?xml version="1.0" encoding="windows-1252"?>'
    '<rss version="2.0"><channel>'
    "<title>Tëst – 日本語 😀</title>"
    "<item>"
    "<title>Café – 日本語 😀</title>"
    "<description>Körper – 中文 😛 and Ω</description>"
    "</item>"
    "<item>"
    "<title>Second ✓</title>"
    "<description>Zweiter Körper 🤯</description>"
    "</item>"
    "</channel></rss>"
)

# Same bytes, but with an unescaped ampersand. The strict XML parser rejects
# the document, so feedparser has to fall back to the loose parser.
ILL_FORMED_XML = (
    '<?xml version="1.0" encoding="windows-1252"?>'
    '<rss version="2.0"><channel>'
    "<title>Tëst – 日本語 😀</title>"
    "<item>"
    "<title>Café & 日本語 😀</title>"
    "<link>http://example.com/feed?a=1&b=2</link>"
    "<description>Körper – 中文 & 😛 Ω</description>"
    "</item>"
    "<item>"
    "<title>Second ✓</title>"
    "<description>Zweiter Körper 🤯</description>"
    "</item>"
    "</channel></rss>"
)

# The declaration claims us-ascii while the bytes are UTF-8 with a BOM, so the
# BOM (not the declaration) has to decide the encoding.
BOM_BEATS_DECLARATION_XML = (
    '<?xml version="1.0" encoding="us-ascii"?>'
    '<rss version="2.0"><channel>'
    "<title>Tëst – 日本語 😀</title>"
    "<item>"
    "<title>Café – 日本語 😀</title>"
    "<description>Körper – 中文 😛 and Ω</description>"
    "</item>"
    "<item><title>Second ✓</title>"
    "<description>Zweiter Körper 🤯</description></item>"
    "</channel></rss>"
)


def _utf8_with_bom(text: str) -> bytes:
    return codecs.BOM_UTF8 + text.encode("utf-8")


# The exact mojibake a full read produces when windows-1252 is used to decode
# UTF-8 bytes. Short reads must reproduce it byte for byte.
MOJIBAKE_TITLE = "CafÃ© â€“ æ—¥æœ¬èªž ðŸ˜€"
MOJIBAKE_DESCRIPTION = "KÃ¶rper â€“ ä¸\xadæ–‡ ðŸ˜› and Î©"
MOJIBAKE_LOOSE_TITLE = "CafÃ© & æ—¥æœ¬èªž ðŸ˜€"
MOJIBAKE_LOOSE_DESCRIPTION = "KÃ¶rper â€“ ä¸\xadæ–‡ & ðŸ˜› Î©"

# For every fixture and header combination, the full read produces this
# (encoding, bozo exception class name, first title). Short reads must match.
# The table makes the resolving rule explicit instead of only asserting
# "the result did not change".
EXPECTED_WELL_FORMED = {
    "http-utf8": ("utf-8", None, "Café – 日本語 😀"),
    "http-windows1252": ("windows-1252", None, MOJIBAKE_TITLE),
    "http-us-ascii": (
        "windows-1252",
        "CharacterEncodingOverride",
        MOJIBAKE_TITLE,
    ),
    "no-header": ("windows-1252", None, MOJIBAKE_TITLE),
}

EXPECTED_ILL_FORMED = {
    "http-utf8": ("utf-8", "SAXParseException", "Café & 日本語 😀"),
    "http-windows1252": (
        "windows-1252",
        "SAXParseException",
        MOJIBAKE_LOOSE_TITLE,
    ),
    "http-us-ascii": (
        "windows-1252",
        "SAXParseException",
        MOJIBAKE_LOOSE_TITLE,
    ),
    "no-header": (
        "windows-1252",
        "SAXParseException",
        MOJIBAKE_LOOSE_TITLE,
    ),
}

HEADERS = {
    "http-utf8": {"content-type": "text/xml; charset=utf-8"},
    "http-windows1252": {"content-type": "application/xml; charset=windows-1252"},
    "http-us-ascii": {"content-type": "text/xml; charset=us-ascii"},
    "no-header": {},
}

HEADER_IDS = pytest.mark.parametrize("header_id", tuple(HEADERS))


def test_short_read_stream_is_passthrough_and_genuinely_short():
    # The stream must stay seekable; otherwise _open_resource() would buffer
    # it and the streaming code path under test would never run.
    stream = RotatingShortReadStream(b"abcdef")
    assert stream.seekable()
    assert feedparser.api._open_resource(stream, {}) is stream

    # A large request must really come back short, or these tests would not
    # be exercising short reads at all.
    assert len(stream.read(1024)) <= max(ROTATING_CHUNK_SIZES)


def test_non_seekable_short_read_stream_is_buffered():
    stream = NonSeekableShortReadStream(b"abcdef")
    opened = feedparser.api._open_resource(stream, {})
    assert opened is not stream
    assert isinstance(opened, io.BytesIO)
    assert opened.read() == b"abcdef"


@CHUNK_SIZES
@HEADER_IDS
def test_strict_parser_succeeds_with_conflicting_encodings(chunk_size, header_id):
    # The document is well-formed, so the strict XML parser must win even
    # though the BOM, the HTTP charset, and the XML declaration disagree.
    data = _utf8_with_bom(WELL_FORMED_XML)
    encoding, exception_name, title = EXPECTED_WELL_FORMED[header_id]
    headers = HEADERS[header_id]

    baseline, short_read = parse_both(
        data,
        headers=headers,
        stream_factory=lambda content: ShortReadStream(content, chunk_size),
    )

    for result in (baseline, short_read):
        assert result["encoding"] == encoding
        assert _bozo_exception_name(result) == exception_name
        assert len(result["entries"]) == 2
        assert result["version"] == "rss20"
        assert _first_entry(result)["title"] == title
        # The strict parser ran: this must not be a SAX failure.
        assert _bozo_exception_name(result) != "SAXParseException"

    # The body must survive as well as the title, and it must match the exact
    # text the full read produced (not merely equal the other short-read run).
    expected_body = (
        "Körper – 中文 😛 and Ω"
        if encoding == "utf-8"
        else MOJIBAKE_DESCRIPTION
    )
    assert _first_entry(short_read)["description"] == expected_body
    assert _first_entry(baseline)["description"] == expected_body
    assert short_read["entries"][1]["description"] == (
        "Zweiter Körper 🤯" if encoding == "utf-8" else "Zweiter KÃ¶rper ðŸ¤¯"
    )


@CHUNK_SIZES
@HEADER_IDS
def test_loose_fallback_on_ill_formed_feed(chunk_size, header_id):
    # The unescaped "&" makes the strict parser fail; the loose parser must
    # recover the same entries from a short-read stream as from a full read.
    data = _utf8_with_bom(ILL_FORMED_XML)
    encoding, exception_name, title = EXPECTED_ILL_FORMED[header_id]
    headers = HEADERS[header_id]

    baseline, short_read = parse_both(
        data,
        headers=headers,
        stream_factory=lambda content: ShortReadStream(content, chunk_size),
    )

    for result in (baseline, short_read):
        assert result["encoding"] == encoding
        assert _bozo_exception_name(result) == exception_name
        assert len(result["entries"]) == 2
        assert result["version"] == "rss20"
        assert _first_entry(result)["title"] == title
        # The loose parser really ran, and kept the raw query string.
        assert _first_entry(result)["link"] == "http://example.com/feed?a=1&b=2"

    expected_body = (
        "Körper – 中文 & 😛 Ω"
        if encoding == "utf-8"
        else MOJIBAKE_LOOSE_DESCRIPTION
    )
    assert _first_entry(short_read)["description"] == expected_body
    assert _first_entry(baseline)["description"] == expected_body


@CHUNK_SIZES
def test_forced_loose_parser_matches_full_read(chunk_size, use_loose_parser):
    # With the strict XML parser disabled entirely, only the loose parser can
    # run; short reads must still produce the baseline result.
    data = _utf8_with_bom(WELL_FORMED_XML)
    headers = {"content-type": "text/xml; charset=utf-8"}

    baseline, short_read = parse_both(
        data,
        headers=headers,
        stream_factory=lambda content: ShortReadStream(content, chunk_size),
    )

    assert len(short_read["entries"]) == 2
    assert _first_entry(short_read)["title"] == "Café – 日本語 😀"
    assert _first_entry(short_read)["description"] == "Körper – 中文 😛 and Ω"
    assert _first_entry(short_read)["title"] == _first_entry(baseline)["title"]
    assert short_read["entries"][1]["description"] == (
        baseline["entries"][1]["description"]
    )


@CHUNK_SIZES
def test_character_encoding_override_marker_survives_short_reads(chunk_size):
    # A charset the bytes cannot satisfy forces a CharacterEncodingOverride.
    # Short reads must neither invent nor lose that marker.
    data = _utf8_with_bom(WELL_FORMED_XML)
    headers = {"content-type": "text/xml; charset=us-ascii"}
    baseline, short_read = parse_both(
        data,
        headers=headers,
        stream_factory=lambda content: ShortReadStream(content, chunk_size),
    )

    assert _bozo_exception_name(baseline) == "CharacterEncodingOverride"
    assert _bozo_exception_name(short_read) == "CharacterEncodingOverride"
    assert short_read["encoding"] == baseline["encoding"]
    assert _first_entry(short_read)["title"] == _first_entry(baseline)["title"]


@pytest.mark.parametrize("header_id", ("no-header", "text-xml-no-charset"))
def test_bom_overrides_conflicting_xml_declaration(header_id):
    # The declaration says us-ascii but the BOM says utf-8 and the bytes are
    # UTF-8; the BOM must win, producing a non-fatal override marker and the
    # original characters. This is the three-way conflict with the HTTP
    # charset absent.
    data = _utf8_with_bom(BOM_BEATS_DECLARATION_XML)
    headers = {"text-xml-no-charset": {"content-type": "text/xml"}}.get(
        header_id, {}
    )

    baseline, short_read = parse_both(data, headers=headers)

    for result in (baseline, short_read):
        assert result["encoding"] == "utf-8"
        assert _bozo_exception_name(result) == "CharacterEncodingOverride"
        assert len(result["entries"]) == 2
        assert _first_entry(result)["title"] == "Café – 日本語 😀"
        assert _first_entry(result)["description"] == "Körper – 中文 😛 and Ω"


@CHUNK_SIZES
def test_bom_split_across_reads(chunk_size):
    # With chunk sizes 1 and 2 the three-byte UTF-8 BOM is split across reads.
    # A detection pass that loses those bytes mis-detects the encoding, so
    # compare the detected encoding and the exact parsed text.
    data = _utf8_with_bom(WELL_FORMED_XML)
    headers = {"content-type": "text/xml; charset=utf-8"}
    baseline, short_read = parse_both(
        data,
        headers=headers,
        stream_factory=lambda content: ShortReadStream(content, chunk_size),
    )

    assert short_read["encoding"] == baseline["encoding"] == "utf-8"
    assert _first_entry(short_read)["title"] == "Café – 日本語 😀"
    assert _first_entry(short_read)["title"] == _first_entry(baseline)["title"]
    assert _first_entry(short_read)["description"] == (
        _first_entry(baseline)["description"]
    )


@pytest.mark.parametrize("chunk_size", (2, 3, 4, 5, 7))
def test_chunk_boundary_lands_inside_multibyte_characters(chunk_size):
    # Prove the coverage claim instead of assuming it: with these chunk sizes
    # at least one multi-byte character really is split by a chunk boundary,
    # and the resulting text is still identical to the full read.
    data = _utf8_with_bom(WELL_FORMED_XML)
    splits = split_multibyte_character(data, chunk_size)
    assert splits, (
        f"chunk size {chunk_size} never splits a multi-byte character; "
        "pick a size that does"
    )

    headers = {"content-type": "text/xml; charset=utf-8"}
    baseline, short_read = parse_both(
        data,
        headers=headers,
        stream_factory=lambda content: ShortReadStream(content, chunk_size),
    )

    assert short_read["encoding"] == baseline["encoding"]
    assert _first_entry(short_read)["title"] == _first_entry(baseline)["title"]
    assert _first_entry(short_read)["description"] == (
        _first_entry(baseline)["description"]
    )
    assert _first_entry(short_read)["title"] == "Café – 日本語 😀"


@CHUNK_SIZES
def test_multibyte_character_split_at_first_read_boundary(chunk_size):
    # No BOM: detection has to rely on the XML declaration and the byte
    # content, and the first multi-byte characters straddle read boundaries.
    data = WELL_FORMED_XML.encode("utf-8")
    baseline, short_read = parse_both(
        data,
        headers={"content-type": "text/xml; charset=utf-8"},
        stream_factory=lambda content: ShortReadStream(content, chunk_size),
    )

    assert short_read["encoding"] == baseline["encoding"] == "utf-8"
    assert _first_entry(short_read)["title"] == "Café – 日本語 😀"
    assert _first_entry(short_read)["title"] == _first_entry(baseline)["title"]
    assert short_read["entries"][1]["description"] == "Zweiter Körper 🤯"


def _large_feed_data() -> bytes:
    """Build a feed larger than the encoding-detection prefix.

    Keeping the document bigger than the prefix is what makes the prefix
    consumption and replay path observable at all.
    """

    item = (
        "<item>"
        "<title>Item 日本語 😀 number {index}</title>"
        "<description>description 🤯 日本二 &amp; {index}</description>"
        "</item>"
    )
    text = (
        '<?xml version="1.0" encoding="windows-1252"?>\n'
        '<rss version="2.0"><channel><title>Tëst – 日本語 😀</title>'
        + "".join(item.format(index=index) for index in range(3000))
        + "</channel></rss>"
    )
    data = _utf8_with_bom(text)
    assert len(data) > feedparser.encodings.CONVERT_FILE_PREFIX_LEN
    return data


def test_read_boundaries_beyond_the_encoding_prefix():
    # Build a feed much larger than the detection prefix so that read
    # boundaries also occur inside multi-byte characters after the prefix has
    # been consumed and the remainder has to be replayed.
    data = _large_feed_data()

    baseline, short_read = parse_both(
        data, headers={"content-type": "text/xml; charset=utf-8"}
    )

    assert len(short_read["entries"]) == len(baseline["entries"]) == 3000
    assert short_read["entries"][0]["title"] == "Item 日本語 😀 number 0"
    assert short_read["entries"][0]["title"] == baseline["entries"][0]["title"]
    assert short_read["entries"][-1]["title"] == baseline["entries"][-1]["title"]
    assert short_read["entries"][-1]["description"] == (
        baseline["entries"][-1]["description"]
    )
    # "&amp;" is unescaped by the parser, so the trailing number survives.
    assert short_read["entries"][-1]["description"].endswith("& 2999")


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
def test_non_ascii_compatible_bom_split_across_reads(bom, encoding, chunk_size):
    # These encodings are sniffed purely from the leading BOM. Splitting the
    # BOM (and every following code unit) across reads must not defeat
    # detection or corrupt the replayed prefix.
    text = (
        '<?xml version="1.0"?>\n'
        '<rss version="2.0"><channel>'
        "<item><title>Tëst 日本一 😛</title>"
        "<description>Body 🤯 日本語</description></item>"
        "</channel></rss>"
    )
    data = bom + text.encode(encoding)
    baseline, short_read = parse_both(
        data,
        stream_factory=lambda content: ShortReadStream(content, chunk_size),
    )

    assert short_read["encoding"] == baseline["encoding"] == encoding
    assert short_read["bozo"] == baseline["bozo"]
    assert _first_entry(short_read)["title"] == "Tëst 日本一 😛"
    assert _first_entry(short_read)["title"] == _first_entry(baseline)["title"]
    assert _first_entry(short_read)["description"] == (
        _first_entry(baseline)["description"]
    )


def test_non_seekable_short_read_stream_matches_full_read():
    data = _utf8_with_bom(WELL_FORMED_XML)
    headers = {"content-type": "text/xml; charset=utf-8"}
    baseline, short_read = parse_both(
        data, headers=headers, stream_factory=NonSeekableShortReadStream
    )

    assert _first_entry(short_read)["title"] == "Café – 日本語 😀"
    assert _first_entry(short_read)["title"] == _first_entry(baseline)["title"]
    assert _first_entry(short_read)["description"] == (
        _first_entry(baseline)["description"]
    )


@pytest.mark.parametrize("optimistic", (True, False))
def test_optimistic_encoding_detection_modes(optimistic):
    # Both detection strategies read the stream differently; short reads must
    # not change the result of either.
    data = _utf8_with_bom(WELL_FORMED_XML)
    headers = {"content-type": "text/xml; charset=us-ascii"}
    baseline, short_read = parse_both(
        data,
        headers=headers,
        optimistic_encoding_detection=optimistic,
    )

    assert short_read["encoding"] == baseline["encoding"]
    assert _bozo_exception_name(short_read) == _bozo_exception_name(baseline)
    assert _first_entry(short_read)["description"] == (
        _first_entry(baseline)["description"]
    )


def test_prefix_consumption_replay_is_identical_under_short_reads():
    # Exercise the layer below parse(): detection consumes a prefix, and the
    # StreamFactory replays it before the rest of the stream. The consumed
    # prefix, the stream position, and the fully decoded text must all be
    # identical between a short-read and a full-read stream. A lost BOM, a
    # duplicated XML declaration, or lost prefix bytes all show up here.
    #
    # A feed larger than the detection prefix is used so the remainder really
    # comes from the underlying stream rather than from the prefix alone.
    data = _large_feed_data()
    headers = {"content-type": "text/xml; charset=utf-8"}

    full_file = io.BytesIO(data)
    full_result: dict[str, typing.Any] = {}
    full_factory = feedparser.encodings.convert_file_to_utf8(
        headers, full_file, full_result, True
    )

    short_file = RotatingShortReadStream(data)
    short_result: dict[str, typing.Any] = {}
    short_factory = feedparser.encodings.convert_file_to_utf8(
        headers, short_file, short_result, True
    )

    # Detection resolved the same encoding and consumed the same bytes.
    assert short_result.get("encoding") == full_result.get("encoding")
    assert short_factory.prefix == full_factory.prefix
    assert short_file.tell() == full_file.tell()

    full_text = full_factory.get_text_file().read()
    short_text = short_factory.get_text_file().read()
    assert short_text == full_text
    # The replayed stream is the whole document exactly once.
    assert short_text.count("<?xml") == 1
    assert short_text.count("</channel></rss>") == 1
    assert short_text.rstrip().endswith("</rss>")
    assert "Item 日本語 😀 number 2999" in short_text


def test_detection_prefix_is_not_the_whole_document():
    # Guard the previous test: if detection consumed the entire document, it
    # would not exercise prefix replay at all.
    data = _large_feed_data()
    headers = {"content-type": "text/xml; charset=utf-8"}
    result: dict[str, typing.Any] = {}
    factory = feedparser.encodings.convert_file_to_utf8(
        headers, RotatingShortReadStream(data), result, True
    )
    assert factory.prefix
    assert b"</channel></rss>" not in factory.prefix
