"""The dedicated browser: its own profile and a CDP port, launched detached so it outlives
this process. It never touches the user's everyday browser profile."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.async_api import Browser, Page, Playwright, async_playwright

PORT = int(os.environ.get("CARTCLAW_CDP_PORT", "9333"))
PROFILE = Path(
    os.environ.get("CARTCLAW_PROFILE") or Path.home() / ".cartclaw" / "profile"
)
CDP = f"http://127.0.0.1:{PORT}"
HOME_URL = "https://www.amazon.com/"
SIGNIN_URL = (
    "https://www.amazon.com/your-orders/orders"  # redirects to sign-in when signed out
)


class BrowserError(Exception):
    pass


def _candidates() -> list[str]:
    if os.environ.get("CARTCLAW_BROWSER"):
        return [os.environ["CARTCLAW_BROWSER"]]
    if sys.platform == "win32":
        # MCP clients may start servers with a trimmed environment, so fall back to the defaults.
        roots = [
            os.environ.get("PROGRAMFILES") or r"C:\Program Files",
            os.environ.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)",
            os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"),
        ]
        rels = [
            r"BraveSoftware\Brave-Browser\Application\brave.exe",
            r"Google\Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
            r"Chromium\Application\chrome.exe",
        ]
        return [str(Path(root) / rel) for rel in rels for root in roots if root]
    if sys.platform == "darwin":
        return [
            f"/Applications/{app}.app/Contents/MacOS/{app}"
            for app in ("Brave Browser", "Google Chrome", "Microsoft Edge", "Chromium")
        ]
    return [
        shutil.which(n) or ""
        for n in (
            "brave-browser",
            "brave",
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "microsoft-edge",
        )
    ]


def find_browser() -> str:
    for path in _candidates():
        if path and Path(path).exists():
            return path
    raise BrowserError(
        "No Chromium browser found (Brave, Chrome, Edge or Chromium). "
        "Set CARTCLAW_BROWSER to the browser's full path."
    )


def cdp_up() -> bool:
    try:
        with urllib.request.urlopen(f"{CDP}/json/version", timeout=1):
            return True
    except OSError:
        return False


def launch(url: str = HOME_URL) -> None:
    PROFILE.mkdir(parents=True, exist_ok=True)
    args = [
        find_browser(),
        f"--user-data-dir={PROFILE}",
        f"--remote-debugging-port={PORT}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        url,
    ]
    quiet = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    if sys.platform != "win32":
        subprocess.Popen(args, start_new_session=True, **quiet)
        return
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            args, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **quiet
        )
    except OSError:
        # The parent's job object forbids breakaway and would kill the browser when this
        # process exits. A WMI-created process is parented outside the job.
        ps = (
            "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
            "-Arguments @{ CommandLine = $env:CARTCLAW_CMD }; exit $r.ReturnValue"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            check=True,
            capture_output=True,
            timeout=30,
            env={**os.environ, "CARTCLAW_CMD": subprocess.list2cmdline(args)},
        )


def ensure_running(url: str = HOME_URL, wait_s: float = 20) -> None:
    if cdp_up():
        return
    launch(url)
    for _ in range(int(wait_s * 4)):
        if cdp_up():
            return
        time.sleep(0.25)
    raise BrowserError(
        f"Started the browser but its control port {PORT} never answered."
    )


class Session:
    """One CDP connection to the dedicated browser, shared by every tool call."""

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self.lock = asyncio.Lock()

    async def browser(self) -> Browser:
        if self._browser and self._browser.is_connected():
            return self._browser
        await asyncio.to_thread(ensure_running)
        if self._pw is None:
            self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(CDP)
        return self._browser

    async def page(self) -> Page:
        browser = await self.browser()
        ctx = browser.contexts[0]
        for page in ctx.pages:
            if "amazon." in page.url:
                return page
        return await ctx.new_page()


def login() -> None:
    """`cartclaw login`: open the Amazon window on the sign-in page."""
    if cdp_up():
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            page = p.chromium.connect_over_cdp(CDP).contexts[0].new_page()
            page.goto(SIGNIN_URL)
            page.bring_to_front()
    else:
        ensure_running(SIGNIN_URL)
    print(
        f"An Amazon window is open (profile: {PROFILE}).\n"
        "Sign in there and tick 'Keep me signed in'. You only do this once."
    )
