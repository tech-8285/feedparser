"""Integration tests for multi-layer xml:base resolution in Atom feeds.

Every collectable link (entry links, author links, embedded XHTML markup, and
enclosures) must resolve against the base that is in scope for that element.
The feed document URL, the feed element, the entry element, and the embedded
XHTML element may each contribute a relative xml:base. The tests below combine
those layers with an HTTP redirect, reordered entries, and missing or invalid
bases to ensure that one entry's base never leaks into the next one.
"""

from __future__ import annotations

import re

import pytest
import responses

import feedparser
import feedparser.api

DOCUMENT_URL = "https://example.com/syndication/current/feed.atom"
DOCUMENT_ROOT = "https://example.com/syndication/current/"
DOCUMENT_HEADERS = {
    "content-location": DOCUMENT_URL,
    "content-type": "application/atom+xml",
}

FEED_BASE = DOCUMENT_ROOT + "assets/"

_EMBEDDED_URL = re.compile(r'(?:href|src)="([^"]+)"')


@pytest.fixture(params=("strict", "loose"))
def parser_mode(request, monkeypatch) -> str:
    if request.param == "loose":
        monkeypatch.setattr(feedparser.api, "_XML_AVAILABLE", False)
    return request.param


def _embedded_urls(markup: str) -> frozenset[str]:
    return frozenset(_EMBEDDED_URL.findall(markup))


def _build_feed(*entries: str, feed_base: str | None = "assets/") -> str:
    base_attr = "" if feed_base is None else f' xml:base="{feed_base}"'
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<feed xmlns="http://www.w3.org/2005/Atom"{base_attr}>'
        "<id>urn:example:feed</id>"
        "<title>Multi-layer base</title>"
        f"{''.join(entries)}"
        "</feed>"
    )


def _make_entry(
    name: str,
    *,
    entry_base: str | None = None,
    content_base: str | None = None,
    enclosure_href: str | None = None,
    content_extra: str = "",
) -> str:
    entry_base_attr = "" if entry_base is None else f' xml:base="{entry_base}"'
    content_base_attr = "" if content_base is None else f' xml:base="{content_base}"'
    enclosure = f"media/{name}.mp3" if enclosure_href is None else enclosure_href
    return (
        f"<entry{entry_base_attr}>"
        f"<id>urn:entry:{name}</id>"
        f"<title>{name}</title>"
        f"<author><name>{name.title()} Author</name>"
        f"<uri>people/{name}.html</uri></author>"
        f'<link rel="enclosure" type="audio/mpeg" href="{enclosure}"/>'
        f'<content type="xhtml"{content_base_attr}>'
        '<div xmlns="http://www.w3.org/1999/xhtml">'
        f'<a href="stories/{name}.html">{name}</a>'
        f'<img src="images/{name}.png" alt="{name}"/>'
        f"{content_extra}"
        "</div>"
        "</content>"
        "</entry>"
    )


def _parse(xml: str, parser_mode: str, **kwargs) -> feedparser.FeedParserDict:
    result = feedparser.parse(xml, response_headers=DOCUMENT_HEADERS, **kwargs)
    assert result["bozo"] is False, result.get("bozo_exception")
    assert result["version"] == "atom10"
    return result


def _entries_by_id(
    result: feedparser.FeedParserDict,
) -> dict[str, feedparser.FeedParserDict]:
    return {entry["id"]: entry for entry in result["entries"]}


def _snapshot(result: feedparser.FeedParserDict) -> dict[str, tuple]:
    snapshot = {}
    for entry in result["entries"]:
        author = entry.get("author_detail", {}).get("href")
        enclosure = next(
            (
                link.get("href")
                for link in entry.get("links", [])
                if link.get("rel") == "enclosure"
            ),
            None,
        )
        content = entry.get("content", [{}])[0]
        snapshot[entry["id"]] = (
            author,
            enclosure,
            content.get("base"),
            _embedded_urls(content.get("value", "")),
        )
    return snapshot


def _assert_entry(
    entry: feedparser.FeedParserDict,
    *,
    author: str,
    enclosure: str,
    content_base: str,
    content_urls: frozenset[str],
) -> None:
    assert entry["author_detail"]["href"] == author
    assert entry["enclosures"][0]["href"] == enclosure
    content = entry["content"][0]
    assert content["base"] == content_base
    assert _embedded_urls(content["value"]) == content_urls


