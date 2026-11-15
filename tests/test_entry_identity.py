"""Integration tests for entry identity vs. display link in incremental fetching.

These tests pin down the contract that an incremental-fetch pipeline relies on
when it deduplicates entries: which parsed fields form a stable identity, how
that identity relates to the displayable link, and where the two are coupled
(RSS ``guid`` with ``isPermaLink`` semantics, Atom ``id``/``link`` separation,
and the legacy ``entry.guid`` alias).

Covered boundaries:

* ``isPermaLink`` present/absent/true/false, crossed with link present/absent.
* Missing and duplicate GUIDs (duplicate IDs must map to the same dedup key;
  the parser itself must not drop or merge the entries).
* Atom ``id`` as identity, ``link rel="alternate"`` as display link, and the
  legacy ``entry.guid`` alias for ``entry.id``.
* Invalid (non-resolvable/dangerous-scheme) links must not silently rewrite
  the entry identity.
* Reordering items in the feed must not change the dedup keys available
  downstream.
* Entries missing an identity, a link, or both.
"""

from __future__ import annotations

import pytest

import feedparser

RSS_TEMPLATE = (
    '<?xml version="1.0"?>'
    '<rss version="2.0" xml:base="http://example.org/feeds/rss.xml">'
    "<channel>"
    "<title>Test feed</title>"
    "<link>http://example.org/</link>"
    "<description>Test feed</description>"
    "{items}"
    "</channel>"
    "</rss>"
)

ATOM_TEMPLATE = (
    '<?xml version="1.0"?>'
    '<feed xmlns="http://www.w3.org/2005/Atom" '
    'xml:base="http://example.org/feeds/atom.xml">'
    "<title>Test feed</title>"
    "<id>urn:example:feed</id>"
    "{entries}"
    "</feed>"
)


def parse_rss(*items: str):
    """Parse an RSS 2.0 feed built from the given ``<item>`` fragments."""
    return feedparser.parse(RSS_TEMPLATE.format(items="".join(items)))


def parse_atom(*entries: str):
    """Parse an Atom feed built from the given ``<entry>`` fragments."""
    return feedparser.parse(ATOM_TEMPLATE.format(entries="".join(entries)))


def dedup_key(entry) -> tuple:
    """Derive the dedup key an incremental fetcher would use downstream.

    Identity (``id``) is authoritative; the display ``link`` is only a
    fallback when the entry carries no identity at all.
    """
    return (entry.get("id"), entry.get("link"))


# ---------------------------------------------------------------------------
# RSS guid: isPermaLink flag crossed with link presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item, expected_id, expected_link, expected_guidislink",
    [
        # No isPermaLink attribute (defaults to true), no <link> element:
        # the guid doubles as the display link.
        (
            "<item><title>a</title><guid>http://example.org/g/1</guid></item>",
            "http://example.org/g/1",
            "http://example.org/g/1",
            True,
        ),
        # No isPermaLink attribute, but an explicit <link> wins for display.
        (
            "<item><title>a</title>"
            "<link>http://example.org/l/1</link>"
            "<guid>http://example.org/g/1</guid></item>",
            "http://example.org/g/1",
            "http://example.org/l/1",
            False,
        ),
        # Explicit isPermaLink="true" behaves like the default.
        (
            "<item><title>a</title>"
            '<guid isPermaLink="true">http://example.org/g/1</guid></item>',
            "http://example.org/g/1",
            "http://example.org/g/1",
            True,
        ),
        (
            "<item><title>a</title>"
            "<link>http://example.org/l/1</link>"
            '<guid isPermaLink="true">http://example.org/g/1</guid></item>',
            "http://example.org/g/1",
            "http://example.org/l/1",
            False,
        ),
        # isPermaLink="false": the guid is a pure identity, never a link,
        # whether or not a <link> element is present.
        (
            "<item><title>a</title>"
            "<link>http://example.org/l/1</link>"
            '<guid isPermaLink="false">urn:uuid:1234</guid></item>',
            "urn:uuid:1234",
            "http://example.org/l/1",
            False,
        ),
        (
            "<item><title>a</title>"
            '<guid isPermaLink="false">urn:uuid:1234</guid></item>',
            "urn:uuid:1234",
            None,
            False,
        ),
    ],
    ids=[
        "default-no-link",
        "default-with-link",
        "permalink-true-no-link",
        "permalink-true-with-link",
        "permalink-false-with-link",
        "permalink-false-no-link",
    ],
)
def test_rss_guid_permalink_matrix(
    item, expected_id, expected_link, expected_guidislink
):
    result = parse_rss(item)
    assert result.bozo is False
    (entry,) = result.entries
    assert entry["id"] == expected_id
    assert entry.get("link") == expected_link
    assert entry["guidislink"] is expected_guidislink
    # The identity must never be silently replaced by the display link.
    if expected_id != expected_link:
        assert entry["id"] != entry.get("link")


