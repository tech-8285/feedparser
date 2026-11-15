"""Integration tests for multi-layer relative URI base resolution in Atom feeds.

Atom feeds may declare ``xml:base`` at three nested levels:

1.  the feed element,
2.  an individual entry element, and
3.  an inline XHTML element inside entry content or a summary.

When the feed is fetched over HTTP, the final response URL (after any
redirects) acts as the outermost base URI.  Every harvestable link -- links
inside the entry body, author links, and enclosure targets -- must be resolved
against the base that is in scope at the point where the link appears.

These tests additionally verify that:

*   an ``xml:base`` declared on one entry never leaks into a later entry,
    including when the entry order is swapped or when a base is missing,
    empty, or uses an unacceptable URI scheme, and
*   HTML sanitization strips dangerous markup without changing the absolute
    URIs that were resolved for legitimate links.
"""

from __future__ import annotations

import pytest
import responses

import feedparser
import feedparser.api

FEED_BASE = "http://feed.example.test/root/"

# A single feed exercising every base-URI layer.  The first entry declares an
# entry-level base and an inline XHTML-level base; the second entry declares no
# base of its own and must therefore fall back to the feed-level base.
LAYERED_FEED = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:base="{FEED_BASE}">
  <title>Layered base feed</title>
  <entry xml:base="entry-one/">
    <title>first entry</title>
    <author>
      <name>First Author</name>
      <uri>authors/first/</uri>
    </author>
    <content type="xhtml" xml:base="content/v1/">
      <div xmlns="http://www.w3.org/1999/xhtml">
        <p>Read <a href="article.html">the article</a>.</p>
      </div>
    </content>
    <link rel="enclosure"
          href="downloads/asset.bin"
          type="application/octet-stream"
          length="42"/>
  </entry>
  <entry>
    <title>second entry</title>
    <author>
      <name>Second Author</name>
      <uri>authors/second/</uri>
    </author>
    <content type="xhtml">
      <div xmlns="http://www.w3.org/1999/xhtml">
        <p>See <a href="other.html">the other article</a>.</p>
      </div>
    </content>
    <link rel="enclosure"
          href="downloads/other.bin"
          type="application/octet-stream"/>
  </entry>
</feed>
""".encode("utf-8")


@pytest.fixture(params=("strict", "loose"), ids=("strict", "loose"))
def parsed_feed(request, monkeypatch):
    """Parse the test feed with both the strict and loose XML parsers."""

    if request.param == "loose":
        monkeypatch.setattr(feedparser.api, "_XML_AVAILABLE", False)
    return feedparser.parse(LAYERED_FEED)


def test_layered_bases_resolve_body_author_and_enclosure_links(parsed_feed):
    """Links resolve against the feed, entry, and inline XHTML bases in turn."""

    result = parsed_feed
    assert result.bozo is False
    assert len(result.entries) == 2

    first, second = result.entries

    # The nested content-level base is the join of all three xml:base layers,
    # and the body link is resolved relative to that final base.
    first_content = first.content[0]
    assert first_content["base"] == (
        "http://feed.example.test/root/entry-one/content/v1/"
    )
    assert (
        'href="http://feed.example.test/root/entry-one/content/v1/article.html"'
        in first_content["value"]
    )

    # The author link is declared outside the content element, so it resolves
    # against the entry-level base rather than the inline XHTML base.
    assert first.author_detail["url"] == (
        "http://feed.example.test/root/entry-one/authors/first/"
    )

    # The enclosure is likewise an entry-level link.
    assert len(first.enclosures) == 1
    assert first.enclosures[0]["href"] == (
        "http://feed.example.test/root/entry-one/downloads/asset.bin"
    )
    assert first.enclosures[0]["type"] == "application/octet-stream"


def test_feed_level_base_is_used_when_entry_declares_no_base(parsed_feed):
    """An entry without its own xml:base inherits the feed-level base only."""

    second = parsed_feed.entries[1]

    second_content = second.content[0]
    assert second_content["base"] == FEED_BASE
    assert f'href="{FEED_BASE}other.html"' in second_content["value"]
    assert second.author_detail["url"] == (
        "http://feed.example.test/root/authors/second/"
    )
    assert second.enclosures[0]["href"] == (
        "http://feed.example.test/root/downloads/other.bin"
    )


# Two entries with deliberately distinct entry-level bases.  The same pair of
# entries is parsed in both orders so that any base-scope leakage would change
# the resolved links of the entry that is parsed second.
SCOPED_ENTRY_A = """
  <entry xml:base="scope-a/">
    <title>scoped entry A</title>
    <author><name>Author A</name><uri>people/a/</uri></author>
    <content type="xhtml" xml:base="xhtml-a/">
      <div xmlns="http://www.w3.org/1999/xhtml">
        <a href="page-a.html">A</a>
        <a href="nested/deep-a.html">deep A</a>
      </div>
    </content>
    <link rel="enclosure" href="files/a.bin" type="application/octet-stream"/>
  </entry>
