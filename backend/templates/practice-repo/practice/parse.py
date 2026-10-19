"""Read simple ``key=value`` settings from one line of text."""


def parse(text: str) -> dict[str, str]:
    """Turn ``"name=Ada; lang=en"`` into ``{"name": "Ada", "lang": "en"}``.

    Pairs are separated by ``;``. Spaces around keys and values are ignored,
    empty pairs are skipped, and a later key wins over an earlier one.
    A pair without ``=`` or with an empty key raises ``ValueError``.
    """
    result: dict[str, str] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"not a key=value pair: {part!r}")
        result[key] = value.strip()
    return result
