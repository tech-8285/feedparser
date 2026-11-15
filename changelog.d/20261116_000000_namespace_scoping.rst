Fixed
-----

*   Namespace declarations are now scoped to the element that declares them.
    Rebinding a prefix (or the default namespace) inside one element no longer
    changes how extension elements (such as MediaRSS or Dublin Core) are
    attributed in subsequent elements or entries, and locally declared unknown
    namespaces no longer affect later known fields. The strict and loose
    parsers now attribute extension content consistently.
