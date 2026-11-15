"""Integration tests for feed updates over HTTP.

These tests run a real local HTTP server (instead of a mocked transport)
so that conditional requests, permanent redirects, and temporary failures
exercise the exact code paths used against a live feed publisher.

The scenarios under test are subtle:

*   A ``304 Not Modified`` response has an empty body, so feedparser reports
    an empty ``entries`` list. That is *not* the same as the feed being
    emptied; a client must retain the entries it parsed from an earlier
    response.
*   A transient failure (HTTP 5xx or a connection error) likewise produces
    no parseable content and must never rewrite previously known entries
    into a successful, empty result.
*   After a redirect the feed metadata (``href``, ``etag``, title, entries)
    must correspond to the document actually fetched at the final URL.
"""

from __future__ import annotations

import http.server
import threading

import pytest
import requests
import responses

import feedparser
from feedparser.http import ACCEPT_HEADER

# Two distinct revisions of the same feed.  Revision 1 contains one entry,
# revision 2 replaces it with a different entry so the entry sets can be
# compared unambiguously.
ETAG_V1 = '"feed-etag-v1"'
ETAG_V2 = '"feed-etag-v2"'
LAST_MODIFIED_V1 = "Wed, 21 Oct 2015 07:28:00 GMT"
LAST_MODIFIED_V2 = "Thu, 22 Oct 2015 07:28:00 GMT"

FEED_V1 = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
    <channel>
        <title>Example feed</title>
        <item><guid isPermaLink="false">entry-one</guid></item>
    </channel>
</rss>
"""

FEED_V2 = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
    <channel>
        <title>Example feed</title>
        <item><guid isPermaLink="false">entry-two</guid></item>
    </channel>
</rss>
"""

CONTENT_TYPE = "application/rss+xml; charset=utf-8"
ENTRIES_V1 = {"entry-one"}
ENTRIES_V2 = {"entry-two"}


class _StatefulHandler(http.server.BaseHTTPRequestHandler):
    """Handles conditional, redirect, and failure endpoints.

    Every request is recorded so tests can inspect the conditional headers
    feedparser (or a requests-based client) actually sent.
    """

    request_log: list[dict[str, str]] = []
    # Once flipped, /conditional serves revision 2 instead of answering 304
    # for the revision-1 validator.  Tests flip it to simulate a publisher
    # updating the feed between polls.
    feed_revised = False
    # The /not-modified endpoint always answers 304, regardless of state.
    # The /flaky endpoint fails once and serves the feed on later requests.
    flaky_hits = 0

    def log_message(self, *args) -> None:  # silence test-server output
        pass

    def _record(self) -> None:
        type(self).request_log.append(
            {
                "method": self.command,
                "path": self.path,
                "if_none_match": self.headers.get("If-None-Match", ""),
                "if_modified_since": self.headers.get(
                    "If-Modified-Since", ""
                ),
                "accept": self.headers.get("Accept", ""),
            }
        )

    def _serve_feed(self, body: bytes, etag: str, last_modified: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE)
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_modified)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_modified(self, etag: str) -> None:
        self.send_response(304)
        self.send_header("ETag", etag)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._record()

        if self.path == "/conditional":
            has_validator = (
                self.headers.get("If-None-Match") == ETAG_V1
                or self.headers.get("If-Modified-Since")
                == LAST_MODIFIED_V1
            )
            if not has_validator:
                # Initial, unconditional fetch.
                self._serve_feed(FEED_V1, ETAG_V1, LAST_MODIFIED_V1)
            elif type(self).feed_revised:
                # The publisher changed the entity: the stale validator no
                # longer matches, so the full revised feed is returned.
                self._serve_feed(FEED_V2, ETAG_V2, LAST_MODIFIED_V2)
            else:
                # The validator still matches the current entity.
                self._not_modified(ETAG_V1)
            return

        if self.path == "/not-modified":
            # Always 304, even if no validator is presented.
            self._not_modified(ETAG_V1)
            return

        if self.path == "/redirect":
            self.send_response(301)
            self.send_header("Location", "/target")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if self.path == "/target":
            self._serve_feed(FEED_V1, ETAG_V1, LAST_MODIFIED_V1)
            return

        if self.path == "/flaky":
            # A new handler instance serves every request, so the hit counter
            # must live on (and be mutated via) the handler class.
            handler_cls = type(self)
            handler_cls.flaky_hits += 1
            if handler_cls.flaky_hits == 1:
                # First request hits a transient outage, then the endpoint
                # recovers for subsequent polls.
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._serve_feed(FEED_V1, ETAG_V1, LAST_MODIFIED_V1)
            return

        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture(scope="module")
