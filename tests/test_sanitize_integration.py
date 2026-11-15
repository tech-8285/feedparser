"""Integration tests for content sanitization.

These tests parse complete feeds rather than calling the sanitizer directly.
They check that, no matter whether entry content arrives as plain text,
escaped HTML, inline XHTML, or a CDATA block, the *visible* body text and the
meaning of links survive parsing and sanitization:

* entity references are decoded once (never dropped, never double-escaped),
* escaped markup is treated as text, not as tags,
* code samples keep their angle brackets and ampersands,
* relative links keep their path and query string,
* sanitizing removes dangerous markup without changing the visible body,
* and entry identity (title, id, link, enclosures) is stable either way.
"""

from __future__ import annotations

import html.parser
import io
import re
from xml.sax.saxutils import escape

import pytest

import feedparser

BASE_URI = "http://example.com/feeds/main.xml"
TRANSPORTS = ("text", "html", "xhtml", "cdata")
HTML_TRANSPORTS = ("html", "xhtml", "cdata")

# Match a complete entity or character reference.
ENTITY_RE = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")


@pytest.fixture(params=["strict", "loose"])
def parser_mode(request, monkeypatch):
    """Run each test with the strict XML parser and the loose SGML parser."""
    import feedparser.api

    if request.param == "loose":
        monkeypatch.setattr(feedparser.api, "_XML_AVAILABLE", False)
    return request.param


class Renderer(html.parser.HTMLParser):
    """Render sanitized markup the way a consumer would.

    Character and entity references are resolved, so comparing against the
    expected visible text catches characters that were lost or escaped twice.
    """

    def __init__(self, *, skip_script_text=False):
        super().__init__(convert_charrefs=True)
        self._skip_script_text = skip_script_text
        self._script_depth = 0
        self.text_parts: list[str] = []
        self.elements: list[tuple[str, dict[str, str]]] = []
        self.anchors: list[dict[str, object]] = []
        self._anchor: dict[str, object] | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if tag in ("script", "style"):
            self._script_depth += 1
        if tag == "a":
            self._anchor = {"attrs": attributes, "text": ""}
            self.anchors.append(self._anchor)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._script_depth -= 1
        if tag == "a":
            self._anchor = None

    def handle_data(self, data):
        if self._skip_script_text and self._script_depth:
            return
        self.text_parts.append(data)
        if self._anchor is not None:
            self._anchor["text"] += data

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    @property
    def tags(self) -> list[str]:
        return [tag for tag, _ in self.elements]

    def element(self, tag: str, **attributes: str):
        for element_tag, element_attrs in self.elements:
            if element_tag != tag:
                continue
            if all(
                element_attrs.get(key) == value for key, value in attributes.items()
            ):
                return element_attrs
        raise AssertionError(f"no <{tag} {attributes}> element in {self.elements!r}")


def render(value: str, **kwargs) -> Renderer:
    renderer = Renderer(**kwargs)
    renderer.feed(value)
    renderer.close()
    return renderer


def html_markup(transport: str) -> str:
    """HTML sent by a feed for one entry.

    Combines named and numeric entity references, escaped tags, nested inline
    tags, code text, a relative link with a query string, and dangerous
    markup that sanitization must remove.
    """
    return (
        f"<p>Entry marker: {transport}. Fish &amp; chips, &copy; 2026 &#169; "
        "and a &lt; b &amp;&amp; c.</p>"
        "<p>Literal: &lt;b&gt;not bold&lt;/b&gt; and "
        "&lt;i&gt;not italic&lt;/i&gt;.</p>"
        "<p><code>if a &lt; b &amp;&amp; c</code> then "
        "<b>real <em>bold</em></b>.</p>"
        f'<p>See the <a href="/articles/{transport}?x=1&amp;y=2" '
        'title="Tom &amp; Jerry">code <em>&amp;</em> docs</a>.</p>'
        '<p class="note" style="color: red; position: fixed;" '
        'onclick="steal()">Attributes.</p>'
        f'<script>window.alert("{transport}");</script>'
    )


def text_payload() -> str:
    """Plain text entry content, written with XML escapes."""
    return (
        "Entry marker: text. Fish &amp; chips &#169; 2026 &amp;&amp; a &lt; b; "
        "&lt;b&gt;not bold&lt;/b&gt;; "
        '&lt;script&gt;alert("text")&lt;/script&gt; stays literal.'
    )


def expected_text_payload() -> str:
    return (
        "Entry marker: text. Fish & chips © 2026 && a < b; "
        "<b>not bold</b>; "
        '<script>alert("text")</script> stays literal.'
    )


