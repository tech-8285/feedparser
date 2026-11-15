"""Tests for feeds truncated in the middle of transmission."""

import io

import feedparser

RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
<channel>
<title>Test feed</title>
<link>http://example.com/</link>
<description>Feed description</description>
<item>
<title>Entry 1</title>
<author>author1@example.com</author>
<link>http://example.com/1</link>
<description>Body 1</description>
<enclosure url="http://example.com/1.mp3" length="123" type="audio/mpeg" />
</item>
<item>
<title>Entry 2</title>
<author>author2@example.com</author>
<link>http://example.com/2</link>
<description>Body 2</description>
<enclosure url="http://example.com/2.mp3" length="456" type="audio/mpeg" />
</item>
</channel>
</rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Test feed</title>
<link href="http://example.com/" />
<entry>
<title>Entry 1</title>
<author><name>Alice</name></author>
<link href="http://example.com/1" />
<summary>Body 1</summary>
</entry>
<entry>
<title>Entry 2</title>
<author><name>Bob</name></author>
<link href="http://example.com/2" />
<summary>Body 2</summary>
</entry>
</feed>
"""


def truncate(document, marker, offset=0):
    return document[: document.index(marker) + offset]


def test_complete_feeds_are_unchanged():
    for document in (RSS, ATOM):
        result = feedparser.parse(document)
        assert result.bozo is False
        assert len(result.entries) == 2


def test_truncation_at_entry_boundary():
    # Everything of the incomplete trailing entry is gone;
    # the complete entries are still delivered and the result is flagged.
    for document, marker in ((RSS, "<item>\n<title>Entry 2"), (ATOM, "<entry>\n<title>Entry 2")):
        result = feedparser.parse(truncate(document, marker))
        assert result.bozo == 1
        assert len(result.entries) == 1
        assert result.entries[0].title == "Entry 1"


def test_truncation_inside_entry_body():
    # The half-parsed entry must not be delivered as a complete entry.
    for document in (RSS, ATOM):
        result = feedparser.parse(truncate(document, "Body 2", 3))
        assert result.bozo == 1
        assert len(result.entries) == 1
        entry = result.entries[0]
        assert entry.title == "Entry 1"
        assert entry.summary == "Body 1"


def test_truncation_inside_enclosure():
    # The final entry's enclosure is cut off mid-tag;
    # the partial entry must not be delivered, and the previous
    # entry's enclosure must remain intact.
    result = feedparser.parse(truncate(RSS, "2.mp3", 4))
    assert result.bozo == 1
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.title == "Entry 1"
    assert entry.author == "author1@example.com"
    assert entry.summary == "Body 1"
    assert [enclosure.href for enclosure in entry.enclosures] == [
        "http://example.com/1.mp3"
    ]


def test_truncation_after_final_entry():
    # All entries are complete; only the feed-level closing tags are missing.
    result = feedparser.parse(truncate(RSS, "</item>\n</channel>", len("</item>")))
    assert result.bozo == 1
    assert [entry.title for entry in result.entries] == ["Entry 1", "Entry 2"]


def test_truncation_before_any_entry():
    result = feedparser.parse(truncate(RSS, "<item>"))
    assert result.bozo == 1
    assert result.entries == []


class _ChunkyStream(io.RawIOBase):
    """A non-seekable binary stream that returns at most `step` bytes per read."""

    def __init__(self, data, step):
        self._buffer = io.BytesIO(data)
        self._step = step

    def read(self, size=-1):
        if size is None or size < 0:
            chunks = []
            while True:
                chunk = self._buffer.read(self._step)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        return self._buffer.read(min(size, self._step))

    def readable(self):
        return True

    def seekable(self):
        return False


def test_truncated_feed_result_does_not_depend_on_chunking():
    truncated = truncate(RSS, "Body 2", 3)
    expected = feedparser.parse(truncated)
    sources = [
        truncated,
        truncated.encode("utf-8"),
        io.StringIO(truncated),
        io.BytesIO(truncated.encode("utf-8")),
        _ChunkyStream(truncated.encode("utf-8"), 1),
        _ChunkyStream(truncated.encode("utf-8"), 7),
    ]
    for source in sources:
        result = feedparser.parse(source)
        assert bool(result.bozo) == bool(expected.bozo)
        assert len(result.entries) == len(expected.entries) == 1
        assert result.entries[0].title == expected.entries[0].title
        assert result.entries[0].summary == expected.entries[0].summary
