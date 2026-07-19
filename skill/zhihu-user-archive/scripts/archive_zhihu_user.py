#!/usr/bin/env python3
"""Archive public Zhihu user content with explicit coverage reporting.

Core archiving uses only Python's standard library. Optional interactive browser
authentication uses Playwright with a dedicated profile; the script never reads
the user's normal browser cookie store or attempts to solve verification challenges.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import random
import re
import shutil
import sys
import tempfile
import threading
import time
from http.client import IncompleteRead
from http.cookiejar import Cookie, CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

VERSION = "1.3.0"
DEFAULT_BASE_URL = "https://www.zhihu.com/api/v4/"
ARTICLE_DETAIL_BASE_URL = "https://zhuanlan.zhihu.com/api/"
DEFAULT_TYPES = "answers,articles,pins,columns,activities,comments-authored"


def platform_user_agent(platform_name: str | None = None) -> str:
    platform_name = platform_name or sys.platform
    if platform_name == "darwin":
        system = "Macintosh; Intel Mac OS X 10_15_7"
    elif platform_name.startswith("linux"):
        system = "X11; Linux x86_64"
    else:
        system = "Windows NT 10.0; Win64; x64"
    return f"Mozilla/5.0 ({system}) AppleWebKit/537.36 Chrome/126 Safari/537.36"

ENDPOINTS = {
    "answers": ("answer", "members/{token}/answers", {"sort_by": "created"}),
    "articles": ("article", "members/{token}/articles", {"sort_by": "created"}),
    "pins": ("pin", "members/{token}/pins", {}),
    "columns": ("column", "members/{token}/column-contributions", {}),
    "activities": ("activity", "members/{token}/activities", {}),
    "comments-authored": ("comment_authored", "members/{token}/comments", {"order": "reverse"}),
    "questions": ("question", "members/{token}/questions", {}),
    "zvideos": ("zvideo", "members/{token}/zvideos", {}),
}

COMMENT_ENDPOINTS = {
    "answer": ("comment_v5/answers/{id}/root_comment", "answers/{id}/root_comments"),
    "article": ("comment_v5/articles/{id}/root_comment", "articles/{id}/root_comments"),
    "pin": ("comment_v5/pins/{id}/root_comment", "pins/{id}/root_comments"),
}


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li", "h1", "h2", "h3", "blockquote"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "li", "h1", "h2", "h3", "blockquote"}:
            self.parts.append("\n")


def html_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        parts = [html_to_text(item) for item in value]
        return "\n\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("content", "own_text", "text", "title", "url"):
            if value.get(key):
                return html_to_text(value[key])
        return ""
    text = str(value)
    if "<" not in text:
        return html.unescape(text).strip()
    parser = TextExtractor()
    try:
        parser.feed(text)
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\u200b", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def pick(d: Any, *paths: str, default: Any = None) -> Any:
    for path in paths:
        cur = d
        ok = True
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                ok = False
                break
            cur = cur[part]
        if ok and cur is not None:
            return cur
    return default


def stable_id(kind: str, raw: dict[str, Any]) -> str:
    value = pick(raw, "id", "url_token", "token", "target.id")
    if value is not None:
        return str(value)
    material = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((kind + "\n" + material).encode("utf-8")).hexdigest()[:24]


def normalize(kind: str, raw: dict[str, Any], parent_type: str = "", parent_id: str = "") -> dict[str, Any]:
    obj = raw
    if kind == "activity" and isinstance(raw.get("target"), dict):
        obj = raw["target"]
    title = pick(obj, "title", "question.title", "name", default="")
    content = pick(obj, "content", "detail", "excerpt", "description", default="")
    excerpt = pick(obj, "excerpt", "excerpt_title", "description", default="")
    url = pick(obj, "url", "url_token", "question.url", default="")
    if isinstance(url, str) and url.startswith("/"):
        url = "https://www.zhihu.com" + url
    author = pick(obj, "author", default={})
    if not isinstance(author, dict):
        author = {}
    if isinstance(author.get("member"), dict):
        author = author["member"]
    record = {
        "archive_type": kind,
        "id": stable_id(kind, raw),
        "title": html_to_text(title),
        "url": str(url or ""),
        "text": html_to_text(content),
        "excerpt": html_to_text(excerpt),
        "created_time": pick(obj, "created_time", "created", default=""),
        "updated_time": pick(obj, "updated_time", "updated", default=""),
        "author_name": str(pick(author, "name", default="") or ""),
        "author_token": str(pick(author, "url_token", default="") or ""),
        "comment_count": pick(obj, "comment_count", "comments_count", default=""),
        "voteup_count": pick(obj, "voteup_count", "vote_count", "like_count", default=""),
        "parent_type": parent_type,
        "parent_id": parent_id,
        "raw": raw,
    }
    return record


class ArchiveError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, unavailable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.unavailable = unavailable


def make_browser_auth(args: argparse.Namespace) -> Any:
    try:
        from browser_auth import BrowserAuth, BrowserAuthError

        return BrowserAuth(
            profile_dir=getattr(args, "browser_profile_dir", None),
            browser_executable=getattr(args, "browser_executable", None),
            login_timeout=getattr(args, "browser_login_timeout", 300.0),
        )
    except (ImportError, BrowserAuthError) as exc:
        raise ArchiveError(str(exc), unavailable=True) from exc


def resolve_auth(args: argparse.Namespace) -> tuple[str, Callable[[str], str] | None]:
    mode = getattr(args, "auth_mode", "public")
    explicit_cookie = read_cookie(getattr(args, "cookie_file", None), getattr(args, "cookie_env", "ZHIHU_COOKIE"))
    if mode == "public":
        return "", None
    if mode == "cookie":
        if not explicit_cookie:
            raise ArchiveError("--auth-mode cookie requires --cookie-file or a non-empty --cookie-env value")
        return explicit_cookie, None

    manager_holder: dict[str, Any] = {}

    def manager() -> Any:
        if "value" not in manager_holder:
            manager_holder["value"] = make_browser_auth(args)
        return manager_holder["value"]

    def refresh(probe_url: str) -> str:
        try:
            return manager().cookie_header(probe_url=probe_url or None, force_interactive=False)
        except Exception as exc:
            raise ArchiveError(str(exc), unavailable=True) from exc

    if mode == "browser":
        return refresh(""), refresh
    return explicit_cookie, refresh


def read_cookie(path: str | None, env_name: str) -> str:
    raw = ""
    if path:
        raw = Path(path).read_text(encoding="utf-8-sig").strip()
    elif os.environ.get(env_name):
        raw = os.environ[env_name].strip()
    if not raw:
        return ""
    if raw.startswith("{"):
        data = json.loads(raw)
        return str(data.get("Cookie") or data.get("cookie") or "").strip()
    lines = [line for line in raw.splitlines() if line.strip() and not line.startswith("#")]
    if lines and all("\t" in line for line in lines):
        pairs = []
        for line in lines:
            parts = line.split("\t")
            if len(parts) >= 7:
                pairs.append(f"{parts[5]}={parts[6]}")
        return "; ".join(pairs)
    return " ".join(raw.splitlines()).strip()


def cookie_jar_from_header(cookie_header: str, base_url: str) -> CookieJar:
    """Seed a stateful cookie jar from an explicit Cookie request header."""
    parsed = urlparse(base_url)
    hostname = parsed.hostname or ""
    jar = CookieJar()
    for item in cookie_header.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if not name:
            continue
        jar.set_cookie(
            Cookie(
                version=0,
                name=name,
                value=value.strip(),
                port=None,
                port_specified=False,
                domain=hostname,
                domain_specified=False,
                domain_initial_dot=False,
                path="/",
                path_specified=True,
                secure=parsed.scheme == "https",
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
        )
    return jar


def require_safe_delay(base_url: str, delay: float) -> None:
    hostname = (urlparse(base_url).hostname or "").lower()
    if (hostname == "zhihu.com" or hostname.endswith(".zhihu.com")) and delay < 1.5:
        raise ArchiveError("Real Zhihu requests require --delay of at least 1.5 seconds")


class ApiClient:
    def __init__(
        self,
        base_url: str,
        cookie: str,
        delay: float,
        retries: int,
        timeout: float,
        auth_provider: Callable[[str], str] | None = None,
    ) -> None:
        require_safe_delay(base_url, delay)
        self.base_url = base_url.rstrip("/") + "/"
        self.base_host = urlparse(self.base_url).netloc
        self.delay = max(0.0, delay)
        self.retries = max(0, retries)
        self.timeout = timeout
        self.last_request = 0.0
        self.auth_provider = auth_provider
        self._install_cookie_header(cookie)

    def _install_cookie_header(self, cookie: str) -> None:
        self.cookie = cookie
        self.cookie_jar = cookie_jar_from_header(cookie, self.base_url)
        self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))

    def _refresh_auth(self, url: str) -> bool:
        if not self.auth_provider:
            return False
        cookie = self.auth_provider(url)
        if not cookie:
            return False
        self._install_cookie_header(cookie)
        return True

    def _sleep(self) -> None:
        elapsed = time.monotonic() - self.last_request
        target = self.delay + random.uniform(0, min(0.5, self.delay / 4 if self.delay else 0))
        if elapsed < target:
            time.sleep(target - elapsed)

    def get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = path_or_url if path_or_url.startswith(("http://", "https://")) else urljoin(self.base_url, path_or_url)
        if params:
            sep = "&" if "?" in url else "?"
            url += sep + urlencode(params)
        if urlparse(url).netloc != self.base_host:
            raise ArchiveError(f"Refusing cross-host pagination URL: {url}")
        attempt = 0
        auth_refresh_attempted = False
        while attempt <= self.retries:
            headers = {
                "Accept": "application/json, text/plain, */*",
                "User-Agent": platform_user_agent(),
                "Referer": "https://www.zhihu.com/",
            }
            self._sleep()
            req = Request(url, headers=headers, method="GET")
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    self.last_request = time.monotonic()
                    body = response.read().decode("utf-8", errors="replace")
                    ctype = response.headers.get("Content-Type", "")
                    if "json" not in ctype and body.lstrip().startswith("<"):
                        if not auth_refresh_attempted:
                            auth_refresh_attempted = True
                        else:
                            raise ArchiveError("HTML/security-verification response received", response.status, True)
                        if self._refresh_auth(url):
                            continue
                        raise ArchiveError("HTML/security-verification response received", response.status, True)
                    data = json.loads(body)
                    if not isinstance(data, dict):
                        raise ArchiveError("Expected a JSON object")
                    return data
            except HTTPError as exc:
                self.last_request = time.monotonic()
                body = exc.read(1000).decode("utf-8", errors="replace")
                if exc.code in {401, 403} and not auth_refresh_attempted:
                    auth_refresh_attempted = True
                    if self._refresh_auth(url):
                        continue
                if exc.code in {401, 403, 404}:
                    raise ArchiveError(f"HTTP {exc.code}: endpoint unavailable", exc.code, True) from exc
                if exc.code == 429:
                    if attempt >= self.retries:
                        raise ArchiveError("HTTP 429: rate limited", 429, True) from exc
                    retry_after = exc.headers.get("Retry-After")
                    time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else max(5.0, self.delay * 3))
                    attempt += 1
                    continue
                if attempt >= self.retries:
                    raise ArchiveError(f"HTTP {exc.code}: {body[:300]}", exc.code) from exc
                attempt += 1
            except (URLError, TimeoutError, IncompleteRead, json.JSONDecodeError) as exc:
                self.last_request = time.monotonic()
                if attempt >= self.retries:
                    raise ArchiveError(f"Transport/JSON error: {exc}") from exc
                time.sleep(min(8.0, 1.5 * (2**attempt)))
                attempt += 1
        raise ArchiveError("Request failed")


def iter_pages(client: ApiClient, path: str, params: dict[str, Any], max_items: int) -> Iterable[dict[str, Any]]:
    params = dict(params)
    params.setdefault("limit", min(20, max_items) if max_items else 20)
    params.setdefault("offset", 0)
    next_url: str | None = path
    first = True
    yielded = 0
    seen_urls: set[str] = set()
    while next_url:
        data = client.get(next_url, params if first else None)
        first = False
        rows = data.get("data", [])
        if not isinstance(rows, list):
            raise ArchiveError("Endpoint data is not a list")
        for row in rows:
            if isinstance(row, dict):
                yield row
                yielded += 1
                if max_items and yielded >= max_items:
                    return
        paging = data.get("paging") or {}
        if paging.get("is_end", not rows):
            return
        candidate = paging.get("next")
        if not candidate:
            return
        if candidate in seen_urls:
            raise ArchiveError("Pagination loop detected")
        seen_urls.add(candidate)
        next_url = candidate


def iter_pages_fallback(client: ApiClient, paths: Iterable[str], params: dict[str, Any], max_items: int) -> Iterable[dict[str, Any]]:
    last_error: ArchiveError | None = None
    for path in paths:
        yielded = False
        try:
            for row in iter_pages(client, path, params, max_items):
                yielded = True
                yield row
            return
        except ArchiveError as exc:
            if yielded or not exc.unavailable:
                raise
            last_error = exc
    if last_error:
        raise last_error


def load_existing(path: Path) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    records: list[dict[str, Any]] = []
    keys: set[tuple[str, str]] = set()
    if not path.exists():
        return records, keys
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                key = (str(record["archive_type"]), str(record["id"]))
            except Exception as exc:
                raise ArchiveError(f"Invalid existing JSONL at line {line_no}: {exc}") from exc
            if key not in keys:
                records.append(record)
                keys.add(key)
    return records, keys


def _decode_jsonl_record(line: str, line_number: int, path: Path) -> dict[str, Any]:
    try:
        record = json.loads(line)
        if not isinstance(record, dict):
            raise TypeError("record is not an object")
        archive_type = record["archive_type"]
        record_id = record["id"]
        if archive_type in (None, "") or record_id in (None, ""):
            raise ValueError("archive_type and id must be non-empty")
    except Exception as exc:
        raise ArchiveError(f"Invalid JSONL {path} at line {line_number}: {exc}") from exc
    return record


def iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    """Yield normalized JSONL records with their physical source line numbers."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            yield line_number, _decode_jsonl_record(line, line_number, path)


