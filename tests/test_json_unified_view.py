"""Acceptance tests for the unified entry view across RSS and JSON Feed.

The same content expressed in RSS and in JSON Feed must produce entries
whose identity, body, authors, attachments, and source relationships are
usable through the same accessors, regardless of the source format.
"""

import feedparser

RSS_DOCUMENT = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Example feed</title>
    <link>https://example.org/</link>
    <item>
      <guid>42</guid>
      <title>Hello</title>
      <link>https://example.org/42</link>
      <description>Summary</description>
      <content:encoded>Full body</content:encoded>
      <author>me@example.org</author>
      <enclosure url="https://example.org/a.mp3" length="123" type="audio/mpeg"/>
      <source url="https://origin.example/feed">Origin</source>
    </item>
    <item>
      <guid>43</guid>
      <title>Second</title>
    </item>
  </channel>
</rss>
"""

JSON_DOCUMENT = """{
    "version": "https://jsonfeed.org/version/1",
    "title": "Example feed",
    "home_page_url": "https://example.org/",
    "items": [
        {
            "id": "42",
            "title": "Hello",
            "url": "https://example.org/42",
            "summary": "Summary",
            "content_text": "Full body",
            "author": {"name": "me", "url": "mailto:me@example.org"},
            "attachments": [
                {
                    "url": "https://example.org/a.mp3",
                    "mime_type": "audio/mpeg",
                    "size_in_bytes": 123
                }
            ],
            "external_url": "https://origin.example/feed"
        },
        {
            "id": "43",
            "title": "Second"
        }
    ]
}
"""

rss_entries = feedparser.parse(RSS_DOCUMENT).entries
json_entries = feedparser.parse(JSON_DOCUMENT).entries


def test_entry_collections_align_across_formats():
    assert [e.id for e in rss_entries] == [e.id for e in json_entries] == ["42", "43"]
    assert [e.title for e in rss_entries] == [e.title for e in json_entries]


def test_identity_and_links_are_equivalent():
    rss, json_ = rss_entries[0], json_entries[0]
    assert rss.id == json_.id == "42"
    assert rss.link == json_.link == "https://example.org/42"
    # Both formats expose the alternate link through `links`.
    assert rss.links[0].rel == json_.links[0].rel == "alternate"
    assert rss.links[0].href == json_.links[0].href == "https://example.org/42"


def test_body_is_a_content_list_in_both_formats():
    for entry in (rss_entries[0], json_entries[0]):
        assert isinstance(entry.content, list)
        assert entry.content[0].value == "Full body"
        assert entry.content[0].type in ("text/plain", "text/html")


def test_authors_are_equivalent():
    rss, json_ = rss_entries[0], json_entries[0]
    assert json_.author == "me"
    assert json_.author_detail.email == "me@example.org"
    assert json_.authors[0].name == "me"
    assert rss.author_detail.email == json_.author_detail.email == "me@example.org"


def test_attachments_are_enclosures_in_both_formats():
    rss, json_ = rss_entries[0], json_entries[0]
    assert rss.enclosures[0].href == json_.enclosures[0].href
    assert rss.enclosures[0].type == json_.enclosures[0].type == "audio/mpeg"
    assert str(json_.enclosures[0].length) == rss.enclosures[0].length == "123"


def test_source_relationships_are_equivalent():
    rss, json_ = rss_entries[0], json_entries[0]
    assert rss.source.href == json_.source.href == "https://origin.example/feed"
    # Entries without a source agree on its absence in both formats.
    assert "source" not in rss_entries[1]
    assert "source" not in json_entries[1]


def test_missing_fields_are_absent_in_both_formats():
    for entry in (rss_entries[1], json_entries[1]):
        assert "summary" not in entry
        assert "author" not in entry
        assert "content" not in entry
        # Both formats omit `links` when there is nothing to put in it.
        assert entry.get("enclosures") is None
