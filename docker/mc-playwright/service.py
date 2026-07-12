"""Visual Verifier Service — Playwright-basierte Screenshots + Performance-Metriken.

Zentraler Service den Agents + Backend nutzen koennen. Schreibt Outputs in
/shared-deliverables/<task_id>/ damit das Backend sie als TaskDeliverable
registrieren kann und der Agent sie als Telegram-Anhang senden.

Endpoints:
  POST /snapshot       — Screenshot (desktop/mobile, optional full_page + scroll)
  POST /scroll-capture — 3 Screenshots: top/middle/bottom
  POST /metrics        — Performance-Metriken (TTFB, LCP, FCP, total bytes)
  POST /verify         — alles auf einmal (Convenience fuer Agents)
  GET  /health         — Liveness

Interaktions-Mode (2026-04-23):
  auth_token         — JWT wird als localStorage["mc_auth_token"] gesetzt vor navigate
  login              — Form-Login: fill username/password → click submit → wait for URL
  interactions       — Liste: click / fill / wait_for / scroll / evaluate vor Screenshot
  wait_for_selector  — Finale Wartezeit vor Screenshot (Modal, Toast, etc.)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import uvicorn  # noqa: F401  (for side-effects on deployment)
from fastapi import FastAPI, HTTPException
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from pydantic import BaseModel, Field
from media import (
    VIEWPORTS,
    BrandingSpec,
    ComposeRequest,
    ComposeResponse,
    RecordRequest,
    RecordResponse,
    build_branded_compose_cmd,
    build_compose_cmd,
    build_transcode_cmd,
    fill_bench_template,
    render_outro_rows_html,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mc.playwright_service")

app = FastAPI(title="mc-playwright visual verifier", version="1.1.0")

# Shared Volume — Backend + dieser Service schreiben/lesen beide hier.
SHARED_DELIVERABLES = Path(os.environ.get("SHARED_DELIVERABLES", "/shared-deliverables"))

# LocalStorage-Key den das MC-Frontend nutzt (frontend-v2/src/lib/api.ts)
MC_AUTH_STORAGE_KEY = "mc_auth_token"


def _safe_filename(name: str) -> str:
    """URL- und pfad-sichere Filenames fuer Screenshots."""
    return re.sub(r"[^a-zA-Z0-9_-]", "-", name)[:60]


def _task_dir(task_id: str) -> Path:
    d = SHARED_DELIVERABLES / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic Schemas
# ──────────────────────────────────────────────────────────────────────────────


class LoginSpec(BaseModel):
    """Form-basierter Login.

    Flow: navigate(url) → fill(user_selector, username) → fill(pass_selector, password)
          → click(submit_selector) → wait for URL oder Selector.
    """
    url: str = Field(description="Login-Page URL (z.B. http://caddy/login)")
    username: str
    password: str
    username_selector: str = Field(
        default='input[type="email"], input[name="email"], input[name="username"]',
        description="CSS-Selector fuer Username/Email-Feld",
    )
    password_selector: str = Field(
        default='input[type="password"]',
        description="CSS-Selector fuer Password-Feld",
    )
    submit_selector: str = Field(
        default='button[type="submit"]',
        description="CSS-Selector fuer Submit-Button",
    )
    wait_for_url: str | None = Field(
        default=None,
        description="Regex — wartet bis URL matcht. Alternative: wait_for_selector.",
    )
    wait_for_selector: str | None = Field(
        default=None,
        description="CSS-Selector — wartet bis sichtbar. Alternative zu wait_for_url.",
    )


class InteractionSpec(BaseModel):
    """Einzelne Browser-Interaktion vor dem Screenshot.

    Reihenfolge der Felder entscheidend: click benoetigt nur `selector`,
    fill benoetigt `selector` + `value`, wait_for benoetigt `selector`, evaluate
    benoetigt `script` (vorsichtig nutzen).
    """
    action: Literal["click", "fill", "wait_for", "scroll_to", "evaluate", "press"]
    selector: str | None = None
    value: str | None = None
    script: str | None = None  # fuer action=evaluate
    wait_after_ms: int = 300   # kurz atmen lassen nach jeder Aktion


class SnapshotRequest(BaseModel):
    url: str
    task_id: str = Field(description="Ziel-Task fuer /deliverables/<task_id>/")
    viewport: str = "desktop"
    full_page: bool = True
    wait_until: str = "networkidle"
    wait_ms: int = 500  # zusaetzlich nach wait_until
    name_suffix: str = "snapshot"

    # --- Interaktions-Mode -----------------------------------------------------
    auth_token: str | None = None
    login: LoginSpec | None = None
    interactions: list[InteractionSpec] = Field(default_factory=list)
    wait_for_selector: str | None = None


class VerifyRequest(BaseModel):
    url: str
    task_id: str
    viewports: list[str] = Field(default_factory=lambda: ["desktop", "mobile"])
    scroll: bool = False
    metrics: bool = True

    # --- Interaktions-Mode -----------------------------------------------------
    auth_token: str | None = None
    login: LoginSpec | None = None
    interactions: list[InteractionSpec] = Field(default_factory=list)
    wait_for_selector: str | None = None
    full_page: bool = True  # fuer Modal-only (False): nur Viewport schiessen


class SnapshotResponse(BaseModel):
    path: str
    viewport: str
    bytes: int


class MetricsResponse(BaseModel):
    url: str
    status_code: int
    ttfb_ms: float | None
    fcp_ms: float | None
    lcp_ms: float | None
    total_bytes: int
    load_total_ms: float


# ──────────────────────────────────────────────────────────────────────────────
# Interaction Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _build_storage_state(target_url: str, auth_token: str | None) -> dict | None:
    """Baut storage_state dict fuer new_context — setzt localStorage[key]=token
    fuer den Origin von target_url.

    Wichtig: storage_state muss BEIM Context-Create uebergeben werden — das ist
    race-frei (anders als `page.add_init_script` das bei navigation-timing
    klemmen kann wenn Client-JS beim Hydrate localStorage schon liest).
    """
    if not auth_token:
        return None
    parsed = urlparse(target_url)
    if not parsed.scheme or not parsed.netloc:
        logger.warning("cannot build storage_state — invalid target_url: %s", target_url)
        return None
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return {
        "cookies": [],
        "origins": [{
            "origin": origin,
            "localStorage": [{"name": MC_AUTH_STORAGE_KEY, "value": auth_token}],
        }],
    }


async def _new_context_with_auth(
    browser: Browser,
    viewport: dict,
    target_url: str,
    auth_token: str | None,
) -> BrowserContext:
    """Erstellt BrowserContext mit optional vorgesetztem localStorage-Token.

    Nutzt storage_state (origin-scoped, race-frei) — das Token ist BEVOR
    irgendeine Page geladen wird im richtigen Origin. Fallback: wenn
    target_url keinen validen origin hat, wird ohne storage_state erstellt.
    """
    state = _build_storage_state(target_url, auth_token)
    if state is not None:
        logger.info(
            "auth_token pre-set via storage_state (origin=%s)",
            state["origins"][0]["origin"],
        )
        return await browser.new_context(viewport=viewport, storage_state=state)
    return await browser.new_context(viewport=viewport)


async def _do_form_login(page: Page, spec: LoginSpec) -> dict:
    """Macht Form-Login + wartet bis fertig.

    Returns dict mit:
      succeeded (bool): heuristisch — Login gilt als gescheitert wenn nach
                        Submit/Wait die Page weiterhin auf der Login-URL bleibt
                        und kein expliziter wait_for_url/selector den Erfolg
                        bewiesen hat.
      final_url (str):  page.url nach Submit + Wait
      reason (str|None): nur gesetzt bei succeeded=False — fuer bessere Fehler-Meldungen
                        Backend-Layer.

    Raises HTTPException(502) nur bei harten Fehlern (Page nicht erreichbar,
    Form-Selectors nicht gefunden, Wait-Timeout). Soft-Failures (Login-Form
    bleibt nach Submit sichtbar weil Backend abgelehnt hat) werden als
    succeeded=False zurueckgegeben — der Backend-Layer entscheidet ob das ein
    422-Fehler ist oder nicht.
    """
    logger.info("form-login: %s", spec.url)
    try:
        await page.goto(spec.url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        raise HTTPException(502, f"Login-Page nicht erreichbar: {e}")

    try:
        await page.fill(spec.username_selector, spec.username, timeout=10000)
        await page.fill(spec.password_selector, spec.password, timeout=10000)
        await page.click(spec.submit_selector, timeout=10000)
    except Exception as e:
        raise HTTPException(502, f"Login-Form-Aktion fehlgeschlagen: {e}")

    explicit_wait = bool(spec.wait_for_url or spec.wait_for_selector)
    initial_url = page.url
    try:
        if spec.wait_for_url:
            await page.wait_for_url(re.compile(spec.wait_for_url), timeout=15000)
        elif spec.wait_for_selector:
            await page.wait_for_selector(spec.wait_for_selector, timeout=15000)
        else:
            # Default: warte bis URL sich aendert (= Login-Erfolg) ODER Timeout.
            # WICHTIG: networkidle alleine reicht nicht — bei SPA-Frontends
            # (Next.js etc.) ist das Network oft schon idle BEVOR der client-side
            # router.push() die URL aendert. Dann meldet networkidle "fertig"
            # waehrend die Page noch auf /login ist, der spaetere Path-Check
            # in Bug-B-Heuristik fasst das faelschlicherweise als "failed" auf.
            try:
                await page.wait_for_function(
                    "(initial) => window.location.href !== initial",
                    arg=initial_url,
                    timeout=10000,
                )
            except Exception:
                # URL hat sich nicht geaendert — ggf. echter Login-Fehler.
                # Fallback: kurz networkidle abwarten als best-effort.
                await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception as e:
        raise HTTPException(502, f"Login-Wait fehlgeschlagen: {e}")

    final_url = page.url
    # Heuristik: wenn KEIN explicit wait gesetzt war (Default-Pfad) UND die
    # Page nach Wait auf demselben Pfad wie spec.url ist, ist der Login
    # vermutlich gescheitert (Backend lehnte ab → Auth-Guard blieb auf /login,
    # Form bleibt sichtbar). Dies ist genau der 2026-04-23 Bug B Fall:
    # mc-playwright meldete "fertig" obwohl Backend 401 wirft → Screenshot
    # zeigt Login-Maske statt eingeloggter Page.
    succeeded = True
    reason = None
    if not explicit_wait:
        try:
            initial_path = urlparse(spec.url).path or "/"
            final_path = urlparse(final_url).path or "/"
            if initial_path == final_path:
                succeeded = False
                reason = (
                    f"Page blieb nach Submit auf der Login-URL ({final_path}) — "
                    "vermutlich hat das Backend Username/Password abgelehnt, "
                    "der Auth-Guard hat redirected oder die Form ist sichtbar geblieben. "
                    "Setze einen `wait_for_url` (z.B. die Ziel-Page-URL) oder `wait_for_selector` "
                    "(z.B. ein Element das nur nach Login existiert) im LoginSpec wenn dein Login "
                    "tatsaechlich erfolgreich ist und der Default-Pfad-Check zu pessimistisch ist."
                )
        except Exception as e:
            logger.warning("login success-check (path-compare) failed: %s — assuming success", e)

    logger.info(
        "form-login %s: %s (explicit_wait=%s)",
        "OK" if succeeded else "FAILED",
        final_url, explicit_wait,
    )
    return {"succeeded": succeeded, "final_url": final_url, "reason": reason}


async def _apply_interaction(page: Page, step: InteractionSpec) -> None:
    """Fuehrt eine einzelne Interaktion aus."""
    logger.info("interaction: %s selector=%s", step.action, step.selector)
    try:
        if step.action == "click":
            if not step.selector:
                raise ValueError("click: selector fehlt")
            await page.click(step.selector, timeout=10000)
        elif step.action == "fill":
            if not step.selector or step.value is None:
                raise ValueError("fill: selector + value noetig")
            await page.fill(step.selector, step.value, timeout=10000)
        elif step.action == "press":
            if not step.selector or step.value is None:
                raise ValueError("press: selector + value (key) noetig")
            await page.press(step.selector, step.value, timeout=10000)
        elif step.action == "wait_for":
            if not step.selector:
                raise ValueError("wait_for: selector fehlt")
            await page.wait_for_selector(step.selector, timeout=15000)
        elif step.action == "scroll_to":
            if not step.selector:
                raise ValueError("scroll_to: selector fehlt")
            el = await page.query_selector(step.selector)
            if el:
                await el.scroll_into_view_if_needed(timeout=5000)
        elif step.action == "evaluate":
            if not step.script:
                raise ValueError("evaluate: script fehlt")
            await page.evaluate(step.script)
        else:
            raise ValueError(f"unbekannte action: {step.action}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"interaction {step.action} ({step.selector}) fehlgeschlagen: {e}")

    if step.wait_after_ms > 0:
        await page.wait_for_timeout(step.wait_after_ms)


async def _prepare_page(
    page: Page,
    target_url: str,
    *,
    login: LoginSpec | None,
    interactions: list[InteractionSpec],
    wait_for_selector: str | None,
    wait_until: str = "networkidle",
    wait_ms: int = 500,
) -> dict | None:
    """Gemeinsamer Prepare-Flow: Login → Navigate → Interaktionen → Finale Wartezeit.

    Returns das LoginResult-Dict (oder None wenn kein Login). Caller entscheidet
    ob `succeeded=False` ein Hard-Fail oder nur ein Warn-Signal ist.

    Hinweis: `auth_token` wird NICHT hier gesetzt — er muss schon beim Context-
    Create via `storage_state` uebergeben werden (siehe _new_context_with_auth).
    Das ist race-frei gegenueber `add_init_script` wo der Client-side Auth-Guard
    von Next.js beim Hydrate redirecten kann bevor das Script localStorage setzt.
    """
    login_result: dict | None = None
    if login is not None:
        login_result = await _do_form_login(page, login)

    # Jetzt zur eigentlichen Ziel-URL
    try:
        await page.goto(target_url, wait_until=wait_until, timeout=30000)
    except Exception as e:
        raise HTTPException(502, f"Page navigation failed ({target_url}): {e}")

    if wait_ms > 0:
        await page.wait_for_timeout(wait_ms)

    for step in interactions:
        await _apply_interaction(page, step)

    if wait_for_selector:
        try:
            await page.wait_for_selector(wait_for_selector, timeout=15000)
        except Exception as e:
            raise HTTPException(502, f"wait_for_selector '{wait_for_selector}' timeout: {e}")

    return login_result


# ──────────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"ok": True, "service": "mc-playwright", "version": "1.1.0"}


# ──────────────────────────────────────────────────────────────────────────────
# PDF-Generation Endpoint (2026-04-23)
# ──────────────────────────────────────────────────────────────────────────────
#
# Zentrale Primitive fuer Markdown/HTML → PDF. Agents (besonders FreeCode +
# Sparky) brauchen kein eigenes puppeteer/chromium Setup mehr — der bestehende
# Playwright-Sidecar hat Chromium ARM-nativ und rendert mit `page.pdf()`.
#
# Background: Ohne diesen Endpoint versuchten Agents lokales puppeteer zu
# installieren, was zu Rosetta x86-vs-ARM Konflikten fuehrte ("rosetta error:
# failed to open elf") + bis zu 2h stuck-Zeit pro Task. Ironisch: der
# Playwright-Sidecar lief die ganze Zeit ARM-korrekt daneben.


class PdfRequest(BaseModel):
    """Markdown/HTML → PDF via page.pdf()."""
    task_id: str
    title: str
    # Entweder markdown ODER html mitschicken (nicht beide).
    markdown: str | None = None
    html: str | None = None
    # Optional CSS zusaetzlich zum default-Stylesheet
    custom_css: str | None = None
    # Filename-Prefix; final: <prefix>.pdf im /shared-deliverables/<task_id>/
    filename_prefix: str = "report"
    # A4/Letter/Legal, default A4
    format: Literal["A4", "Letter", "Legal"] = "A4"
    # Margins in CSS-Einheiten (z.B. "2cm", "1in")
    margin_top: str = "2cm"
    margin_right: str = "2cm"
    margin_bottom: str = "2cm"
    margin_left: str = "2cm"
    # Header/Footer (HTML-Strings, optional). Siehe Playwright page.pdf() docs.
    header_html: str | None = None
    footer_html: str | None = None
    # Print-Backgrounds (sonst keine Farben im PDF)
    print_background: bool = True


class PdfResponse(BaseModel):
    path: str
    bytes: int
    title: str
    task_id: str
    pages: int | None = None  # best-effort via byte heuristic


# Default-Stylesheet fuer Markdown→PDF. Bewusst zurueckhaltend: gut lesbar
# fuer Research-Reports, kein AI-slop purple-gradient. Geist Mono fuer Code
# passt zu MCs Design-DNA.
_DEFAULT_PDF_CSS = """
@page { size: A4; margin: 2cm; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 11pt;
  line-height: 1.55;
  color: #1a1a1a;
  max-width: 180mm;
}
h1 { font-size: 20pt; font-weight: 600; margin: 0 0 0.6em; letter-spacing: -0.01em; }
h2 { font-size: 15pt; font-weight: 600; margin: 1.5em 0 0.5em; letter-spacing: -0.01em;
     border-bottom: 1px solid #e5e5e5; padding-bottom: 0.2em; }
h3 { font-size: 12.5pt; font-weight: 600; margin: 1.2em 0 0.4em; }
h4 { font-size: 11pt; font-weight: 600; margin: 1em 0 0.3em; color: #333; }
p  { margin: 0 0 0.8em; }
code { font-family: "SF Mono", "Menlo", "Monaco", "Courier New", monospace;
       font-size: 10pt; background: #f4f4f4; padding: 1px 4px; border-radius: 2px; }
pre { background: #f8f8f8; border: 1px solid #e5e5e5; border-radius: 4px;
      padding: 10px 12px; overflow: auto; font-size: 9.5pt; line-height: 1.4; }
pre code { background: transparent; padding: 0; font-size: inherit; }
blockquote { margin: 0 0 1em; padding: 4px 14px; border-left: 3px solid #ccc;
             color: #555; font-style: italic; }
ul, ol { margin: 0 0 1em; padding-left: 1.6em; }
li { margin: 0.15em 0; }
table { border-collapse: collapse; width: 100%; margin: 0 0 1em; font-size: 10pt; }
th, td { border: 1px solid #ddd; padding: 6px 9px; text-align: left; vertical-align: top; }
th { background: #f6f6f6; font-weight: 600; }
a { color: #0366d6; text-decoration: none; }
a:hover { text-decoration: underline; }
hr { border: 0; border-top: 1px solid #e5e5e5; margin: 1.5em 0; }
img { max-width: 100%; height: auto; }
.page-break { page-break-before: always; }
"""


def _markdown_to_html(md_text: str, custom_css: str | None, title: str) -> str:
    """Rendert Markdown zu vollstaendigem HTML mit Styling."""
    import markdown as _md

    body_html = _md.markdown(
        md_text,
        extensions=["tables", "fenced_code", "codehilite", "toc", "sane_lists"],
        extension_configs={
            "codehilite": {"guess_lang": False, "noclasses": True},
        },
    )
    css = _DEFAULT_PDF_CSS
    if custom_css:
        css += "\n\n/* custom */\n" + custom_css

    # Title-safe escape
    import html as _html_mod
    title_safe = _html_mod.escape(title or "Report")

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>{title_safe}</title>
<style>{css}</style>
</head>
<body>
{body_html}
</body>
</html>"""


@app.post("/pdf", response_model=PdfResponse)
async def generate_pdf(req: PdfRequest):
    """Markdown oder HTML → PDF via Playwright page.pdf().

    Entweder `markdown` ODER `html` mitschicken. Bei `markdown` rendert der
    Service es mit Default-Stylesheet (MC-Design-DNA) + optional custom_css.
    Bei `html` wird der String unveraendert genommen (Caller verantwortlich
    fuer kompletten HTML-Wrapper + CSS).
    """
    if not req.markdown and not req.html:
        raise HTTPException(422, "Entweder 'markdown' ODER 'html' mitgeben.")
    if req.markdown and req.html:
        raise HTTPException(422, "'markdown' und 'html' schliessen sich aus.")

    # HTML vorbereiten
    if req.markdown:
        final_html = _markdown_to_html(req.markdown, req.custom_css, req.title)
    else:
        final_html = req.html  # type: ignore[assignment]

    # Output-Pfad: /shared-deliverables/<task_id>/<prefix>.pdf
    out_dir = _task_dir(req.task_id)
    out_path = out_dir / f"{_safe_filename(req.filename_prefix)}.pdf"

    pdf_kwargs: dict = {
        "format": req.format,
        "print_background": req.print_background,
        "margin": {
            "top": req.margin_top,
            "right": req.margin_right,
            "bottom": req.margin_bottom,
            "left": req.margin_left,
        },
    }
    if req.header_html or req.footer_html:
        pdf_kwargs["display_header_footer"] = True
        if req.header_html:
            pdf_kwargs["header_template"] = req.header_html
        if req.footer_html:
            pdf_kwargs["footer_template"] = req.footer_html

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            # setContent + wait_until=networkidle fuer externe Assets
            # (Bilder, Webfonts). Default commit+load reicht fuer inline-HTML
            # ohne externe Dependencies — schnellst.
            wait_strategy = "networkidle" if ("<img" in final_html or "fonts.googleapis" in final_html) else "load"
            await page.set_content(final_html, wait_until=wait_strategy)  # type: ignore[arg-type]
            pdf_bytes = await page.pdf(**pdf_kwargs)
            out_path.write_bytes(pdf_bytes)
        finally:
            await browser.close()

    size = out_path.stat().st_size
    # Best-effort page count: Chromium PDF hat typisch ~1500-3000 bytes/page
    # fuer Text-Content mit Default-Styling. Heuristik — fuer UI-Anzeige.
    estimated_pages = max(1, size // 2500)

    logger.info(
        "PDF generated: task=%s title=%s path=%s bytes=%d pages~=%d",
        req.task_id, req.title, out_path, size, estimated_pages,
    )

    return PdfResponse(
        path=str(out_path),
        bytes=size,
        title=req.title,
        task_id=req.task_id,
        pages=estimated_pages,
    )


@app.post("/snapshot", response_model=SnapshotResponse)
async def snapshot(req: SnapshotRequest):
    """Ein Screenshot, viewport-konfiguriert, full_page option."""
    if req.viewport not in VIEWPORTS:
        raise HTTPException(422, f"viewport muss einer von {list(VIEWPORTS)} sein")

    out = _task_dir(req.task_id) / f"{_safe_filename(req.name_suffix)}-{req.viewport}.png"
    vp = VIEWPORTS[req.viewport]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await _new_context_with_auth(browser, vp, req.url, req.auth_token)
            page = await context.new_page()
            await _prepare_page(
                page, req.url,
                login=req.login,
                interactions=req.interactions,
                wait_for_selector=req.wait_for_selector,
                wait_until=req.wait_until,
                wait_ms=req.wait_ms,
            )
            await page.screenshot(path=str(out), full_page=req.full_page)
        finally:
            await browser.close()

    return SnapshotResponse(
        path=str(out),
        viewport=req.viewport,
        bytes=out.stat().st_size,
    )


@app.post("/metrics", response_model=MetricsResponse)
async def metrics(req: SnapshotRequest):
    """Performance-Metriken via Browser-API (Navigation Timing + LCP/FCP).

    Metrics werden OHNE Interactions/Login gemessen — reine Page-Load-Zahlen.
    """
    t0 = time.monotonic()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        total_bytes = 0
        status_code = 0

        async def _on_response(resp):
            nonlocal total_bytes, status_code
            try:
                body = await resp.body()
                total_bytes += len(body)
            except Exception:
                pass
            if resp.url == req.url:
                status_code = resp.status

        try:
            context = await browser.new_context(viewport=VIEWPORTS["desktop"])
            page = await context.new_page()
            page.on("response", _on_response)
            try:
                await page.goto(req.url, wait_until="load", timeout=30000)
            except Exception as e:
                raise HTTPException(502, f"Page navigation failed: {e}")
            # FCP/LCP ueber JS abfragen (Web Performance API)
            perf = await page.evaluate(
                """() => {
                    const navi = performance.getEntriesByType('navigation')[0];
                    const paint = performance.getEntriesByType('paint');
                    const fcp = paint.find(p => p.name === 'first-contentful-paint');
                    const lcps = performance.getEntriesByType('largest-contentful-paint');
                    const lcp = lcps.length ? lcps[lcps.length - 1] : null;
                    return {
                        ttfb_ms: navi ? navi.responseStart : null,
                        fcp_ms: fcp ? fcp.startTime : null,
                        lcp_ms: lcp ? lcp.startTime : null,
                    };
                }"""
            )
        finally:
            await browser.close()

    return MetricsResponse(
        url=req.url,
        status_code=status_code,
        ttfb_ms=perf.get("ttfb_ms"),
        fcp_ms=perf.get("fcp_ms"),
        lcp_ms=perf.get("lcp_ms"),
        total_bytes=total_bytes,
        load_total_ms=round((time.monotonic() - t0) * 1000, 1),
    )


@app.post("/verify")
async def verify(req: VerifyRequest):
    """Convenience: Screenshots fuer alle requested Viewports + optional Scroll + Metrics.

    Unterstuetzt Auth (Token oder Form-Login) + pre-Screenshot Interaktionen.
    Login/Token/Interaktionen werden PRO Viewport wiederholt (jeder Viewport =
    frischer Context), das ist simpel und vermeidet Session-Leaks zwischen
    Viewport-Sizes.
    """
    if not req.viewports:
        raise HTTPException(422, "viewports darf nicht leer sein")
    for vp in req.viewports:
        if vp not in VIEWPORTS:
            raise HTTPException(422, f"viewport '{vp}' unbekannt — erlaubt: {list(VIEWPORTS)}")

    # Unique run-id damit parallele verify-Aufrufe sich nicht gegenseitig die
    # Screenshot-Files ueberschreiben (alte Bilder behalten ihren Namen). Ein
    # 6-stelliger Hex ist kurz genug fuers Filesystem + visuell unterscheidbar.
    import uuid as _uuid
    run_id = _uuid.uuid4().hex[:6]

    result: dict = {
        "screenshots": [],
        "scroll_shots": [],
        "metrics": None,
        "login": None,  # gefuellt mit {succeeded, final_url, reason} bei Form-Login
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for vp_name in req.viewports:
                vp = VIEWPORTS[vp_name]
                context = await _new_context_with_auth(browser, vp, req.url, req.auth_token)
                page = await context.new_page()
                try:
                    login_result = await _prepare_page(
                        page, req.url,
                        login=req.login,
                        interactions=req.interactions,
                        wait_for_selector=req.wait_for_selector,
                    )
                    # Erster Login-Run gewinnt — bei multi-viewport ist's eh derselbe Login
                    if login_result is not None and result["login"] is None:
                        result["login"] = login_result
                except HTTPException:
                    await context.close()
                    raise
                # Unique filename pro run damit parallele runs sich nicht ueberschreiben
                shot = _task_dir(req.task_id) / f"verify-{vp_name}-{run_id}.png"
                await page.screenshot(path=str(shot), full_page=req.full_page)
                result["screenshots"].append({
                    "path": str(shot),
                    "viewport": vp_name,
                    "bytes": shot.stat().st_size,
                })

                if req.scroll and vp_name == "desktop":
                    for pos_name, pos in [("top", 0), ("middle", 0.5), ("bottom", 1.0)]:
                        await page.evaluate(
                            f"window.scrollTo(0, document.body.scrollHeight * {pos})"
                        )
                        await page.wait_for_timeout(400)
                        sshot = _task_dir(req.task_id) / f"scroll-{pos_name}-{run_id}.png"
                        await page.screenshot(path=str(sshot), full_page=False)
                        result["scroll_shots"].append({
                            "path": str(sshot),
                            "position": pos_name,
                            "bytes": sshot.stat().st_size,
                        })
                await context.close()
        finally:
            await browser.close()

    # Separate Metrics-Call (eigener Browser-Kontext fuer saubere Timing-Messung).
    # Metrics messen die Ziel-URL OHNE Login/Interaktionen — reine Page-Performance.
    if req.metrics:
        metrics_resp = await metrics(SnapshotRequest(
            url=req.url, task_id=req.task_id, name_suffix="metrics-only",
        ))
        result["metrics"] = metrics_resp.model_dump()

    logger.info(
        "verify[%s]: %s → %d screenshots, %d scroll-shots, metrics=%s, auth=%s, login=%s, interactions=%d",
        run_id, req.url, len(result["screenshots"]), len(result["scroll_shots"]),
        result["metrics"] is not None,
        bool(req.auth_token),
        req.login is not None,
        len(req.interactions),
    )
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Record & Compose (2026-07-11, Benchmark Studio Baustein 2)
# ──────────────────────────────────────────────────────────────────────────────
#
# /record:  HTML-Datei oder URL laden, N Sekunden Video aufnehmen (Playwright
#           record_video, webm) + Screenshot, dann ffmpeg-Transcode zu H.264
#           mp4 (X-kompatibel). /compose: N mp4s -> beschriftetes Grid-Video.
# Lehre aus dem Grok-Review: JEDER Subprocess laeuft mit Timeout + captured
# stderr — nichts haengt still.

FFMPEG_TIMEOUT_S = 300

# Bench-video branding templates (frame.html + outro.html + shared.css +
# fonts/embedded-fonts.css, see docker/mc-playwright/templates/bench/).
BENCH_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates" / "bench"
BENCH_FRAME_VIEWPORT = {"width": 1920, "height": 1080}
BENCH_MODE_LINE = "side by side"  # frame.html's fixed {{MODE_LINE}} value


async def _screenshot_bench_card(html_text: str, out_png: Path) -> None:
    """Writes a filled bench template to a scratch dir alongside its shared
    assets (shared.css, fonts/ — referenced via relative href in the
    template) and screenshots it at the canonical 1920x1080 frame size."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "shared.css").symlink_to(BENCH_TEMPLATES_DIR / "shared.css")
        (tmp_dir / "fonts").symlink_to(BENCH_TEMPLATES_DIR / "fonts")
        html_path = tmp_dir / "card.html"
        html_path.write_text(html_text, encoding="utf-8")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(viewport=BENCH_FRAME_VIEWPORT)
                page = await context.new_page()
                await page.goto(html_path.as_uri(), wait_until="networkidle", timeout=30000)
                await page.screenshot(path=str(out_png))
            finally:
                await browser.close()


async def _render_branding_assets(branding: BrandingSpec, render_dir: Path) -> tuple[Path, Path]:
    """Fills frame.html + outro.html with the branding payload and
    screenshots both to PNGs inside render_dir. Returns (frame_png, outro_png)."""
    frame_template = (BENCH_TEMPLATES_DIR / "frame.html").read_text(encoding="utf-8")
    outro_template = (BENCH_TEMPLATES_DIR / "outro.html").read_text(encoding="utf-8")

    model_a, model_b = branding.models
    frame_tokens = {
        "TITLE": branding.title,
        "RUN_LABEL": branding.run_label,
        "MODEL_A": model_a.label,
        "TAG_A": model_a.tag,
        "MODEL_B": model_b.label,
        "TAG_B": model_b.tag,
        "PROMPT_LINE": branding.prompt_line,
        "MODE_LINE": BENCH_MODE_LINE,
    }
    frame_html = fill_bench_template(frame_template, frame_tokens)

    outro_tokens = {"RUN_LABEL": branding.run_label}
    outro_html = fill_bench_template(outro_template, outro_tokens)
    # ROWS is a markup block (already html-escaped per-cell by
    # render_outro_rows_html), not a plain scalar token — filled separately
    # so fill_bench_template's blanket html.escape() doesn't double-escape it.
    outro_html = outro_html.replace("{{ROWS}}", render_outro_rows_html(branding.outro_rows))

    frame_png = render_dir / "frame.png"
    outro_png = render_dir / "outro.png"
    await _screenshot_bench_card(frame_html, frame_png)
    await _screenshot_bench_card(outro_html, outro_png)
    return frame_png, outro_png


def _require_shared_path(raw: str, what: str) -> Path:
    """Containment: alle Record/Compose-Pfade muessen unter /shared-deliverables liegen."""
    resolved = Path(raw).resolve()
    root = SHARED_DELIVERABLES.resolve()
    if not resolved.is_relative_to(root):
        raise HTTPException(422, f"{what} muss unter {root} liegen: {raw}")
    return resolved


def _run_ffmpeg(cmd: list[str]) -> None:
    """Runs ffmpeg with timeout + captured stderr. 502 on failure — never hangs."""
    logger.info("ffmpeg: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(502, f"ffmpeg timeout nach {FFMPEG_TIMEOUT_S}s")
    except FileNotFoundError:
        raise HTTPException(502, "ffmpeg binary nicht gefunden — Image ohne ffmpeg gebaut?")
    if proc.returncode != 0:
        raise HTTPException(
            502,
            f"ffmpeg failed (rc={proc.returncode}): {proc.stderr[-800:]}",
        )


@app.post("/record", response_model=RecordResponse)
async def record(req: RecordRequest):
    """Laedt html_path/url, nimmt duration_s Sekunden Video auf (webm via
    Playwright record_video), transkodiert zu H.264 mp4 und speichert
    zusaetzlich screenshot.png (Thumbnail/Fallback) nach output_dir."""
    out_dir = _require_shared_path(req.output_dir, "output_dir")
    if req.html_path:
        html = _require_shared_path(req.html_path, "html_path")
        if not html.is_file():
            raise HTTPException(422, f"html_path existiert nicht: {req.html_path}")
    out_dir.mkdir(parents=True, exist_ok=True)

    vp = VIEWPORTS[req.viewport]
    screenshot_path = out_dir / "screenshot.png"
    video_path = out_dir / "recording.mp4"

    with tempfile.TemporaryDirectory() as tmp:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    viewport=vp,
                    record_video_dir=tmp,
                    record_video_size=vp,
                )
                page = await context.new_page()
                try:
                    await page.goto(req.target_url, wait_until="load", timeout=30000)
                except Exception as e:
                    raise HTTPException(502, f"Navigation failed ({req.target_url}): {e}")
                await page.wait_for_timeout(req.duration_s * 1000)
                await page.screenshot(path=str(screenshot_path), full_page=False)
                video = page.video
                await context.close()  # flushes the webm to record_video_dir
                webm_path = await video.path()
            finally:
                await browser.close()

        # ffmpeg braucht das webm bevor der TemporaryDirectory-Context endet
        await asyncio.to_thread(
            _run_ffmpeg, build_transcode_cmd(str(webm_path), str(video_path))
        )

    logger.info(
        "record: %s -> %s (%ds, %s)",
        req.target_url, video_path, req.duration_s, req.viewport,
    )
    return RecordResponse(
        video_path=str(video_path),
        screenshot_path=str(screenshot_path),
        duration_s=req.duration_s,
        bytes=video_path.stat().st_size,
    )


@app.post("/compose", response_model=ComposeResponse)
async def compose(req: ComposeRequest):
    """Komponiert N mp4-Aufnahmen zu einem Grid-Video mit Modell-Labels
    (2x1 / 3x1 / 2x2 je nach Anzahl). speed_labels (optional) werden an die
    Labels angehaengt (z.B. 'DeepSeek · 87 tok/s').

    Mit `branding` gesetzt (immer genau 2 inputs, ComposeRequest validiert
    das bereits): statt des neutralen Grids werden die zwei Aufnahmen in die
    Slots des argyelan-Frame-Templates compositiert + ein 2s Outro-Card
    angehaengt (Benchmark Studio Video-Branding, 2026-07-12)."""
    for raw in req.inputs:
        resolved = _require_shared_path(raw, "input")
        if not resolved.is_file():
            raise HTTPException(422, f"Input nicht gefunden: {raw}")
    out_path = _require_shared_path(req.output_path, "output_path")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if req.branding is not None:
        with tempfile.TemporaryDirectory() as render_tmp:
            render_dir = Path(render_tmp)
            frame_png, outro_png = await _render_branding_assets(req.branding, render_dir)
            cmd = build_branded_compose_cmd(
                req.inputs, str(frame_png), str(outro_png), str(out_path)
            )
            await asyncio.to_thread(_run_ffmpeg, cmd)
        logger.info("compose (branded): %d inputs -> %s", len(req.inputs), out_path)
    else:
        cmd = build_compose_cmd(
            req.inputs, req.labels, str(out_path), speed_labels=req.speed_labels
        )
        await asyncio.to_thread(_run_ffmpeg, cmd)
        logger.info("compose: %d inputs -> %s", len(req.inputs), out_path)

    return ComposeResponse(
        output_path=str(out_path),
        bytes=out_path.stat().st_size,
        inputs=len(req.inputs),
    )
