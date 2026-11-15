"""Tests for the JSON Feed parser and its unified entry view.

The tests focus on:

* cross-format parity between equivalent JSON Feed and RSS 2.0 documents,
* preserving the distinction between missing keys, explicit nulls and
  empty strings,
* isolating malformed attachments and entries instead of discarding data,
* and keeping the legacy FeedParserDict access paths working.
"""

from __future__ import annotations

import io
import json

import pytest

import feedparser


def parse_json(payload: dict) -> feedparser.FeedParserDict:
    return feedparser.parse(
        io.StringIO(json.dumps(payload)),
        response_headers={"Content-Type": "application/json"},
    )


def json_feed(items, version="https://jsonfeed.org/version/1.1"):
    return {"version": version, "title": "feed", "items": items}


# ---------------------------------------------------------------------------
# Cross-format parity
# ---------------------------------------------------------------------------

RSS_FEED = """<?xml version="1.0"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>feed</title>
    <item>
      <title>Entry title</title>
      <guid>entry-1</guid>
      <link>https://example.org/1</link>
      <description>Entry summary</description>
      <content:encoded><![CDATA[<p>body</p>]]></content:encoded>
      <author>Alice</author>
      <enclosure url="https://example.org/a.mp3"
                 length="1024" type="audio/mpeg"/>
      <category>news</category>
    </item>
  </channel>
</rss>
"""

JSON_FEED = json_feed(
    [
        {
            "id": "entry-1",
            "title": "Entry title",
            "url": "https://example.org/1",
            "summary": "Entry summary",
            "content_html": "<p>body</p>",
            "author": {"name": "Alice"},
            "attachments": [
                {
                    "url": "https://example.org/a.mp3",
                    "mime_type": "audio/mpeg",
                    "size_in_bytes": 1024,
                }
            ],
            "tags": ["news"],
        }
    ]
)


def _links_by_rel(entry):
    return {link["rel"]: link for link in entry["links"]}


def test_cross_format_entry_parity():
    rss = feedparser.parse(io.StringIO(RSS_FEED))
    js = parse_json(JSON_FEED)

    assert rss.version == "rss20"
    assert js.version == "json11"
    assert len(rss.entries) == len(js.entries) == 1

    r, j = rss.entries[0], js.entries[0]

    # Identity
    assert r["id"] == j["id"] == "entry-1"
    assert r.guid == j.guid == "entry-1"
    assert r["link"] == j["link"] == "https://example.org/1"

    # Scalar text fields
    assert r["title"] == j["title"] == "Entry title"
    assert r["summary"] == j["summary"] == "Entry summary"

    # Body: both expose content as a list of detail dicts
    assert isinstance(r["content"], list)
    assert isinstance(j["content"], list)
    assert r["content"][0]["value"] == j["content"][0]["value"] == "<p>body</p>"
    assert r["content"][0]["type"] == j["content"][0]["type"] == "text/html"
    assert set(r["content"][0]) == set(j["content"][0])

    # Detail dicts share the XML parser's key shape.
    for detail_key in ("title_detail", "summary_detail"):
        assert detail_key in r
        assert detail_key in j
        assert r[detail_key]["value"] == j[detail_key]["value"]
        assert r[detail_key]["language"] == j[detail_key]["language"]
        assert r[detail_key]["base"] == j[detail_key]["base"]

    # Titles default to text/plain in both formats. RSS descriptions default
    # to text/html, while a JSON Feed summary is plain text by specification.
    assert r["title_detail"]["type"] == j["title_detail"]["type"] == "text/plain"
    assert r["summary_detail"]["type"] == "text/html"
    assert j["summary_detail"]["type"] == "text/plain"

    # Author
    assert r["author"] == j["author"] == "Alice"
    assert r["author_detail"] == j["author_detail"] == {"name": "Alice"}
    assert r["authors"] == j["authors"] == [{"name": "Alice"}]

    # Tags
    assert r["tags"] == j["tags"] == [{"term": "news", "scheme": None, "label": None}]
    assert r["category"] == j["category"] == "news"

    # Enclosures, including string-typed length
    assert (
        r["enclosures"]
        == j["enclosures"]
        == [
            {
                "href": "https://example.org/a.mp3",
                "type": "audio/mpeg",
                "length": "1024",
            }
        ]
    )
    assert _links_by_rel(r)["enclosure"] == _links_by_rel(j)["enclosure"]
    assert _links_by_rel(r)["alternate"] == _links_by_rel(j)["alternate"]


