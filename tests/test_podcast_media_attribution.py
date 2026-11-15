"""Integration tests for media attribution in podcast subscriptions.

A podcast entry may carry several ``<enclosure>`` elements, several Media RSS
``<media:group>`` elements (each with its own content, thumbnails and group
metadata such as title, description, credits and rating), entry-level
thumbnails, and Podlove Simple Chapters.  Collecting media from such an entry
only works if every attachment keeps the URL, media type, byte size and
chapter information that belonged to its *own* source, and if no attachment
or group is attributed to a neighbouring group or entry.

These tests build feeds from the same logical episodes and check that

* every attachment is reported with the URL, media type and size written for
  it, including attachments that omit the type or the size,
* a URL that two entries publish with different attributes is reported
  differently in each entry and is never overwritten by the other entry,
* chapters stay with the entry that declared them,
* reordering the attachments inside an entry, including writing the
  attachments before the entry title and description, and moving group-level
  metadata (title, description, credits, rating) between the groups of an
  entry, changes neither the per-entry media sets nor their chapter
  association,
* reordering the entries produces an equivalent set of collectible media,
  compared attachment by attachment rather than by counting attachments,
* the strict and the loose parser agree on all of the above.

Group titles and descriptions are entry metadata rather than attachments:
they are projected onto the entry's title and summary/content instead of
participating in media attribution, so they are checked separately and never
as part of the attachment model.
"""

from __future__ import annotations

import dataclasses
import datetime
import itertools
import re
import xml.sax.saxutils

import pytest

import feedparser

MEDIA_NS = "http://search.yahoo.com/mrss/"
PSC_NS = "http://podlove.org/simple-chapters"
BASE = "http://media.example.com/podcasts"


# ---------------------------------------------------------------------------
# Feed model
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Enclosure:
    url: str
    media_type: str | None = None
    length: int | None = None


@dataclasses.dataclass(frozen=True)
class Content:
    url: str
    media_type: str | None = None
    filesize: int | None = None
    duration: int | None = None


@dataclasses.dataclass(frozen=True)
class Thumbnail:
    url: str
    width: int | None = None
    height: int | None = None


@dataclasses.dataclass(frozen=True)
class Credit:
    role: str
    name: str


@dataclasses.dataclass(frozen=True)
class Group:
    contents: tuple[Content, ...] = ()
    thumbnails: tuple[Thumbnail, ...] = ()
    title: str | None = None
    description: str | None = None
    credits: tuple[Credit, ...] = ()
    rating: str | None = None


@dataclasses.dataclass(frozen=True)
class Chapter:
    start: str
    title: str | None = None
    href: str | None = None


@dataclasses.dataclass(frozen=True)
class Chapters:
    chapters: tuple[Chapter, ...]
    version: str = "1.1"


Attachment = Enclosure | Content | Thumbnail | Group | Chapters


@dataclasses.dataclass(frozen=True)
class Episode:
    guid: str
    title: str
    summary: str
    attachments: tuple[Attachment, ...]


def _attributes(**values) -> str:
    """Render XML attributes, omitting the ones the episode does not publish."""
    return "".join(
        f' {name}="{xml.sax.saxutils.escape(str(value))}"'
        for name, value in values.items()
        if value is not None
    )


def render_attachment(attachment: Attachment) -> str:
    if isinstance(attachment, Enclosure):
        attributes = _attributes(
            url=attachment.url,
            type=attachment.media_type,
            length=attachment.length,
        )
        return f"<enclosure{attributes}/>"

    if isinstance(attachment, Content):
        attributes = _attributes(
            url=attachment.url,
            type=attachment.media_type,
            fileSize=attachment.filesize,
            duration=attachment.duration,
        )
        return f"<media:content{attributes}/>"

    if isinstance(attachment, Thumbnail):
        attributes = _attributes(
            url=attachment.url,
            width=attachment.width,
            height=attachment.height,
        )
        return f"<media:thumbnail{attributes}/>"

    if isinstance(attachment, Group):
        children = []
        if attachment.title is not None:
            children.append(f"<media:title>{attachment.title}</media:title>")
        if attachment.description is not None:
            children.append(
                f'<media:description type="plain">{attachment.description}'
                f"</media:description>"
            )
        children.extend(render_attachment(item) for item in attachment.contents)
        children.extend(render_attachment(item) for item in attachment.thumbnails)
        children.extend(
            f'<media:credit role="{credit.role}">{credit.name}</media:credit>'
            for credit in attachment.credits
        )
        if attachment.rating is not None:
            children.append(f"<media:rating>{attachment.rating}</media:rating>")
        return "<media:group>" + "".join(children) + "</media:group>"

    if isinstance(attachment, Chapters):
        children = "".join(
            f'<psc:chapter start="{chapter.start}"'
            f"{_attributes(title=chapter.title, href=chapter.href)}/>"
            for chapter in attachment.chapters
        )
        return f'<psc:chapters version="{attachment.version}">{children}</psc:chapters>'

    raise TypeError(attachment)


