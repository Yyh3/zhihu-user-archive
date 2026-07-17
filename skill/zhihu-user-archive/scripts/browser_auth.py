#!/usr/bin/env python3
"""Interactive Zhihu authentication through an isolated browser profile."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import time
from collections.abc import Mapping
from typing import Any


LOGIN_URL = "https://www.zhihu.com/signin"
PROFILE_MARKER = ".zhihu-user-archive-profile"


class BrowserAuthError(RuntimeError):
    pass


def default_profile_dir(
    platform_name: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    platform_name = platform_name or sys.platform
    home = home or Path.home()
    environ = environ if environ is not None else os.environ
    if platform_name.startswith("win") and environ.get("LOCALAPPDATA"):
        return Path(environ["LOCALAPPDATA"]) / "Codex" / "zhihu-user-archive" / "browser-profile"
    if platform_name == "darwin":
        return home / "Library" / "Application Support" / "Codex" / "zhihu-user-archive" / "browser-profile"
    return home / ".local" / "share" / "codex" / "zhihu-user-archive" / "browser-profile"


def browser_candidates(
    platform_name: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[Path]:
    platform_name = platform_name or sys.platform
    home = home or Path.home()
    environ = environ if environ is not None else os.environ
    candidates: list[Path] = []
    if platform_name.startswith("win"):
        for env_name, suffixes in {
            "PROGRAMFILES(X86)": ["Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"],
            "PROGRAMFILES": ["Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"],
            "LOCALAPPDATA": ["Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"],
        }.items():
            base = environ.get(env_name)
            if base:
                candidates.extend(Path(base) / suffix for suffix in suffixes)
    elif platform_name == "darwin":
        app_binaries = [
            "Google Chrome.app/Contents/MacOS/Google Chrome",
            "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "Chromium.app/Contents/MacOS/Chromium",
            "Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        ]
        for applications in (Path("/Applications"), home / "Applications"):
            candidates.extend(applications / binary for binary in app_binaries)
    return candidates


def find_browser_executable(
    explicit: str | None = None,
    bundled: str | Path | None = None,
    platform_name: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    if explicit:
        candidate = Path(os.path.expandvars(os.path.expanduser(explicit))).resolve()
        if candidate.is_file():
            return candidate
        raise BrowserAuthError(f"Browser executable not found: {candidate}")

    if bundled:
        bundled_path = Path(bundled).expanduser()
        if bundled_path.is_file():
            return bundled_path.resolve()
    for candidate in browser_candidates(platform_name, home, environ):
        if candidate.is_file():
            return candidate.resolve()
    for command in ("msedge", "microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        resolved = shutil.which(command)
        if resolved:
            return Path(resolved).resolve()
    raise BrowserAuthError(
        "No compatible Chromium browser was found. Install Chrome, Edge, or Chromium, or install the "
        f'Playwright browser with:\n  "{sys.executable}" -m playwright install chromium'
    )


def _playwright_api() -> tuple[Any, type[Exception]]:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserAuthError(
            "Browser authentication requires Playwright. Install it in the active runtime with:\n"
            f'  "{sys.executable}" -m pip install playwright'
        ) from exc
    return sync_playwright, PlaywrightError


def _is_zhihu_cookie(cookie: dict[str, Any]) -> bool:
    domain = str(cookie.get("domain") or "").lstrip(".").lower()
    return domain == "zhihu.com" or domain.endswith(".zhihu.com")


def cookie_header_from_cookies(cookies: list[dict[str, Any]]) -> str:
    relevant = [cookie for cookie in cookies if _is_zhihu_cookie(cookie) and cookie.get("name")]
    relevant.sort(key=lambda cookie: len(str(cookie.get("domain") or "")))
    values: dict[str, str] = {}
    for cookie in relevant:
        values[str(cookie["name"])] = str(cookie.get("value") or "")
    return "; ".join(f"{name}={value}" for name, value in values.items())


def _has_z_c0(cookies: list[dict[str, Any]]) -> bool:
    return any(cookie.get("name") == "z_c0" and cookie.get("value") and _is_zhihu_cookie(cookie) for cookie in cookies)


class BrowserAuth:
    def __init__(
        self,
        profile_dir: str | Path | None = None,
        browser_executable: str | None = None,
        login_timeout: float = 300.0,
    ) -> None:
        self.profile_dir = Path(profile_dir).expanduser().resolve() if profile_dir else default_profile_dir().resolve()
        self.browser_executable = browser_executable
        self.login_timeout = max(30.0, float(login_timeout))

    @property
    def marker(self) -> Path:
        return self.profile_dir / PROFILE_MARKER

    def _prepare_profile(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if not self.marker.exists():
            self.marker.write_text("Dedicated profile for zhihu-user-archive.\n", encoding="utf-8")

    def _probe_ready(self, context: Any, cookies: list[dict[str, Any]], probe_url: str | None) -> bool:
        if not _has_z_c0(cookies):
            return False
        if not probe_url:
            return True
        try:
            response = context.request.get(
                probe_url,
                headers={"Accept": "application/json, text/plain, */*", "Referer": "https://www.zhihu.com/"},
                timeout=10_000,
            )
            content_type = (response.headers.get("content-type") or "").lower()
            return 200 <= response.status < 400 and "json" in content_type
        except Exception:
            return False

    def _collect(self, interactive: bool, probe_url: str | None) -> str:
        sync_playwright, PlaywrightError = _playwright_api()
        self._prepare_profile()
        context = None
        timeout = self.login_timeout if interactive else min(15.0, self.login_timeout)
        try:
            with sync_playwright() as playwright:
                executable = find_browser_executable(
                    self.browser_executable,
                    bundled=playwright.chromium.executable_path,
                )
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    executable_path=str(executable),
                    headless=not interactive,
                    viewport=None if interactive else {"width": 1280, "height": 720},
                    args=["--no-first-run", "--no-default-browser-check"],
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    try:
                        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
                    except PlaywrightError:
                        pass
                    if interactive:
                        print(
                            "A dedicated browser window is open. Sign in to Zhihu or complete the visible "
                            "security verification; the window will close automatically when the session is ready.",
                            file=sys.stderr,
                        )
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        cookies = context.cookies()
                        if self._probe_ready(context, cookies, probe_url):
                            header = cookie_header_from_cookies(cookies)
                            if header:
                                return header
                        if not interactive:
                            break
                        if page.is_closed():
                            raise BrowserAuthError("The dedicated browser window was closed before login completed.")
                        time.sleep(1.5)
                finally:
                    context.close()
                    context = None
        except BrowserAuthError:
            raise
        except PlaywrightError as exc:
            raise BrowserAuthError(
                "Could not start the dedicated browser profile. Close any previous zhihu-user-archive "
                f"browser window and retry. Details: {exc}"
            ) from exc
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
        if interactive:
            raise BrowserAuthError(f"Zhihu login was not completed within {int(timeout)} seconds.")
        raise BrowserAuthError("The saved dedicated browser session is missing or expired.")

    def cookie_header(self, probe_url: str | None = None, force_interactive: bool = False) -> str:
        if self.marker.exists() and not force_interactive:
            try:
                return self._collect(interactive=False, probe_url=probe_url)
            except BrowserAuthError:
                pass
        return self._collect(interactive=True, probe_url=probe_url)

    def clear_profile(self) -> bool:
        if not self.profile_dir.exists():
            return False
        resolved = self.profile_dir.resolve()
        if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
            raise BrowserAuthError(f"Refusing to remove unsafe profile path: {resolved}")
        if not self.marker.is_file():
            raise BrowserAuthError(
                f"Refusing to remove unmarked directory: {resolved}. Delete it manually after verifying the path."
            )
        shutil.rmtree(resolved)
        return True