def test_json_feed_versions():
    assert parse_json(json_feed([], "https://jsonfeed.org/version/1")).version == (
        "json1"
    )
    assert (
        parse_json(json_feed([], "https://jsonfeed.org/version/1.1")).version
        == "json11"
    )


# ---------------------------------------------------------------------------
# Missing / null / empty-string semantics
# ---------------------------------------------------------------------------


def test_scalar_three_way_semantics():
    result = parse_json(
        json_feed(
            [
                {"id": "missing"},
                {"id": "null", "title": None, "summary": None},
                {"id": "empty", "title": "", "summary": ""},
            ]
        )
    )
    missing, null, empty = result.entries

    assert "title" not in missing
    assert "summary" not in missing

    assert null["title"] is None
    assert null["summary"] is None
    assert null["title_detail"]["value"] is None
    assert null["summary_detail"]["value"] is None

    assert empty["title"] == ""
    assert empty["summary"] == ""
    assert empty["title_detail"]["value"] == ""
    assert empty["summary_detail"]["value"] == ""


def test_identity_three_way_semantics():
    result = parse_json(
        json_feed(
            [
                {"title": "no-id"},
                {"id": None, "title": "null-id"},
                {"id": "", "title": "empty-id"},
            ]
        )
    )
    missing, null, empty = result.entries

    assert "id" not in missing
    assert null["id"] is None
    assert null.guid is None
    assert empty["id"] == ""


def test_content_three_way_semantics():
    result = parse_json(
        json_feed(
            [
                {"id": "missing"},
                {"id": "null", "content_text": None},
                {"id": "empty", "content_text": ""},
            ]
        )
    )
    missing, null, empty = result.entries

    assert "content" not in missing
    assert null["content"] == [
        {"value": None, "type": "text/plain", "language": None, "base": ""}
    ]
    assert empty["content"][0]["value"] == ""
    assert empty["content"][0]["type"] == "text/plain"


def test_content_html_type_and_sanitization():
    result = parse_json(
        json_feed(
            [
                {
                    "id": "1",
                    "content_html": "<p>ok</p><script>alert(1)</script>",
                }
            ]
        )
    )
    content = result.entries[0]["content"]
    assert content[0]["type"] == "text/html"
    assert content[0]["value"] == "<p>ok</p>"


def test_author_three_way_semantics():
    result = parse_json(
        json_feed(
            [
                {"id": "missing"},
                {"id": "null", "author": None},
                {"id": "empty", "author": {}},
                {"id": "name-null", "author": {"name": None}},
                {"id": "name-empty", "author": {"name": ""}},
            ]
        )
    )
    missing, null, empty, name_null, name_empty = result.entries

    # Missing author: no author-related keys at all.
    assert "author" not in missing
    assert "author_detail" not in missing
    assert "authors" not in missing

    # Explicit null: keys exist, scalar author is None.
    assert null["author"] is None
    assert null["author_detail"] == {}
    assert null["authors"] == [{}]

    # Empty object: detail exists but there is no scalar author value.
    assert "author" not in empty
    assert empty["author_detail"] == {}
    assert empty["authors"] == [{}]

    # Null vs empty-string name remain distinguishable.
    assert name_null["author"] is None
    assert name_null["author_detail"]["name"] is None
    assert name_empty["author"] == ""
    assert name_empty["author_detail"]["name"] == ""


def test_author_mailto_and_null_url():
    result = parse_json(
        json_feed(
            [
                {"id": "mailto", "author": {"name": "A", "url": "mailto:a@x.com"}},
                {"id": "null-url", "author": {"name": "B", "url": None}},
                {"id": "href", "author": {"name": "C", "url": "https://x/c"}},
            ]
        )
    )
    mailto, null_url, href = result.entries
    assert mailto["author_detail"] == {"name": "A", "email": "a@x.com"}
    assert null_url["author_detail"] == {"name": "B", "href": None}
    assert href["author_detail"] == {"name": "C", "href": "https://x/c"}


def test_tags_missing_null_and_empty_distinction():
    result = parse_json(
        json_feed(
            [
                {"id": "missing"},
                {"id": "null", "tags": None},
                {"id": "empty", "tags": []},
                {"id": "filled", "tags": ["a", "b"]},
            ]
        )
    )
    missing, null, empty, filled = result.entries

    assert "tags" not in missing
    assert null["tags"] == []
    assert empty["tags"] == []
    assert filled["tags"] == [
        {"term": "a", "scheme": None, "label": None},
        {"term": "b", "scheme": None, "label": None},
    ]


def test_empty_string_attachment_fields_are_kept():
    result = parse_json(
        json_feed(
            [
                {
                    "id": "1",
                    "attachments": [{"url": "", "mime_type": ""}],
                }
            ]
        )
    )
    assert not result.bozo
    assert result.entries[0]["enclosures"] == [{"href": "", "type": ""}]