def render_feed(episodes: tuple[Episode, ...], *, title_first: bool = True) -> str:
    """Render the episodes as one RSS feed.

    With ``title_first=False`` the attachments are written *before* the entry
    title and description, which exercises group metadata that appears earlier
    in the document than the entry-level values it could overwrite.
    """
    items = ""
    for episode in episodes:
        body = "".join(render_attachment(item) for item in episode.attachments)
        identity = f'<guid isPermaLink="false">{episode.guid}</guid>'
        metadata = ""
        if episode.title is not None:
            metadata += f"<title>{episode.title}</title>"
        if episode.summary is not None:
            metadata += f"<description>{episode.summary}</description>"
        if title_first:
            items += f"<item>{identity}{metadata}{body}</item>"
        else:
            items += f"<item>{identity}{body}{metadata}</item>"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<rss version="2.0" xmlns:media="{MEDIA_NS}" xmlns:psc="{PSC_NS}">'
        f"<channel><title>Media attribution podcast</title>{items}</channel></rss>"
    )


# ---------------------------------------------------------------------------
# Expected values, derived independently of feedparser
# ---------------------------------------------------------------------------

_CHAPTER_START = re.compile(r"^((\d{2}):)?(\d{2}):(\d{2})(\.(\d{3}))?$")


def parse_chapter_start(value: str) -> datetime.timedelta:
    """Convert a Podlove chapter start to a timedelta, per the PSC spec."""
    match = _CHAPTER_START.match(value)
    assert match is not None, value
    _, hours, minutes, seconds, _, milliseconds = match.groups()
    return datetime.timedelta(
        0,
        int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds),
        int(milliseconds or 0) * 1000,
    )


def _text(value: object) -> str | None:
    """Render a value the way an XML attribute value is reported."""
    return None if value is None else str(value)


def expected_attribution(episode: Episode) -> dict:
    """Flatten the episode's attachments the way feedparser aggregates an item.

    Feedparser flattens attachments onto the entry: enclosures become
    ``links`` with ``rel="enclosure"``, group content joins the entry's
    ``media_content``, group thumbnails join ``media_thumbnail``, and group
    credits and ratings join the entry's own.  Group titles and descriptions
    are deliberately not part of this model; they are entry metadata.
    """
    enclosures: set[tuple] = set()
    contents: set[tuple] = set()
    thumbnails: set[tuple] = set()
    credits: set[tuple] = set()
    ratings: list[str] = []
    chapter_blocks: list[Chapters] = []

    for attachment in episode.attachments:
        if isinstance(attachment, Enclosure):
            enclosures.add(
                (attachment.url, attachment.media_type, _text(attachment.length))
            )
        elif isinstance(attachment, Content):
            contents.add(
                (
                    attachment.url,
                    attachment.media_type,
                    _text(attachment.filesize),
                    _text(attachment.duration),
                )
            )
        elif isinstance(attachment, Thumbnail):
            thumbnails.add(
                (attachment.url, _text(attachment.width), _text(attachment.height))
            )
        elif isinstance(attachment, Group):
            for item in attachment.contents:
                contents.add(
                    (
                        item.url,
                        item.media_type,
                        _text(item.filesize),
                        _text(item.duration),
                    )
                )
            for item in attachment.thumbnails:
                thumbnails.add((item.url, _text(item.width), _text(item.height)))
            credits.update((credit.role, credit.name) for credit in attachment.credits)
            if attachment.rating is not None:
                ratings.append(attachment.rating)
        elif isinstance(attachment, Chapters):
            chapter_blocks.append(attachment)
        else:
            raise TypeError(attachment)

    assert len(chapter_blocks) <= 1
    assert len(ratings) <= 1

    chapters = None
    if chapter_blocks:
        chapters = tuple(
            (
                chapter.start,
                chapter.title,
                chapter.href,
                parse_chapter_start(chapter.start),
            )
            for chapter in chapter_blocks[0].chapters
        )

    return {
        "enclosures": frozenset(enclosures),
        "contents": frozenset(contents),
        "thumbnails": frozenset(thumbnails),
        "credits": frozenset(credits),
        "rating": ratings[0] if ratings else None,
        "chapters": chapters,
    }