def expected_visible_text(transport: str) -> str:
    return (
        f"Entry marker: {transport}. Fish & chips, © 2026 © "
        "and a < b && c."
        "Literal: <b>not bold</b> and <i>not italic</i>."
        "if a < b && c then real bold."
        "See the code & docs."
        "Attributes."
    )


def expected_script_text(transport: str) -> str:
    return f'window.alert("{transport}");'


def atom_feed() -> str:
    entries = []
    for transport in TRANSPORTS:
        links = (
            f'<link rel="alternate" href="http://example.com/entries/{transport}"/>'
            f'<link rel="enclosure" href="http://example.com/audio/{transport}.mp3" '
            'type="audio/mpeg"/>'
        )
        if transport == "text":
            content = f'<content type="text">{text_payload()}</content>'
        elif transport == "html":
            content = f'<content type="html">{escape(html_markup(transport))}</content>'
        elif transport == "xhtml":
            xhtml = html_markup(transport).replace("&copy;", "&#169;")
            content = (
                '<content type="xhtml">'
                '<div xmlns="http://www.w3.org/1999/xhtml">'
                f"{xhtml}</div></content>"
            )
        else:
            content = (
                '<content type="html"><![CDATA['
                f"{html_markup(transport)}]]></content>"
            )
        entries.append(
            f"<entry><title>{transport}</title>"
            f"<id>tag:example.com,2026:{transport}</id>"
            f"{links}{content}</entry>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<title>Content sanitization</title>"
        '<link rel="alternate" href="http://example.com/feed"/>'
        f'<link rel="self" href="{BASE_URI}"/>'
        + "".join(entries)
        + "</feed>"
    )


def rss_feed() -> str:
    items = []
    for transport in ("html", "cdata"):
        markup = html_markup(transport)
        if transport == "cdata":
            description = f"<![CDATA[{markup}]]>"
        else:
            description = escape(markup)
        items.append(
            f"<item><title>{transport}</title>"
            f'<guid isPermaLink="false">tag:example.com,2026:{transport}</guid>'
            f"<link>http://example.com/entries/{transport}</link>"
            f'<enclosure url="http://example.com/audio/{transport}.mp3" '
            'length="0" type="audio/mpeg"/>'
            f"<description>{description}</description></item>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<rss version="2.0"><channel><title>Content sanitization</title>'
        "<link>http://example.com/feed</link>"
        + "".join(items)
        + "</channel></rss>"
    )


def parse_atom(**kwargs):
    return feedparser.parse(
        io.BytesIO(atom_feed().encode("utf-8")),
        response_headers={
            "content-type": "application/atom+xml",
            "content-location": BASE_URI,
        },
        **kwargs,
    )


def parse_rss(**kwargs):
    return feedparser.parse(
        io.BytesIO(rss_feed().encode("utf-8")),
        response_headers={
            "content-type": "application/rss+xml",
            "content-location": BASE_URI,
        },
        **kwargs,
    )


def entry_named(result, title: str):
    matching = [entry for entry in result.entries if entry.title == title]
    assert len(matching) == 1, [entry.title for entry in result.entries]
    return matching[0]


def identity(entry) -> dict[str, object]:
    """Everything a consumer uses to attribute and follow an entry."""
    return {
        "title": entry.title,
        "id": entry.get("id"),
        "guid": entry.get("guid"),
        "link": entry.get("link"),
        "links": [
            (link.get("rel"), link.get("href"), link.get("type"))
            for link in entry.get("links", [])
        ],
        "content_types": [item.get("type") for item in entry.get("content", [])],
    }


def assert_not_double_escaped(value: str) -> None:
    """Every ampersand starts exactly one reference; no escaped entity text."""
    assert "&amp;amp;" not in value
    assert "&amp;lt;" not in value
    assert "&amp;gt;" not in value
    assert "&amp;quot;" not in value
    assert "&amp;copy;" not in value
    assert "&amp;#" not in value
    assert value.count("&") == len(ENTITY_RE.findall(value)), value


