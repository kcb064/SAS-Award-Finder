"""Browser-context fetcher for the SAS BFF.

Phase 1 spike verdict (docs/api-notes.md): plain httpx and bundled Chromium are Cloudflare-403'd;
only **real Google Chrome** (`channel="chrome"`) driving an **in-page `fetch()`** returns 200, and
the `cf_clearance` cookie is TLS-fingerprint-bound so it can't be replayed out of the browser.
So this is the single fetch path — there is no httpx fast-lane.

One persistent context is kept alive for the app's lifetime (a warmed page on the award-finder
URL). `fetch_json()` runs an in-page fetch of a BFF path; on a block it re-warms once and retries.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from app.providers.sas_direct.endpoints import WARM_PAGE

_STEALTH_INIT = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]
_BLOCK_MARKERS = ("Just a moment", "Denied boarding", "Attention Required")


class FetchError(RuntimeError):
    """A SAS fetch failed (block, timeout, or non-200) after the engine's own retry."""


@dataclass(slots=True)
class FetchResult:
    status: int
    ok: bool
    content_type: str
    text: str
    byte_size: int
    duration_ms: int


class BrowserFetcher:
    def __init__(
        self,
        *,
        profile_dir: Path,
        headless: bool = True,
        channel: str = "chrome",
        user_agent: str | None = None,
        nav_timeout_ms: int = 60_000,
    ) -> None:
        self.profile_dir = profile_dir
        self.headless = headless
        self.channel = channel
        self.user_agent = user_agent or None
        self.nav_timeout_ms = nav_timeout_ms
        self._pw = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._resolved_ua: str | None = None
        self._start_lock = asyncio.Lock()

    # ---- lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        async with self._start_lock:
            if self._ctx is not None:
                return
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._pw = await async_playwright().start()
            ua = self.user_agent or self._resolved_ua or await self._resolve_user_agent()
            self._resolved_ua = ua
            ctx_kwargs: dict = dict(
                user_data_dir=str(self.profile_dir),
                headless=self.headless,
                channel=self.channel,
                args=_LAUNCH_ARGS,
                viewport={"width": 1366, "height": 900},
                locale="en-US",
                timezone_id="Europe/Copenhagen",
            )
            if ua:
                ctx_kwargs["user_agent"] = ua
            self._ctx = await self._pw.chromium.launch_persistent_context(**ctx_kwargs)
            await self._ctx.add_init_script(_STEALTH_INIT)
            self._page = await self._ctx.new_page()
            await self._warm()

    async def _resolve_user_agent(self) -> str | None:
        """Derive a Cloudflare-safe User-Agent from the real browser.

        Headless Chrome reports `HeadlessChrome/<ver>` in its UA, and Cloudflare hard-blocks that
        token (proven in the Phase 1 spike). We launch a throwaway browser, read the native UA, and
        strip `Headless` — keeping the *real* installed version so the UA stays consistent with the
        browser's client hints across Chrome updates. Headed runs need no change (returns None).
        """
        assert self._pw is not None
        browser = await self._pw.chromium.launch(
            headless=self.headless, channel=self.channel, args=_LAUNCH_ARGS
        )
        try:
            page = await browser.new_page()
            ua = await page.evaluate("navigator.userAgent")
        finally:
            await browser.close()
        if "Headless" in ua:
            return ua.replace("HeadlessChrome", "Chrome")
        return None

    async def _warm(self) -> None:
        assert self._page is not None
        await self._page.goto(WARM_PAGE, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
        # Give any Cloudflare interstitial a moment to auto-clear.
        for _ in range(4):
            await self._page.wait_for_timeout(1500)
            title = await self._page.title()
            if not any(m in title for m in _BLOCK_MARKERS):
                return

    async def aclose(self) -> None:
        if self._ctx is not None:
            await self._ctx.close()
            self._ctx = None
            self._page = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    @property
    def started(self) -> bool:
        return self._ctx is not None

    # ---- fetch -----------------------------------------------------------------------

    async def fetch_json(self, path: str) -> FetchResult:
        """In-page fetch of a BFF path. Re-warms once and retries if the first attempt is blocked."""
        if self._ctx is None:
            await self.start()
        result = await self._attempt(path)
        if not result.ok:
            # Session may have gone stale / hit a challenge — re-warm and try once more.
            await self._warm()
            result = await self._attempt(path)
        if not result.ok:
            raise FetchError(
                f"BFF fetch blocked: status={result.status} ct={result.content_type} "
                f"bytes={result.byte_size} path={path}"
            )
        return result

    async def _attempt(self, path: str) -> FetchResult:
        assert self._page is not None
        started = time.monotonic()
        raw = await self._page.evaluate(
            """async (p) => {
                try {
                    const r = await fetch(p, {headers: {accept: 'application/json'}});
                    const t = await r.text();
                    return {status: r.status, ct: r.headers.get('content-type') || '', text: t};
                } catch (e) {
                    return {status: -1, ct: '', text: String(e)};
                }
            }""",
            path,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        status = int(raw["status"])
        ct = raw["ct"] or ""
        text = raw["text"] or ""
        ok = status == 200 and "json" in ct.lower()
        return FetchResult(
            status=status,
            ok=ok,
            content_type=ct,
            text=text,
            byte_size=len(text.encode("utf-8")),
            duration_ms=duration_ms,
        )