def test_multilayer_bases_resolve_collectable_links(parser_mode):
    xml = _build_feed(
        _make_entry("alpha", entry_base="alpha/", content_base="body/"),
        _make_entry("beta"),
        _make_entry("gamma", entry_base="gamma/"),
        _make_entry(
            "delta",
            entry_base="delta/",
            enclosure_href="https://cdn.example.net/audio/delta.mp3",
        ),
    )
    result = _parse(xml, parser_mode)
    entries = _entries_by_id(result)

    # Feed, entry, and embedded XHTML each contribute a relative layer.
    _assert_entry(
        entries["urn:entry:alpha"],
        author=FEED_BASE + "alpha/people/alpha.html",
        enclosure=FEED_BASE + "alpha/media/alpha.mp3",
        content_base=FEED_BASE + "alpha/body/",
        content_urls=frozenset(
            {
                FEED_BASE + "alpha/body/stories/alpha.html",
                FEED_BASE + "alpha/body/images/alpha.png",
            }
        ),
    )

    # No entry base and no embedded base: both fall back to the feed base.
    _assert_entry(
        entries["urn:entry:beta"],
        author=FEED_BASE + "people/beta.html",
        enclosure=FEED_BASE + "media/beta.mp3",
        content_base=FEED_BASE,
        content_urls=frozenset(
            {
                FEED_BASE + "stories/beta.html",
                FEED_BASE + "images/beta.png",
            }
        ),
    )

    # Entry layer only: the embedded content inherits it.
    _assert_entry(
        entries["urn:entry:gamma"],
        author=FEED_BASE + "gamma/people/gamma.html",
        enclosure=FEED_BASE + "gamma/media/gamma.mp3",
        content_base=FEED_BASE + "gamma/",
        content_urls=frozenset(
            {
                FEED_BASE + "gamma/stories/gamma.html",
                FEED_BASE + "gamma/images/gamma.png",
            }
        ),
    )

    # An already-absolute enclosure target is preserved verbatim.
    _assert_entry(
        entries["urn:entry:delta"],
        author=FEED_BASE + "delta/people/delta.html",
        enclosure="https://cdn.example.net/audio/delta.mp3",
        content_base=FEED_BASE + "delta/",
        content_urls=frozenset(
            {
                FEED_BASE + "delta/stories/delta.html",
                FEED_BASE + "delta/images/delta.png",
            }
        ),
    )


def test_redirect_final_url_combines_with_multilayer_bases(parser_mode):
    original_url = f"https://example.com/short/{parser_mode}/feed.atom"
    final_url = "https://example.com/deep/2024/feed.atom"
    final_root = "https://example.com/deep/2024/"
    final_feed_base = final_root + "assets/"

    responses.get(
        original_url,
        status=302,
        headers={"Location": final_url},
    )
    responses.get(
        final_url,
        body=_build_feed(
            _make_entry("alpha", entry_base="alpha/", content_base="body/"),
            _make_entry("beta"),
        ).encode("utf-8"),
        content_type="application/atom+xml",
    )

    result = feedparser.parse(original_url)
    assert result["bozo"] is False, result.get("bozo_exception")
    assert result["status"] == 200
    assert result["href"] == final_url

    entries = _entries_by_id(result)

    # Resolution starts at the redirect target, then layers feed, entry, XHTML.
    _assert_entry(
        entries["urn:entry:alpha"],
        author=final_feed_base + "alpha/people/alpha.html",
        enclosure=final_feed_base + "alpha/media/alpha.mp3",
        content_base=final_feed_base + "alpha/body/",
        content_urls=frozenset(
            {
                final_feed_base + "alpha/body/stories/alpha.html",
                final_feed_base + "alpha/body/images/alpha.png",
            }
        ),
    )
    _assert_entry(
        entries["urn:entry:beta"],
        author=final_feed_base + "people/beta.html",
        enclosure=final_feed_base + "media/beta.mp3",
        content_base=final_feed_base,
        content_urls=frozenset(
            {
                final_feed_base + "stories/beta.html",
                final_feed_base + "images/beta.png",
            }
        ),
    )

    # The pre-redirect location must not leak into any resolved target.
    for entry in result["entries"]:
        for value in (
            entry["author_detail"]["href"],
            entry["enclosures"][0]["href"],
            entry["content"][0]["base"],
            *entry["content"][0]["value"].split('"'),
        ):
            assert "short" not in value