def test_atom_html_content_keeps_visible_text_and_link_meaning(parser_mode):
    result = parse_atom(sanitize_html=True)
    assert result.bozo is False, result.get("bozo_exception")
    assert [entry.title for entry in result.entries] == list(TRANSPORTS)

    rendered_text = {}
    for transport in HTML_TRANSPORTS:
        entry = entry_named(result, transport)
        expected_type = (
            "application/xhtml+xml" if transport == "xhtml" else "text/html"
        )
        assert entry.content[0].get("type") == expected_type

        value = entry.content[0].value
        assert_not_double_escaped(value)
        assert "<script" not in value
        assert "onclick" not in value

        # Characters that look like tags must be escaped exactly once, so a
        # consumer renders them as text instead of interpreting them.
        assert value.count("&lt;b&gt;not bold&lt;/b&gt;") == 1
        assert value.count("&lt;i&gt;not italic&lt;/i&gt;") == 1
        assert value.count("and a &lt; b &amp;&amp; c.") == 1
        assert value.count("<b>real <em>bold</em></b>") == 1
        assert value.count("<code>if a &lt; b &amp;&amp; c</code>") == 1
        # HTML transports pass entity references through as written, so the
        # consumer sees exactly the two copyright signs that were sent.
        # XHTML is XML: the parser resolves the character references before
        # sanitization, so the value holds the characters themselves.
        if transport == "xhtml":
            assert value.count("©") == 2
        else:
            assert value.count("&copy;") == 1
            assert value.count("&#169;") == 1
            assert value.count("©") == 0

        document = render(value)
        rendered_text[transport] = document.text
        assert document.text == expected_visible_text(transport)
        # The escaped spans must stay text; only the real nested tags parse.
        assert document.tags == [
            "p",
            "p",
            "p",
            "code",
            "b",
            "em",
            "p",
            "a",
            "em",
            "p",
        ]

        [anchor] = document.anchors
        assert anchor["attrs"]["href"] == (
            f"http://example.com/articles/{transport}?x=1&y=2"
        )
        assert anchor["attrs"]["title"] == "Tom & Jerry"
        assert anchor["text"] == "code & docs"

        note = document.element("p", **{"class": "note"})
        assert note["style"] == "color: red;"
        assert "onclick" not in note

    # The same body arrives through four different transports, so the
    # rendered text must be identical once the entry marker is normalized.
    normalized = {
        transport: text.replace(
            f"Entry marker: {transport}.", "Entry marker: <transport>."
        )
        for transport, text in rendered_text.items()
    }
    assert len(set(normalized.values())) == 1, normalized


def test_atom_text_content_stays_literal(parser_mode):
    sanitized = entry_named(parse_atom(sanitize_html=True), "text")
    unsanitized = entry_named(parse_atom(sanitize_html=False), "text")

    for entry in (sanitized, unsanitized):
        assert entry.content[0].get("type") == "text/plain"
        value = entry.content[0].value
        assert value == expected_text_payload()
        assert "<b>not bold</b>" in value
        assert '<script>alert("text")</script>' in value
        # Plain text is not HTML, so nothing may be escaped on the way out.
        assert "&amp;" not in value
        assert "&lt;" not in value

    assert sanitized.content[0].value == unsanitized.content[0].value


def test_atom_sanitization_removes_only_dangerous_markup(parser_mode):
    sanitized = parse_atom(sanitize_html=True)
    unsanitized = parse_atom(sanitize_html=False)

    for transport in HTML_TRANSPORTS:
        clean = entry_named(sanitized, transport)
        raw = entry_named(unsanitized, transport)
        clean_document = render(clean.content[0].value)
        raw_document = render(raw.content[0].value)

        # The visible body is unchanged; only the script text disappears.
        clean_body = render(clean.content[0].value, skip_script_text=True)
        raw_body = render(raw.content[0].value, skip_script_text=True)
        assert clean_body.text == raw_body.text == expected_visible_text(transport)

        assert "script" not in clean_document.tags
        assert "script" in raw_document.tags
        assert raw_document.text.endswith(expected_script_text(transport))
        assert expected_script_text(transport) not in clean_document.text

        clean_note = clean_document.element("p", **{"class": "note"})
        raw_note = raw_document.element("p", **{"class": "note"})
        assert clean_note["style"] == "color: red;"
        assert raw_note["style"] == "color: red; position: fixed;"
        assert "onclick" not in clean_note
        assert raw_note["onclick"] == "steal()"

        # Link meaning is the same with or without sanitization.
        [clean_anchor] = clean_document.anchors
        [raw_anchor] = raw_document.anchors
        assert clean_anchor == raw_anchor

        assert_not_double_escaped(raw.content[0].value)


