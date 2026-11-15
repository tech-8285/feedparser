"""Integration tests for entry identity versus display link semantics.

An incremental harvester deduplicates entries by a stable key.  These
tests define how that key relates to the parsed entry:

* the stable identity (``entries[i].id``) comes from an RSS ``<guid>``,
  an Atom ``<id>``, or an RSS 1.0 ``rdf:about`` attribute;
* the display link (``entries[i].link``) is a separate, best-effort
  value that may fall back to the identity when no link is supplied;
* an unusable link never becomes the identity, and the order of entries
  (or of the identity and link elements inside one entry) never changes
  the key that a downstream harvester computes.

The tests parse whole documents through the public API so that handler
ordering, URI resolution, and the legacy ``FeedParserDict`` aliases all
take part.
"""

from __future__ import annotations

import pytest

import feedparser

ATOM10_NS = "http://www.w3.org/2005/Atom"
ATOM03_NS = "http://purl.org/atom/ns#"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RSS10_NS = "http://purl.org/rss/1.0/"

BASE_URI = "http://example.com/base/"
OPAQUE_GUID = "urn:uuid:0b6f9f5e-4f6a-4f3e-9f43-6a1c2d7e8f01"
DUPLICATE_GUID = "urn:uuid:duplicated-anchor"
TAG_ID = "tag:example.com,2024:entry-1"
ENTRY_LINK = "http://example.com/entry/1"
OTHER_LINK = "http://example.com/entry/2"
RELATIVE_LINK = "entry/1"
OPAQUE_LINK = "x://bad.test/entry/1"
SCRIPT_LINK = "javascript:alert(1)"
MALFORMED_LINK = "http://bad]test/entry/1"


# Document builders
# -----------------


def rss_document(*items: str, base: str | None = None) -> str:
    base_attribute = f' xml:base="{base}"' if base else ""
    return (
        '<?xml version="1.0"?>'
        f'<rss version="2.0"{base_attribute}>'
        "<channel><title>Example feed</title>"
        f"{''.join(items)}"
        "</channel></rss>"
    )


def rss_item(*children: str, title: str = "Entry") -> str:
    return f"<item><title>{title}</title>{''.join(children)}</item>"


def rss_guid(value: str, *, is_permalink: bool | None = None) -> str:
    if is_permalink is None:
        return f"<guid>{value}</guid>"
    flag = "true" if is_permalink else "false"
    return f'<guid isPermaLink="{flag}">{value}</guid>'


def atom_document(
    *entries: str,
    base: str | None = None,
    namespace: str = ATOM10_NS,
    version: str | None = None,
) -> str:
    base_attribute = f' xml:base="{base}"' if base else ""
    version_attribute = f' version="{version}"' if version else ""
    return (
        '<?xml version="1.0"?>'
        f'<feed xmlns="{namespace}"{version_attribute}{base_attribute}>'
        "<title>Example feed</title>"
        f"{''.join(entries)}"
        "</feed>"
    )


def atom_entry(*children: str, title: str = "Entry") -> str:
    return f"<entry><title>{title}</title>{''.join(children)}</entry>"


def rdf_document(*items: str, base: str | None = None) -> str:
    base_attribute = f' xml:base="{base}"' if base else ""
    return (
        '<?xml version="1.0"?>'
        "<rdf:RDF"
        f' xmlns:rdf="{RDF_NS}"'
        f' xmlns="{RSS10_NS}"'
        f"{base_attribute}>"
        '<channel rdf:about="http://example.com/">'
        "<title>Example feed</title>"
        "<link>http://example.com/</link>"
        "</channel>"
        f"{''.join(items)}"
        "</rdf:RDF>"
    )


def rdf_item(about: str | None, *children: str, title: str = "Entry") -> str:
    about_attribute = f' rdf:about="{about}"' if about else ""
    return f"<item{about_attribute}><title>{title}</title>{''.join(children)}</item>"


# Helpers modelling a downstream incremental harvester
# ----------------------------------------------------


def parse_document(document: str) -> feedparser.FeedParserDict:
    result = feedparser.parse(document)
    assert result["bozo"] is False, result.get("bozo_exception")
    return result


def dedupe_key(entry: feedparser.FeedParserDict) -> str | None:
    """Derive the key an incremental harvester would deduplicate on.

    A non-empty identity wins.  An empty identity is equivalent to a
    missing one, and only then may the display link serve as a fallback.
    The link is never allowed to override a supplied identity.
    """

    return entry.get("id") or entry.get("link") or None