# ---------------------------------------------------------------------------
# Malformed data isolation and bozo handling
# ---------------------------------------------------------------------------


def test_malformed_attachment_does_not_swallow_entries():
    result = parse_json(
        json_feed(
            [
                {
                    "id": "1",
                    "attachments": [
                        {"mime_type": "audio/mpeg"},  # missing url
                        "not-an-object",
                        {"url": "https://x/good.mp3", "mime_type": "audio/mpeg"},
                    ],
                },
                {"id": "2", "attachments": [{"url": "u", "mime_type": None}]},
            ]
        )
    )

    assert result.bozo
    assert isinstance(result.bozo_exception, Exception)
    assert [entry["id"] for entry in result.entries] == ["1", "2"]

    # Only the valid attachment survives.
    assert result.entries[0]["enclosures"] == [
        {"href": "https://x/good.mp3", "type": "audio/mpeg"}
    ]
    # The second entry has no usable enclosure but still exists.
    assert result.entries[1].get("enclosures") == []


def test_malformed_entry_is_skipped_but_siblings_survive():
    result = parse_json(
        json_feed(
            [
                {"id": "good-1"},
                "not-an-object",
                {"id": "good-2"},
            ]
        )
    )
    assert result.bozo
    assert isinstance(result.bozo_exception, TypeError)
    assert [entry["id"] for entry in result.entries] == ["good-1", "good-2"]


@pytest.mark.parametrize(
    "items",
    [
        None,
        "not-a-list",
        42,
    ],
)
def test_invalid_items_collection(items):
    result = parse_json(json_feed([]) | {"items": items})
    assert result.bozo
    assert isinstance(result.bozo_exception, TypeError)
    assert result.entries == []


def test_invalid_tags_and_attachments_types_mark_bozo():
    result = parse_json(
        json_feed(
            [
                {"id": "1", "tags": "oops"},
                {"id": "2", "attachments": "oops"},
                {"id": "3", "tags": ["ok", 5]},
            ]
        )
    )
    assert result.bozo
    entries = {entry["id"]: entry for entry in result.entries}
    assert "tags" not in entries["1"]
    assert "links" not in entries["2"]
    # The non-string tag is dropped, the valid one remains.
    assert entries["3"]["tags"] == [{"term": "ok", "scheme": None, "label": None}]


def test_well_formed_feed_is_not_bozo():
    result = parse_json(json_feed([{"id": "1"}]))
    assert not result.bozo
    assert "bozo_exception" not in result


# ---------------------------------------------------------------------------
# Regression tests for crash bugs and legacy access
# ---------------------------------------------------------------------------


def test_date_updated_mapping():
    result = parse_json(
        json_feed([{"id": "1", "date_updated": "2020-01-02T03:04:05Z"}])
    )
    assert not result.bozo
    entry = result.entries[0]
    assert entry["updated"] == "2020-01-02T03:04:05Z"
    assert entry["updated_parsed"][:6] == (2020, 1, 2, 3, 4, 5)


def test_null_dates_do_not_crash():
    result = parse_json(
        json_feed(
            [
                {
                    "id": "1",
                    "date_published": None,
                    "date_updated": None,
                }
            ]
        )
    )
    assert not result.bozo
    entry = result.entries[0]
    assert entry["published"] is None
    assert entry["published_parsed"] is None
    assert entry["updated"] is None
    assert entry["updated_parsed"] is None


def test_null_author_does_not_crash():
    result = parse_json(json_feed([{"id": "1", "author": None}]))
    assert not result.bozo
    assert result.entries[0]["author"] is None


def test_legacy_access_paths():
    result = parse_json(
        json_feed(
            [
                {
                    "id": "legacy",
                    "tags": ["cat"],
                    "attachments": [{"url": "https://x/a", "mime_type": "audio/mpeg"}],
                },
                {"id": "plain"},
            ]
        )
    )
    entry, plain = result.entries

    # guid/id aliasing
    assert entry.guid == entry.id == "legacy"
    # category view backed by tags
    assert entry.category == "cat"
    # enclosures work through both subscript and .get()
    assert entry["enclosures"] == [{"href": "https://x/a", "type": "audio/mpeg"}]
    assert entry.get("enclosures") == entry["enclosures"]
    # An entry without links behaves exactly like an XML entry: .get()
    # resolves to None instead of raising, while direct subscript raises.
    assert plain.get("enclosures") is None
    with pytest.raises(KeyError):
        plain["enclosures"]
