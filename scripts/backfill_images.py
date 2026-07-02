"""Backfill image_url for existing news articles — run inside Docker container."""

import asyncio
import os
import re
import sys
from urllib.parse import urljoin

import httpx
import asyncpg


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

META_PATTERNS = [
    r'<meta\s+[^>]*?(?:property=["\']og:image["\'][^>]*?content=["\']([^"\']+)["\']|content=["\']([^"\']+)["\'][^>]*?property=["\']og:image["\'])',
    r'<meta\s+[^>]*?(?:name=["\']og:image["\'][^>]*?content=["\']([^"\']+)["\']|content=["\']([^"\']+)["\'][^>]*?name=["\']og:image["\'])',
    r'<meta\s+[^>]*?(?:property=["\']twitter:image["\'][^>]*?content=["\']([^"\']+)["\']|content=["\']([^"\']+)["\'][^>]*?property=["\']twitter:image["\'])',
    r'<meta\s+[^>]*?(?:name=["\']twitter:image["\'][^>]*?content=["\']([^"\']+)["\']|content=["\']([^"\']+)["\'][^>]*?name=["\']twitter:image["\'])',
    r'<meta\s+[^>]*?(?:property=["\']og:image:secure_url["\'][^>]*?content=["\']([^"\']+)["\']|content=["\']([^"\']+)["\'][^>]*?property=["\']og:image:secure_url["\'])',
]

IMG_FALLBACK_RE = re.compile(
    r'<img[^>]*?(?:class=["\'][^"\']*(?:hero|featured|cover|main|header)[^"\']*["\'])[^>]*?src=["\']([^"\']+)["\']',
    re.IGNORECASE | re.DOTALL,
)


def extract_image_from_html(html: str, base_url: str = "") -> str | None:
    for pattern in META_PATTERNS:
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if match:
            url = match.group(1) or match.group(2)
            url = url.strip() if url else ""
            if not url:
                continue
            if url.startswith("http://") or url.startswith("https://"):
                return url
            if url.startswith("//"):
                return "https:" + url
            if base_url and url.startswith("/"):
                return urljoin(base_url, url)
            if url.startswith("data:"):
                continue

    m = IMG_FALLBACK_RE.search(html)
    if m:
        url = m.group(1).strip()
        if url.startswith("http"):
            return url
        if url.startswith("//"):
            return "https:" + url
        if base_url and url.startswith("/"):
            return urljoin(base_url, url)
    return None


async def scrape_image_url(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=HEADERS)
            if resp.status_code != 200:
                return None
            html = resp.text
    except Exception:
        return None
    return extract_image_from_html(html, base_url=url)


async def main(dsn: str, batch_limit: int = 200):
    conn = await asyncpg.connect(dsn)

    rows = await conn.fetch(
        "SELECT id, title, url, image_url FROM news_articles WHERE image_url IS NULL OR image_url = '' ORDER BY scraped_at DESC LIMIT $1",
        batch_limit,
    )

    total = len(rows)
    if total == 0:
        print("No articles need backfill.")
        await conn.close()
        return

    print(f"Found {total} articles without image_url")
    updated = 0
    failed = 0

    for idx, row in enumerate(rows, 1):
        article_id, title, url, current = row
        image = await scrape_image_url(url)
        if image and image != current:
            await conn.execute("UPDATE news_articles SET image_url = $1 WHERE id = $2", image, article_id)
            updated += 1
            print(f"[{idx}/{total}] OK: {title[:60]} → {image[:60]}")
        else:
            failed += 1
            print(f"[{idx}/{total}] FAIL: {title[:60]}")

    await conn.close()
    print(f"Done. Updated:{updated} Failed:{failed}")


if __name__ == "__main__":
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url:
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    elif len(sys.argv) > 1:
        db_url = sys.argv[1]
    else:
        print("Usage: DATABASE_URL env var or pass DSN as first arg")
        sys.exit(1)

    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    asyncio.run(main(db_url, limit))
