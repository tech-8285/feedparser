"""Integration tests for sanitization of entry content.

These tests parse an entry from several serializations (Atom text, escaped
HTML, CDATA HTML, inline XHTML, and RSS escaped HTML / CDATA) and verify that
the visible text and link targets survive sanitization and relative URI
resolution. The same entry is then parsed with sanitization disabled to
confirm that only dangerous constructs differ, and that entry identity and
harvestable targets remain stable.
"""

from __future__ import annotations

import io
import re
from html.parser import HTMLParser

import pytest

import feedparser
import feedparser.api

CONTENT_LOCATION = "http://example.com/feed.xml"

# The markup is written the way an HTML author would write it: character
# references describe visible characters, and escaped tags describe literal
# text. Whitespace between block elements keeps the visible words separated.
HTML_BODY = (
    '<div class="body">\n'
    "<p>Fish &amp; Chips &#233; &#8212; "
    "not &lt;fish&gt; &amp; not &amp;amp;.</p>\n"
    "<p><em>nested <strong>strong</strong> text</em></p>\n"
    "<pre><code>if a &lt; b &amp;&amp; c &gt; d: print('&amp;amp;')\n"
    "&lt;script&gt;alert('x')&lt;/script&gt;\n"
    '&lt;a href="/not-a-link"&gt;code link text&lt;/a&gt;</code></pre>\n'
    '<p>Links: <a href="../docs/guide.html?a=1&amp;b=2">guide</a>, '
    '<a href="/about/">about</a>, '
    '<a href="javascript:steal()">unsafe</a></p>\n'
    '<p onclick="steal()">Attributed &amp; kept</p>\n'
    "<script>alert('boo')</script>\n"
    "</div>"
)

TEXT_BODY = (
    "Fish & Chips <not-a-tag>, café &amp; more; "
    'an <a href="/relative/page.html">inline link</a> stays text.'
)

ATOM_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:base="http://example.com/feed/">
  <title type="text">Atom feed</title>
  <id>urn:feed:atom</id>
  <updated>2020-01-01T00:00:00Z</updated>
  <entry>
    <title type="text">Entry &amp; One</title>
    <id>urn:entry:one</id>
    <link rel="alternate" href="posts/one.html"/>
    <updated>2020-01-01T00:00:00Z</updated>
    <content type="{ctype}">{payload}</content>
  </entry>
  <entry>
    <title type="text">Entry Two</title>
    <id>urn:entry:two</id>
    <link rel="alternate" href="posts/two.html"/>
    <updated>2020-01-02T00:00:00Z</updated>
    <content type="text">second &amp; plain</content>
  </entry>
</feed>
"""

RSS_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>RSS feed</title>
    <link>http://example.com/feed.xml</link>
    <description>feed</description>
    <item>
      <title>Entry &amp; One</title>
      <link>http://example.com/posts/one.html</link>
      <guid isPermaLink="false">urn:entry:one</guid>
      <description>{payload}</description>
    </item>
    <item>
      <title>Entry Two</title>
      <link>http://example.com/posts/two.html</link>
      <guid isPermaLink="false">urn:entry:two</guid>
      <description>second &amp; plain</description>
    </item>
  </channel>
</rss>
"""


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cdata(text: str) -> str:
    return "<![CDATA[" + text + "]]>"


def _atom(ctype: str, payload: str) -> bytes:
    document = ATOM_TEMPLATE.format(ctype=ctype, payload=payload)
    return document.encode("utf-8")


def _rss(payload: str) -> bytes:
    return RSS_TEMPLATE.format(payload=payload).encode("utf-8")


# carrier name -> (document, content type, expected entry link href)
CARRIERS = {
    "atom-text": (
        _atom("text", _xml_escape(TEXT_BODY)),
        "application/atom+xml",
        "http://example.com/feed/posts/one.html",
    ),
    "atom-html-escaped": (
        _atom("html", _xml_escape(HTML_BODY)),
        "application/atom+xml",
        "http://example.com/feed/posts/one.html",
    ),
    "atom-html-cdata": (
        _atom("html", _cdata(HTML_BODY)),
        "application/atom+xml",
        "http://example.com/feed/posts/one.html",
    ),
    "atom-xhtml": (
        _atom("xhtml", HTML_BODY),
        "application/atom+xml",
        "http://example.com/feed/posts/one.html",
    ),
    "rss-html-escaped": (
        _rss(_xml_escape(HTML_BODY)),
        "application/rss+xml",
        "http://example.com/posts/one.html",
    ),
    "rss-html-cdata": (
        _rss(_cdata(HTML_BODY)),
        "application/rss+xml",
        "http://example.com/posts/one.html",
    ),
}

