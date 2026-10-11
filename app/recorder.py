"""Remote browser that records a HAR: the panel shows screenshots and forwards clicks/keys.

Needs the optional `playwright` package and its Chromium (see deploy/install-recorder.sh).
Only one recording runs at a time; it is closed automatically after IDLE_SECONDS without input.
"""
import asyncio
import base64
import time

from . import har, visual
from .config import DATA_DIR

WIDTH, HEIGHT = 1280, 800
IDLE_SECONDS = 20 * 60
SPECIAL_KEYS = {"Enter", "Backspace", "Tab", "Escape", "Delete", "ArrowUp", "ArrowDown", "ArrowLeft",
                "ArrowRight", "Home", "End", "PageUp", "PageDown"}


async def _skip_trackers(route):
    """Ads/analytics/Google hosts are slow or blocked from Iranian servers and hold pages up; they carry no data."""
    if route.request.resource_type != "document" and har.is_tracker(route.request.url):
        await route.abort()
    else:
        await route.continue_()


class RecorderError(Exception):
    pass


class Recorder:
    def __init__(self):
        self.pw = self.browser = self.context = self.page = None
        self.category_id: int | None = None
        self.har_path = None
        self.last_input = 0.0
        self.lock = asyncio.Lock()
        self._watchdog: asyncio.Task | None = None
        self._cdp = self._cdp_page = None

    @property
    def active(self) -> bool:
        return self.page is not None

    def status(self) -> dict:
        return {"active": self.active, "category_id": self.category_id,
                "url": self.page.url if self.page else None, "width": WIDTH, "height": HEIGHT}

    async def start(self, category_id: int, url: str, record_har: bool = True):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RecorderError("مرورگر ضبط روی سرور نصب نیست؛ یک بار دستور sudo bash deploy/install-recorder.sh را اجرا کنید")
        async with self.lock:
            if self.active:
                raise RecorderError("یک ضبط دیگر در حال انجام است؛ اول آن را تمام یا لغو کنید")
            rec_dir = DATA_DIR / "recordings"
            rec_dir.mkdir(parents=True, exist_ok=True)
            self.har_path = rec_dir / f"cat_{category_id}_{int(time.time())}.har"
            try:
                self.pw = await async_playwright().start()
                self.browser = await self.pw.chromium.launch(args=["--disable-dev-shm-usage", "--lang=fa-IR"])
                sess = visual.session_file(category_id)
                opts = {"viewport": {"width": WIDTH, "height": HEIGHT}, "locale": "fa-IR",
                        # reuse the login saved by an earlier session of this section
                        "storage_state": str(sess) if sess.exists() else None}
                if record_har:
                    opts.update(record_har_path=str(self.har_path), record_har_content="embed")
                else:
                    self.har_path = None
                self.context = await self.browser.new_context(**opts)
                await self.context.route("**/*", _skip_trackers)
                self.page = await self.context.new_page()
                # A link that opens a new tab: follow it in the single visible tab instead
                self.context.on("page", self._adopt_page)
            except Exception as e:
                await self._close(save=False)
                raise RecorderError(f"اجرای مرورگر ناموفق بود: {e}")
            self.category_id = category_id
            self.last_input = time.monotonic()
            self._watchdog = asyncio.create_task(self._idle_watch())
        await self.goto(url)

    def _adopt_page(self, page):
        self.page = page

    async def goto(self, url: str):
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        await self._page().goto(url, wait_until="domcontentloaded", timeout=45000)

    def _page(self):
        if not self.active:
            raise RecorderError("ضبطی در حال انجام نیست")
        self.last_input = time.monotonic()
        return self.page

    async def screenshot(self) -> bytes:
        if not self.active:
            raise RecorderError("ضبطی در حال انجام نیست")
        # Straight from Chrome: page.screenshot() waits for web fonts and hangs when a font host is slow/blocked
        if self._cdp_page is not self.page:
            self._cdp = await self.context.new_cdp_session(self.page)
            self._cdp_page = self.page
        shot = await asyncio.wait_for(self._cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 60}), 10)
        return base64.b64decode(shot["data"])

    async def input(self, ev: dict):
        page = self._page()
        kind = ev.get("type")
        if kind == "click":
            await page.mouse.click(float(ev["x"]), float(ev["y"]))
        elif kind == "scroll":
            await page.mouse.move(float(ev.get("x", WIDTH / 2)), float(ev.get("y", HEIGHT / 2)))
            await page.mouse.wheel(0, float(ev.get("dy", 0)))
        elif kind == "text":
            await page.keyboard.type(str(ev.get("text", "")))
        elif kind == "key" and ev.get("key") in SPECIAL_KEYS:
            await page.keyboard.press(ev["key"])
        elif kind == "goto":
            await self.goto(str(ev.get("url", "")))
        elif kind == "back":
            await page.go_back(wait_until="domcontentloaded")
        elif kind == "reload":
            await page.reload(wait_until="domcontentloaded")

    async def pick(self, x: float, y: float, where: str, item_selector: str | None, attr: str, up: int = 0) -> dict:
        """What is under (x, y) (or the box `up` levels around it), with selectors to find it again (see visual.PICK_JS)."""
        page = self._page()
        return await page.evaluate(visual.PICK_JS, [x, y, where, item_selector or None, attr, max(int(up), 0)])

    async def hover(self, x: float, y: float, mode: str, where: str, item_selector: str | None, attr: str) -> dict | None:
        """What a click at (x, y) would pick, for the live outline in the panel (see visual.HOVER_JS)."""
        if not self.active:
            raise RecorderError("ضبطی در حال انجام نیست")
        return await self.page.evaluate(visual.HOVER_JS, [x, y, mode, where, item_selector or None, attr])

    async def stop(self, save: bool = True) -> tuple[int | None, bytes | None]:
        """Close the browser; returns (category_id, HAR bytes) when saving."""
        async with self.lock:
            cid, path = self.category_id, self.har_path
            if save and self.context and cid is not None:
                # keep the login (cookies) for collection runs and the next session
                try:
                    visual.SESSIONS.mkdir(parents=True, exist_ok=True)
                    await self.context.storage_state(path=str(visual.session_file(cid)))
                except Exception:
                    pass
            await self._close(save)
            if save and path and path.exists():
                data = path.read_bytes()
                path.unlink(missing_ok=True)  # the HAR holds session cookies; do not keep it around
                return cid, data
            if path:
                path.unlink(missing_ok=True)
            return cid, None

    async def _close(self, save: bool):
        if self._watchdog and self._watchdog is not asyncio.current_task():
            self._watchdog.cancel()
        self._watchdog = None
        for obj in (self.context, self.browser):  # closing the context writes the HAR file
            if obj:
                try:
                    await obj.close()
                except Exception:
                    pass
        if self.pw:
            try:
                await self.pw.stop()
            except Exception:
                pass
        self._cdp = self._cdp_page = None
        self.pw = self.browser = self.context = self.page = None
        self.category_id = None

    async def _idle_watch(self):
        while self.active:
            await asyncio.sleep(30)
            if time.monotonic() - self.last_input > IDLE_SECONDS:
                async with self.lock:
                    await self._close(save=False)
                    if self.har_path:
                        self.har_path.unlink(missing_ok=True)


recorder = Recorder()