def server():
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _StatefulHandler
    )
    # The session-wide ``responses`` mock (see tests/conftest.py) refuses
    # unregistered requests, so let requests to the live test server through.
    responses.add_passthru(
        f"http://127.0.0.1:{httpd.server_address[1]}/"
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def reset_handler_state():
    _StatefulHandler.request_log = []
    _StatefulHandler.feed_revised = False
    _StatefulHandler.flaky_hits = 0
    yield


def entry_ids(result: feedparser.FeedParserDict) -> set[str]:
    """Return the set of entry identifiers parsed from a result."""

    return {entry.id for entry in result.entries}


def unused_port_url(path: str) -> str:
    """Build a URL on a port that is guaranteed not to be listening."""

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StatefulHandler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}{path}"
    httpd.server_close()
    return url


def conditional_fetch(
    session: requests.Session,
    url: str,
    previous: feedparser.FeedParserDict | None = None,
) -> feedparser.FeedParserDict:
    """Fetch a feed the way a polling client must.

    Conditional validators from the previous response are re-sent.  A 304 or
    a transport/HTTP failure does not produce feed content, so the entries
    parsed previously are retained instead of being replaced by an empty
    list.
    """

    headers = {"Accept": ACCEPT_HEADER}
    if previous is not None:
        if previous.get("etag"):
            headers["If-None-Match"] = previous["etag"]
        if previous.get("modified"):
            headers["If-Modified-Since"] = previous["modified"]

    try:
        response = session.get(url, headers=headers, timeout=10)
    except requests.RequestException as exc:
        if previous is None:
            raise
        # A transport failure does not clear the feed: retain the state of
        # the last good response and only record the bozo metadata.
        previous["bozo"] = True
        previous["bozo_exception"] = exc
        return previous

    if response.status_code == 304:
        assert previous is not None
        # Not Modified carries no body; keep every previously parsed entry
        # and merely refresh the HTTP-level metadata.
        previous["status"] = response.status_code
        previous["href"] = response.url
        if "ETag" in response.headers:
            previous["etag"] = response.headers["ETag"]
        return previous

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        if previous is None:
            raise
        # An HTTP failure (e.g. 503) delivers no usable feed; retain the
        # state of the last good response instead of recording an empty one.
        previous["bozo"] = True
        previous["bozo_exception"] = exc
        return previous

    result = feedparser.parse(
        response.content,
        response_headers={
            key.lower(): value for key, value in response.headers.items()
        },
    )
    # A successful response clears any bozo state left by an earlier failure.
    result["bozo"] = False
    result.pop("bozo_exception", None)
    # feedparser only populates these keys when it fetches the URL itself;
    # a requests-based client has to carry them over from the response so the
    # next poll can present the correct validators and final URL.
    result["status"] = response.status_code
    result["href"] = response.url
    if "ETag" in response.headers:
        result["etag"] = response.headers["ETag"]
    if "Last-Modified" in response.headers:
        result["modified"] = response.headers["Last-Modified"]
    return result


# ---------------------------------------------------------------------------
# requests-based conditional workflow
# ---------------------------------------------------------------------------


def test_etag_conditional_request_retains_entries_on_304(server):
    base = f"http://127.0.0.1:{server.server_address[1]}"
    url = base + "/conditional"
    session = requests.Session()

    # Initial fetch: full content plus an ETag validator.
    first = conditional_fetch(session, url)
    assert first.status == 200
    assert first.etag == ETAG_V1
    assert entry_ids(first) == ENTRIES_V1

    # Second fetch echoes the ETag and gets a 304: no body, no entry loss.
    second = conditional_fetch(session, url, previous=first)
    assert second.status == 304
    assert entry_ids(second) == ENTRIES_V1

    conditional_request = _StatefulHandler.request_log[-1]
    assert conditional_request["if_none_match"] == ETAG_V1
    assert conditional_request["accept"] == ACCEPT_HEADER

    # The publisher updates the feed between polls.  The client keeps sending
    # the stale v1 validator, which no longer matches, so it receives the
    # full revised document and the entry set is actually swapped.
    _StatefulHandler.feed_revised = True
    third = conditional_fetch(session, url, previous=second)
    assert third.status == 200
    assert third.etag == ETAG_V2
    assert entry_ids(third) == ENTRIES_V2
    assert entry_ids(third) != ENTRIES_V1


def test_last_modified_conditional_request_retains_entries_on_304(server):
    base = f"http://127.0.0.1:{server.server_address[1]}"
    url = base + "/conditional"
    session = requests.Session()

    first = conditional_fetch(session, url)
    assert first.status == 200
    assert first.modified == LAST_MODIFIED_V1
    assert entry_ids(first) == ENTRIES_V1

    second = conditional_fetch(session, url, previous=first)
    assert second.status == 304
    # An empty 304 body must not be interpreted as an emptied feed.
    assert entry_ids(second) == ENTRIES_V1
    assert _StatefulHandler.request_log[-1]["if_modified_since"] == (
        LAST_MODIFIED_V1
    )


