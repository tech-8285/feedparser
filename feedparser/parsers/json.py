# The JSON feed parser
# Copyright 2017 Beat Bolli
# All rights reserved.
#
# This file is a part of feedparser.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice,
#   this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS 'AS IS'
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import json

from ..datetimes import _parse_date
from ..sanitizer import sanitize_html
from ..util import FeedParserDict


class JSONParser:
    VERSIONS = {
        "https://jsonfeed.org/version/1": "json1",
        "https://jsonfeed.org/version/1.1": "json11",
    }
    FEED_FIELDS = (
        ("title", "title"),
        ("icon", "image"),
        ("home_page_url", "link"),
        ("description", "description"),
    )
    ITEM_FIELDS = (
        ("title", "title"),
        ("id", "guid"),
        ("url", "link"),
        ("summary", "summary"),
        ("external_url", "source"),
    )

    def __init__(self, baseuri=None, baselang=None, encoding=None):
        self.baseuri = baseuri or ""
        self.lang = baselang or None
        self.encoding = encoding or "utf-8"  # character encoding

        self.version = None
        self.feeddata = FeedParserDict()
        self.namespacesInUse = []
        self.entries = []

        # Non-fatal parsing problems. The first one is retained, mirroring
        # StrictXMLParser.error(); the feed stays partially usable.
        self.bozo = 0
        self.exc = None

    def _mark_bozo(self, exc):
        if not self.bozo:
            self.exc = exc
        self.bozo = 1

    def _detail(self, value, content_type):
        """Build a content detail dict shaped like the XML parser's."""
        return FeedParserDict(
            value=value,
            type=content_type,
            language=self.lang,
            base=self.baseuri,
        )

    def feed(self, file):
        data = json.load(file)

        v = data.get("version", "")
        try:
            self.version = self.VERSIONS[v]
        except KeyError:
            raise ValueError("Unrecognized JSONFeed version '%s'" % v)

        for src, dst in self.FEED_FIELDS:
            if src in data:
                self.feeddata[dst] = data[src]
        if "author" in data:
            self.parse_author(data["author"], self.feeddata)
        # TODO: hubs; expired has no RSS equivalent

        items = data.get("items")
        if not isinstance(items, list):
            self._mark_bozo(TypeError("JSON Feed 'items' must be a list"))
            self.entries = []
            return

        # Parse every item independently so one malformed item cannot
        # discard the remaining entries.
        entries = []
        for item in items:
            try:
                entries.append(self.parse_entry(item))
            except Exception as exc:
                self._mark_bozo(exc)
        self.entries = entries

    def parse_entry(self, e):
        if not isinstance(e, dict):
            raise TypeError("JSON Feed items must be JSON objects")

        entry = FeedParserDict()
        # Presence-based copying keeps missing keys, explicit nulls and
        # empty strings distinguishable in the unified entry view.
        for src, dst in self.ITEM_FIELDS:
            if src in e:
                entry[dst] = e[src]

        if "url" in e:
            entry.setdefault("links", []).append(
                FeedParserDict(
                    rel="alternate",
                    type="text/html",
                    href=e["url"],
                )
            )

        if "title" in e:
            entry["title_detail"] = self._detail(e["title"], "text/plain")
        if "summary" in e:
            entry["summary_detail"] = self._detail(e["summary"], "text/plain")

        if "content_text" in e:
            entry["content"] = [self._parse_text_content(e["content_text"])]
        elif "content_html" in e:
            entry["content"] = [self._parse_html_content(e["content_html"])]

        if "date_published" in e:
            entry["published"] = e["date_published"]
            entry["published_parsed"] = self._parse_date_value(e["date_published"])
        if "date_updated" in e:
            entry["updated"] = e["date_updated"]
            entry["updated_parsed"] = self._parse_date_value(e["date_updated"])

        if "tags" in e:
            self.parse_tags(e["tags"], entry)

        if "author" in e:
            self.parse_author(e["author"], entry)

        if "attachments" in e:
            self.parse_attachments(e["attachments"], entry)

        return entry

    def _parse_text_content(self, value):
        if value is not None and not isinstance(value, str):
            self._mark_bozo(TypeError("'content_text' must be a string"))
        return self._detail(value, "text/plain")

    def _parse_html_content(self, value):
        if isinstance(value, str):
            value = sanitize_html(value, self.encoding, "application/json")
        elif value is not None:
            self._mark_bozo(TypeError("'content_html' must be a string"))
        return self._detail(value, "text/html")

    def _parse_date_value(self, value):
        if isinstance(value, str):
            return _parse_date(value)
        if value is not None:
            self._mark_bozo(TypeError("JSON Feed dates must be strings"))
        return None

    def parse_tags(self, tags, entry):
        if tags is None:
            # An explicit null is an explicitly empty tag collection.
            entry["tags"] = []
            return
        if not isinstance(tags, list):
            self._mark_bozo(TypeError("JSON Feed 'tags' must be a list"))
            return
        parsed = []
        for tag in tags:
            if not isinstance(tag, str):
                self._mark_bozo(TypeError("JSON Feed tags must be strings"))
                continue
            parsed.append(FeedParserDict(term=tag, scheme=None, label=None))
        entry["tags"] = parsed

    def parse_author(self, parent, dest):
        if parent is None:
            # Explicit null: keep the author keys but empty detail so the
            # value stays distinguishable from a missing author.
            detail = FeedParserDict()
            dest["author_detail"] = detail
            dest["authors"] = [detail]
            dest["author"] = None
            return
        if not isinstance(parent, dict):
            self._mark_bozo(TypeError("JSON Feed 'author' must be an object"))
            return

        detail = FeedParserDict()
        dest["author_detail"] = detail
        dest["authors"] = [detail]

        if "name" in parent:
            # May be None or an empty string; both are meaningful values.
            detail["name"] = parent["name"]
            dest["author"] = parent["name"]
        if "url" in parent:
            url = parent["url"]
            if url is None:
                detail["href"] = None
            elif isinstance(url, str):
                if url.startswith("mailto:"):
                    detail["email"] = url[7:]
                else:
                    detail["href"] = url
            else:
                self._mark_bozo(TypeError("JSON Feed author url must be a string"))

    def parse_attachments(self, attachments, entry):
        if not isinstance(attachments, list):
            self._mark_bozo(TypeError("JSON Feed 'attachments' must be a list"))
            return

        links = entry.setdefault("links", [])
        for attachment in attachments:
            if not isinstance(attachment, dict):
                self._mark_bozo(TypeError("JSON Feed attachments must be objects"))
                continue
            if "url" not in attachment or "mime_type" not in attachment:
                self._mark_bozo(
                    ValueError("JSON Feed attachments require 'url' and 'mime_type'")
                )
                continue
            url = attachment["url"]
            mime_type = attachment["mime_type"]
            if url is None or mime_type is None:
                self._mark_bozo(
                    ValueError(
                        "JSON Feed attachment 'url' and 'mime_type' must not be null"
                    )
                )
                continue

            link = FeedParserDict(
                rel="enclosure",
                href=url,
                type=mime_type,
            )
            size = attachment.get("size_in_bytes")
            if size is not None:
                # XML enclosure lengths are always strings.
                link["length"] = str(size)
            links.append(link)