# Permalink markers crossed with link presence and link legality
# ---------------------------------------------------------------


@pytest.mark.parametrize(
    "is_permalink, link_value, expected_link",
    (
        (None, None, OPAQUE_GUID),
        (True, None, OPAQUE_GUID),
        (False, None, None),
        (None, ENTRY_LINK, ENTRY_LINK),
        (True, ENTRY_LINK, ENTRY_LINK),
        (False, ENTRY_LINK, ENTRY_LINK),
        (None, RELATIVE_LINK, RELATIVE_LINK),
        (True, RELATIVE_LINK, RELATIVE_LINK),
        (False, RELATIVE_LINK, RELATIVE_LINK),
        (None, OPAQUE_LINK, OPAQUE_LINK),
        (True, OPAQUE_LINK, OPAQUE_LINK),
        (False, OPAQUE_LINK, OPAQUE_LINK),
        (None, SCRIPT_LINK, SCRIPT_LINK),
        (True, SCRIPT_LINK, SCRIPT_LINK),
        (False, SCRIPT_LINK, SCRIPT_LINK),
        (None, MALFORMED_LINK, MALFORMED_LINK),
        (True, MALFORMED_LINK, MALFORMED_LINK),
        (False, MALFORMED_LINK, MALFORMED_LINK),
    ),
    ids=[
        "permalink-absent+link-absent",
        "permalink-true+link-absent",
        "permalink-false+link-absent",
        "permalink-absent+link-absolute",
        "permalink-true+link-absolute",
        "permalink-false+link-absolute",
        "permalink-absent+link-relative",
        "permalink-true+link-relative",
        "permalink-false+link-relative",
        "permalink-absent+link-opaque-scheme",
        "permalink-true+link-opaque-scheme",
        "permalink-false+link-opaque-scheme",
        "permalink-absent+link-script",
        "permalink-true+link-script",
        "permalink-false+link-script",
        "permalink-absent+link-malformed",
        "permalink-true+link-malformed",
        "permalink-false+link-malformed",
    ],
)
def test_permalink_guid_identity_is_never_taken_from_the_link(
    is_permalink: bool | None,
    link_value: str | None,
    expected_link: str | None,
) -> None:
    children = [rss_guid(OPAQUE_GUID, is_permalink=is_permalink)]
    if link_value is not None:
        # Put the link first so ``guidislink`` reflects the documented
        # "guid acts as link only if no link element is present" rule.
        children.insert(0, f"<link>{link_value}</link>")

    entry = parse_document(rss_document(rss_item(*children)))["entries"][0]

    # The stable identity is always the guid, even when the guid is
    # marked as a permalink and a separate link is available.
    assert entry["id"] == OPAQUE_GUID
    assert dedupe_key(entry) == OPAQUE_GUID

    if expected_link is None:
        assert "link" not in entry
        assert "links" not in entry
    else:
        assert entry["link"] == expected_link

    if link_value is None:
        assert "links" not in entry
        assert entry["guidislink"] is (is_permalink is not False)
    else:
        assert entry["links"][0]["href"] == link_value
        assert entry["guidislink"] is False


@pytest.mark.parametrize(
    "is_permalink, link_value, expected_link",
    (
        (None, RELATIVE_LINK, BASE_URI + RELATIVE_LINK),
        (None, ENTRY_LINK, ENTRY_LINK),
        (None, OPAQUE_LINK, OPAQUE_LINK),
        (None, SCRIPT_LINK, SCRIPT_LINK),
        (None, MALFORMED_LINK, ""),
        (True, RELATIVE_LINK, BASE_URI + RELATIVE_LINK),
        (True, MALFORMED_LINK, ""),
        (False, RELATIVE_LINK, BASE_URI + RELATIVE_LINK),
        (False, OPAQUE_LINK, OPAQUE_LINK),
        (False, SCRIPT_LINK, SCRIPT_LINK),
        (False, MALFORMED_LINK, ""),
    ),
    ids=[
        "permalink-absent+relative",
        "permalink-absent+absolute",
        "permalink-absent+opaque-scheme",
        "permalink-absent+script",
        "permalink-absent+malformed",
        "permalink-true+relative",
        "permalink-true+malformed",
        "permalink-false+relative",
        "permalink-false+opaque-scheme",
        "permalink-false+script",
        "permalink-false+malformed",
    ],
)
def test_link_resolution_never_rewrites_the_stable_identity(
    is_permalink: bool | None,
    link_value: str,
    expected_link: str,
) -> None:
    document = rss_document(
        rss_item(
            f"<link>{link_value}</link>",
            rss_guid(OPAQUE_GUID, is_permalink=is_permalink),
        ),
        base=BASE_URI,
    )
    entry = parse_document(document)["entries"][0]

    assert entry["id"] == OPAQUE_GUID
    assert dedupe_key(entry) == OPAQUE_GUID
    assert entry["link"] == expected_link

    if expected_link:
        assert entry["links"][0]["href"] == expected_link
    else:
        # URI resolution rejected the link; it leaves no href behind and
        # must not fall back to the identity.
        assert "href" not in entry["links"][0]


