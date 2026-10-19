"""Make short, URL-safe names."""
import re


def slugify(text: str) -> str:
    """``"Hello, World!"`` → ``"hello-world"``."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
