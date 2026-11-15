"""Integration tests for incremental timelines built from entry dates.

Subscribers that poll feeds incrementally need a stable per-entry timestamp:
``updated`` when the publisher supplied a usable value, otherwise
``published``.  These tests exercise that timeline across RFC 822, W3C,
ISO-like (asctime, MSSQL) and custom date rules, and check that

* the normalized UTC value agrees with an independent standard-library
  conversion of the original source string,
* invalid update values never hide a valid publication date,
* timezone conversion changes neither the normalized values nor the order,
* a custom handler only claims the inputs it understands while every other
  date format keeps using the built-in handlers.
"""

from __future__ import annotations

import calendar
import datetime as dt
import email.utils
import io
import json
import os
import re
import time

import pytest

import feedparser
import feedparser.api
import feedparser.datetimes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def reference_utc_epoch(value: str) -> int:
    """Convert a date string to a UTC epoch using the standard library only."""
    value = value.strip()
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    else:
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        return int(stamp.timestamp())

    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        pass
    else:
        return calendar.timegm((day.year, day.month, day.day, 0, 0, 0, 0, 0, 0))

    stamp = email.utils.parsedate_to_datetime(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return int(stamp.timestamp())


def utc_epoch(value) -> int | None:
    """Convert an already-normalized 9-tuple to a UTC epoch."""
    if value is None:
        return None
    return calendar.timegm(value)


def stored(entry: dict, key: str):
    """Read a stored value, bypassing FeedParserDict's ``updated`` fallback."""
    return dict.get(entry, key)


def entry_id(entry: dict) -> str | None:
    # Titles are the stable, human-readable labels in these fixtures; RSS 1.0
    # entries otherwise expose their ``rdf:about`` URI as the id.
    return dict.get(entry, "title") or dict.get(entry, "id")


def timeline_key(entry: dict) -> int | None:
    """Timestamp used for incremental collection.

    A usable ``updated`` value wins; an invalid (``None``) or missing one
    falls back to ``published``.  Note that an invalid update is stored as
    ``None`` and therefore does *not* trigger FeedParserDict's deprecated
    ``updated_parsed`` -> ``published_parsed`` fallback.
    """
    updated = stored(entry, "updated_parsed")
    if updated is not None:
        return utc_epoch(updated)
    return utc_epoch(stored(entry, "published_parsed"))


def timeline(feed) -> list[str | None]:
    """Order entries by timestamp, keeping document order for ties."""
    keyed = [
        (timeline_key(entry), index, entry) for index, entry in enumerate(feed.entries)
    ]
    keyed.sort(key=lambda item: (item[0] is None, item[0], item[1]))
    return [entry_id(entry) for _, _, entry in keyed]


def snapshot(feed) -> dict:
    """Represent parsed dates in a comparable, warning-free form."""
    result = {}
    for entry in feed.entries:
        result[entry_id(entry)] = {
            "published": stored(entry, "published"),
            "published_parsed": stored(entry, "published_parsed"),
            "updated": stored(entry, "updated"),
            "updated_parsed": stored(entry, "updated_parsed"),
        }
    return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def register_date_handler():
    """Register a date handler and restore the global handler list afterwards."""
    baseline = list(feedparser.datetimes._date_handlers)

    def _register(handler):
        feedparser.registerDateHandler(handler)
        return handler

    yield _register
    feedparser.datetimes._date_handlers[:] = baseline


@pytest.fixture
def set_local_timezone():
    """Change ``TZ`` for the current process, or skip if unsupported."""
    tzset = getattr(time, "tzset", None)
    if tzset is None:
        pytest.skip("time.tzset() is unavailable on this platform")
    original = os.environ.get("TZ")

    def _set(name: str) -> None:
        os.environ["TZ"] = name
        tzset()

    yield _set

    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    tzset()


@pytest.fixture
def parse(monkeypatch):
    """Parse a feed with the strict or the fallback (loose) XML parser."""

    def _parse(source, *, loose: bool = False):
        if loose:
            monkeypatch.setattr(feedparser.api, "_XML_AVAILABLE", False)
        return feedparser.parse(source)

    return _parse


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------

# A single RSS feed mixing RFC 822, W3C, asctime, MSSQL and invalid values.
# Expected UTC epochs (independently computed):
#   date-only         2024-07-01              -> 1719792000
#   dc-date           2024-07-01T09:00-01:00  -> 1719828000
#   mssql             2024-07-01 13:15+03:00  -> 1719828900
#   w3c               2024-07-01T13:30Z       -> 1719840600
#   asctime           Jul  1 12:00:00 2024    -> 1719835200
#   rfc822            09:00 -0500             -> 1719842400
#   broken-published  modified 15:00 -01:00   -> 1719849600
FEED_MIXED_PROTOCOLS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"
     xmlns:atom="http://www.w3.org/2005/Atom"
     xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:dcterms="http://purl.org/dc/terms/">
  <channel>
    <title>Mixed protocols</title>
    <link>http://example.com/</link>
    <description>Mixed date protocols</description>
    <item>
      <guid isPermaLink="false">rfc822</guid>
      <title>rfc822</title>
      <pubDate>Mon, 01 Jul 2024 09:00:00 -0500</pubDate>
      <dcterms:modified>2024-07-01T14:00:00Z</dcterms:modified>
    </item>
    <item>
      <guid isPermaLink="false">w3c</guid>
      <title>w3c</title>
      <dcterms:issued>2024-07-01T13:30:00Z</dcterms:issued>
      <atom:updated>Pending</atom:updated>
    </item>
    <item>
      <guid isPermaLink="false">asctime</guid>
      <title>asctime</title>
      <pubDate>Mon Jul  1 12:00:00 2024</pubDate>
    </item>
    <item>
      <guid isPermaLink="false">mssql</guid>
      <title>mssql</title>
      <pubDate>2024-07-01 13:15:00+03:00</pubDate>
    </item>
    <item>
      <guid isPermaLink="false">broken-published</guid>
      <title>broken-published</title>
      <pubDate>not a date</pubDate>
      <dcterms:modified>2024-07-01T15:00:00-01:00</dcterms:modified>
    </item>
    <item>
      <guid isPermaLink="false">date-only</guid>
      <title>date-only</title>
      <pubDate>2024-07-01</pubDate>
    </item>
    <item>
      <guid isPermaLink="false">dc-date</guid>
      <title>dc-date</title>
      <dc:date>2024-07-01T09:00:00-01:00</dc:date>
    </item>
  </channel>
</rss>
"""

# The order above, sorted by the normalized incremental timestamp.
EXPECTED_MIXED_PROTOCOL_ORDER = [
    "date-only",
    "dc-date",
    "mssql",
    "asctime",
    "w3c",
    "rfc822",
    "broken-published",
]

# (entry id, field, source string, expected UTC epoch)
EXPECTED_MIXED_PROTOCOL_VALUES = [
    ("rfc822", "published", "Mon, 01 Jul 2024 09:00:00 -0500", 1719842400),
    ("rfc822", "updated", "2024-07-01T14:00:00Z", 1719842400),
    ("w3c", "published", "2024-07-01T13:30:00Z", 1719840600),
    ("w3c", "updated", "Pending", None),
    ("asctime", "published", "Mon Jul  1 12:00:00 2024", 1719835200),
    ("mssql", "published", "2024-07-01 13:15:00+03:00", 1719828900),
    ("broken-published", "published", "not a date", None),
    ("broken-published", "updated", "2024-07-01T15:00:00-01:00", 1719849600),
    ("date-only", "published", "2024-07-01", 1719792000),
    ("dc-date", "updated", "2024-07-01T09:00:00-01:00", 1719828000),
]


def _rss_feed(items: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss version="2.0"\n'
        '     xmlns:atom="http://www.w3.org/2005/Atom"\n'
        '     xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
        '     xmlns:dcterms="http://purl.org/dc/terms/">\n'
        "<channel><title>timeline</title>"
        "<link>http://example.com/</link>"
        "<description>timeline</description>\n"
        f"{items}</channel></rss>\n"
    ).encode()


FEED_ATOM_INVALID_UPDATE = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>timeline</title>
  <entry>
    <id>pub-ok-upd-invalid</id>
    <title>pub-ok-upd-invalid</title>
    <published>2024-07-01T12:00:00+02:00</published>
    <updated>Pending</updated>
  </entry>
  <entry>
    <id>pub-invalid-upd-ok</id>
    <title>pub-invalid-upd-ok</title>
    <published>not a date</published>
    <updated>2024-07-01T12:00:00Z</updated>
  </entry>
  <entry>
    <id>both-valid</id>
    <title>both-valid</title>
    <published>2024-07-01T12:00:00+02:00</published>
    <updated>2024-07-01T10:30:00Z</updated>
  </entry>
</feed>
"""

FEED_RDF_INVALID_UPDATE = b"""<?xml version="1.0" encoding="utf-8"?>
<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns="http://purl.org/rss/1.0/"
    xmlns:dcterms="http://purl.org/dc/terms/">
  <channel rdf:about="http://example.com/">
    <title>timeline</title>
    <link>http://example.com/</link>
    <description>timeline</description>
  </channel>
  <item rdf:about="http://example.com/pub-ok-upd-invalid">
    <title>pub-ok-upd-invalid</title>
    <dcterms:issued>2024-07-01T12:00:00+02:00</dcterms:issued>
    <dcterms:modified>Pending</dcterms:modified>
  </item>
  <item rdf:about="http://example.com/pub-invalid-upd-ok">
    <title>pub-invalid-upd-ok</title>
    <dcterms:issued>not a date</dcterms:issued>
    <dcterms:modified>2024-07-01T10:05:00Z</dcterms:modified>
  </item>
</rdf:RDF>
"""

FEED_PUBLISHED_ONLY = _rss_feed(
    """
    <item>
      <guid isPermaLink="false">published-only</guid>
      <title>published-only</title>
      <pubDate>Mon, 01 Jul 2024 10:00:00 GMT</pubDate>
    </item>
    """
)

# Same instants written with three different protocols.
FEED_RFC822_ORDER = _rss_feed(
    """
    <item>
      <guid isPermaLink="false">a</guid>
      <pubDate>Mon, 01 Jul 2024 09:00:00 -0500</pubDate>
    </item>
    <item>
      <guid isPermaLink="false">b</guid>
      <pubDate>Mon, 01 Jul 2024 12:00:00 GMT</pubDate>
    </item>
    <item>
      <guid isPermaLink="false">c</guid>
      <pubDate>Mon, 01 Jul 2024 18:15:00 +0545</pubDate>
    </item>
    """
)

FEED_W3C_ORDER = _rss_feed(
    """
    <item>
      <guid isPermaLink="false">a</guid>
      <dcterms:issued>2024-07-01T16:00:00+02:00</dcterms:issued>
    </item>
    <item>
      <guid isPermaLink="false">b</guid>
      <dcterms:issued>2024-07-01T12:00:00Z</dcterms:issued>
    </item>
    <item>
      <guid isPermaLink="false">c</guid>
      <dcterms:issued>2024-07-01T18:15:00+05:45</dcterms:issued>
    </item>
    """
)

FEED_ISO_LIKE_ORDER = _rss_feed(
    """
    <item>
      <guid isPermaLink="false">a</guid>
      <dcterms:issued>2024-07-01 14:00:00Z</dcterms:issued>
    </item>
    <item>
      <guid isPermaLink="false">b</guid>
      <dcterms:issued>Mon Jul  1 12:00:00 2024</dcterms:issued>
    </item>
    <item>
      <guid isPermaLink="false">c</guid>
      <dcterms:issued>2024-07-01 12:30:00Z</dcterms:issued>
    </item>
    """
)

# Entries whose lexicographic source order differs from chronological order.
# Expected epochs:
#   late-offset        2024-07-01T00:30:00+05:30 -> 1719774000
#   early-date-string  2024-06-30T22:00:00Z      -> 1719784800
#   cross-date-line    2024-06-30T20:30:00-03:00 -> 1719790200
#   utc / minus-five / plus-two                  -> 1719842400 (tie)
FEED_OFFSETS = _rss_feed(
    """
    <item>
      <guid isPermaLink="false">utc</guid>
      <pubDate>2024-07-01T14:00:00Z</pubDate>
    </item>
    <item>
      <guid isPermaLink="false">minus-five</guid>
      <pubDate>2024-07-01T09:00:00-05:00</pubDate>
    </item>
    <item>
      <guid isPermaLink="false">plus-two</guid>
      <pubDate>2024-07-01T16:00:00+02:00</pubDate>
    </item>
    <item>
      <guid isPermaLink="false">late-offset</guid>
      <pubDate>2024-07-01T00:30:00+05:30</pubDate>
    </item>
    <item>
      <guid isPermaLink="false">early-date-string</guid>
      <pubDate>2024-06-30T22:00:00Z</pubDate>
    </item>
    <item>
      <guid isPermaLink="false">cross-date-line</guid>
      <pubDate>2024-06-30T20:30:00-03:00</pubDate>
    </item>
    """
)

EXPECTED_OFFSET_ORDER = [
    "late-offset",
    "early-date-string",
    "cross-date-line",
    "utc",
    "minus-five",
    "plus-two",
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loose", (False, True), ids=("strict", "loose"))
def test_mixed_protocols_normalize_and_keep_source(parse, loose):
    feed = parse(FEED_MIXED_PROTOCOLS, loose=loose)
    assert feed.bozo is False

    entries = {entry_id(entry): entry for entry in feed.entries}
    for identifier, field, source, expected_epoch in EXPECTED_MIXED_PROTOCOL_VALUES:
        entry = entries[identifier]
        # The original source string is preserved verbatim...
        assert stored(entry, field) == source
        # ...and the normalized value is a UTC timestamp.
        parsed = stored(entry, f"{field}_parsed")
        assert utc_epoch(parsed) == expected_epoch, f"{identifier}.{field}"
        if expected_epoch is not None:
            # Compare against an independent standard-library conversion.
            assert reference_utc_epoch(source) == expected_epoch

    assert timeline(feed) == EXPECTED_MIXED_PROTOCOL_ORDER

    # Ordering the raw source strings (the naive alternative) is wrong:
    # offsets are ignored and the protocol prefixes dominate.
    raw_order = [
        entry_id(entry)
        for entry in sorted(
            feed.entries,
            key=lambda entry: (
                (stored(entry, "updated") or stored(entry, "published")) or ""
            ),
        )
    ]
    assert raw_order != EXPECTED_MIXED_PROTOCOL_ORDER


@pytest.mark.parametrize("loose", (False, True), ids=("strict", "loose"))
def test_incremental_key_prefers_updated_and_falls_back_to_published(parse, loose):
    feed = parse(FEED_MIXED_PROTOCOLS, loose=loose)
    entries = {entry_id(entry): entry for entry in feed.entries}

    # A valid update value wins.
    assert timeline_key(entries["rfc822"]) == 1719842400
    assert timeline_key(entries["dc-date"]) == 1719828000
    assert timeline_key(entries["broken-published"]) == 1719849600

    # A stored-but-invalid update must not hide a valid publication date.
    assert timeline_key(entries["w3c"]) == 1719840600
    assert stored(entries["w3c"], "updated_parsed") is None
    # The key is present, then, which is what keeps the compatibility fallback
    # from substituting the publication date.
    assert dict.__contains__(entries["w3c"], "updated_parsed")

    # Invalid values never produce a timestamp at all.
    assert timeline_key(entries["date-only"]) == 1719792000
    assert stored(entries["date-only"], "updated_parsed") is None
    assert stored(entries["date-only"], "published_parsed") is not None


CROSS_PROTOCOL_CASES = (
    pytest.param(
        FEED_RFC822_ORDER,
        {
            "a": ("Mon, 01 Jul 2024 09:00:00 -0500", 1719842400),
            "b": ("Mon, 01 Jul 2024 12:00:00 GMT", 1719835200),
            "c": ("Mon, 01 Jul 2024 18:15:00 +0545", 1719837000),
        },
        id="rfc822",
    ),
    pytest.param(
        FEED_W3C_ORDER,
        {
            "a": ("2024-07-01T16:00:00+02:00", 1719842400),
            "b": ("2024-07-01T12:00:00Z", 1719835200),
            "c": ("2024-07-01T18:15:00+05:45", 1719837000),
        },
        id="w3c",
    ),
    pytest.param(
        FEED_ISO_LIKE_ORDER,
        {
            "a": ("2024-07-01 14:00:00Z", 1719842400),
            "b": ("Mon Jul  1 12:00:00 2024", 1719835200),
            "c": ("2024-07-01 12:30:00Z", 1719837000),
        },
        id="iso-like",
    ),
)


@pytest.mark.parametrize("loose", (False, True), ids=("strict", "loose"))
@pytest.mark.parametrize(("source", "expected"), CROSS_PROTOCOL_CASES)
def test_protocol_does_not_change_timeline(parse, source, expected, loose):
    feed = parse(source, loose=loose)
    assert feed.bozo is False

    entries = {entry_id(entry): entry for entry in feed.entries}
    for identifier, (raw, epoch) in expected.items():
        assert stored(entries[identifier], "published") == raw
        assert utc_epoch(stored(entries[identifier], "published_parsed")) == epoch
        assert reference_utc_epoch(raw) == epoch

    # Identical instants yield an identical incremental timeline regardless of
    # the protocol (and the string representation) used by the publisher.
    assert timeline(feed) == ["b", "c", "a"]


@pytest.mark.parametrize(
    ("feed_source", "entry_id_", "expected_published_epoch"),
    (
        pytest.param(
            FEED_ATOM_INVALID_UPDATE,
            "pub-ok-upd-invalid",
            1719828000,
            id="atom",
        ),
        pytest.param(
            FEED_RDF_INVALID_UPDATE,
            "pub-ok-upd-invalid",
            1719828000,
            id="rdf",
        ),
    ),
)
@pytest.mark.parametrize("loose", (False, True), ids=("strict", "loose"))
def test_invalid_update_does_not_shadow_valid_published(
    parse, feed_source, entry_id_, expected_published_epoch, loose
):
    feed = parse(feed_source, loose=loose)
    assert feed.bozo is False

    entries = {entry_id(entry): entry for entry in feed.entries}
    entry = entries[entry_id_]

    # The invalid update is kept as source text but stored as an unusable
    # (None) parsed value -- it must not be replaced by the publication date.
    assert stored(entry, "updated") == "Pending"
    assert stored(entry, "updated_parsed") is None
    # Accessing the value raises no DeprecationWarning: the key exists, so
    # FeedParserDict's fallback is not used (the global filter turns
    # warnings into errors, which makes this assertion meaningful).
    assert entry["updated_parsed"] is None

    # The valid publication date is untouched.
    assert stored(entry, "published_parsed") is not None
    assert utc_epoch(stored(entry, "published_parsed")) == expected_published_epoch
    assert timeline_key(entry) == expected_published_epoch


def test_rss_invalid_update_does_not_shadow_valid_published():
    feed = feedparser.parse(
        _rss_feed(
            """
            <item>
              <guid isPermaLink="false">entry</guid>
              <pubDate>Mon, 01 Jul 2024 10:00:00 GMT</pubDate>
              <lastBuildDate>Pending</lastBuildDate>
            </item>
            """
        )
    )
    assert feed.bozo is False
    entry = feed.entries[0]
    assert stored(entry, "updated") == "Pending"
    assert stored(entry, "updated_parsed") is None
    assert utc_epoch(stored(entry, "published_parsed")) == 1719828000
    assert timeline_key(entry) == 1719828000


def test_invalid_published_keeps_valid_update():
    feed = feedparser.parse(FEED_ATOM_INVALID_UPDATE)
    entries = {entry_id(entry): entry for entry in feed.entries}
    entry = entries["pub-invalid-upd-ok"]

    assert stored(entry, "published") == "not a date"
    assert stored(entry, "published_parsed") is None
    assert utc_epoch(stored(entry, "updated_parsed")) == 1719835200
    assert timeline_key(entry) == 1719835200

    # Entries with an unusable update use the publication date, and valid
    # updates win over the publication date even when they are older.
    assert timeline(feed) == [
        "pub-ok-upd-invalid",
        "both-valid",
        "pub-invalid-upd-ok",
    ]


def test_missing_update_falls_back_to_published_with_warning():
    feed = feedparser.parse(FEED_PUBLISHED_ONLY)
    entry = feed.entries[0]

    # No update was ever supplied: the key is absent...
    assert dict.__contains__(entry, "updated_parsed") is False
    # ...which is distinct from an invalid update stored as None.
    assert timeline_key(entry) == 1719828000
    # The public compatibility behavior exposes the publication date, but
    # warns that this fallback will be removed.
    with pytest.warns(DeprecationWarning):
        assert entry["updated_parsed"] == entry["published_parsed"]


@pytest.mark.parametrize(
    "timezone_name",
    (
        "UTC",
        "America/New_York",
        "Asia/Kathmandu",
        "Pacific/Kiritimati",
    ),
)
def test_timezone_does_not_change_order(parse, set_local_timezone, timezone_name):
    set_local_timezone(timezone_name)

    feed = parse(FEED_OFFSETS)
    assert feed.bozo is False

    entries = {entry_id(entry): entry for entry in feed.entries}
    assert {
        identifier: utc_epoch(stored(entry, "published_parsed"))
        for identifier, entry in entries.items()
    } == {
        "utc": 1719842400,
        "minus-five": 1719842400,
        "plus-two": 1719842400,
        "late-offset": 1719774000,
        "early-date-string": 1719784800,
        "cross-date-line": 1719790200,
    }
    assert timeline(feed) == EXPECTED_OFFSET_ORDER

    # The source strings sort differently than the normalized timestamps.
    assert sorted(entries) == sorted(EXPECTED_OFFSET_ORDER)
    assert (
        sorted(
            entries,
            key=lambda identifier: stored(entries[identifier], "published"),
        )
        != EXPECTED_OFFSET_ORDER
    )


def test_normalized_values_are_stable_across_timezones(set_local_timezone):
    results = []
    for timezone_name in ("UTC", "America/Los_Angeles", "Asia/Tokyo"):
        set_local_timezone(timezone_name)
        feed = feedparser.parse(FEED_MIXED_PROTOCOLS)
        results.append(snapshot(feed))

    assert results[0] == results[1] == results[2]


JSON_FEED = {
    "version": "https://jsonfeed.org/version/1.1",
    "title": "timeline",
    "items": [
        {
            "id": "json-rfc3339",
            "date_published": "2024-07-01T12:00:00+02:00",
        },
        {
            "id": "json-invalid",
            "date_published": "not a date",
        },
        {
            "id": "json-zulu",
            "date_published": "2024-07-01T11:00:00Z",
        },
    ],
}


def test_json_feed_dates_normalize_and_preserve_source():
    feed = feedparser.parse(io.BytesIO(json.dumps(JSON_FEED).encode()))
    assert feed.bozo is False

    entries = {entry_id(entry): entry for entry in feed.entries}
    assert stored(entries["json-rfc3339"], "published") == "2024-07-01T12:00:00+02:00"
    assert utc_epoch(stored(entries["json-rfc3339"], "published_parsed")) == 1719828000

    assert stored(entries["json-invalid"], "published") == "not a date"
    assert stored(entries["json-invalid"], "published_parsed") is None

    assert utc_epoch(stored(entries["json-zulu"], "published_parsed")) == 1719831600

    assert timeline(feed) == ["json-rfc3339", "json-zulu", "json-invalid"]


# A custom rule for "DD.MM.YYYY HH:MM:SS" timestamps declared in CEST
# (+02:00).  It must not claim any other input.
_EU_DATE_PATTERN = re.compile(r"(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2}):(\d{2})")


def _parse_date_eu(value: str):
    match = _EU_DATE_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    day, month, year, hour, minute, second = (int(part) for part in match.groups())
    try:
        stamp = dt.datetime(year, month, day, hour, minute, second)
    except ValueError:
        # A dotted but impossible date; let the other handlers try.
        return None
    return (stamp - dt.timedelta(hours=2)).utctimetuple()


FEED_WITH_CUSTOM_FORMAT = _rss_feed(
    """
    <item>
      <guid isPermaLink="false">eu-published</guid>
      <title>eu-published</title>
      <pubDate>01.07.2024 15:00:00</pubDate>
    </item>
    <item>
      <guid isPermaLink="false">eu-updated</guid>
      <title>eu-updated</title>
      <pubDate>Mon, 01 Jul 2024 15:00:00 +0200</pubDate>
      <dcterms:modified>01.07.2024 12:30:00</dcterms:modified>
    </item>
    <item>
      <guid isPermaLink="false">eu-impossible</guid>
      <title>eu-impossible</title>
      <pubDate>31.02.2024 10:00:00</pubDate>
    </item>
    <item>
      <guid isPermaLink="false">rfc822</guid>
      <title>rfc822</title>
      <pubDate>Mon, 01 Jul 2024 09:00:00 -0500</pubDate>
      <dcterms:modified>2024-07-01T14:00:00Z</dcterms:modified>
    </item>
    <item>
      <guid isPermaLink="false">w3c</guid>
      <title>w3c</title>
      <dcterms:issued>2024-07-01T13:30:00Z</dcterms:issued>
      <atom:updated>Pending</atom:updated>
    </item>
    <item>
      <guid isPermaLink="false">asctime</guid>
      <title>asctime</title>
      <pubDate>Mon Jul  1 12:00:00 2024</pubDate>
    </item>
    <item>
      <guid isPermaLink="false">mssql</guid>
      <title>mssql</title>
      <pubDate>2024-07-01 13:15:00+03:00</pubDate>
    </item>
    <item>
      <guid isPermaLink="false">broken-published</guid>
      <title>broken-published</title>
      <pubDate>not a date</pubDate>
      <dcterms:modified>2024-07-01T15:00:00-01:00</dcterms:modified>
    </item>
    <item>
      <guid isPermaLink="false">date-only</guid>
      <title>date-only</title>
      <pubDate>2024-07-01</pubDate>
    </item>
    <item>
      <guid isPermaLink="false">dc-date</guid>
      <title>dc-date</title>
      <dc:date>2024-07-01T09:00:00-01:00</dc:date>
    </item>
    """
)


@pytest.mark.parametrize("loose", (False, True), ids=("strict", "loose"))
def test_custom_handler_only_claims_its_own_inputs(parse, register_date_handler, loose):
    before = parse(FEED_WITH_CUSTOM_FORMAT, loose=loose)
    assert before.bozo is False
    before_entries = {entry_id(entry): entry for entry in before.entries}

    # The built-in handlers do not understand the dotted format at all.
    for identifier in ("eu-published", "eu-impossible"):
        assert stored(before_entries[identifier], "published_parsed") is None
    # The update value is dotted, but this entry's publication date is plain
    # RFC 822 and must keep working.
    assert (
        utc_epoch(stored(before_entries["eu-updated"], "published_parsed"))
        == 1719838800
    )
    assert stored(before_entries["eu-updated"], "updated_parsed") is None

    register_date_handler(_parse_date_eu)
    after = parse(FEED_WITH_CUSTOM_FORMAT, loose=loose)
    assert after.bozo is False
    after_entries = {entry_id(entry): entry for entry in after.entries}

    # The custom rule now claims exactly the dotted timestamps...
    assert stored(after_entries["eu-published"], "published") == "01.07.2024 15:00:00"
    assert (
        utc_epoch(stored(after_entries["eu-published"], "published_parsed"))
        == 1719838800
    )
    assert (
        utc_epoch(stored(after_entries["eu-updated"], "updated_parsed")) == 1719829800
    )
    # ...handles impossible dates by declining (no bozo, no timestamp)...
    assert stored(after_entries["eu-impossible"], "published_parsed") is None
    # ...and leaves every built-in format exactly as it was.
    for identifier in before_entries:
        if identifier.startswith("eu-"):
            continue
        assert after_entries[identifier] == before_entries[identifier]

    assert timeline(after) == [
        "date-only",
        "dc-date",
        "mssql",
        "eu-updated",
        "asctime",
        "eu-published",
        "w3c",
        "rfc822",
        "broken-published",
        "eu-impossible",
    ]

    # Removing the handler restores the built-in behavior immediately.
    feedparser.datetimes._date_handlers.remove(_parse_date_eu)
    restored = parse(FEED_WITH_CUSTOM_FORMAT, loose=loose)
    assert snapshot(restored) == snapshot(before)


def test_custom_handler_that_rejects_input_does_not_break_other_formats(
    register_date_handler,
):
    def _rejecting_handler(value: str):
        if value.strip().startswith("RFCTZ:"):
            raise ValueError("unsupported pseudo timezone")
        return None

    def _short_tuple_handler(value: str):
        if value.strip().startswith("SHORT:"):
            return 2024, 7, 1
        return None

    feed_source = _rss_feed(
        """
        <item>
          <guid isPermaLink="false">rejected</guid>
          <pubDate>RFCTZ: 2024-07-01T12:00:00Z</pubDate>
        </item>
        <item>
          <guid isPermaLink="false">short</guid>
          <pubDate>SHORT: 2024-07-01</pubDate>
        </item>
        <item>
          <guid isPermaLink="false">standard</guid>
          <pubDate>Mon, 01 Jul 2024 10:00:00 GMT</pubDate>
        </item>
        """
    )

    register_date_handler(_rejecting_handler)
    register_date_handler(_short_tuple_handler)
    feed = feedparser.parse(feed_source)
    assert feed.bozo is False

    entries = {entry_id(entry): entry for entry in feed.entries}
    # Handlers that raise or return a non-9-tuple are skipped silently.
    assert stored(entries["rejected"], "published_parsed") is None
    assert stored(entries["short"], "published_parsed") is None
    # The remaining formats still use the built-in rules.
    assert utc_epoch(stored(entries["standard"], "published_parsed")) == 1719828000
    assert timeline(feed) == ["standard", "rejected", "short"]