def verify_jsonl(path: Path) -> dict[str, Any]:
    """Inspect an archive without stopping at malformed or duplicate records."""
    path = Path(path)
    keys: set[tuple[str, str]] = set()
    valid_records = 0
    bad_lines = 0
    duplicate_keys = 0
    counts_by_type: dict[str, int] = {}
    unique_counts_by_type: dict[str, int] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = _decode_jsonl_record(line, line_number, path)
            except ArchiveError:
                bad_lines += 1
                continue
            archive_type = str(record["archive_type"])
            key = (archive_type, str(record["id"]))
            valid_records += 1
            counts_by_type[archive_type] = counts_by_type.get(archive_type, 0) + 1
            if key in keys:
                duplicate_keys += 1
            else:
                keys.add(key)
                unique_counts_by_type[archive_type] = unique_counts_by_type.get(archive_type, 0) + 1
    return {
        "valid_records": valid_records,
        "bad_lines": bad_lines,
        "duplicate_keys": duplicate_keys,
        "counts_by_type": counts_by_type,
        "unique_counts_by_type": unique_counts_by_type,
    }


def repair_jsonl_tail(path: Path) -> bool:
    """Back up and remove one interrupted, non-newline-terminated JSONL tail."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        handle.seek(end - 1)
        if handle.read(1) == b"\n":
            return False
        position = end
        last_newline = -1
        while position > 0 and last_newline < 0:
            size = min(8192, position)
            position -= size
            handle.seek(position)
            block = handle.read(size)
            found = block.rfind(b"\n")
            if found >= 0:
                last_newline = position + found
        tail_start = last_newline + 1
        handle.seek(tail_start)
        tail = handle.read()
    try:
        json.loads(tail.decode("utf-8"))
        return False
    except (UnicodeError, json.JSONDecodeError):
        backup = path.with_name(
            f"{path.name}.corrupt-tail-backup-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}"
        )
        shutil.copy2(path, backup)
        with path.open("r+b") as handle:
            handle.truncate(tail_start)
        return True


class JsonlWriter:
    """Append-only JSONL writer whose durable append precedes index commit."""

    def __init__(self, path: Path, state: Any) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state = state
        self.handle = self.path.open("ab")
        self.hot_keys: set[tuple[str, str]] = set()
        self.needs_separator = self.path.stat().st_size > 0
        if self.needs_separator:
            with self.path.open("rb") as existing:
                existing.seek(-1, os.SEEK_END)
                self.needs_separator = existing.read(1) != b"\n"

    def append(self, record: dict[str, Any]) -> bool:
        archive_type = str(record["archive_type"])
        record_id = str(record["id"])
        key = (archive_type, record_id)
        if key in self.hot_keys or self.state.has_record(archive_type, record_id):
            self.hot_keys.add(key)
            return False
        if self.needs_separator:
            self.handle.write(b"\n")
            self.handle.flush()
            self.needs_separator = False
        offset = self.handle.tell()
        encoded = (
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        self.handle.write(encoded)
        self.handle.flush()
        root_id = str(record.get("root_id") or record.get("reply_comment_id") or "")
        self.state.upsert_record(
            archive_type,
            record_id,
            str(record.get("parent_type") or ""),
            str(record.get("parent_id") or ""),
            root_id,
            int(record.get("child_comment_count") or 0),
            offset,
        )
        self.hot_keys.add(key)
        return True

    def close(self) -> None:
        if not self.handle.closed:
            self.handle.close()

    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def atomic_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)


def add_record(record: dict[str, Any], records: list[dict[str, Any]], keys: set[tuple[str, str]], handle: Any) -> bool:
    key = (record["archive_type"], str(record["id"]))
    if key in keys:
        return False
    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    handle.flush()
    records.append(record)
    keys.add(key)
    return True


def child_comments(client: ApiClient, root: dict[str, Any], max_items: int) -> Iterable[dict[str, Any]]:
    root_id = root.get("id")
    embedded = root.get("child_comments") or []
    seen: set[str] = set()
    for row in embedded if isinstance(embedded, list) else []:
        if isinstance(row, dict):
            seen.add(str(row.get("id")))
            yield row
    expected = int(root.get("child_comment_count") or len(seen))
    if root_id and expected > len(seen):
        paths = (
            f"comment_v5/comment/{root_id}/child_comment",
            f"comments/{root_id}/child_comments",
        )
        for row in iter_pages_fallback(client, paths, {}, max_items):
            rid = str(row.get("id"))
            if rid not in seen:
                seen.add(rid)
                yield row


CSV_FIELDS = [
    "archive_type", "id", "title", "url", "text", "excerpt", "created_time", "updated_time",
    "author_name", "author_token", "comment_count", "voteup_count", "parent_type", "parent_id",
]


def export_csv_stream(path: Path, records_path: Path) -> None:
    """Regenerate CSV one JSONL record at a time and atomically publish it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            seen: set[tuple[str, str]] = set()
            for _, record in iter_jsonl(records_path):
                key = (str(record["archive_type"]), str(record["id"]))
                if key in seen:
                    continue
                seen.add(key)
                writer.writerow(record)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _markdown_record(record: dict[str, Any]) -> str:
    kind = str(record["archive_type"])
    heading = record.get("title") or record.get("excerpt") or f"{kind} {record['id']}"
    heading = re.sub(r"\s+", " ", str(heading)).strip()[:160]
    lines = [f"## {heading}", ""]
    meta = []
    if record.get("url"):
        meta.append(f"[原文]({record['url']})")
    if record.get("created_time") != "":
        meta.append(f"created: {record['created_time']}")
    if record.get("parent_id"):
        meta.append(f"parent: {record['parent_type']} {record['parent_id']}")
    if meta:
        lines.extend([" | ".join(meta), ""])
    text = record.get("text") or record.get("excerpt") or ""
    lines.extend([str(text).strip(), "", "---", ""])
    return "\n".join(lines)