# Relative identities: the permalink marker decides canonicalization
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "document, expected_id, expected_link",
    (
        (
            rss_document(
                rss_item(rss_guid(RELATIVE_LINK, is_permalink=True)),
                base=BASE_URI,
            ),
            BASE_URI + RELATIVE_LINK,
            BASE_URI + RELATIVE_LINK,
        ),
        (
            rss_document(
                rss_item(rss_guid(RELATIVE_LINK, is_permalink=False)),
                base=BASE_URI,
            ),
            RELATIVE_LINK,
            None,
        ),
        (
            atom_document(
                atom_entry(f"<id>{RELATIVE_LINK}</id>"),
                base=BASE_URI,
            ),
            BASE_URI + RELATIVE_LINK,
            BASE_URI + RELATIVE_LINK,
        ),
        (
            atom_document(
                atom_entry(f"<id>{RELATIVE_LINK}</id>"),
                base=BASE_URI,
                namespace=ATOM03_NS,
                version="0.3",
            ),
            BASE_URI + RELATIVE_LINK,
            BASE_URI + RELATIVE_LINK,
        ),
        (
            rdf_document(rdf_item(RELATIVE_LINK), base=BASE_URI),
            # RSS 1.0 rdf:about is an opaque identifier, not a link, so it
            # is deliberately left unresolved.
            RELATIVE_LINK,
            None,
        ),
    ),
    ids=["rss20-permalink", "rss20-opaque", "atom10", "atom03", "rss10"],
)
def test_relative_identity_canonicalization(
    document: str,
    expected_id: str,
    expected_link: str | None,
) -> None:
    entry = parse_document(document)["entries"][0]

    assert entry["id"] == expected_id
    assert dedupe_key(entry) == expected_id
    if expected_link is None:
        assert "link" not in entry
    else:
        assert entry["link"] == expected_link


# Unusable links must not become, or overwrite, identities
# --------------------------------------------------------


@pytest.mark.parametrize(
    "link_value, expected_link",
    (
        (OPAQUE_LINK, OPAQUE_LINK),
        (SCRIPT_LINK, SCRIPT_LINK),
        (MALFORMED_LINK, ""),
    ),
    ids=["opaque-scheme", "script", "malformed"],
)
def test_unusable_link_does_not_replace_identity(
    link_value: str,
    expected_link: str,
) -> None:
    document = rss_document(
        rss_item(
            f"<link>{link_value}</link>",
            rss_guid(OPAQUE_GUID, is_permalink=False),
        ),
        base=BASE_URI,
    )
    entry = parse_document(document)["entries"][0]

    assert entry["id"] == OPAQUE_GUID
    assert dedupe_key(entry) == OPAQUE_GUID
    assert entry["link"] == expected_link


def test_unusable_link_without_identity_does_not_create_one() -> None:
    document = rss_document(
        rss_item(f"<link>{MALFORMED_LINK}</link>"),
        base=BASE_URI,
    )
    entry = parse_document(document)["entries"][0]

    assert "id" not in entry
    assert "guid" not in entry
    assert entry["link"] == ""
    assert dedupe_key(entry) is None


def test_unresolvable_permalink_identity_is_not_recovered_from_link() -> None:
    """A permalink guid that cannot be resolved yields no identity.

    The valid display link remains available, but it is only used as the
    documented last-resort key because the identity is empty.
    """

    document = rss_document(
        rss_item(
            f"<link>{ENTRY_LINK}</link>",
            rss_guid(MALFORMED_LINK, is_permalink=True),
        ),
        base=BASE_URI,
    )
    entry = parse_document(document)["entries"][0]

    assert entry["id"] == ""
    assert entry["link"] == ENTRY_LINK
    assert dedupe_key(entry) == ENTRY_LINK


