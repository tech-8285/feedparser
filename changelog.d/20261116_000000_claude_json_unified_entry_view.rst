Changed
-------

*   Align the JSON Feed entry view with the RSS/Atom entry view.

    *   ``entries[i].content`` is now a list of content dictionaries, as it
        is for the XML formats.
    *   JSON Feed attachments are now stored as enclosure links, so
        ``entries[i].enclosures`` works identically across formats.
    *   ``external_url`` now maps to ``entries[i].source.href``, mirroring
        the RSS ``<source>`` element.
    *   JSON Feed tags now populate ``entries[i].tags``, so
        ``entries[i].category`` works across formats.
    *   A missing field, an explicit null, and an empty string now remain
        distinguishable instead of collapsing into one result.

Fixed
-----

*   JSON Feed ``date_modified`` now populates ``entries[i].updated``.
*   A malformed JSON Feed attachment no longer discards every entry in
    the feed; the malformed attachment is skipped instead.
*   Explicit nulls in JSON Feed authors, content, dates, and attachments
    no longer abort parsing.