def export_markdown_stream(directory: Path, records_path: Path) -> None:
    """Spool Markdown bodies by type, then add count headers and publish atomically."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    spool_handles: dict[str, Any] = {}
    spool_paths: dict[str, Path] = {}
    counts: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    try:
        for _, record in iter_jsonl(records_path):
            kind = str(record["archive_type"])
            key = (kind, str(record["id"]))
            if key in seen:
                continue
            seen.add(key)
            if kind not in spool_handles:
                handle = tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", newline="\n", prefix=f".{kind}.", suffix=".spool",
                    dir=directory, delete=False,
                )
                spool_handles[kind] = handle
                spool_paths[kind] = Path(handle.name)
                counts[kind] = 0
            if counts[kind]:
                spool_handles[kind].write("\n")
            spool_handles[kind].write(_markdown_record(record))
            counts[kind] += 1
        for handle in spool_handles.values():
            handle.close()
        for kind, count in counts.items():
            destination = directory / f"{kind}.md"
            temporary = directory / f".{kind}.{time.time_ns()}.tmp"
            try:
                with temporary.open("w", encoding="utf-8") as target:
                    target.write(f"# {kind} ({count})\n\n")
                    with spool_paths[kind].open("r", encoding="utf-8") as source:
                        shutil.copyfileobj(source, target)
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
    finally:
        for handle in spool_handles.values():
            if not handle.closed:
                handle.close()
        for spool_path in spool_paths.values():
            if spool_path.exists():
                spool_path.unlink()


def export_csv(path: Path, records: list[dict[str, Any]]) -> None:
    """Compatibility helper for callers that already own a small in-memory list."""
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def export_markdown(directory: Path, records: list[dict[str, Any]]) -> None:
    """Compatibility helper retaining the historical list-based interface."""
    with tempfile.TemporaryDirectory() as tmp:
        records_path = Path(tmp) / "records.jsonl"
        atomic_jsonl(records_path, records)
        export_markdown_stream(directory, records_path)


def resolve_token(value: str) -> str:
    value = value.strip().rstrip("/")
    match = re.search(r"zhihu\.com/people/([^/?#]+)", value)
    return match.group(1) if match else value


def parse_expected_counts(value: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in (part.strip() for part in value.split(",")):
        if not item:
            continue
        if "=" not in item:
            raise ArchiveError(f"Invalid expected count {item!r}; use category=number")
        name, raw_count = item.split("=", 1)
        if name not in ENDPOINTS:
            raise ArchiveError(f"Unknown expected-count category: {name}")
        result[name] = int(raw_count)
    return result


def run_archive(args: argparse.Namespace) -> int:
    from archive_state import ArchiveState
    from comment_pipeline import AdaptiveCommentPipeline, CommentOptions

    require_safe_delay(args.base_url, args.delay)

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    jsonl = output / "records.jsonl"
    if jsonl.exists() and not args.resume:
        raise ArchiveError(f"{jsonl} exists; use --resume or choose a new output directory")
    repaired_tail = repair_jsonl_tail(jsonl)
    state_path = Path(getattr(args, "state_db", None) or output / "archive_state.sqlite3").resolve()
    state_existed = state_path.exists()
    records, keys = load_existing(jsonl) if args.resume else ([], set())
    existing_by_key = {(record["archive_type"], str(record["id"])): record for record in records}
    cookie, auth_provider = resolve_auth(args)
    client = ApiClient(args.base_url, cookie, args.delay, args.retries, args.timeout, auth_provider)
    token = resolve_token(args.user)
    requested = [x.strip() for x in args.types.split(",") if x.strip()]
    expected_counts = parse_expected_counts(args.expected_counts)
    start_offsets = parse_expected_counts(args.start_offsets)
    invalid = [x for x in requested if x not in ENDPOINTS]
    if invalid:
        raise ArchiveError(f"Unknown --types values: {', '.join(invalid)}")
    if not args.resume and state_existed:
        for candidate in (state_path, Path(str(state_path) + "-wal"), Path(str(state_path) + "-shm")):
            if candidate.exists():
                candidate.unlink()
        state_existed = False
    state = ArchiveState(state_path)
    if jsonl.exists() and (args.resume or repaired_tail or not state_existed):
        try:
            state.rebuild_from_jsonl(jsonl)
        except ValueError as exc:
            state.close()
            raise ArchiveError(str(exc)) from exc
    manifest: dict[str, Any] = {
        "version": VERSION,
        "started_at": int(time.time()),
        "target": {"input": args.user, "url_token": token, "profile_url": f"https://www.zhihu.com/people/{token}"},
        "requested_types": requested,
        "content_comments": args.content_comments,
        "authentication": {"mode": getattr(args, "auth_mode", "public"), "browser_fallback": auth_provider is not None},
        "expected_counts": expected_counts,
        "start_offsets": start_offsets,
        "state_db": str(state_path),
        "categories": {},
        "complete": False,
    }
    collected_for_comments: list[dict[str, Any]] = []
    try:
        with JsonlWriter(jsonl, state) as writer:
            for requested_type in requested:
                kind, endpoint, params = ENDPOINTS[requested_type]
                params = dict(params)
                if requested_type in start_offsets:
                    params["offset"] = start_offsets[requested_type]
                category = {"status": "complete", "fetched": 0, "added": 0, "error": ""}
                manifest["categories"][requested_type] = category
                try:
                    for raw in iter_pages(client, endpoint.format(token=token), params, args.max_items_per_type):
                        category["fetched"] += 1
                        record = normalize(kind, raw)
                        fresh_parent = record
                        record_key = (record["archive_type"], str(record["id"]))
                        if writer.append(record):
                            category["added"] += 1
                            records.append(record)
                            keys.add(record_key)
                            existing_by_key[record_key] = record
                        else:
                            record = existing_by_key.get(record_key, record)
                        if kind in COMMENT_ENDPOINTS:
                            collected_for_comments.append(fresh_parent)
                    if args.max_items_per_type and category["fetched"] >= args.max_items_per_type:
                        category["status"] = "partial"
                        category["coverage_warning"] = "Capped by --max-items-per-type."
                    if requested_type == "activities" and category["fetched"] == 0:
                        category["status"] = "partial"
                        category["coverage_warning"] = "A zero-row activity feed does not prove there is no historical activity."
                    if requested_type == "comments-authored":
                        category["coverage_warning"] = "Zero results do not prove the user authored no comments."
                        category["status"] = "partial"
                except ArchiveError as exc:
                    if exc.status == 429:
                        category["status"] = "partial" if category["fetched"] else "failed"
                        category["error"] = str(exc)
                        category["http_status"] = exc.status
                        atomic_json(output / "manifest.json", manifest)
                        raise
                    if category["fetched"]:
                        category["status"] = "partial"
                    else:
                        category["status"] = "unavailable" if exc.unavailable else "failed"
                    category["error"] = str(exc)
                    category["http_status"] = exc.status
                atomic_json(output / "manifest.json", manifest)

            if args.content_comments == "all":
                category = {
                    "status": "complete", "fetched": 0, "added": 0, "parents_attempted": 0,
                    "parents_skipped_existing": 0, "parents_refreshed_existing": 0,
                    "parents_resumed_partial": 0,
                    "parents_skipped_zero": 0, "errors": [],
                    "progress": {
                        "current_stage": "", "page_count": 0,
                        "roots_skipped_known": 0, "records_added": 0,
                    },
                    "performance": {
                        "pages_requested": 0,
                        "records_returned": 0,
                        "records_added": 0,
                        "roots_seen": 0,
                        "roots_skipped_known": 0,
                        "child_trees_skipped_complete": 0,
                        "child_tree_traversals": 0,
                        "child_pages_requested": 0,
                        "parents_completed_by_count": 0,
                        "parents_partial_after_all_stages": 0,
                        "endpoint_http_status_counts": {},
                        "elapsed_seconds_by_stage": {},
                        "mismatch_count": 0,
                        "missing_sum": 0,
                        "top_mismatches": [],
                    },
                }
                manifest["categories"]["content-comments"] = category
                existing_comment_parents = {
                    (str(record.get("parent_type") or ""), str(record.get("parent_id") or ""))
                    for record in records if record.get("archive_type") == "comment_received"
                }
                unique_parents: dict[tuple[str, str], dict[str, Any]] = {}
                for parent in collected_for_comments:
                    ptype = parent["archive_type"]
                    if ptype not in COMMENT_ENDPOINTS:
                        continue
                    parent_key = (str(ptype), str(parent["id"]))
                    if parent_key in unique_parents:
                        category["parents_skipped_duplicate"] = category.get("parents_skipped_duplicate", 0) + 1
                        continue
                    unique_parents[parent_key] = parent
                parents = sorted(
                    unique_parents.values(),
                    key=lambda parent: (int(parent.get("comment_count") or 0), str(parent.get("id") or "")),
                )
                strategy = getattr(args, "comment_strategy", "adaptive")
                common_options = dict(
                    legacy_fallback=getattr(args, "legacy_fallback", "auto"),
                    legacy_root_threshold=getattr(args, "legacy_root_threshold", 1),
                    max_comments_per_parent=args.max_comments_per_parent,
                    checkpoint_every_page=getattr(args, "checkpoint_every_page", True),
                )

                def write_comment(record: dict[str, Any]) -> bool:
                    added = writer.append(record)
                    if added:
                        records.append(record)
                        key = (str(record["archive_type"]), str(record["id"]))
                        keys.add(key)
                        existing_by_key[key] = record
                    return added

                performance = category["performance"]
                numeric_metrics = (
                    "pages_requested", "records_returned", "records_added", "roots_seen",
                    "roots_skipped_known", "child_trees_skipped_complete", "child_tree_traversals",
                    "child_pages_requested",
                )

                def add_metrics(parent: dict[str, Any], result: dict[str, Any], final: bool) -> None:
                    ptype = str(parent["archive_type"])
                    for metric in numeric_metrics:
                        performance[metric] += int(result.get(metric, 0))
                    for status, count in result.get("endpoint_http_status_counts", {}).items():
                        performance["endpoint_http_status_counts"][status] = (
                            performance["endpoint_http_status_counts"].get(status, 0) + count
                        )
                    for stage, elapsed in result.get("elapsed_seconds_by_stage", {}).items():
                        performance["elapsed_seconds_by_stage"][stage] = round(
                            performance["elapsed_seconds_by_stage"].get(stage, 0.0) + float(elapsed), 6
                        )
                    if final:
                        expected = int(result.get("expected", 0))
                        archived = int(result.get("archived", 0))
                        if result.get("status") == "complete" and archived == expected:
                            performance["parents_completed_by_count"] += 1
                        else:
                            performance["parents_partial_after_all_stages"] += 1
                            category["status"] = "partial"
                        if archived != expected:
                            category["status"] = "partial"
                            performance["mismatch_count"] += 1
                            missing = max(0, expected - archived)
                            performance["missing_sum"] += missing
                            performance["top_mismatches"].append({
                                "parent_type": ptype,
                                "parent_id": str(parent["id"]),
                                "expected": expected,
                                "archived": archived,
                                "missing": missing,
                                "stage": str(result.get("stage") or ""),
                                "status": str(result.get("status") or ""),
                                "title": str(parent.get("title") or ""),
                            })
                            performance["top_mismatches"].sort(
                                key=lambda item: (
                                    -int(item["missing"]),
                                    item["parent_type"],
                                    item["parent_id"],
                                )
                            )
                            del performance["top_mismatches"][50:]
                    category["fetched"] = performance["records_returned"]
                    category["added"] = performance["records_added"]
                    category["progress"] = {
                        "current_stage": str(result.get("stage") or ""),
                        "page_count": performance["pages_requested"],
                        "roots_skipped_known": performance["roots_skipped_known"],
                        "records_added": performance["records_added"],
                    }
                    if result.get("errors"):
                        error_entry = {
                            "parent_type": ptype, "parent_id": parent["id"],
                            "errors": list(result["errors"]),
                            "error": str(result["errors"][0]),
                        }
                        status_counts = result.get("endpoint_http_status_counts", {})
                        if len(status_counts) == 1:
                            status_value = next(iter(status_counts))
                            if str(status_value).isdigit():
                                error_entry["http_status"] = int(status_value)
                        category["errors"].append(error_entry)

                def add_failed_parent(
                    parent: dict[str, Any], exc: ArchiveError
                ) -> None:
                    category["status"] = "partial"
                    manifest["complete"] = False
                    failed_metrics = getattr(exc, "comment_metrics", None)
                    if isinstance(failed_metrics, dict):
                        add_metrics(parent, failed_metrics, True)
                        return
                    performance["parents_partial_after_all_stages"] += 1
                    error_entry = {
                        "parent_type": str(parent["archive_type"]),
                        "parent_id": parent["id"],
                        "error": str(exc),
                    }
                    if exc.status is not None:
                        error_entry["http_status"] = exc.status
                    category["errors"].append(error_entry)

                eligible: list[dict[str, Any]] = []
                for parent in parents:
                    ptype = str(parent["archive_type"])
                    parent_key = (ptype, str(parent["id"]))
                    expected = int(parent.get("comment_count") or 0)
                    archived = state.archived_parent_count(ptype, str(parent["id"]))
                    task = state.load_parent_task(ptype, str(parent["id"]))
                    confirmed_complete = bool(
                        task is not None and task.status == "complete" and archived == expected
                    )
                    if args.resume and confirmed_complete and not args.refresh_existing_comments:
                        category["parents_skipped_existing"] += 1
                        continue
                    if args.resume and parent_key in existing_comment_parents and args.refresh_existing_comments:
                        category["parents_refreshed_existing"] += 1
                    elif args.resume and archived:
                        category["parents_resumed_partial"] += 1
                    if parent.get("comment_count") in {0, "0"}:
                        category["parents_skipped_zero"] += 1
                        continue
                    category["parents_attempted"] += 1
                    eligible.append(parent)

                first_pipeline = AdaptiveCommentPipeline(
                    client, state, write_comment, CommentOptions(strategy="single-pass", **common_options),
                    normalize_func=normalize, error_type=ArchiveError,
                )
                later: list[dict[str, Any]] = []
                for parent in eligible:
                    ptype = str(parent["archive_type"])
                    try:
                        result = first_pipeline.run_parent(parent)
                        final = strategy == "single-pass" or (
                            strategy == "adaptive" and result.get("status") in {"complete", "capped"}
                        )
                        add_metrics(parent, result, final)
                        if not final:
                            later.append(parent)
                    except ArchiveError as exc:
                        add_failed_parent(parent, exc)
                        if exc.status == 429:
                            atomic_json(output / "manifest.json", manifest)
                            raise
                    atomic_json(output / "manifest.json", manifest)

                later.sort(
                    key=lambda parent: (
                        -(int(parent.get("comment_count") or 0) - state.archived_parent_count(
                            str(parent["archive_type"]), str(parent["id"])
                        )),
                        str(parent["id"]),
                    )
                )
                if later:
                    later_pipeline = AdaptiveCommentPipeline(
                        client, state, write_comment, CommentOptions(strategy=strategy, **common_options),
                        normalize_func=normalize, error_type=ArchiveError,
                    )
                    for parent in later:
                        ptype = str(parent["archive_type"])
                        try:
                            result = later_pipeline.run_parent(parent)
                            add_metrics(parent, result, True)
                        except ArchiveError as exc:
                            add_failed_parent(parent, exc)
                            if exc.status == 429:
                                atomic_json(output / "manifest.json", manifest)
                                raise
                        atomic_json(output / "manifest.json", manifest)
    finally:
        state.close()

    verification = verify_jsonl(jsonl)
    export_csv_stream(output / "records.csv", jsonl)
    export_markdown_stream(output / "markdown", jsonl)
    manifest["finished_at"] = int(time.time())
    manifest["counts_by_type"] = verification["unique_counts_by_type"]
    manifest["total_unique_records"] = sum(manifest["counts_by_type"].values())
    manifest["record_verification"] = verification
    for requested_type, expected in expected_counts.items():
        if requested_type not in manifest["categories"]:
            continue
        kind = ENDPOINTS[requested_type][0]
        actual = manifest["counts_by_type"].get(kind, 0)
        category = manifest["categories"][requested_type]
        category["expected_visible_count"] = expected
        category["archived_unique_count"] = actual
        if actual != expected and category.get("status") not in {"unavailable", "failed"}:
            category["status"] = "partial"
            category["coverage_warning"] = f"Visible count is {expected}, but archive contains {actual} unique records."
    manifest["complete"] = all(c.get("status") == "complete" for c in manifest["categories"].values())
    atomic_json(output / "manifest.json", manifest)
    print(json.dumps({
        "output": str(output), "records": manifest["total_unique_records"],
        "complete": manifest["complete"], "record_verification": verification,
    }, ensure_ascii=False))
    return 0 if manifest["complete"] else 2


def self_test() -> int:
    routes: dict[str, dict[str, Any]] = {
        "/api/v4/members/test/answers": {
            "data": [{"id": 1, "content": "<p>Hello <b>world</b></p>", "question": {"title": "Q"}, "author": {"name": "Tester", "url_token": "test"}}],
            "paging": {"is_end": True},
        },
        "/api/v4/members/test/articles": {"data": [], "paging": {"is_end": True}},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            payload = routes.get(path)
            if payload is None:
                self.send_response(404)
                self.end_headers()
                return
            required_cookie = str(payload.get("required_cookie") or "")
            if required_cookie and required_cookie not in (self.headers.get("Cookie") or ""):
                self.send_response(403)
                self.end_headers()
                return
            response_payload = {
                key: value for key, value in payload.items() if key not in {"required_cookie", "set_cookie"}
            }
            body = json.dumps(response_payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if payload.get("set_cookie"):
                self.send_header("Set-Cookie", str(payload["set_cookie"]))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            ns = argparse.Namespace(
                user="test", output=tmp, types="answers,articles", content_comments="none", cookie_file=None,
                cookie_env="ZHIHU_COOKIE", base_url=f"http://127.0.0.1:{server.server_port}/api/v4/", delay=0,
                retries=0, timeout=5, max_items_per_type=0, max_comments_per_parent=0, resume=False,
                refresh_existing_comments=False,
                expected_counts="",
                start_offsets="",
                auth_mode="auto", browser_profile_dir=None, browser_executable="__must_not_be_loaded__",
                browser_login_timeout=300,
            )
            code = run_archive(ns)
            manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
            rows = (Path(tmp) / "records.jsonl").read_text(encoding="utf-8").splitlines()
            assert code == 0 and manifest["complete"] and len(rows) == 1
            assert json.loads(rows[0])["text"] == "Hello world"
            auth_calls: list[str] = []

            def auth_provider(url: str) -> str:
                auth_calls.append(url)
                return f"z_c0=test-{len(auth_calls)}"

            routes["/api/v4/members/auth-test-1/answers"] = {
                "data": [{"id": 2, "content": "Authenticated"}],
                "paging": {"is_end": True},
                "required_cookie": "z_c0=test-1",
            }
            routes["/api/v4/members/auth-test-2/answers"] = {
                "data": [{"id": 3, "content": "Reauthenticated"}],
                "paging": {"is_end": True},
                "required_cookie": "z_c0=test-2",
            }
            auth_client = ApiClient(
                f"http://127.0.0.1:{server.server_port}/api/v4/", "", 0, 0, 5, auth_provider
            )
            auth_data = auth_client.get("members/auth-test-1/answers")
            reauth_data = auth_client.get("members/auth-test-2/answers")
            assert auth_data["data"][0]["id"] == 2
            assert reauth_data["data"][0]["id"] == 3 and len(auth_calls) == 2

            routes["/api/v4/cookie-seed"] = {
                "ok": True,
                "set_cookie": "BEC=fresh; Path=/; HttpOnly",
            }
            routes["/api/v4/cookie-check"] = {
                "ok": True,
                "required_cookie": "BEC=fresh",
            }
            cookie_client = ApiClient(
                f"http://127.0.0.1:{server.server_port}/api/v4/", "BEC=stale", 0, 0, 5
            )
            cookie_client.get("cookie-seed")
            assert cookie_client.get("cookie-check")["ok"] is True

            from browser_auth import browser_candidates, default_profile_dir

            mac_home = Path("/Users/tester")
            assert default_profile_dir("darwin", mac_home, {}) == (
                mac_home / "Library" / "Application Support" / "Codex" /
                "zhihu-user-archive" / "browser-profile"
            )
            mac_candidates = {path.as_posix() for path in browser_candidates("darwin", mac_home, {})}
            assert "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" in mac_candidates
            assert "/Users/tester/Applications/Chromium.app/Contents/MacOS/Chromium" in mac_candidates
            assert "Macintosh" in platform_user_agent("darwin")
        print("SELF-TEST OK")
        return 0
    finally:
        server.shutdown()
        server.server_close()


def rebuild_exports(output_value: str) -> int:
    output = Path(output_value).resolve()
    records_path = output / "records.jsonl"
    verification = verify_jsonl(records_path)
    export_csv_stream(output / "records.csv", records_path)
    export_markdown_stream(output / "markdown", records_path)
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = {"version": VERSION, "categories": {}}
    counts = verification["unique_counts_by_type"]
    manifest["finished_at"] = int(time.time())
    manifest["total_unique_records"] = sum(counts.values())
    manifest["counts_by_type"] = counts
    manifest["record_verification"] = verification
    manifest["complete"] = False
    manifest["coverage_warning"] = "Exports rebuilt after an interrupted incremental run; review category gaps before claiming completeness."
    atomic_json(manifest_path, manifest)
    print(json.dumps({
        "output": str(output), "records": manifest["total_unique_records"], "rebuilt": True,
        "record_verification": verification,
    }, ensure_ascii=False))
    return 0


def rebuild_state_index(output_value: str, state_db_value: str | None = None) -> int:
    from archive_state import ArchiveState

    output = Path(output_value).resolve()
    jsonl = output / "records.jsonl"
    if not jsonl.exists():
        raise ArchiveError(f"Missing {jsonl}")
    repair_jsonl_tail(jsonl)
    state_path = Path(state_db_value or output / "archive_state.sqlite3").resolve()
    state = ArchiveState(state_path)
    try:
        try:
            count = state.rebuild_from_jsonl(jsonl)
        except ValueError as exc:
            raise ArchiveError(str(exc)) from exc
    finally:
        state.close()
    print(json.dumps({
        "output": str(output), "state_db": str(state_path), "rebuilt_record_count": count,
    }, ensure_ascii=False))
    return 0


def enrich_details(args: argparse.Namespace) -> int:
    require_safe_delay(args.base_url, args.delay)
    require_safe_delay(ARTICLE_DETAIL_BASE_URL, args.delay)
    output = Path(args.output).resolve()
    jsonl_path = output / "records.jsonl"
    records, _ = load_existing(jsonl_path)
    requested = {x.strip() for x in args.types.split(",") if x.strip()}
    supported = {"answers": "answer", "articles": "article"}
    selected = {plural: kind for plural, kind in supported.items() if plural in requested}
    if not selected:
        raise ArchiveError("--enrich-details requires --types answers,articles or either category")

    cookie, auth_provider = resolve_auth(args)
    answer_client = ApiClient(args.base_url, cookie, args.delay, args.retries, args.timeout, auth_provider)
    article_client = ApiClient(ARTICLE_DETAIL_BASE_URL, cookie, args.delay, args.retries, args.timeout, auth_provider)
    sidecar = output / "details.jsonl"
    detail_records, detail_keys = load_existing(sidecar)
    detail_map = {(r["archive_type"], str(r["id"])): r for r in detail_records}
    targets = [r for r in records if r.get("archive_type") in selected.values()]
    if args.max_items_per_type:
        capped: list[dict[str, Any]] = []
        per_kind: dict[str, int] = {}
        for record in targets:
            kind = str(record["archive_type"])
            if per_kind.get(kind, 0) < args.max_items_per_type:
                capped.append(record)
                per_kind[kind] = per_kind.get(kind, 0) + 1
        targets = capped

    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"version": VERSION}
    progress: dict[str, Any] = {
        "status": "in_progress", "targets": len(targets), "fetched": 0,
        "skipped_existing": 0, "errors": [], "started_at": int(time.time()),
    }
    manifest["detail_enrichment"] = progress
    atomic_json(manifest_path, manifest)

    consecutive_unavailable = 0
    with sidecar.open("a", encoding="utf-8", newline="\n") as handle:
        for original in targets:
            kind = str(original["archive_type"])
            key = (kind, str(original["id"]))
            original_content = (original.get("raw") or {}).get("content")
            if original_content not in (None, "", []):
                progress["skipped_existing"] += 1
                continue
            if key in detail_keys:
                progress["skipped_existing"] += 1
                continue
            try:
                if kind == "answer":
                    raw = answer_client.get(
                        f"answers/{original['id']}",
                        {"include": "content,excerpt,question,author,created_time,updated_time,comment_count"},
                    )
                else:
                    raw = article_client.get(f"articles/{original['id']}")
                if raw.get("content") in (None, "", []):
                    raise ArchiveError("Detail response did not contain content")
                enriched = normalize(kind, raw)
                if not enriched.get("title"):
                    enriched["title"] = original.get("title", "")
                if not enriched.get("url"):
                    enriched["url"] = original.get("url", "")
                add_record(enriched, detail_records, detail_keys, handle)
                detail_map[key] = enriched
                progress["fetched"] += 1
                consecutive_unavailable = 0
            except ArchiveError as exc:
                progress["errors"].append({"archive_type": kind, "id": original["id"], "error": str(exc), "http_status": exc.status})
                consecutive_unavailable = consecutive_unavailable + 1 if exc.unavailable else 0
                if consecutive_unavailable >= 3:
                    progress["stopped_reason"] = "Stopped after three consecutive unavailable responses."
                    break
            atomic_json(manifest_path, manifest)

    merged = [detail_map.get((str(r["archive_type"]), str(r["id"])), r) for r in records]
    atomic_jsonl(jsonl_path, merged)
    verification = verify_jsonl(jsonl_path)
    export_csv_stream(output / "records.csv", jsonl_path)
    export_markdown_stream(output / "markdown", jsonl_path)
    progress["finished_at"] = int(time.time())
    progress["enriched_total"] = sum(
        1 for r in merged
        if r.get("archive_type") in selected.values() and (r.get("raw") or {}).get("content") not in (None, "", [])
    )
    progress["status"] = "complete" if not progress["errors"] and progress["fetched"] + progress["skipped_existing"] == len(targets) else "partial"
    manifest["detail_enrichment"] = progress
    manifest["counts_by_type"] = verification["unique_counts_by_type"]
    manifest["total_unique_records"] = sum(manifest["counts_by_type"].values())
    manifest["record_verification"] = verification
    manifest["complete"] = False
    atomic_json(manifest_path, manifest)
    if sidecar.exists():
        sidecar.unlink()
    print(json.dumps({"output": str(output), "detail_enrichment": progress}, ensure_ascii=False))
    return 0 if progress["status"] == "complete" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", help="Zhihu profile URL or url_token")
    parser.add_argument("--output", help="Output directory")
    parser.add_argument("--types", default=DEFAULT_TYPES, help=f"Comma-separated categories (default: {DEFAULT_TYPES})")
    parser.add_argument("--content-comments", choices=["none", "all"], default="none")
    parser.add_argument("--comment-strategy", choices=["adaptive", "single-pass", "exhaustive"], default="adaptive")
    parser.add_argument("--checkpoint-every-page", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--state-db", help="State database path; defaults to <output>/archive_state.sqlite3")
    parser.add_argument("--legacy-fallback", choices=["auto", "always", "never"], default="auto")
    parser.add_argument("--legacy-root-threshold", type=int, default=1)
    parser.add_argument("--rebuild-state-index", action="store_true")
    parser.add_argument(
        "--auth-mode", choices=["auto", "public", "cookie", "browser"], default="auto",
        help="auto tries public/cookie auth then opens a dedicated browser after an auth challenge",
    )
    parser.add_argument("--cookie-file", help="Explicit Cookie header, JSON, or Netscape cookie text file")
    parser.add_argument("--cookie-env", default="ZHIHU_COOKIE", help="Cookie environment variable name")
    parser.add_argument("--browser-profile-dir", help="Dedicated browser profile directory (outside the archive output)")
    parser.add_argument("--browser-executable", help="Path to Edge, Chrome, or Chromium")
    parser.add_argument("--browser-login-timeout", type=float, default=300.0, help="Seconds to wait for interactive login")
    parser.add_argument("--login-browser", action="store_true", help="Open the dedicated profile and establish a Zhihu session")
    parser.add_argument("--logout-browser", action="store_true", help="Delete the marked dedicated browser profile")
    parser.add_argument("--delay", type=float, default=2.0, help="Minimum seconds between requests")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-items-per-type", type=int, default=0, help="0 means unlimited")
    parser.add_argument("--max-comments-per-parent", type=int, default=0, help="0 means unlimited")
    parser.add_argument(
        "--refresh-existing-comments", action="store_true",
        help="With --resume, revisit parents that already have archived comments and add newly exposed comments",
    )
    parser.add_argument("--expected-counts", default="", help="Visible counts, e.g. answers=351,articles=10,pins=399")
    parser.add_argument("--start-offsets", default="", help="Resume API offsets, e.g. answers=151")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=argparse.SUPPRESS)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--rebuild-exports", action="store_true", help="Rebuild CSV, Markdown, and summary from existing JSONL")
    parser.add_argument("--enrich-details", action="store_true", help="Fetch full answer/article content by archived object ID")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.legacy_root_threshold < 0:
        parser.error("--legacy-root-threshold must be >= 0")
    if args.login_browser and args.logout_browser:
        parser.error("choose only one of --login-browser and --logout-browser")
    try:
        if args.login_browser or args.logout_browser:
            manager = make_browser_auth(args)
            if args.logout_browser:
                removed = manager.clear_profile()
                print(json.dumps({"browser_profile": str(manager.profile_dir), "removed": removed}, ensure_ascii=False))
            else:
                manager.cookie_header(force_interactive=True)
                print(json.dumps({"browser_profile": str(manager.profile_dir), "authenticated": True}, ensure_ascii=False))
            return 0
        if args.self_test:
            return self_test()
        if args.rebuild_state_index:
            if not args.output:
                parser.error("--output is required with --rebuild-state-index")
            return rebuild_state_index(args.output, args.state_db)
        if args.rebuild_exports:
            if not args.output:
                parser.error("--output is required with --rebuild-exports")
            return rebuild_exports(args.output)
        if args.enrich_details:
            if not args.output:
                parser.error("--output is required with --enrich-details")
            return enrich_details(args)
        if not args.user or not args.output:
            parser.error("--user and --output are required unless a maintenance command is used")
        return run_archive(args)
    except (ArchiveError, OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