# ---------------------------------------------------------------------------
# Observed values
# ---------------------------------------------------------------------------


def observed_attribution(entry: dict) -> dict:
    """The attachment-facing facts of a parsed entry."""
    chapter_block = entry.get("psc_chapters")
    return {
        "enclosures": frozenset(
            (link.get("href"), link.get("type"), link.get("length"))
            for link in entry["enclosures"]
        ),
        "contents": frozenset(
            (
                item.get("url"),
                item.get("type"),
                item.get("filesize"),
                item.get("duration"),
            )
            for item in entry.get("media_content", [])
        ),
        "thumbnails": frozenset(
            (item.get("url"), item.get("width"), item.get("height"))
            for item in entry.get("media_thumbnail", [])
        ),
        "credits": frozenset(
            (item.get("role"), item.get("content"))
            for item in entry.get("media_credit", [])
        ),
        "rating": (entry.get("media_rating") or {}).get("content"),
        "chapters": None
        if chapter_block is None
        else tuple(
            (
                chapter.get("start"),
                chapter.get("title"),
                chapter.get("href"),
                chapter.get("start_parsed"),
            )
            for chapter in chapter_block["chapters"]
        ),
    }


def collectible_media(entry: dict) -> frozenset[tuple[str, str | None, str | None]]:
    """The set of media payloads a subscriber can download from an entry.

    Enclosures and Media RSS content are merged into one set keyed by URL and
    media type, with the advertised payload size taken from the attribute the
    respective element uses (``length`` for enclosures, ``fileSize`` for
    ``media:content``).  Comparing these sets ignores attachment order while
    still comparing each attachment individually.
    """
    media = {
        (link.get("href"), link.get("type"), link.get("length"))
        for link in entry["enclosures"]
    }
    for item in entry.get("media_content", []):
        media.add((item.get("url"), item.get("type"), item.get("filesize")))
    return frozenset(media)


def attribution_snapshot(feed: dict) -> dict[str, dict]:
    return {entry["id"]: observed_attribution(entry) for entry in feed.entries}


def collectible_snapshot(feed: dict) -> dict[str, frozenset]:
    return {entry["id"]: collectible_media(entry) for entry in feed.entries}


# ---------------------------------------------------------------------------
# Episodes
# ---------------------------------------------------------------------------

EP1 = Episode(
    "ep-1",
    "Episode One",
    "Episode one show notes",
    (
        Enclosure(f"{BASE}/ep1/episode-1.mp3", "audio/mpeg", 1048576),
        Group(
            contents=(
                Content(f"{BASE}/ep1/episode-1-1080p.mp4", "video/mp4", 8388608, 301),
                Content(f"{BASE}/ep1/episode-1-720p.mp4", "video/mp4"),
            ),
            thumbnails=(
                Thumbnail(f"{BASE}/ep1/episode-1-1080p.jpg", 1920, 1080),
                Thumbnail(f"{BASE}/ep1/episode-1-720p.jpg"),
            ),
            title="Episode One (video)",
            description="Episode one on video",
            credits=(Credit("editor", "Editor C"),),
        ),
        Enclosure(f"{BASE}/ep1/episode-1.ogg", "audio/ogg"),
        Group(
            contents=(
                Content(f"{BASE}/ep1/bonus-clip.mp3", "audio/mpeg", 2097152),
                # Same URL as in episode two, but published without a type
                # and with a different size.
                Content(f"{BASE}/shared/notes.mp3", None, 4096),
            ),
            thumbnails=(Thumbnail(f"{BASE}/ep1/bonus-clip.jpg", 640, 640),),
            title="Episode One (bonus)",
            description="Episode one bonus material",
            credits=(Credit("producer", "Producer B"),),
            rating="clean",
        ),
        Group(
            contents=(Content(f"{BASE}/ep1/episode-1-alt.mp3", "audio/mpeg", 3145728),),
            thumbnails=(Thumbnail(f"{BASE}/ep1/episode-1-alt.jpg", 500),),
            title="Episode One (alternate audio)",
            credits=(Credit("host", "Host A"),),
        ),
        Thumbnail(f"{BASE}/ep1/episode-1.jpg"),
        Chapters(
            (
                Chapter("00:00:00", "Intro"),
                Chapter("00:01:10.500", "Main topic", f"{BASE}/ep1/topic"),
                Chapter("00:12:34", "Recap"),
            )
        ),
    ),
)