"""

SCOPED_ENTRY_B = """
  <entry xml:base="scope-b/nested/">
    <title>scoped entry B</title>
    <author><name>Author B</name><uri>people/b/</uri></author>
    <content type="xhtml">
      <div xmlns="http://www.w3.org/1999/xhtml">
        <a href="page-b.html">B</a>
      </div>
    </content>
    <link rel="enclosure" href="files/b.bin" type="application/octet-stream"/>
  </entry>
"""


def _parse_entries_in_order(first_entry: str, second_entry: str):
    feed = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<feed xmlns="http://www.w3.org/2005/Atom" xml:base="{FEED_BASE}">'
        f"{first_entry}{second_entry}"
        "</feed>"
    ).encode("utf-8")
    result = feedparser.parse(feed)
    assert result.bozo is False
    return result.entries


def test_entry_base_scope_does_not_leak_when_entry_order_is_swapped():
    """Each entry's links are identical regardless of parse order."""

    ab = _parse_entries_in_order(SCOPED_ENTRY_A, SCOPED_ENTRY_B)
    ba = _parse_entries_in_order(SCOPED_ENTRY_B, SCOPED_ENTRY_A)

    entry_a_from_ab, entry_b_from_ab = ab
    entry_b_from_ba, entry_a_from_ba = ba

    expected_a_body = (
        "http://feed.example.test/root/scope-a/xhtml-a/page-a.html"
    )
    expected_a_deep = (
        "http://feed.example.test/root/scope-a/xhtml-a/nested/deep-a.html"
    )
    expected_a_author = "http://feed.example.test/root/scope-a/people/a/"
    expected_a_enclosure = "http://feed.example.test/root/scope-a/files/a.bin"

    expected_b_body = (
        "http://feed.example.test/root/scope-b/nested/page-b.html"
    )
    expected_b_author = (
        "http://feed.example.test/root/scope-b/nested/people/b/"
    )
    expected_b_enclosure = (
        "http://feed.example.test/root/scope-b/nested/files/b.bin"
    )

    for entry_a in (entry_a_from_ab, entry_a_from_ba):
        assert expected_a_body in entry_a.content[0]["value"]
        assert expected_a_deep in entry_a.content[0]["value"]
        assert entry_a.author_detail["url"] == expected_a_author
        assert entry_a.enclosures[0]["href"] == expected_a_enclosure

    for entry_b in (entry_b_from_ab, entry_b_from_ba):
        assert expected_b_body in entry_b.content[0]["value"]
        assert entry_b.author_detail["url"] == expected_b_author
        assert entry_b.enclosures[0]["href"] == expected_b_enclosure


# A feed that combines every problematic base declaration: an entry with no
# xml:base, an entry with an empty xml:base, and an entry whose xml:base uses
# an unacceptable URI scheme.  Every entry must fall back to the feed-level
# base instead of inheriting a sibling entry's base or producing a bad URI.
FALLBACK_FEED = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:base="{FEED_BASE}">
  <entry xml:base="preceding/scope/">
    <title>preceding entry with its own base</title>
    <content type="xhtml">
      <div xmlns="http://www.w3.org/1999/xhtml"><a href="preceding.html">p</a></div>
    </content>
  </entry>
  <entry>
    <title>entry without a base declaration</title>
    <author><name>No Base</name><uri>authors/no-base/</uri></author>
    <content type="xhtml">
      <div xmlns="http://www.w3.org/1999/xhtml"><a href="no-base.html">n</a></div>
    </content>
    <link rel="enclosure" href="files/no-base.bin" type="application/octet-stream"/>
  </entry>
  <entry xml:base="">
    <title>entry with an empty base declaration</title>
    <author><name>Empty Base</name><uri>authors/empty-base/</uri></author>
    <content type="xhtml" xml:base="">
      <div xmlns="http://www.w3.org/1999/xhtml"><a href="empty-base.html">e</a></div>
    </content>
    <link rel="enclosure" href="files/empty.bin" type="application/octet-stream"/>
  </entry>
  <entry xml:base="javascript:alert(1)">
    <title>entry with an invalid base scheme</title>
    <author><name>Bad Base</name><uri>authors/bad-base/</uri></author>
    <content type="xhtml" xml:base="javascript:alert(1)">
      <div xmlns="http://www.w3.org/1999/xhtml"><a href="bad-base.html">b</a></div>
    </content>
    <link rel="enclosure" href="files/bad.bin" type="application/octet-stream"/>
  </entry>