# Boundaries: missing or empty identities and links
# -------------------------------------------------


@pytest.mark.parametrize(
    "document, expected_id, expected_link",
    (
        (rss_document(rss_item()), None, None),
        (rss_document(rss_item(f"<link>{ENTRY_LINK}</link>")), None, ENTRY_LINK),
        (
            rss_document(rss_item(rss_guid(OPAQUE_GUID, is_permalink=False))),
            OPAQUE_GUID,
            None,
        ),
        (rss_document(rss_item("<guid></guid>")), "", ""),
        (rss_document(rss_item("<guid>   </guid>")), "", ""),
        (
            rss_document(
                rss_item(
                    rss_guid(OPAQUE_GUID, is_permalink=False),
                    "<link></link>",
                )
            ),
            OPAQUE_GUID,
            "",
        ),
        (
            rss_document(rss_item("<guid></guid>", f"<link>{ENTRY_LINK}</link>")),
            "",
            ENTRY_LINK,
        ),
        (atom_document(atom_entry()), None, None),
        (atom_document(atom_entry(f"<id>{TAG_ID}</id>")), TAG_ID, TAG_ID),
        (atom_document(atom_entry("<id></id>")), "", ""),
        (
            atom_document(
                atom_entry(f'<id>{TAG_ID}</id><link rel="alternate" href=""/>')
            ),
            TAG_ID,
            "",
        ),
        (rdf_document(rdf_item(None, f"<link>{ENTRY_LINK}</link>")), None, ENTRY_LINK),
        (
            rdf_document(rdf_item("http://example.com/item/1")),
            "http://example.com/item/1",
            None,
        ),
    ),
    ids=[
        "rss20-nothing",
        "rss20-link-only",
        "rss20-opaque-guid-only",
        "rss20-empty-guid",
        "rss20-whitespace-guid",
        "rss20-empty-link",
        "rss20-empty-guid-and-link",
        "atom10-nothing",
        "atom10-id-only",
        "atom10-empty-id",
        "atom10-empty-link-href",
        "rss10-link-only",
        "rss10-about-only",
    ],
)
def test_missing_or_empty_identity_and_link(
    document: str,
    expected_id: str | None,
    expected_link: str | None,
) -> None:
    entry = parse_document(document)["entries"][0]

    assert entry.get("id") == expected_id
    assert ("id" in entry) is (expected_id is not None)
    assert ("guid" in entry) is (expected_id is not None)

    assert entry.get("link") == expected_link
    assert ("link" in entry) is (expected_link is not None)

    assert dedupe_key(entry) == (expected_id or expected_link or None)


def test_empty_identity_does_not_collapse_distinct_entries() -> None:
    document = rss_document(
        rss_item("<guid></guid>", f"<link>{ENTRY_LINK}</link>", title="First"),
        rss_item("<guid></guid>", f"<link>{OTHER_LINK}</link>", title="Second"),
    )
    entries = parse_document(document)["entries"]

    assert [entry["id"] for entry in entries] == ["", ""]
    # Empty identities are unusable, so the links carry the keys.
    assert [dedupe_key(entry) for entry in entries] == [ENTRY_LINK, OTHER_LINK]


# Duplicate identities
# --------------------


@pytest.mark.parametrize(
    "is_permalink, expected_link, expected_guid_is_link",
    ((True, DUPLICATE_GUID, True), (False, None, False)),
    ids=["permalink", "opaque"],
)
def test_duplicate_guids_without_links_collapse_to_one_key(
    is_permalink: bool,
    expected_link: str | None,
    expected_guid_is_link: bool,
) -> None:
    document = rss_document(
        rss_item(rss_guid(DUPLICATE_GUID, is_permalink=is_permalink), title="First"),
        rss_item(rss_guid(DUPLICATE_GUID, is_permalink=is_permalink), title="Second"),
    )
    entries = parse_document(document)["entries"]

    assert [entry["id"] for entry in entries] == [DUPLICATE_GUID, DUPLICATE_GUID]
    assert [dedupe_key(entry) for entry in entries] == [DUPLICATE_GUID] * 2
    assert [entry.get("guidislink") for entry in entries] == [expected_guid_is_link] * 2

    if expected_link is None:
        assert ["link" in entry for entry in entries] == [False, False]
    else:
        assert [entry["link"] for entry in entries] == [expected_link] * 2


