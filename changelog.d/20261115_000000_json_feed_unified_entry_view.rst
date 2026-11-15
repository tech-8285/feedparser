Fixed
-----

*   Align JSON Feed entries with the unified RSS/Atom entry view.

    JSON Feed entries now expose ``content`` as a list of content detail
    dictionaries, derive ``enclosures`` from ``links`` like XML feeds, and
    populate ``authors``, ``tags``, ``title_detail``, and
    ``summary_detail``. Missing keys, explicit ``null`` values, and empty
    strings remain distinguishable instead of being merged.

*   Stop one malformed JSON Feed item or attachment from discarding the
    rest of the feed. Recoverable problems are now skipped, the valid
    entries are retained, and the feed is marked as ``bozo``.

*   Fix a ``KeyError`` when a JSON Feed item contains ``date_updated``,
    and fix crashes on ``null`` authors or author URLs.