</feed>
""".encode("utf-8")


@pytest.mark.parametrize("use_loose_parser", (False, True), ids=("strict", "loose"))
def test_missing_empty_and_invalid_bases_fall_back_to_feed_base(
    use_loose_parser, monkeypatch
):
    """Missing, empty, and scheme-rejected bases do not leak or corrupt links."""

    if use_loose_parser:
        monkeypatch.setattr(feedparser.api, "_XML_AVAILABLE", False)

    result = feedparser.parse(FALLBACK_FEED)
    assert result.bozo is False
    preceding, missing, empty, invalid = result.entries

    # The first entry keeps its own base and must not be retroactively altered.
    assert preceding.content[0]["base"] == (
        "http://feed.example.test/root/preceding/scope/"
    )
    assert (
        'href="http://feed.example.test/root/preceding/scope/preceding.html"'
        in preceding.content[0]["value"]
    )

    for entry, slug, enclosure_name in (
        (missing, "no-base", "no-base"),
        (empty, "empty-base", "empty"),
        (invalid, "bad-base", "bad"),
    ):
        # No in-scope entry-level base survives, so the feed base applies.
        assert entry.content[0]["base"] == FEED_BASE
        assert f'href="{FEED_BASE}{slug}.html"' in entry.content[0]["value"]
        assert entry.author_detail["url"] == (
            f"http://feed.example.test/root/authors/{slug}/"
        )
        assert entry.enclosures[0]["href"] == (
            f"http://feed.example.test/root/files/{enclosure_name}.bin"
        )


SANITIZING_FEED = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:base="{FEED_BASE}">
  <entry xml:base="sanitize-entry/">
    <title>sanitization entry</title>
    <content type="xhtml" xml:base="inline/">
      <div xmlns="http://www.w3.org/1999/xhtml">
        <a href="safe.html" onclick="stealCookies()">safe link</a>
        <a href="javascript:alert(1)">dangerous link</a>
        <img src="image.png" onerror="alert(1)" alt="image"/>
      </div>
    </content>
    <summary type="xhtml" xml:base="summary/">
      <div xmlns="http://www.w3.org/1999/xhtml">
        <a href="summary-page.html">summary link</a>
      </div>
    </summary>
  </entry>
</feed>
""".encode("utf-8")


def test_sanitization_preserves_resolved_absolute_links():
    """Dangerous markup is stripped but legitimate resolved URIs survive."""

    result = feedparser.parse(SANITIZING_FEED)
    assert result.bozo is False
    entry = result.entries[0]

    content_value = entry.content[0]["value"]
    content_base = "http://feed.example.test/root/sanitize-entry/inline/"

    # The event handler is removed while the resolved absolute href remains.
    assert "onclick" not in content_value
    assert f'href="{content_base}safe.html"' in content_value

    # Unacceptable URI schemes are neutralized rather than made absolute.
    assert "javascript:alert(1)" not in content_value
    assert 'href=""' in content_value

    # Element attributes are sanitized while the resolved src is retained.
    assert "onerror" not in content_value
    assert f'src="{content_base}image.png"' in content_value

    # The summary declares its own inline base, independent of the content.
    summary_base = "http://feed.example.test/root/sanitize-entry/summary/"
    assert f'href="{summary_base}summary-page.html"' in entry.summary


REDIRECT_URL = "http://127.0.0.1:8097/tests/redirect/old-feed.xml"
FINAL_URL = "http://127.0.0.1:8097/tests/redirect/final/location/feed.xml"

# Relative xml:base values at every layer; each one must be resolved on top of
# the final post-redirect URL rather than the originally requested URL.
REDIRECT_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:base="feed-layer/">
  <entry xml:base="entry-layer/">
    <title>redirected entry</title>
    <author><name>Redirected Author</name><uri>authors/red/</uri></author>
    <content type="xhtml" xml:base="xhtml-layer/">
      <div xmlns="http://www.w3.org/1999/xhtml">
        <a href="body-page.html">body link</a>
      </div>
    </content>
    <link rel="enclosure"
          href="files/red.bin"
          type="application/octet-stream"/>
  </entry>
</feed>
"""

FINAL_BASE = "http://127.0.0.1:8097/tests/redirect/final/location/"


def test_redirect_final_url_composes_with_multi_layer_bases():
    """The post-redirect URL is the outermost base for every relative link."""

    responses.add(
        responses.GET,
        REDIRECT_URL,
        status=302,
        headers={"Location": FINAL_URL},
    )
    responses.add(
        responses.GET,
        FINAL_URL,
        body=REDIRECT_FEED,
        content_type="application/atom+xml",
    )

    result = feedparser.parse(REDIRECT_URL)
    assert result.bozo is False
    assert result.status == 200
    assert result.href == FINAL_URL

    entry = result.entries[0]

    # Feed + entry + inline XHTML bases all join on top of the redirect target.
    expected_inline_base = f"{FINAL_BASE}feed-layer/entry-layer/xhtml-layer/"
    assert entry.content[0]["base"] == expected_inline_base
    assert (
        f'href="{expected_inline_base}body-page.html"'
        in entry.content[0]["value"]
    )

    # The author link stops at the entry layer.
    assert entry.author_detail["url"] == (
        f"{FINAL_BASE}feed-layer/entry-layer/authors/red/"
    )

    # The enclosure resolves to an absolute target at the entry layer.
    assert entry.enclosures[0]["href"] == (
        f"{FINAL_BASE}feed-layer/entry-layer/files/red.bin"
    )

    # Both the original and redirected URLs were fetched exactly once.
    redirect_calls = [
        call for call in responses.calls if call.request.url == REDIRECT_URL
    ]
    final_calls = [
        call for call in responses.calls if call.request.url == FINAL_URL
    ]
    assert len(redirect_calls) == 1
    assert redirect_calls[0].response.status_code == 302
    assert len(final_calls) == 1
