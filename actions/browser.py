"""Browser adapter (Playwright, Chromium).

Prefers accessibility locators (role + name, label, placeholder, text) over CSS selectors and
never falls back to coordinates on its own. With ``browser_user_data_dir`` set, a persistent
profile keeps the user's logins between runs.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
from pathlib import Path
from typing import Any

from actions.base import Adapter, AdapterOutcome, ExecutionContext
from actions.config import ActionSettings
from actions.errors import ActionError, ActionTimeoutError, ActionValidationError, AdapterError, TargetNotFoundError
from actions.models import (
    Action,
    ActionType,
    ClickParams,
    Condition,
    ConditionCheck,
    DownloadParams,
    Evidence,
    HotkeyParams,
    OpenUrlParams,
    PressKeyParams,
    ReadParams,
    ScreenshotParams,
    ScrollParams,
    Target,
    TypeParams,
)

_PLAYWRIGHT_KEYS = {
    "enter": "Enter", "tab": "Tab", "escape": "Escape", "backspace": "Backspace", "delete": "Delete",
    "space": " ", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "home": "Home", "end": "End", "pageup": "PageUp", "pagedown": "PageDown",
    "ctrl": "Control", "shift": "Shift", "alt": "Alt", "win": "Meta", "plus": "Equal", "minus": "Minus",
    **{f"f{i}": f"F{i}" for i in range(1, 13)},
}
_WHEEL_PX = 100
_CLOSED_MARKERS = ("has been closed", "Target closed", "Browser closed")


def _pw_key(key: str) -> str:
    return _PLAYWRIGHT_KEYS.get(key, key)


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return cleaned[:128] or "download"


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(1, 1000):
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise AdapterError(f"could not find a free file name for {path.name}")


class PlaywrightAdapter(Adapter):
    name = "browser"
    supports = frozenset({
        ActionType.OPEN_URL, ActionType.CLICK, ActionType.TYPE, ActionType.PRESS_KEY, ActionType.HOTKEY,
        ActionType.SCROLL, ActionType.READ, ActionType.SCREENSHOT, ActionType.DOWNLOAD,
    })

    def __init__(self, settings: ActionSettings) -> None:
        self.settings = settings
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()

    def available(self) -> bool:
        return importlib.util.find_spec("playwright") is not None

    # ------------------------------------------------------------------ lifecycle

    async def _ensure_page(self) -> Any:
        async with self._lock:
            if self._page is not None and not self._page.is_closed():
                return self._page
            if self._context is None:
                await self._launch()
            pages = [p for p in self._context.pages if not p.is_closed()]
            self._page = pages[-1] if pages else await self._context.new_page()
            return self._page

    async def _launch(self) -> None:
        from playwright.async_api import async_playwright

        s = self.settings
        channel = None if s.browser_channel in ("", "chromium") else s.browser_channel
        self._playwright = await async_playwright().start()
        if s.browser_user_data_dir is not None:
            Path(s.browser_user_data_dir).mkdir(parents=True, exist_ok=True)
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(s.browser_user_data_dir), headless=s.browser_headless, channel=channel, accept_downloads=True,
            )
        else:
            self._browser = await self._playwright.chromium.launch(headless=s.browser_headless, channel=channel)
            self._context = await self._browser.new_context(accept_downloads=True)
        self._context.on("close", lambda _ctx: self._reset())

    def _reset(self) -> None:
        self._context = None
        self._page = None

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    pass
        if self._playwright is not None:
            await self._playwright.stop()
        self._playwright = self._browser = None
        self._reset()

    # ------------------------------------------------------------------ locating

    @staticmethod
    def _locator(page: Any, target: Target) -> Any:
        if target.role:
            loc = page.get_by_role(target.role, name=target.name) if target.name else page.get_by_role(target.role)
        elif target.label:
            loc = page.get_by_label(target.label)
        elif target.placeholder:
            loc = page.get_by_placeholder(target.placeholder)
        elif target.text:
            loc = page.get_by_text(target.text)
        elif target.selector:
            loc = page.locator(target.selector)
        else:
            raise ActionValidationError("browser target has no element locator", target=target.summary())
        return loc

    async def _element(self, page: Any, target: Target, ctx: ExecutionContext) -> tuple[Any, int]:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        all_matches = self._locator(page, target)
        loc = all_matches.first
        try:
            await loc.wait_for(state="visible", timeout=ctx.find_timeout() * 1000)
        except PlaywrightTimeoutError:
            raise TargetNotFoundError("no visible element matched", target=target.summary()) from None
        except PlaywrightError as exc:
            raise ActionValidationError(f"invalid locator: {exc.message}", target=target.summary()) from None
        return loc, await all_matches.count()

    # ------------------------------------------------------------------ actions

    async def execute(self, action: Action, ctx: ExecutionContext) -> AdapterOutcome:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        try:
            page = await self._ensure_page()
            return await self._dispatch(page, action, ctx)
        except ActionError:
            raise
        except PlaywrightTimeoutError as exc:
            raise ActionTimeoutError(exc.message.splitlines()[0], adapter=self.name) from None
        except PlaywrightError as exc:
            message = exc.message.splitlines()[0] if exc.message else str(exc)
            if any(m in message for m in _CLOSED_MARKERS):
                self._reset()
                raise AdapterError(f"browser was closed: {message}", adapter=self.name, retryable=True,
                                   may_have_side_effects=False) from None
            raise AdapterError(message, adapter=self.name, retryable="net::" in message) from None

    async def _dispatch(self, page: Any, action: Action, ctx: ExecutionContext) -> AdapterOutcome:
        t, p, target = action.type, action.parameters, action.target
        ms = lambda: max(500.0, ctx.remaining() * 1000)  # noqa: E731
        out = AdapterOutcome()
        element = target is not None and target.is_element

        if t is ActionType.OPEN_URL:
            assert isinstance(p, OpenUrlParams)
            if p.new_tab:
                page = self._page = await self._context.new_page()
            response = await page.goto(p.url, wait_until="domcontentloaded", timeout=ms())
            out.output = {"url": page.url, "status": response.status if response else None}

        elif t is ActionType.CLICK:
            assert isinstance(p, ClickParams) and target is not None
            loc, matches = await self._element(page, target, ctx)
            await loc.click(button=p.button, click_count=2 if p.double else 1, timeout=ms())
            out.output = {"matches": matches}
            out.evidence.append(Evidence(type="ui_element", value=target.description or str(target.summary()),
                                         metadata={"matches": matches}))

        elif t is ActionType.TYPE:
            assert isinstance(p, TypeParams)
            if element:
                assert target is not None
                loc, _ = await self._element(page, target, ctx)
                if p.clear_first:
                    await loc.fill(p.text, timeout=ms())
                else:
                    await loc.press_sequentially(p.text, delay=10, timeout=ms())
                if p.submit:
                    await loc.press("Enter", timeout=ms())
            else:
                if p.clear_first:
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Backspace")
                await page.keyboard.type(p.text, delay=10)
                if p.submit:
                    await page.keyboard.press("Enter")
            out.output = {"typed_chars": len(p.text)}

        elif t in (ActionType.PRESS_KEY, ActionType.HOTKEY):
            if isinstance(p, PressKeyParams):
                combo, presses = _pw_key(p.key), p.presses
                out.output = {"key": p.key, "presses": p.presses}
            else:
                assert isinstance(p, HotkeyParams)
                combo, presses = "+".join(_pw_key(k) for k in p.keys), 1
                out.output = {"hotkey": p.combo}
            loc = (await self._element(page, target, ctx))[0] if element and target is not None else None
            for _ in range(presses):
                if loc is not None:
                    await loc.press(combo, timeout=ms())
                else:
                    await page.keyboard.press(combo)

        elif t is ActionType.SCROLL:
            assert isinstance(p, ScrollParams)
            if element and target is not None:
                loc, _ = await self._element(page, target, ctx)
                await loc.scroll_into_view_if_needed(timeout=ms())
                await loc.hover(timeout=ms())
            dist = p.amount * _WHEEL_PX
            dx, dy = {"up": (0, -dist), "down": (0, dist), "left": (-dist, 0), "right": (dist, 0)}[p.direction]
            await page.mouse.wheel(dx, dy)
            out.output = {"direction": p.direction, "amount": p.amount}

        elif t is ActionType.READ:
            assert isinstance(p, ReadParams)
            if element and target is not None:
                loc, _ = await self._element(page, target, ctx)
                text = await loc.inner_text(timeout=ms())
                if not text.strip():
                    try:
                        text = await loc.input_value(timeout=1000)
                    except Exception:
                        pass
            else:
                text = await page.inner_text("body", timeout=ms())
            out.output = {"text": text[: p.max_chars], "truncated": len(text) > p.max_chars}
            out.evidence.append(Evidence(type="observed_text", value=text[:500]))

        elif t is ActionType.SCREENSHOT:
            assert isinstance(p, ScreenshotParams)
            path = ctx.evidence_path(action.action_id, "page")
            if element and target is not None:
                loc, _ = await self._element(page, target, ctx)
                await loc.screenshot(path=str(path), timeout=ms())
            else:
                await page.screenshot(path=str(path), full_page=p.full_page, timeout=ms())
            out.output = {"path": str(path)}
            out.evidence.append(Evidence(type="screenshot", value=str(path)))

        elif t is ActionType.DOWNLOAD:
            assert isinstance(p, DownloadParams)
            out = await self._download(page, p, target, ctx)

        else:  # pragma: no cover - guarded by `supports`
            raise AdapterError(f"browser adapter cannot run {t.value}")
        return out

    async def _download(self, page: Any, p: DownloadParams, target: Target | None, ctx: ExecutionContext) -> AdapterOutcome:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        loc = None
        if target is not None and target.is_element:
            loc, _ = await self._element(page, target, ctx)
        try:
            async with page.expect_download(timeout=max(500.0, ctx.remaining() * 1000)) as info:
                if loc is not None:
                    await loc.click(timeout=max(500.0, ctx.remaining() * 1000))
                else:
                    try:
                        await page.goto(p.url, timeout=max(500.0, ctx.remaining() * 1000))
                    except PlaywrightError as exc:
                        # Navigating straight to a file aborts the navigation by design.
                        if "Download is starting" not in exc.message and "ERR_ABORTED" not in exc.message:
                            raise
            download = await info.value
        except PlaywrightTimeoutError:
            raise ActionTimeoutError("no download started", adapter=self.name,
                                     target=target.summary() if target else {"url": p.url}) from None
        failure = await download.failure()
        if failure:
            raise AdapterError(f"download failed: {failure}", adapter=self.name, retryable=True)
        download_dir = Path(self.settings.download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)
        dest = _unique(download_dir / _safe_filename(p.save_as or download.suggested_filename))
        await download.save_as(str(dest))
        size = dest.stat().st_size
        return AdapterOutcome(
            output={"path": str(dest.resolve()), "suggested_filename": download.suggested_filename,
                    "size_bytes": size, "source_url": download.url},
            evidence=[Evidence(type="download_path", value=str(dest.resolve()),
                               metadata={"size_bytes": size, "source_url": download.url})],
        )

    # ------------------------------------------------------------------ checks / evidence

    async def check(self, condition: Condition, ctx: ExecutionContext) -> ConditionCheck:
        kind = condition.kind
        page = self._page if self._page is not None and not self._page.is_closed() else None

        def result(ok: bool, observed: str | None = None) -> ConditionCheck:
            return ConditionCheck(condition=condition, status="satisfied" if ok else "unsatisfied", observed=observed)

        if page is None:
            return ConditionCheck(condition=condition, status="satisfied" if kind == "element_hidden" else "unsatisfied",
                                  detail="no browser page is open")
        value = (condition.value or "").casefold()
        if kind == "url_contains":
            return result(value in page.url.casefold(), observed=page.url)
        if kind == "title_contains":
            title = await page.title()
            return result(value in title.casefold(), observed=title)
        target = condition.target
        if kind == "text_present":
            if target is not None and target.is_element:
                loc = self._locator(page, target).first
                text = await loc.inner_text(timeout=1000) if await loc.count() else ""
            else:
                text = await page.inner_text("body", timeout=2000)
            return result(value in text.casefold(), observed=text[:200])
        if kind in ("element_visible", "element_hidden") and target is not None:
            visible = await self._locator(page, target).first.is_visible()
            return result(visible if kind == "element_visible" else not visible)
        return ConditionCheck(condition=condition, status="unverified", detail=f"browser adapter cannot check {kind}")

    async def observe(self) -> list[Evidence]:
        page = self._page
        if page is None or page.is_closed():
            return []
        return [Evidence(type="url", value=page.url), Evidence(type="page_title", value=await page.title())]

    async def screenshot(self, path: Path, target: Target | None = None) -> Path | None:
        page = self._page
        if page is None or page.is_closed():
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), timeout=5000)
        return path