def test_rss_relative_permalink_guid_is_resolved_against_base():
    """A permalink guid is a link, so base-URI resolution rewrites it.

    This is the one sanctioned case where the displayed link and the
    identity move together: both are resolved against the feed's base URI.
    """
    result = parse_rss("<item><title>a</title><guid>g/1</guid></item>")
    (entry,) = result.entries
    assert entry["id"] == "http://example.org/feeds/g/1"
    assert entry["link"] == "http://example.org/feeds/g/1"


def test_rss_non_permalink_guid_is_not_resolved():
    """An opaque (isPermaLink="false") guid must not be rewritten by
    base-URI resolution, even when it looks relative."""
    result = parse_rss(
        "<item><title>a</title>"
        '<guid isPermaLink="false">item-42</guid>'
        "<link>/l/42</link></item>"
    )
    (entry,) = result.entries
    assert entry["id"] == "item-42"
    # The display link, unlike the identity, is resolved.
    assert entry["link"] == "http://example.org/l/42"


# ---------------------------------------------------------------------------
# Missing identity / missing link boundaries
# ---------------------------------------------------------------------------


def test_rss_item_without_guid_has_no_identity():
    result = parse_rss(
        "<item><title>a</title><link>http://example.org/l/1</link></item>"
    )
    (entry,) = result.entries
    assert "id" not in entry
    assert "guidislink" not in entry
    # Downstream the dedup key must fall back to the link.
    assert dedup_key(entry) == (None, "http://example.org/l/1")


def test_rss_item_without_guid_or_link_has_no_dedup_key():
    result = parse_rss("<item><title>a</title><description>x</description></item>")
    (entry,) = result.entries
    assert "id" not in entry
    assert "link" not in entry
    # Boundary: an entry with neither identity nor link yields a null key;
    # incremental pipelines must be able to recognize this case.
    assert dedup_key(entry) == (None, None)


# ---------------------------------------------------------------------------
# Duplicate GUIDs
# ---------------------------------------------------------------------------


def test_rss_duplicate_guids_share_one_dedup_key():
    """The parser keeps both entries; dedup is downstream's job, and both
    entries must map to the same identity key."""
    result = parse_rss(
        "<item><title>first</title>"
        '<guid isPermaLink="false">urn:dup</guid></item>',
        "<item><title>second</title>"
        '<guid isPermaLink="false">urn:dup</guid></item>',
    )
    assert len(result.entries) == 2
    keys = [entry["id"] for entry in result.entries]
    assert keys == ["urn:dup", "urn:dup"]
    # Many-to-one: two entries collapse to a single dedup key.
    assert len({dedup_key(entry) for entry in result.entries}) == 1


def test_atom_duplicate_ids_share_one_dedup_key():
    result = parse_atom(
        "<entry><title>first</title><id>tag:example.org,2020:dup</id>"
        '<link href="http://example.org/p/a"/></entry>',
        "<entry><title>second</title><id>tag:example.org,2020:dup</id>"
        '<link href="http://example.org/p/b"/></entry>',
    )
    assert len(result.entries) == 2
    assert [entry["id"] for entry in result.entries] == [
        "tag:example.org,2020:dup",
        "tag:example.org,2020:dup",
    ]
    # The display links differ; the identity-based dedup key does not.
    assert result.entries[0]["link"] != result.entries[1]["link"]
    assert len({entry["id"] for entry in result.entries}) == 1


# ---------------------------------------------------------------------------
# Atom identity vs. display link, and the legacy guid alias
# ---------------------------------------------------------------------------


def test_atom_id_is_identity_and_alternate_link_is_display():
    result = parse_atom(
        "<entry><title>a</title>"
        "<id>tag:example.org,2020:1</id>"
        '<link rel="alternate" href="/p/1"/></entry>'
    )
    (entry,) = result.entries
    assert entry["id"] == "tag:example.org,2020:1"
    assert entry["link"] == "http://example.org/p/1"
    # A non-URL identity must not be treated as the display link.
    assert entry["id"] != entry["link"]


