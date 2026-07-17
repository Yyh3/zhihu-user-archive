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
import sys
import tempfile
import threading
import time
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
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
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


def export_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = [
        "archive_type", "id", "title", "url", "text", "excerpt", "created_time", "updated_time",
        "author_name", "author_token", "comment_count", "voteup_count", "parent_type", "parent_id",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def export_markdown(directory: Path, records: list[dict[str, Any]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(record["archive_type"], []).append(record)
    for kind, rows in grouped.items():
        lines = [f"# {kind} ({len(rows)})", ""]
        for row in rows:
            heading = row.get("title") or row.get("excerpt") or f"{kind} {row['id']}"
            heading = re.sub(r"\s+", " ", str(heading)).strip()[:160]
            lines.extend([f"## {heading}", ""])
            meta = []
            if row.get("url"):
                meta.append(f"[原文]({row['url']})")
            if row.get("created_time") != "":
                meta.append(f"created: {row['created_time']}")
            if row.get("parent_id"):
                meta.append(f"parent: {row['parent_type']} {row['parent_id']}")
            if meta:
                lines.extend([" | ".join(meta), ""])
            text = row.get("text") or row.get("excerpt") or ""
            lines.extend([str(text).strip(), "", "---", ""])
        (directory / f"{kind}.md").write_text("\n".join(lines), encoding="utf-8")


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
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    jsonl = output / "records.jsonl"
    if jsonl.exists() and not args.resume:
        raise ArchiveError(f"{jsonl} exists; use --resume or choose a new output directory")
    records, keys = load_existing(jsonl) if args.resume else ([], set())
    cookie, auth_provider = resolve_auth(args)
    client = ApiClient(args.base_url, cookie, args.delay, args.retries, args.timeout, auth_provider)
    token = resolve_token(args.user)
    requested = [x.strip() for x in args.types.split(",") if x.strip()]
    expected_counts = parse_expected_counts(args.expected_counts)
    start_offsets = parse_expected_counts(args.start_offsets)
    invalid = [x for x in requested if x not in ENDPOINTS]
    if invalid:
        raise ArchiveError(f"Unknown --types values: {', '.join(invalid)}")
    manifest: dict[str, Any] = {
        "version": VERSION,
        "started_at": int(time.time()),
        "target": {"input": args.user, "url_token": token, "profile_url": f"https://www.zhihu.com/people/{token}"},
        "requested_types": requested,
        "content_comments": args.content_comments,
        "authentication": {"mode": getattr(args, "auth_mode", "public"), "browser_fallback": auth_provider is not None},
        "expected_counts": expected_counts,
        "start_offsets": start_offsets,
        "categories": {},
        "complete": False,
    }
    mode = "a" if args.resume else "w"
    collected_for_comments: list[dict[str, Any]] = []
    with jsonl.open(mode, encoding="utf-8", newline="\n") as handle:
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
                    if add_record(record, records, keys, handle):
                        category["added"] += 1
                    if kind in COMMENT_ENDPOINTS:
                        collected_for_comments.append(record)
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
                "parents_skipped_zero": 0, "errors": [],
            }
            manifest["categories"]["content-comments"] = category
            existing_comment_parents = {
                (str(record.get("parent_type") or ""), str(record.get("parent_id") or ""))
                for record in records if record.get("archive_type") == "comment_received"
            }
            for parent in collected_for_comments:
                ptype = parent["archive_type"]
                endpoints = COMMENT_ENDPOINTS.get(ptype)
                if not endpoints:
                    continue
                parent_key = (str(ptype), str(parent["id"]))
                if args.resume and parent_key in existing_comment_parents and not args.refresh_existing_comments:
                    category["parents_skipped_existing"] += 1
                    continue
                if args.resume and parent_key in existing_comment_parents:
                    category["parents_refreshed_existing"] += 1
                if parent.get("comment_count") in {0, "0"}:
                    category["parents_skipped_zero"] += 1
                    continue
                category["parents_attempted"] += 1
                try:
                    paths = tuple(endpoint.format(id=parent["id"]) for endpoint in endpoints)
                    params = {"order_by": "score", "offset": ""}
                    for raw in iter_pages_fallback(client, paths, params, args.max_comments_per_parent):
                        category["fetched"] += 1
                        root = normalize("comment_received", raw, ptype, parent["id"])
                        if add_record(root, records, keys, handle):
                            category["added"] += 1
                        for child in child_comments(client, raw, args.max_comments_per_parent):
                            category["fetched"] += 1
                            child_record = normalize("comment_received", child, ptype, parent["id"])
                            if add_record(child_record, records, keys, handle):
                                category["added"] += 1
                except ArchiveError as exc:
                    category["status"] = "partial"
                    category["errors"].append({"parent_type": ptype, "parent_id": parent["id"], "error": str(exc)})
                atomic_json(output / "manifest.json", manifest)

    export_csv(output / "records.csv", records)
    export_markdown(output / "markdown", records)
    manifest["finished_at"] = int(time.time())
    manifest["total_unique_records"] = len(records)
    manifest["counts_by_type"] = {}
    for record in records:
        kind = record["archive_type"]
        manifest["counts_by_type"][kind] = manifest["counts_by_type"].get(kind, 0) + 1
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
    print(json.dumps({"output": str(output), "records": len(records), "complete": manifest["complete"]}, ensure_ascii=False))
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
    records, _ = load_existing(output / "records.jsonl")
    export_csv(output / "records.csv", records)
    export_markdown(output / "markdown", records)
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = {"version": VERSION, "categories": {}}
    counts: dict[str, int] = {}
    for record in records:
        kind = str(record["archive_type"])
        counts[kind] = counts.get(kind, 0) + 1
    manifest["finished_at"] = int(time.time())
    manifest["total_unique_records"] = len(records)
    manifest["counts_by_type"] = counts
    manifest["complete"] = False
    manifest["coverage_warning"] = "Exports rebuilt after an interrupted incremental run; review category gaps before claiming completeness."
    atomic_json(manifest_path, manifest)
    print(json.dumps({"output": str(output), "records": len(records), "rebuilt": True}, ensure_ascii=False))
    return 0


def enrich_details(args: argparse.Namespace) -> int:
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
                        {"include": "content,excerpt,question,author,created_time,updated_time"},
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
    export_csv(output / "records.csv", merged)
    export_markdown(output / "markdown", merged)
    progress["finished_at"] = int(time.time())
    progress["enriched_total"] = sum(
        1 for r in merged
        if r.get("archive_type") in selected.values() and (r.get("raw") or {}).get("content") not in (None, "", [])
    )
    progress["status"] = "complete" if not progress["errors"] and progress["fetched"] + progress["skipped_existing"] == len(targets) else "partial"
    manifest["detail_enrichment"] = progress
    counts: dict[str, int] = {}
    for record in merged:
        kind = str(record["archive_type"])
        counts[kind] = counts.get(kind, 0) + 1
    manifest["total_unique_records"] = len(merged)
    manifest["counts_by_type"] = counts
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