@pytest.mark.parametrize(
    "document, identity",
    (
        (
            rss_document(
                rss_item(
                    rss_guid(OPAQUE_GUID, is_permalink=False),
                    f"<link>{ENTRY_LINK}</link>",
                    title="First",
                ),
                rss_item(
                    rss_guid(OPAQUE_GUID, is_permalink=False),
                    f"<link>{OTHER_LINK}</link>",
                    title="Second",
                ),
            ),
            OPAQUE_GUID,
        ),
        (
            atom_document(
                atom_entry(
                    f'<id>{TAG_ID}</id><link rel="alternate" href="{ENTRY_LINK}"/>',
                    title="First",
                ),
                atom_entry(
                    f'<id>{TAG_ID}</id><link rel="alternate" href="{OTHER_LINK}"/>',
                    title="Second",
                ),
            ),
            TAG_ID,
        ),
        (
            rdf_document(
                rdf_item(
                    "http://example.com/item/1",
                    f"<link>{ENTRY_LINK}</link>",
                    title="First",
                ),
                rdf_item(
                    "http://example.com/item/1",
                    f"<link>{OTHER_LINK}</link>",
                    title="Second",
                ),
            ),
            "http://example.com/item/1",
        ),
    ),
    ids=["rss20", "atom10", "rss10"],
)
def test_duplicate_identities_keep_their_own_links(
    document: str,
    identity: str,
) -> None:
    entries = parse_document(document)["entries"]

    # The harvester sees one identity with two candidate links; the
    # per-entry link is preserved rather than overwritten by the identity.
    assert [dedupe_key(entry) for entry in entries] == [identity, identity]
    assert [entry["link"] for entry in entries] == [ENTRY_LINK, OTHER_LINK]
    assert [entry["title"] for entry in entries] == ["First", "Second"]


def test_duplicate_identity_with_only_one_link() -> None:
    document = rss_document(
        rss_item(rss_guid(OPAQUE_GUID), title="First"),
        rss_item(
            rss_guid(OPAQUE_GUID),
            f"<link>{OTHER_LINK}</link>",
            title="Second",
        ),
    )
    entries = parse_document(document)["entries"]

    assert [dedupe_key(entry) for entry in entries] == [OPAQUE_GUID] * 2
    # The first entry has no link element, so its link falls back to the
    # permalink identity; the second keeps its own link.
    assert [entry["link"] for entry in entries] == [OPAQUE_GUID, OTHER_LINK]


# Ordering must not change downstream keys
# ----------------------------------------


def test_entry_order_does_not_change_dedupe_keys() -> None:
    first = rss_item(
        rss_guid(OPAQUE_GUID, is_permalink=False),
        f"<link>{ENTRY_LINK}</link>",
        title="First",
    )
    second = rss_item(f"<link>{OTHER_LINK}</link>", title="Second")
    third = rss_item(rss_guid(DUPLICATE_GUID), title="Third")

    def snapshot(document: str) -> dict[str, tuple[str | None, str | None]]:
        return {
            entry["title"]: (dedupe_key(entry), entry.get("link"))
            for entry in parse_document(document)["entries"]
        }

    forward = snapshot(rss_document(first, second, third))
    reversed_ = snapshot(rss_document(third, second, first))

    assert forward == reversed_
    assert forward == {
        "First": (OPAQUE_GUID, ENTRY_LINK),
        "Second": (OTHER_LINK, OTHER_LINK),
        "Third": (DUPLICATE_GUID, DUPLICATE_GUID),
    }


