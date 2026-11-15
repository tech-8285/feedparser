Fixed
-----

*   Fix encoding detection and parsing for feeds read from streams that
    return short reads, where a single read(size) may yield fewer bytes
    than requested.

    Such streams could split a byte order mark or a multi-byte character
    across reads, which corrupted the prefix-consumption bookkeeping,
    duplicated the XML declaration, detected the wrong encoding, and
    flagged well-formed feeds as bozo. Streams are now normalized so that
    read(size) fills the request, making short-read streams parse
    identically to reading the whole feed at once.