EP2 = Episode(
    "ep-2",
    "Episode Two",
    "Episode two show notes",
    (
        Enclosure(f"{BASE}/ep2/episode-2.mp3", "audio/mpeg", 2097152),
        Group(
            contents=(
                Content(f"{BASE}/ep2/episode-2.m4a", "audio/mp4", 3145728),
                Content(f"{BASE}/shared/notes.mp3", "audio/mpeg", 8192),
            ),
            thumbnails=(Thumbnail(f"{BASE}/ep2/episode-2.jpg", 300, 300),),
            title="Episode Two (audio)",
            description="Episode two in AAC",
            credits=(Credit("host", "Host H"),),
            rating="explicit",
        ),
        Content(f"{BASE}/ep2/episode-2-teaser.mp4", "video/mp4", 512),
        Chapters(
            (
                Chapter("00:00:05", "Two intro"),
                Chapter("00:04:00", "Two interview", f"{BASE}/ep2/interview"),
            )
        ),
        Thumbnail(f"{BASE}/ep2/episode-2-wide.jpg", 1280),
    ),
)

# An episode with missing types and sizes, and with its chapters declared next
# to a group rather than inside it.
EP3 = Episode(
    "ep-3",
    "Episode Three",
    "Episode three show notes",
    (
        Enclosure(f"{BASE}/ep3/episode-3.mp3"),
        Group(
            contents=(Content(f"{BASE}/ep3/episode-3-video.mp4"),),
            thumbnails=(Thumbnail(f"{BASE}/ep3/episode-3.jpg"),),
        ),
        Chapters((Chapter("00:00:09", "Three intro"),)),
        Enclosure(f"{BASE}/ep3/episode-3.opus", "audio/ogg"),
    ),
)

EPISODES = (EP1, EP2, EP3)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@pytest.fixture(params=[False, True], ids=["strict", "loose"])
def parser_flavour(request, monkeypatch):
    """Run every feed through both the strict and the loose parser."""
    import feedparser.api

    monkeypatch.setattr(feedparser.api, "_XML_AVAILABLE", not request.param)
    return request.param


def parse_episodes(
    episodes: tuple[Episode, ...],
    *,
    title_first: bool = True,
) -> dict:
    result = feedparser.parse(render_feed(episodes, title_first=title_first))
    assert result["bozo"] is False, result.get("bozo_exception")
    return result


def assert_matches_model(feed: dict, episodes: tuple[Episode, ...]) -> None:
    observed = attribution_snapshot(feed)
    expected = {episode.guid: expected_attribution(episode) for episode in episodes}
    assert observed.keys() == expected.keys()

    for guid, fields in expected.items():
        for field, value in fields.items():
            assert observed[guid][field] == value, (
                f"{guid}: {field}"
                f"\n  observed: {observed[guid][field]!r}"
                f"\n  expected: {value!r}"
            )


def with_attachments(episode: Episode, attachments) -> Episode:
    return dataclasses.replace(episode, attachments=tuple(attachments))


def group_count(episode: Episode) -> int:
    return sum(isinstance(item, Group) for item in episode.attachments)


def rotate_group_metadata(episode: Episode, shift: int) -> Episode:
    """Move title, description, credits and rating between an episode's groups."""
    groups = [
        index
        for index, attachment in enumerate(episode.attachments)
        if isinstance(attachment, Group)
    ]
    if len(groups) < 2 or shift % len(groups) == 0:
        return episode

    attachments = list(episode.attachments)
    metadata = [
        (
            attachments[index].title,
            attachments[index].description,
            attachments[index].credits,
            attachments[index].rating,
        )
        for index in groups
    ]
    shift %= len(groups)
    rotated = metadata[shift:] + metadata[:shift]
    for index, (title, description, credits, rating) in zip(groups, rotated):
        attachments[index] = dataclasses.replace(
            attachments[index],
            title=title,
            description=description,
            credits=credits,
            rating=rating,
        )
    return with_attachments(episode, attachments)


def attachment_orderings(episode: Episode) -> list[tuple]:
    """Representative attachment orders, anchored by the declared order."""
    attachments = episode.attachments
    orderings = [attachments, tuple(reversed(attachments))]
    for shift in range(1, len(attachments)):
        orderings.append(attachments[shift:] + attachments[:shift])
    orderings.append(attachments[::2] + attachments[1::2])
    orderings.append(attachments[1::2] + attachments[::2])

    unique: list[tuple] = []
    for ordering in orderings:
        if ordering not in unique:
            unique.append(ordering)
    return unique