def test_atom_entry_without_link_falls_back_to_id_as_link():
    """Pin the documented coupling: with no alternate link, the Atom id
    is exposed as the display link as well."""
    result = parse_atom(
        "<entry><title>a</title><id>tag:example.org,2020:3</id></entry>"
    )
    (entry,) = result.entries
    assert entry["id"] == "tag:example.org,2020:3"
    assert entry["link"] == "tag:example.org,2020:3"


def test_atom_non_alternate_link_is_not_the_display_link():
    result = parse_atom(
        "<entry><title>a</title>"
        "<id>tag:example.org,2020:4</id>"
        '<link rel="self" href="http://example.org/x"/></entry>'
    )
    (entry,) = result.entries
    assert entry["id"] == "tag:example.org,2020:4"
    # rel="self" is not a display link, so the id fallback still applies.
    assert entry["link"] == "tag:example.org,2020:4"


def test_atom_entry_without_id_has_no_identity():
    result = parse_atom(
        "<entry><title>a</title>"
        '<link rel="alternate" href="http://example.org/p/2"/></entry>'
    )
    (entry,) = result.entries
    assert "id" not in entry
    assert dedup_key(entry) == (None, "http://example.org/p/2")


@pytest.mark.parametrize("parser", [parse_rss, parse_atom])
def test_legacy_guid_alias_matches_id(parser):
    """The old-style ``entry.guid`` alias must always reflect ``entry.id``,
    so pipelines using either name compute the same dedup key."""
    if parser is parse_rss:
        result = parser(
            "<item><title>a</title>"
            '<guid isPermaLink="false">urn:aliased</guid></item>'
        )
    else:
        result = parser(
            "<entry><title>a</title><id>urn:aliased</id></entry>"
        )
    (entry,) = result.entries
    assert entry["guid"] == entry["id"] == "urn:aliased"


# ---------------------------------------------------------------------------
# Invalid links must not rewrite identity
# ---------------------------------------------------------------------------


def test_invalid_link_does_not_rewrite_identity():
    """A link with a non-resolvable/dangerous scheme must not silently
    replace or alter the entry's stable identity."""
    result = parse_rss(
        "<item><title>a</title>"
        '<guid isPermaLink="false">urn:safe</guid>'
        "<link>javascript:alert(1)</link></item>"
    )
    (entry,) = result.entries
    assert entry["id"] == "urn:safe"
    assert entry["id"] != entry.get("link")
    assert not str(entry.get("link", "")).startswith("urn:safe")


def test_invalid_link_leaves_identity_only_dedup_key():
    """With an opaque guid and a bad link, the identity half of the dedup
    key is still exactly the guid — nothing leaks from the link."""
    result = parse_rss(
        "<item><title>a</title>"
        '<guid isPermaLink="false">urn:stable</guid>'
        "<link>javascript:alert(1)</link></item>"
    )
    (entry,) = result.entries
    key = dedup_key(entry)
    assert key[0] == "urn:stable"


# ---------------------------------------------------------------------------
# Order invariance and parse stability of dedup keys
# ---------------------------------------------------------------------------

ITEM_A = "<item><title>a</title><guid isPermaLink='false'>urn:1</guid></item>"
ITEM_B = (
    "<item><title>b</title><guid isPermaLink='false'>urn:2</guid>"
    "<link>http://example.org/2</link></item>"
)
ITEM_C = "<item><title>c</title></item>"  # no identity, no link


def test_entry_order_does_not_change_dedup_keys():
    forward = parse_rss(ITEM_A, ITEM_B, ITEM_C)
    shuffled = parse_rss(ITEM_C, ITEM_A, ITEM_B)
    keys_forward = [dedup_key(entry) for entry in forward.entries]
    keys_shuffled = [dedup_key(entry) for entry in shuffled.entries]
    # The same multiset of dedup keys must be available downstream
    # regardless of the order items appear in the feed.
    assert sorted(map(repr, keys_forward)) == sorted(map(repr, keys_shuffled))
    # Keys travel with the entry, not with its position.
    assert keys_forward[0] == keys_shuffled[1] == ("urn:1", None)
    assert keys_forward[1] == keys_shuffled[2] == ("urn:2", "http://example.org/2")


def test_repeated_parsing_yields_identical_dedup_keys():
    first = parse_rss(ITEM_A, ITEM_B, ITEM_C)
    second = parse_rss(ITEM_A, ITEM_B, ITEM_C)
    assert [dedup_key(e) for e in first.entries] == [
        dedup_key(e) for e in second.entries
    ]