def test_permanent_redirect_tracks_final_url_and_its_metadata(server):
    base = f"http://127.0.0.1:{server.server_address[1]}"
    session = requests.Session()

    result = conditional_fetch(session, base + "/redirect")

    assert result.status == 200
    # The href must be the URL the document was ultimately fetched from.
    assert result.href == base + "/target"
    assert result.etag == ETAG_V1
    assert result.feed.title == "Example feed"
    assert entry_ids(result) == ENTRIES_V1

    assert _StatefulHandler.request_log[0]["path"] == "/redirect"
    assert _StatefulHandler.request_log[-1]["path"] == "/target"


def test_temporary_http_failure_does_not_overwrite_entries(server):
    base = f"http://127.0.0.1:{server.server_address[1]}"
    session = requests.Session()

    known = conditional_fetch(session, base + "/target")
    assert entry_ids(known) == ENTRIES_V1

    # The flaky endpoint answers 503 on its first request.
    failed = conditional_fetch(session, base + "/flaky", previous=known)
    assert failed.bozo is True
    assert isinstance(
        failed.bozo_exception, requests.exceptions.HTTPError
    )
    # The failure must not masquerade as a successful, empty feed.
    assert entry_ids(failed) == ENTRIES_V1

    # The endpoint recovers; a retry yields fresh, parsed content.
    recovered = conditional_fetch(session, base + "/flaky", previous=failed)
    assert recovered.status == 200
    assert recovered.bozo is False
    assert entry_ids(recovered) == ENTRIES_V1


def test_connection_failure_does_not_turn_into_empty_success(server):
    base = f"http://127.0.0.1:{server.server_address[1]}"
    session = requests.Session()

    known = conditional_fetch(session, base + "/target")
    assert entry_ids(known) == ENTRIES_V1

    result = conditional_fetch(
        session, unused_port_url("/never-listened"), previous=known
    )
    assert result.bozo is True
    assert isinstance(
        result.bozo_exception, requests.exceptions.ConnectionError
    )
    # Metadata from the last good response is retained, not overwritten.
    assert result.status == 200
    assert result.etag == ETAG_V1
    assert entry_ids(result) == ENTRIES_V1


# ---------------------------------------------------------------------------
# feedparser.parse(url) HTTP integration
# ---------------------------------------------------------------------------


def test_parse_url_304_is_distinct_from_emptied_feed(server):
    base = f"http://127.0.0.1:{server.server_address[1]}"

    # A genuine 200 response parses the document's entries.
    first = feedparser.parse(base + "/target")
    assert first.status == 200
    assert entry_ids(first) == ENTRIES_V1
    assert first.etag == ETAG_V1

    # The dedicated endpoint always answers 304 with an empty body.
    not_modified = feedparser.parse(base + "/not-modified")
    assert not_modified.status == 304
    assert not_modified.entries == []
    # The validator is still captured so a client can keep polling, and the
    # empty entry list is attributable to the 304 status, not a cleared feed.
    assert not_modified.etag == ETAG_V1
    assert not_modified.bozo is False

    # Re-fetching real content restores a populated, non-empty result.
    refreshed = feedparser.parse(base + "/target")
    assert refreshed.status == 200
    assert entry_ids(refreshed) == ENTRIES_V1


def test_parse_url_follows_redirect_and_matches_final_document(server):
    base = f"http://127.0.0.1:{server.server_address[1]}"

    result = feedparser.parse(base + "/redirect")

    assert result.status == 200
    assert result.href == base + "/target"
    assert result.etag == ETAG_V1
    assert result.feed.title == "Example feed"
    assert entry_ids(result) == ENTRIES_V1
    assert result.bozo is False


def test_parse_url_failures_are_not_successful_empty_feeds(server):
    base = f"http://127.0.0.1:{server.server_address[1]}"

    # An HTTP-level failure: empty entries, but flagged via the status code.
    service_failure = feedparser.parse(base + "/flaky")
    assert service_failure.status == 503
    assert service_failure.entries == []
    # A 5xx is a complete HTTP response, not a transport exception.
    assert service_failure.bozo is False

    # A transport-level failure: bozo must be set with the exception.
    connection_failure = feedparser.parse(unused_port_url("/never-listened"))
    assert connection_failure.entries == []
    assert connection_failure.bozo is True
    assert isinstance(
        connection_failure.bozo_exception,
        requests.exceptions.ConnectionError,
    )
    assert "status" not in connection_failure