@pytest.mark.parametrize(
    "first, second, first_is_parser_link, second_is_parser_link",
    (
        (
            rss_item(rss_guid(OPAQUE_GUID), f"<link>{ENTRY_LINK}</link>"),
            rss_item(f"<link>{ENTRY_LINK}</link>", rss_guid(OPAQUE_GUID)),
            True,
            False,
        ),
        (
            atom_entry(f'<id>{TAG_ID}</id><link rel="alternate" href="{ENTRY_LINK}"/>'),
            atom_entry(f'<link rel="alternate" href="{ENTRY_LINK}"/><id>{TAG_ID}</id>'),
            True,
            False,
        ),
    ),
    ids=["rss20", "atom10"],
)
def test_sibling_order_does_not_change_identity_link_or_key(
    first: str,
    second: str,
    first_is_parser_link: bool,
    second_is_parser_link: bool,
) -> None:
    """Reordering ``id``/``guid`` and ``link`` siblings changes only a hint.

    ``guidislink`` is computed from whether a link was seen before the
    identity, so it is order dependent and must not be used as a dedupe
    key.  The identity, the display link, and the derived key are all
    order independent.
    """

    if first.startswith("<item"):
        left = parse_document(rss_document(first))["entries"][0]
        right = parse_document(rss_document(second))["entries"][0]
    else:
        left = parse_document(atom_document(first))["entries"][0]
        right = parse_document(atom_document(second))["entries"][0]

    assert left["guidislink"] is first_is_parser_link
    assert right["guidislink"] is second_is_parser_link

    assert (left["id"], left["link"], dedupe_key(left)) == (
        right["id"],
        right["link"],
        dedupe_key(right),
    )
    assert left["id"] in (OPAQUE_GUID, TAG_ID)
    assert left["link"] == ENTRY_LINK
    assert dedupe_key(left) == left["id"]


# Atom identity and legacy aliases
# --------------------------------


def test_atom_identity_is_exposed_through_the_guid_alias() -> None:
    result = parse_document(atom_document(atom_entry(f"<id>{TAG_ID}</id>")))
    entry = result["entries"][0]

    assert entry["id"] == TAG_ID
    assert entry["guid"] == TAG_ID
    assert "guid" in entry

    # No link element exists, so the identity is also the link fallback.
    assert entry["link"] == TAG_ID
    assert dedupe_key(entry) == TAG_ID


def test_legacy_aliases_report_the_same_identity() -> None:
    result = parse_document(
        rss_document(
            rss_item(
                rss_guid(OPAQUE_GUID, is_permalink=False),
                f"<link>{ENTRY_LINK}</link>",
            )
        )
    )
    entry = result["entries"][0]

    assert result["items"] is result["entries"]
    assert result["channel"] is result["feed"]

    assert entry["id"] == OPAQUE_GUID
    assert entry["guid"] == entry["id"]
    assert "guid" in entry

    # The ``url`` alias applies to link dictionaries, not to entries; the
    # aliases never reinterpret an opaque identity as a link.
    link = entry["links"][0]
    assert link["href"] == link["url"] == ENTRY_LINK
    assert entry.get("url") is None
    assert entry["link"] == ENTRY_LINK
    assert dedupe_key(entry) == OPAQUE_GUID


def test_missing_identity_has_no_guid_alias() -> None:
    result = parse_document(rss_document(rss_item(f"<link>{ENTRY_LINK}</link>")))
    entry = result["entries"][0]

    assert "id" not in entry
    assert "guid" not in entry
    assert entry["link"] == ENTRY_LINK
    with pytest.raises(KeyError):
        entry["guid"]


@pytest.mark.parametrize(
    "document, identity",
    (
        (
            rss_document(
                rss_item(
                    rss_guid(OPAQUE_GUID),
                    f'<link rel="self" href="{ENTRY_LINK}"/>',
                )
            ),
            OPAQUE_GUID,
        ),
        (
            rss_document(
                rss_item(
                    rss_guid(OPAQUE_GUID),
                    f'<link rel="alternate" type="application/json" '
                    f'href="{ENTRY_LINK}"/>',
                )
            ),
            OPAQUE_GUID,
        ),
        (
            rss_document(
                rss_item(
                    rss_guid(OPAQUE_GUID),
                    f'<link rel="enclosure" href="{ENTRY_LINK}"/>',
                )
            ),
            OPAQUE_GUID,
        ),
        (
            atom_document(
                atom_entry(f'<id>{TAG_ID}</id><link rel="self" href="{ENTRY_LINK}"/>')
            ),
            TAG_ID,
        ),
        (
            atom_document(
                atom_entry(
                    f"<id>{TAG_ID}</id>"
                    f'<link rel="alternate" type="application/json" '
                    f'href="{ENTRY_LINK}"/>'
                )
            ),
            TAG_ID,
        ),
    ),
    ids=[
        "rss20-self",
        "rss20-json-alternate",
        "rss20-enclosure",
        "atom10-self",
        "atom10-json-alternate",
    ],
)
def test_non_display_links_leave_identity_and_fallback_intact(
    document: str,
    identity: str,
) -> None:
    """Links that are not HTML alternates never become ``entries[i].link``.

    They stay in ``entries[i].links``; the display link therefore falls
    back to the identity, and the harvester's key is the identity either
    way.
    """

    entry = parse_document(document)["entries"][0]

    assert entry["id"] == identity
    assert entry["link"] == identity
    assert dedupe_key(entry) == identity
    assert [link["href"] for link in entry["links"]] == [ENTRY_LINK]
    assert entry["guidislink"] is True


