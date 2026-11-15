Fixed
-----

*   Deliver fully parsed entries from truncated RSS and Atom feeds.
    A partially parsed entry at the end of a truncated feed is no longer
    delivered as if it were complete, and the result is flagged
    as not well-formed.