MARKUP_CARRIERS = [name for name in CARRIERS if name != "atom-text"]

# Visible text of the markup above, as a reader would see it, with runs of
# whitespace collapsed. The `<code>` element holds escaped tags, so they must
# appear as text rather than as elements, and each character reference must be
# decoded exactly once: `&amp;#233;` would render as the literal text
# "&#233;", and `&amp;amp;` must render as "&amp;" rather than "&".
EXPECTED_VISIBLE_TEXT = (
    "Fish & Chips é — not <fish> & not &amp;. "
    "nested strong text "
    "if a < b && c > d: print('&amp;') "
    "<script>alert('x')</script> "
    '<a href="/not-a-link">code link text</a> '
    "Links: guide, about, unsafe "
    "Attributed & kept"
)


class _VisibleContent(HTMLParser):
    """Collect the visible text, anchor targets and element names."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.anchors: list[dict[str, str]] = []
        self.tags: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs) -> None:
        self.tags.append(tag)
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if tag == "a":
            self.anchors.append(dict(attrs))

    def handle_startendtag(self, tag, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in {"script", "style"}:
            return
        self._skip_depth -= 1

    def handle_endtag(self, tag) -> None:
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data) -> None:
        if self._skip_depth:
            return
        self.text.append(data)

    @property
    def visible_text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self.text)).strip()


def _visible(markup: str) -> _VisibleContent:
    parser = _VisibleContent()
    parser.feed(markup)
    parser.close()
    return parser


def _parse(
    carrier: str,
    *,
    sanitize_html: bool,
    resolve_relative_uris: bool,
) -> feedparser.FeedParserDict:
    document, content_type, _ = CARRIERS[carrier]
    return feedparser.parse(
        io.BytesIO(document),
        response_headers={
            "content-type": content_type,
            "content-location": CONTENT_LOCATION,
        },
        sanitize_html=sanitize_html,
        resolve_relative_uris=resolve_relative_uris,
    )


def _content_value(entry) -> str:
    if entry.get("content"):
        return entry.content[0]["value"]
    return entry.summary_detail["value"]


@pytest.fixture(params=[False, True], ids=["strict", "loose"])
def parser_flavor(request, monkeypatch):
    """Run each case with the strict XML parser and the loose parser."""
    if request.param:
        monkeypatch.setattr(feedparser.api, "_XML_AVAILABLE", False)
    return request.param


@pytest.mark.parametrize("carrier", MARKUP_CARRIERS)
@pytest.mark.parametrize("sanitize_html", [True, False], ids=["sanitize", "raw"])
@pytest.mark.parametrize(
    "resolve_relative_uris", [True, False], ids=["resolve", "keep"]
)
def test_visible_text_is_preserved(
    carrier, sanitize_html, resolve_relative_uris, parser_flavor
):
    result = _parse(
        carrier,
        sanitize_html=sanitize_html,
        resolve_relative_uris=resolve_relative_uris,
    )
    assert result.bozo is False, result.get("bozo_exception")
    assert len(result.entries) == 2

    value = _content_value(result.entries[0])
    visible = _visible(value)

    # The words are neither lost nor duplicated, and the escaped markup inside
    # <code> is text, not markup.
    assert visible.visible_text == EXPECTED_VISIBLE_TEXT
    assert visible.tags.count("code") == 1
    assert "<fish>" in visible.visible_text
    assert "fish" not in visible.tags
    assert '<a href="/not-a-link">' in visible.visible_text

    # Character references are decoded exactly once.
    assert visible.visible_text.count("é") == 1
    assert visible.visible_text.count("—") == 1
    assert "&#233;" not in visible.visible_text
    assert "&#8212;" not in visible.visible_text
    assert "not &." not in visible.visible_text

    # Real links keep their meaning: the same count, and the same targets.
    assert len(visible.anchors) == 3
    expected_resolved = "http://example.com/docs/guide.html?a=1&b=2"
    expected_unresolved = "../docs/guide.html?a=1&b=2"
    expected_href = expected_resolved if resolve_relative_uris else expected_unresolved
    assert visible.anchors[0]["href"] == expected_href
    assert "?a=1&amp;amp;b=2" not in value
    expected_about = "http://example.com/about/" if resolve_relative_uris else "/about/"
    assert visible.anchors[1]["href"] == expected_about
    assert visible.anchors[2]["href"] == ("" if sanitize_html else "javascript:steal()")

    if sanitize_html:
        assert "script" not in visible.tags
        assert "onclick" not in value
        assert "<script" not in value
    else:
        assert visible.tags.count("script") == 1
        assert 'onclick="steal()"' in value
        assert "<script>alert('boo')</script>" in value


@pytest.mark.parametrize(
    "resolve_relative_uris", [True, False], ids=["resolve", "keep"]
)
@pytest.mark.parametrize("sanitize_html", [True, False], ids=["sanitize", "raw"])
def test_text_type_is_not_parsed_as_markup(
    sanitize_html, resolve_relative_uris, parser_flavor
):
    result = _parse(
        "atom-text",
        sanitize_html=sanitize_html,
        resolve_relative_uris=resolve_relative_uris,
    )
    assert result.bozo is False, result.get("bozo_exception")
    assert len(result.entries) == 2

    value = _content_value(result.entries[0])
    # Plain text is handed back verbatim: angle brackets are not tags, the
    # apparent link is not a harvestable target, and no extra escaping or
    # unescaping is applied.
    assert value == TEXT_BODY
    assert "<not-a-tag>" in value
    assert 'href="/relative/page.html"' in value

    assert result.entries[0].links == [
        {
            "rel": "alternate",
            "href": "http://example.com/feed/posts/one.html",
            "type": "text/html",
        }
    ]
    assert all(
        "relative/page.html" not in link["href"] for link in result.entries[0].links
    )


@pytest.mark.parametrize("carrier", MARKUP_CARRIERS)
def test_sanitization_only_removes_dangerous_constructs(carrier, parser_flavor):
    sanitized = _parse(
        carrier,
        sanitize_html=True,
        resolve_relative_uris=True,
    )
    raw = _parse(
        carrier,
        sanitize_html=False,
        resolve_relative_uris=True,
    )

    sanitized_entry, raw_entry = sanitized.entries[0], raw.entries[0]
    sanitized_value = _content_value(sanitized_entry)
    raw_value = _content_value(raw_entry)

    # Visible reading of the entry does not change.
    assert _visible(sanitized_value).visible_text == _visible(raw_value).visible_text
    assert len(_visible(sanitized_value).anchors) == len(_visible(raw_value).anchors)

    # Only the executable parts are dropped.
    assert "<script>alert('boo')</script>" in raw_value
    assert "<script>alert('boo')</script>" not in sanitized_value
    assert 'onclick="steal()"' in raw_value
    assert 'onclick="steal()"' not in sanitized_value
    assert 'href="javascript:steal()"' in raw_value
    assert 'href="javascript:steal()"' not in sanitized_value


@pytest.mark.parametrize("carrier", CARRIERS)
@pytest.mark.parametrize("sanitize_html", [True, False], ids=["sanitize", "raw"])
@pytest.mark.parametrize(
    "resolve_relative_uris", [True, False], ids=["resolve", "keep"]
)
def test_entry_attribution_and_targets_are_stable(
    carrier, sanitize_html, resolve_relative_uris, parser_flavor
):
    result = _parse(
        carrier,
        sanitize_html=sanitize_html,
        resolve_relative_uris=resolve_relative_uris,
    )
    assert result.bozo is False, result.get("bozo_exception")
    assert [entry.id for entry in result.entries] == [
        "urn:entry:one",
        "urn:entry:two",
    ]
    assert [entry.title for entry in result.entries] == ["Entry & One", "Entry Two"]

    expected_link = CARRIERS[carrier][2]
    first, second = result.entries
    assert first.link == expected_link
    assert first.links[0]["href"] == expected_link
    assert second.title == "Entry Two"
    assert _content_value(second) == "second & plain"

    # Content cleaning of the first entry must not leak into, or reorder,
    # the second entry.
    assert first.id != second.id
    assert second.link.endswith("posts/two.html")