def shifts() -> range:
    """Every non-identity rotation of the largest group set."""
    return range(1, max(group_count(episode) for episode in EPISODES))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_attachments_keep_their_own_source(parser_flavour):
    """Each attachment reports the URL, type, size and chapters it declared."""
    feed = parse_episodes(EPISODES)
    assert_matches_model(feed, EPISODES)

    assert [entry["title"] for entry in feed.entries] == [
        episode.title for episode in EPISODES
    ]
    assert [entry["summary"] for entry in feed.entries] == [
        episode.summary for episode in EPISODES
    ]


def test_group_metadata_does_not_replace_entry_metadata(parser_flavour):
    """A group title or description never takes the entry's title or summary."""
    for title_first in (True, False):
        feed = parse_episodes(EPISODES, title_first=title_first)
        assert [entry["title"] for entry in feed.entries] == [
            episode.title for episode in EPISODES
        ], f"title_first={title_first}"
        assert_matches_model(feed, EPISODES)

    # Group descriptions are entry metadata, not attachments.  When the entry
    # description is written ahead of the groups, the encoder's own notes keep
    # the summary slot and a group description is reported as content instead.
    entry = parse_episodes(EPISODES).entries[0]
    assert entry["summary"] == EP1.summary
    assert entry["content"][0]["value"] == "Episode one on video"
    assert entry["content"][0]["type"] == "text/plain"

    # When an entry has no title of its own, a group title may stand in for it,
    # but it must come from this entry's groups and must not leak forward.
    episode = dataclasses.replace(EP1, title=None, summary=None)
    feed = parse_episodes((episode, EP2, EP3))
    group_titles = {
        attachment.title
        for attachment in EP1.attachments
        if isinstance(attachment, Group) and attachment.title is not None
    }
    assert feed.entries[0]["title"] in group_titles
    assert feed.entries[1]["title"] == EP2.title
    assert feed.entries[2]["title"] == EP3.title
    assert all(
        "ep1" not in item["url"] for item in feed.entries[1].get("media_content", [])
    )


def test_attachments_are_not_interchangeable(parser_flavour):
    """Guard against passing the attribution tests with empty or shared sets."""
    feed = parse_episodes(EPISODES)
    media = collectible_snapshot(feed)

    assert len(media) == len(EPISODES)
    for guid, items in media.items():
        assert items, f"{guid} has no collectible media"
    assert len(set(media.values())) == len(EPISODES), "entries share a media set"

    # The same URL carries entry-specific attributes in each entry.
    notes = {
        guid: item
        for guid, items in media.items()
        for item in items
        if item[0].endswith("/shared/notes.mp3")
    }
    assert notes == {
        "ep-1": (f"{BASE}/shared/notes.mp3", None, "4096"),
        "ep-2": (f"{BASE}/shared/notes.mp3", "audio/mpeg", "8192"),
    }

    # Chapters belong to the episode that declared them.
    chapters = {
        guid: fields["chapters"]
        for guid, fields in attribution_snapshot(feed).items()
    }
    assert chapters["ep-1"][-1] == (
        "00:12:34",
        "Recap",
        None,
        datetime.timedelta(seconds=754),
    )
    assert [chapter[0] for chapter in chapters["ep-2"]] == ["00:00:05", "00:04:00"]
    assert chapters["ep-3"] == (
        ("00:00:09", "Three intro", None, datetime.timedelta(seconds=9)),
    )


def test_attachment_order_does_not_change_attribution(parser_flavour):
    """Reordering attachments inside an entry keeps every media relationship."""
    baseline = attribution_snapshot(parse_episodes(EPISODES))

    for episode in EPISODES:
        others = tuple(item for item in EPISODES if item is not episode)
        for ordering in attachment_orderings(episode):
            variant = with_attachments(episode, ordering)
            for title_first in (True, False):
                feed = parse_episodes((variant, *others), title_first=title_first)
                observed = attribution_snapshot(feed)[episode.guid]
                assert observed == baseline[episode.guid], (
                    f"{episode.guid}: attachment order {ordering!r}"
                    f" (title_first={title_first}) changed the result"
                )

    # Exhaust the permutation space of the smallest episode and compare each
    # collectible payload individually.
    episode = EP3
    others = tuple(item for item in EPISODES if item is not episode)
    reference = collectible_media(
        next(
            entry
            for entry in parse_episodes(EPISODES).entries
            if entry["id"] == episode.guid
        )
    )
    for ordering in itertools.permutations(episode.attachments):
        variant = with_attachments(episode, ordering)
        feed = parse_episodes((variant, *others))
        assert attribution_snapshot(feed)[episode.guid] == baseline[episode.guid]
        assert (
            collectible_media(
                next(entry for entry in feed.entries if entry["id"] == episode.guid)
            )
            == reference
        )


