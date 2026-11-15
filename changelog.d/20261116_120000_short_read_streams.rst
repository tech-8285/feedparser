Fixed
-----

*   Detect the encoding and parse feeds correctly when the byte stream returns
    short reads, where a single ``read(size)`` call yields fewer bytes than
    requested.

    Such streams could split a byte order mark or a multi-byte character
    across reads, which corrupted the prefix-consumption bookkeeping, dropped
    or duplicated bytes of the XML declaration, led to the wrong encoding
    being detected, and flagged well-formed feeds as bozo. Detected text now
    matches a full read of the identical bytes for both the strict parser and
    the loose fallback.
