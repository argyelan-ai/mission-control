"""Build-time proof that the image's browser setup does what the comments say.

Run by the Dockerfile right after the image is assembled; a failure stops the
build. Checks the EFFECT, not the Dockerfile text:

1. The browser service.py starts (media.CHROMIUM_LAUNCH_KWARGS) shows the
   eMudhra `CADistrustedCertificates` policy on chrome://policy with status
   OK and exactly the certificates of the policy file — i.e. this browser
   build, on this architecture, reads the directory the policy was copied to
   and accepts the value.
2. A launch WITHOUT the channel (Playwright's default = chrome-headless-shell,
   which reads no managed policy) fails because that browser is not in the
   image. If a future base image renames the headless-shell folder so the
   `rm` misses it, this launch succeeds and the build stops.

No network access needed.
"""
from __future__ import annotations

import asyncio
import json
import sys

from playwright.async_api import async_playwright

from media import CHROMIUM_LAUNCH_KWARGS

POLICY = "CADistrustedCertificates"
POLICY_FILE = "/etc/opt/chrome_for_testing/policies/managed/emudhra-distrust.json"
STATUSES = ("OK", "Error", "Warning", "Ignored")
# All text nodes of chrome://policy in document order, through shadow roots.
DEEP_TEXT = """() => { const out = []; const walk = (n) => {
  if (n.shadowRoot) walk(n.shadowRoot);
  for (const c of n.childNodes) {
    if (c.nodeType === 3) { const t = c.textContent.trim(); if (t) out.push(t); }
    else walk(c);
  } }; walk(document.body); return out; }"""


async def main() -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch(**CHROMIUM_LAUNCH_KWARGS)
        try:
            page = await browser.new_page()
            await page.goto("chrome://policy")
            try:
                await page.get_by_text(POLICY, exact=True).first.wait_for(timeout=15_000)
            except Exception:
                print(f"FAIL: chrome://policy of {browser.version} does not list {POLICY}")
                return 1
            texts = await page.evaluate(DEEP_TEXT)
            # Row: name, value preview, scope/level/source, status, then the
            # expanded "Value" with the full JSON.
            i = texts.index(POLICY)
            v = texts.index("Value", i)
            status = next((t for t in texts[i + 2 : v] if t in STATUSES), None)
            with open(POLICY_FILE, encoding="utf-8") as fh:
                expected = json.load(fh)[POLICY]
            try:
                shown = json.loads(texts[v + 1])
            except ValueError:
                shown = texts[v + 1]
            if status != "OK" or shown != expected:
                print(f"FAIL: {POLICY} on chrome://policy: status {status!r}, "
                      f"value matches the policy file: {shown == expected}")
                return 1
            print(f"ok: {browser.version} applies {POLICY} ({len(expected)} certificates, status OK)")
        finally:
            await browser.close()

        try:
            shell = await p.chromium.launch(headless=True)
        except Exception as exc:  # the expected path
            if "Executable doesn't exist" not in str(exc):
                print(f"FAIL: default launch failed for another reason: {exc}")
                return 1
            print("ok: default launch (chrome-headless-shell) is not available")
        else:
            version = shell.version
            await shell.close()
            print(f"FAIL: default launch started {version} — the headless shell is still in the image")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