def test_missing_and_invalid_bases_do_not_leak_across_reordered_entries(
    parser_mode,
):
    entries = [
        _make_entry("alpha", entry_base="alpha/", content_base="body/"),
        # Missing entry base and missing embedded base.
        _make_entry("beta"),
        # Unacceptable entry base and unacceptable embedded base.
        _make_entry(
            "broken",
            entry_base="javascript:alert('entry')",
            content_base="javascript:alert('content')",
        ),
        _make_entry("gamma", entry_base="gamma/"),
    ]

    forward = _parse(_build_feed(*entries), parser_mode)
    backward = _parse(_build_feed(*reversed(entries)), parser_mode)

    assert [entry["id"] for entry in forward["entries"]] == [
        "urn:entry:alpha",
        "urn:entry:beta",
        "urn:entry:broken",
        "urn:entry:gamma",
    ]
    assert [entry["id"] for entry in backward["entries"]] == [
        "urn:entry:gamma",
        "urn:entry:broken",
        "urn:entry:beta",
        "urn:entry:alpha",
    ]

    # Swapping the entries must not change any resolved target.
    assert _snapshot(forward) == _snapshot(backward)

    entries_by_id = _entries_by_id(forward)

    # A missing entry base falls back to the feed base, not to the previous
    # entry's base.
    _assert_entry(
        entries_by_id["urn:entry:beta"],
        author=FEED_BASE + "people/beta.html",
        enclosure=FEED_BASE + "media/beta.mp3",
        content_base=FEED_BASE,
        content_urls=frozenset(
            {
                FEED_BASE + "stories/beta.html",
                FEED_BASE + "images/beta.png",
            }
        ),
    )

    # An unacceptable base is ignored at both the entry and XHTML layers.
    _assert_entry(
        entries_by_id["urn:entry:broken"],
        author=FEED_BASE + "people/broken.html",
        enclosure=FEED_BASE + "media/broken.mp3",
        content_base=FEED_BASE,
        content_urls=frozenset(
            {
                FEED_BASE + "stories/broken.html",
                FEED_BASE + "images/broken.png",
            }
        ),
    )
    assert "javascript" not in entries_by_id["urn:entry:broken"]["content"][0]["value"]

    # The entry after the invalid one gets its own base again.
    _assert_entry(
        entries_by_id["urn:entry:gamma"],
        author=FEED_BASE + "gamma/people/gamma.html",
        enclosure=FEED_BASE + "gamma/media/gamma.mp3",
        content_base=FEED_BASE + "gamma/",
        content_urls=frozenset(
            {
                FEED_BASE + "gamma/stories/gamma.html",
                FEED_BASE + "gamma/images/gamma.png",
            }
        ),
    )


def test_html_sanitization_does_not_change_resolved_links(parser_mode):
    xml = _build_feed(
        _make_entry(
            "alpha",
            entry_base="alpha/",
            content_base="body/",
            content_extra='<a href="stories/alpha-extra.html">extra</a>'
            '<script>alert("boom")</script>',
        )
    )
    expected_base = FEED_BASE + "alpha/body/"
    expected_urls = frozenset(
        {
            expected_base + "stories/alpha.html",
            expected_base + "stories/alpha-extra.html",
            expected_base + "images/alpha.png",
        }
    )

    sanitized = _parse(xml, parser_mode, sanitize_html=True)
    unsanitized = _parse(xml, parser_mode, sanitize_html=False)

    sanitized_entry = _entries_by_id(sanitized)["urn:entry:alpha"]
    unsanitized_entry = _entries_by_id(unsanitized)["urn:entry:alpha"]

    for entry in (sanitized_entry, unsanitized_entry):
        assert entry["author_detail"]["href"] == FEED_BASE + "alpha/people/alpha.html"
        assert entry["enclosures"][0]["href"] == FEED_BASE + "alpha/media/alpha.mp3"
        assert entry["content"][0]["base"] == expected_base
        assert _embedded_urls(entry["content"][0]["value"]) == expected_urls

    assert "<script" not in sanitized_entry["content"][0]["value"]
    assert "<script" in unsanitized_entry["content"][0]["value"]