def test_group_metadata_rotation_does_not_change_attribution(parser_flavour):
    """Moving group metadata between groups changes no media attribution."""
    feed = parse_episodes(EPISODES)
    baseline = attribution_snapshot(feed)
    baseline_media = collectible_snapshot(feed)

    for shift in shifts():
        episodes = tuple(rotate_group_metadata(episode, shift) for episode in EPISODES)
        for title_first in (True, False):
            rotated = parse_episodes(episodes, title_first=title_first)
            assert attribution_snapshot(rotated) == baseline, (
                f"group metadata rotation {shift} (title_first={title_first}) leaked"
            )
            assert collectible_snapshot(rotated) == baseline_media, (
                f"group metadata rotation {shift} changed collectible media"
            )

    # Rotating the metadata must not drop it: group-level credits and ratings
    # still accumulate on the entry that owns the groups.
    rotated = parse_episodes(
        tuple(rotate_group_metadata(episode, 1) for episode in EPISODES)
    )
    entries = {entry["id"]: entry for entry in rotated.entries}

    def credits(entry):
        # Credits are reported in document order, which the rotation changes.
        return {
            (credit["role"], credit["content"]) for credit in entry["media_credit"]
        }

    assert credits(entries["ep-1"]) == {
        ("editor", "Editor C"),
        ("producer", "Producer B"),
        ("host", "Host A"),
    }
    assert entries["ep-1"]["media_rating"]["content"] == "clean"
    assert credits(entries["ep-2"]) == {("host", "Host H")}
    assert entries["ep-2"]["media_rating"]["content"] == "explicit"
    assert "media_credit" not in entries["ep-3"]
    assert "media_rating" not in entries["ep-3"]


def test_attachment_order_and_group_metadata_are_independent(parser_flavour):
    """Cross-adjusting order and metadata never mixes groups or entries."""
    baseline_feed = parse_episodes(EPISODES)
    baseline = attribution_snapshot(baseline_feed)
    baseline_media = collectible_snapshot(baseline_feed)

    # Three representative orders per entry keeps the cross-product small.
    orderings = {
        episode.guid: attachment_orderings(episode)[:3] for episode in EPISODES
    }
    for shift in shifts():
        for selection in itertools.product(*(orderings[e.guid] for e in EPISODES)):
            episodes = tuple(
                with_attachments(rotate_group_metadata(episode, shift), ordering)
                for episode, ordering in zip(EPISODES, selection)
            )
            for title_first in (True, False):
                feed = parse_episodes(episodes, title_first=title_first)
                assert attribution_snapshot(feed) == baseline, (
                    f"rotation {shift} / order {selection!r}"
                    f" (title_first={title_first}) leaked"
                )
                assert collectible_snapshot(feed) == baseline_media


def test_entry_order_yields_equivalent_collectible_media(parser_flavour):
    """Entry order changes presentation only, not the collectible media."""
    baseline_feed = parse_episodes(EPISODES)
    baseline = collectible_snapshot(baseline_feed)
    baseline_attribution = attribution_snapshot(baseline_feed)

    assert len(baseline) == len(EPISODES)
    for items in baseline.values():
        assert items, "a collectible media set is empty"

    for order in itertools.permutations(EPISODES):
        for title_first in (True, False):
            feed = parse_episodes(order, title_first=title_first)
            assert [entry["id"] for entry in feed.entries] == [
                episode.guid for episode in order
            ]
            assert collectible_snapshot(feed) == baseline
            assert attribution_snapshot(feed) == baseline_attribution

            # Each attachment is compared individually, not just counted.
            for episode in order:
                entry = next(
                    entry for entry in feed.entries if entry["id"] == episode.guid
                )
                reference = next(
                    entry
                    for entry in baseline_feed.entries
                    if entry["id"] == episode.guid
                )
                assert collectible_media(entry) == collectible_media(reference)
                assert (
                    observed_attribution(entry)["chapters"]
                    == observed_attribution(reference)["chapters"]
                )
