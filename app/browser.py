"""A shared headless Chromium.

Vue's showtimes API refuses plain HTTP requests (401), but works from inside a
real browser tab on myvue.com, so we open the cinema page and call the API from
there - exactly what the website itself does.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

_lock = asyncio.Lock()


@asynccontextmanager
async def browser_page() -> AsyncIterator["object"]:
    from playwright.async_api import async_playwright

    async with _lock:  # one browser at a time keeps memory use low on the server
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            try:
                ctx = await browser.new_context(user_agent=UA, locale="en-GB", timezone_id="Europe/London")
                page = await ctx.new_page()
                yield page
            finally:
                await browser.close()