# Identity and link stay independent across feed formats
# ------------------------------------------------------


@pytest.mark.parametrize(
    "document, identity",
    (
        (
            rss_document(rss_item(rss_guid(OPAQUE_GUID), f"<link>{ENTRY_LINK}</link>")),
            OPAQUE_GUID,
        ),
        (
            atom_document(
                atom_entry(
                    f'<id>{TAG_ID}</id><link rel="alternate" href="{ENTRY_LINK}"/>'
                )
            ),
            TAG_ID,
        ),
        (
            atom_document(
                atom_entry(
                    f'<id>{TAG_ID}</id><link rel="alternate" href="{ENTRY_LINK}"/>'
                ),
                namespace=ATOM03_NS,
                version="0.3",
            ),
            TAG_ID,
        ),
        (
            rdf_document(
                rdf_item(
                    "http://example.com/item/1",
                    f"<link>{ENTRY_LINK}</link>",
                )
            ),
            "http://example.com/item/1",
        ),
    ),
    ids=["rss20-guid", "atom10-id", "atom03-id", "rss10-about"],
)
def test_identity_and_display_link_are_independent(
    document: str,
    identity: str,
) -> None:
    entry = parse_document(document)["entries"][0]

    assert identity != ENTRY_LINK
    assert entry["id"] == identity
    assert entry["link"] == ENTRY_LINK
    assert dedupe_key(entry) == identity


# Incremental re-fetch behavior
# -----------------------------


def test_adding_a_link_on_a_later_fetch_keeps_the_identity_stable() -> None:
    """A feed that gains a link element must not change the dedupe key."""

    without_link = parse_document(
        rss_document(rss_item(rss_guid(OPAQUE_GUID, is_permalink=True)))
    )["entries"][0]
    with_link = parse_document(
        rss_document(
            rss_item(
                f"<link>{ENTRY_LINK}</link>",
                rss_guid(OPAQUE_GUID, is_permalink=True),
            )
        )
    )["entries"][0]

    assert without_link["id"] == with_link["id"] == OPAQUE_GUID
    assert dedupe_key(without_link) == dedupe_key(with_link) == OPAQUE_GUID

    # Only the display link changes between the two fetches.
    assert without_link["link"] == OPAQUE_GUID
    assert with_link["link"] == ENTRY_LINK
    assert without_link["guidislink"] is True
    assert with_link["guidislink"] is False


def test_equivalent_identity_encodings_produce_the_same_key() -> None:
    """Character references in an identity must normalize to one key."""

    ampersand = "http://example.com/entry?a=1&amp;b=2"
    numeric = "http://example.com/entry?a=1&#38;b=2"

    keys = {
        dedupe_key(
            parse_document(rss_document(rss_item(rss_guid(value, is_permalink=False))))[
                "entries"
            ][0]
        )
        for value in (ampersand, numeric)
    }

    assert keys == {"http://example.com/entry?a=1&b=2"}


@pytest.mark.parametrize(
    "marker",
    ("TRUE", "True", "0", "no"),
    ids=["upper-true", "title-true", "zero", "no"],
)
def test_non_lowercase_permalink_values_do_not_make_the_identity_a_link(
    marker: str,
) -> None:
    """Only the literal ``isPermaLink="true"`` promotes guid to link.

    Any other spelling leaves the identity intact and simply omits the
    link fallback, so the harvester's key is unaffected.
    """

    document = rss_document(
        rss_item(f'<guid isPermaLink="{marker}">{OPAQUE_GUID}</guid>')
    )
    entry = parse_document(document)["entries"][0]

    assert entry["id"] == OPAQUE_GUID
    assert dedupe_key(entry) == OPAQUE_GUID
    assert "link" not in entry
    assert entry["guidislink"] is False