def test_atom_entry_identity_and_targets_are_stable(parser_mode):
    sanitized = parse_atom(sanitize_html=True)
    unsanitized = parse_atom(sanitize_html=False)
    unresolved = parse_atom(sanitize_html=True, resolve_relative_uris=False)

    assert [identity(entry) for entry in sanitized.entries] == [
        identity(entry) for entry in unsanitized.entries
    ]
    # Turning relative-link resolution off changes only the markup inside
    # the body; the entries and their targets stay the same.
    assert [identity(entry) for entry in sanitized.entries] == [
        identity(entry) for entry in unresolved.entries
    ]

    assert sanitized.feed.get("links") == unsanitized.feed.get("links")
    feed_links = [
        (link.get("rel"), link.get("href"))
        for link in sanitized.feed.get("links", [])
    ]
    assert feed_links == [
        ("alternate", "http://example.com/feed"),
        ("self", BASE_URI),
    ]

    for entry in sanitized.entries:
        transport = entry.title
        assert entry.get("id") == f"tag:example.com,2026:{transport}"
        assert entry.get("guid") == f"tag:example.com,2026:{transport}"
        assert entry.get("link") == f"http://example.com/entries/{transport}"
        assert [
            (link.get("rel"), link.get("href"), link.get("type"))
            for link in entry.links
        ] == [
            ("alternate", f"http://example.com/entries/{transport}", "text/html"),
            (
                "enclosure",
                f"http://example.com/audio/{transport}.mp3",
                "audio/mpeg",
            ),
        ]


def test_relative_links_keep_meaning_when_resolution_is_disabled(parser_mode):
    result = parse_atom(sanitize_html=True, resolve_relative_uris=False)

    for transport in HTML_TRANSPORTS:
        value = entry_named(result, transport).content[0].value
        assert_not_double_escaped(value)
        # The query separator must stay an escaped ampersand, not become
        # "&amp;" or the classic "&y;" corruption.
        assert f'href="/articles/{transport}?x=1&amp;y=2"' in value

        [anchor] = render(value).anchors
        assert anchor["attrs"]["href"] == f"/articles/{transport}?x=1&y=2"


def test_rss_html_and_cdata_descriptions_keep_visible_text_and_links(parser_mode):
    sanitized = parse_rss(sanitize_html=True)
    unsanitized = parse_rss(sanitize_html=False)

    assert sanitized.bozo is False, sanitized.get("bozo_exception")
    assert [entry.title for entry in sanitized.entries] == ["html", "cdata"]
    assert [identity(entry) for entry in sanitized.entries] == [
        identity(entry) for entry in unsanitized.entries
    ]

    for transport in ("html", "cdata"):
        clean = entry_named(sanitized, transport)
        raw = entry_named(unsanitized, transport)

        assert clean.summary_detail.get("type") == "text/html"
        assert clean.description == clean.summary
        assert_not_double_escaped(clean.description)

        clean_document = render(clean.description)
        raw_document = render(raw.description)
        assert clean_document.text == expected_visible_text(transport)
        assert render(clean.description, skip_script_text=True).text == (
            render(raw.description, skip_script_text=True).text
        )
        assert "script" not in clean_document.tags
        assert "script" in raw_document.tags

        [anchor] = clean_document.anchors
        assert anchor["attrs"]["href"] == (
            f"http://example.com/articles/{transport}?x=1&y=2"
        )
        assert anchor["text"] == "code & docs"

        assert clean.get("id") == f"tag:example.com,2026:{transport}"
        assert clean.get("guid") == f"tag:example.com,2026:{transport}"
        assert clean.get("link") == f"http://example.com/entries/{transport}"
        assert [
            (link.get("rel"), link.get("href"), link.get("type"))
            for link in clean.links
        ] == [
            ("alternate", f"http://example.com/entries/{transport}", "text/html"),
            (
                "enclosure",
                f"http://example.com/audio/{transport}.mp3",
                "audio/mpeg",
            ),
        ]


@pytest.mark.parametrize("transport", ["html", "cdata"])
def test_bare_ampersand_is_escaped_exactly_once(parser_mode, transport):
    markup = "<p>Tom & Jerry &amp; friends</p>"
    if transport == "cdata":
        content = f'<content type="html"><![CDATA[{markup}]]></content>'
    else:
        content = f'<content type="html">{escape(markup)}</content>'
    feed = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>Entities</title>'
        "<entry><title>entry</title><id>tag:example.com,2026:entities</id>"
        f"{content}</entry></feed>"
    )

    def parse(**kwargs):
        return feedparser.parse(
            io.BytesIO(feed.encode("utf-8")),
            response_headers={
                "content-type": "application/atom+xml",
                "content-location": BASE_URI,
            },
            **kwargs,
        )

    clean = parse(sanitize_html=True).entries[0].content[0].value
    # The entity reference survives exactly once; the bare ampersand is
    # neither dropped nor turned into a double-escaped "&amp;amp;".
    assert clean == "<p>Tom & Jerry &amp; friends</p>"
    assert clean.count("&amp;") == 1
    assert "&amp;amp;" not in clean
    assert render(clean).text == "Tom & Jerry & friends"

    # Enabling or disabling sanitization must not change the visible text.
    raw = parse(sanitize_html=False).entries[0].content[0].value
    assert raw == clean
    assert render(raw).text == render(clean).text
