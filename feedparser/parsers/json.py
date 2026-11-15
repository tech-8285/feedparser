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
    )

    def __init__(self, baseuri=None, baselang=None, encoding=None):
        self.baseuri = baseuri or ""
        self.lang = baselang or None
        self.encoding = encoding or "utf-8"  # character encoding

        self.version = None
        self.feeddata = FeedParserDict()
        self.namespacesInUse = []
        self.entries = []

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
            items = []
        self.entries = [self.parse_entry(e) for e in items if isinstance(e, dict)]

    def parse_entry(self, e):
        entry = FeedParserDict()
        for src, dst in self.ITEM_FIELDS:
            if src in e:
                entry[dst] = e[src]

        # Map the item's URL to an `alternate` link, as the XML parsers do,
        # so that `entry.links` is usable regardless of the feed format.
        link = e.get("url")
        if isinstance(link, str) and link:
            entry.setdefault("links", []).append(
                FeedParserDict(
                    {
                        "rel": "alternate",
                        "type": "text/html",
                        "href": link,
                    }
                )
            )

        # `content` is always a list of content dictionaries, matching
        # the structure produced by the XML parsers.
        if "content_text" in e:
            content = self.make_content("text/plain", e["content_text"])
        elif "content_html" in e:
            value = e["content_html"]
            if isinstance(value, str):
                value = sanitize_html(value, self.encoding, "application/json")
            content = self.make_content("text/html", value)
        else:
            content = None
        if content is not None:
            entry["content"] = [content]

        if "date_published" in e:
            entry["published"] = e["date_published"]
            entry["published_parsed"] = self.parse_date(e["date_published"])
        if "date_modified" in e:
            entry["updated"] = e["date_modified"]
            entry["updated_parsed"] = self.parse_date(e["date_modified"])

        if "tags" in e:
            tags = e["tags"]
            if isinstance(tags, list):
                entry["tags"] = [FeedParserDict({"term": t}) for t in tags]
            else:
                entry["tags"] = tags

        if "author" in e:
            self.parse_author(e["author"], entry)

        # `external_url` maps to a source dictionary, mirroring how
        # the XML parsers expose an item's `<source>` element.
        if "external_url" in e:
            entry["source"] = FeedParserDict({"href": e["external_url"]})

        if "attachments" in e:
            attachments = e["attachments"]
            if isinstance(attachments, list):
                for attachment in attachments:
                    enclosure = self.parse_attachment(attachment)
                    if enclosure is not None:
                        entry.setdefault("links", []).append(enclosure)

        return entry

    def make_content(self, content_type, value):
        content = FeedParserDict()
        content["type"] = content_type
        content["language"] = self.lang
        content["base"] = self.baseuri
        content["value"] = value
        return content

    @staticmethod
    def parse_author(parent, dest):
        if parent is None:
            # An explicit null remains distinguishable from a missing key.
            dest["author"] = None
            return
        if not isinstance(parent, dict):
            return
        dest["author_detail"] = detail = FeedParserDict()
        dest["authors"] = [detail]
        if "name" in parent:
            dest["author"] = detail["name"] = parent["name"]
        if "url" in parent:
            url = parent["url"]
            if isinstance(url, str) and url.startswith("mailto:"):
                detail["email"] = url[7:]
            else:
                detail["href"] = url

    @staticmethod
    def parse_date(value):
        # Only strings are date candidates; anything else (null, numbers)
        # yields no parsed date rather than aborting the whole parse.
        if isinstance(value, str):
            return _parse_date(value)
        return None

    @staticmethod
    def parse_attachment(attachment):
        # A malformed attachment is skipped rather than allowed
        # to discard the entry -- or the other entries in the feed.
        if not isinstance(attachment, dict):
            return None
        url = attachment.get("url")
        if not isinstance(url, str):
            return None
        enc = FeedParserDict()
        enc["rel"] = "enclosure"
        enc["href"] = url
        if "mime_type" in attachment:
            enc["type"] = attachment["mime_type"]
        if "size_in_bytes" in attachment:
            enc["length"] = attachment["size_in_bytes"]
        return enc
