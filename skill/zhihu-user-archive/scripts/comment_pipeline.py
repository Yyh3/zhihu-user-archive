from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class StageSpec:
    name: str
    endpoint_kind: str
    order_mode: str


@dataclass(frozen=True)
class CommentOptions:
    strategy: str = "adaptive"
    legacy_fallback: str = "auto"
    legacy_root_threshold: int = 1
    max_comments_per_parent: int = 0
    checkpoint_every_page: bool = True


ADAPTIVE_STAGES = (
    StageSpec("v5_score", "v5", "score"),
    StageSpec("v5_ts", "v5", "ts"),
    StageSpec("legacy_comments_normal", "legacy_comments", "normal"),
    StageSpec("legacy_comments_reverse", "legacy_comments", "reverse"),
    StageSpec("legacy_root_normal", "legacy_root_comments", "normal"),
    StageSpec("legacy_root_reverse", "legacy_root_comments", "reverse"),
)


def should_fetch_children(state: Any, root: dict[str, Any]) -> bool:
    root_id = str(root.get("id") or "")
    expected = int(root.get("child_comment_count") or 0)
    if not root_id or expected <= 0:
        return False
    return state.archived_child_count(root_id) < expected


class AdaptiveCommentPipeline:
    """Single-threaded, resumable comment collector with tree-level deduplication."""

    def __init__(
        self,
        client: Any,
        state: Any,
        writer: Callable[[dict[str, Any]], bool],
        options: CommentOptions | None = None,
        *,
        normalize_func: Callable[..., dict[str, Any]] | None = None,
        error_type: type[BaseException] | tuple[type[BaseException], ...] | None = None,
    ) -> None:
        if normalize_func is None or error_type is None:
            # Delayed import keeps archive_zhihu_user free to import this module later.
            from archive_zhihu_user import ArchiveError, normalize

            normalize_func = normalize_func or normalize
            error_type = error_type or ArchiveError
        self.client = client
        self.state = state
        self.writer = writer
        self.options = options or CommentOptions()
        self.normalize = normalize_func
        self.error_type = error_type

    @staticmethod
    def _endpoint(parent_type: str, parent_id: str, kind: str) -> str:
        plural = {"answer": "answers", "article": "articles", "pin": "pins"}.get(
            parent_type, f"{parent_type}s"
        )
        if kind == "legacy_root_comments":
            return f"{plural}/{parent_id}/root_comments"
        if kind == "legacy_comments":
            return f"{plural}/{parent_id}/comments"
        return f"comment_v5/{plural}/{parent_id}/root_comment"

    @staticmethod
    def _offset(next_url: str) -> int:
        if not next_url:
            return 0
        try:
            return int((parse_qs(urlparse(next_url).query).get("offset") or [0])[0])
        except (TypeError, ValueError):
            return 0

    def _write_indexed(
        self,
        raw: dict[str, Any],
        parent_type: str,
        parent_id: str,
        root_id: str = "",
    ) -> bool:
        record = self.normalize("comment_received", raw, parent_type, parent_id)
        record_id = str(record.get("id") or raw.get("id") or "")
        if not record_id:
            return False
        child_count = int(raw.get("child_comment_count") or 0)
        if root_id:
            record["root_id"] = root_id
            record["reply_comment_id"] = str(raw.get("reply_comment_id") or root_id)
        existed = self.state.has_record("comment_received", record_id)
        added = False
        if not existed:
            added = bool(self.writer(record))
        self.state.upsert_record(
            "comment_received",
            record_id,
            parent_type,
            parent_id,
            root_id,
            child_count,
        )
        return added

    def _save_checkpoint(
        self,
        parent_type: str,
        parent_id: str,
        stage_name: str,
        endpoint: str,
        order_mode: str,
        next_url: str,
        is_end: bool,
    ) -> None:
        if not self.options.checkpoint_every_page:
            return
        from archive_state import PageCheckpoint

        self.state.save_checkpoint(
            PageCheckpoint(
                parent_type,
                parent_id,
                stage_name,
                endpoint,
                order_mode,
                next_url,
                self._offset(next_url),
                is_end,
            )
        )

    def _fetch_child_tree(
        self,
        raw_root: dict[str, Any],
        parent_type: str,
        parent_id: str,
        stage_name: str,
        metrics: dict[str, Any],
        expected: int,
    ) -> bool:
        root_id = str(raw_root.get("id") or "")
        if not should_fetch_children(self.state, raw_root):
            if int(raw_root.get("child_comment_count") or 0) > 0:
                metrics["child_trees_skipped_complete"] += 1
            return self._stop_reached(parent_type, parent_id, expected)
        metrics["child_tree_traversals"] += 1
        endpoints = (
            f"comment_v5/comment/{root_id}/child_comment",
            f"comments/{root_id}/child_comments",
        )
        for endpoint in endpoints:
            if self.state.endpoint_status(parent_type, endpoint) == "unavailable":
                continue
            checkpoint = self.state.load_checkpoint(
                parent_type, parent_id, stage_name, endpoint, ""
            )
            if checkpoint and checkpoint.is_end:
                continue
            next_url = checkpoint.cursor if checkpoint and checkpoint.cursor else endpoint
            first = next_url == endpoint
            seen_urls: set[str] = set()
            try:
                while next_url and should_fetch_children(self.state, raw_root):
                    data = self.client.get(next_url, {} if first else None)
                    first = False
                    metrics["child_pages_requested"] += 1
                    rows = data.get("data", [])
                    if not isinstance(rows, list):
                        raise ValueError("Endpoint data is not a list")
                    metrics["records_returned"] += len(rows)
                    for child in rows:
                        if isinstance(child, dict) and self._write_indexed(
                            child, parent_type, parent_id, root_id
                        ):
                            metrics["records_added"] += 1
                        if self._stop_reached(parent_type, parent_id, expected):
                            return True
                    paging = data.get("paging") or {}
                    is_end = bool(paging.get("is_end", not rows))
                    candidate = "" if is_end else str(paging.get("next") or "")
                    self._save_checkpoint(
                        parent_type, parent_id, stage_name, endpoint, "", candidate, is_end
                    )
                    metrics["archived"] = self.state.archived_parent_count(
                        parent_type, parent_id
                    )
                    self._save_parent_metrics(metrics)
                    if is_end or not candidate:
                        break
                    if candidate in seen_urls:
                        raise ValueError("Pagination loop detected")
                    seen_urls.add(candidate)
                    next_url = candidate
                return self._stop_reached(parent_type, parent_id, expected)
            except self.error_type as exc:
                status = getattr(exc, "status", None)
                if status == 404:
                    if status is not None:
                        key = str(status)
                        metrics["endpoint_http_status_counts"][key] = (
                            metrics["endpoint_http_status_counts"].get(key, 0) + 1
                        )
                    self.state.mark_endpoint(
                        parent_type, endpoint, "unavailable", getattr(exc, "status", None), str(exc)
                    )
                    metrics["errors"].append(str(exc))
                    continue
                raise
        return self._stop_reached(parent_type, parent_id, expected)

    def _process_root(
        self,
        raw_root: dict[str, Any],
        parent_type: str,
        parent_id: str,
        stage_name: str,
        metrics: dict[str, Any],
        expected: int,
    ) -> bool:
        root_id = str(raw_root.get("id") or "")
        known = bool(root_id and self.state.has_record("comment_received", root_id))
        metrics["roots_seen"] += 1
        if known and not should_fetch_children(self.state, raw_root):
            metrics["roots_skipped_known"] += 1
        if self._write_indexed(raw_root, parent_type, parent_id):
            metrics["records_added"] += 1
        if self._stop_reached(parent_type, parent_id, expected):
            return True

        embedded = raw_root.get("child_comments") or []
        if isinstance(embedded, list):
            for child in embedded:
                if isinstance(child, dict) and self._write_indexed(
                    child, parent_type, parent_id, root_id
                ):
                    metrics["records_added"] += 1
                if self._stop_reached(parent_type, parent_id, expected):
                    return True

        # The index now contains all embedded children, so a complete tree
        # never incurs child pagination in a later overlapping stage.
        return self._fetch_child_tree(
            raw_root, parent_type, parent_id, stage_name, metrics, expected
        )

    def _expected_reached(self, parent_type: str, parent_id: str, expected: int) -> bool:
        archived = self.state.archived_parent_count(parent_type, parent_id)
        return archived >= expected

    def _cap_reached(self, parent_type: str, parent_id: str, expected: int) -> bool:
        cap = self.options.max_comments_per_parent
        return bool(
            cap
            and cap < expected
            and self.state.archived_parent_count(parent_type, parent_id) >= cap
        )

    def _terminal_status(self, parent_type: str, parent_id: str, expected: int) -> str:
        if self._expected_reached(parent_type, parent_id, expected):
            return "complete"
        if self._cap_reached(parent_type, parent_id, expected):
            return "capped"
        return "short"

    def _stop_reached(self, parent_type: str, parent_id: str, expected: int) -> bool:
        if self._cap_reached(parent_type, parent_id, expected):
            return True
        return self.options.strategy != "exhaustive" and self._expected_reached(
            parent_type, parent_id, expected
        )

    def run_parent(self, parent: dict[str, Any]) -> dict[str, Any]:
        parent_type = str(parent.get("archive_type") or parent.get("parent_type") or "")
        parent_id = str(parent.get("id") or parent.get("parent_id") or "")
        expected = int(parent.get("comment_count") or parent.get("expected_comments") or 0)
        metrics: dict[str, Any] = {
            "parent_type": parent_type,
            "parent_id": parent_id,
            "status": "short",
            "stage": "",
            "expected": expected,
            "archived": self.state.archived_parent_count(parent_type, parent_id),
            "pages_requested": 0,
            "records_returned": 0,
            "roots_seen": 0,
            "roots_skipped_known": 0,
            "child_trees_skipped_complete": 0,
            "child_tree_traversals": 0,
            "child_pages_requested": 0,
            "records_added": 0,
            "endpoint_http_status_counts": {},
            "elapsed_seconds_by_stage": {},
            "errors": [],
        }
        previous_task = self.state.load_parent_task(parent_type, parent_id)
        if previous_task is not None:
            metrics["stage"] = previous_task.stage
        if self._stop_reached(parent_type, parent_id, expected):
            metrics["status"] = self._terminal_status(parent_type, parent_id, expected)
            self._save_parent_metrics(metrics)
            return metrics

        stages = ADAPTIVE_STAGES[:1] if self.options.strategy == "single-pass" else ADAPTIVE_STAGES
        for stage in stages:
            stage_started = time.monotonic()
            remaining = expected - self.state.archived_parent_count(parent_type, parent_id)
            if stage.endpoint_kind.startswith("legacy") and self.options.legacy_fallback == "never":
                continue
            if stage.endpoint_kind == "legacy_root_comments" and (
                self.options.strategy != "exhaustive"
                and
                self.options.legacy_fallback != "always"
                and remaining < self.options.legacy_root_threshold
            ):
                continue
            endpoint = self._endpoint(parent_type, parent_id, stage.endpoint_kind)
            if self.state.endpoint_status(parent_type, endpoint) == "unavailable":
                continue
            checkpoint = self.state.load_checkpoint(
                parent_type, parent_id, stage.name, endpoint, stage.order_mode
            )
            if checkpoint and checkpoint.is_end:
                continue
            metrics["stage"] = stage.name
            next_url = checkpoint.cursor if checkpoint and checkpoint.cursor else endpoint
            first = next_url == endpoint
            seen_urls: set[str] = set()
            try:
                while next_url:
                    parameter = "order_by" if stage.endpoint_kind == "v5" else "order"
                    params = {parameter: stage.order_mode} if first else None
                    data = self.client.get(next_url, params)
                    first = False
                    metrics["pages_requested"] += 1
                    rows = data.get("data", [])
                    if not isinstance(rows, list):
                        raise ValueError("Endpoint data is not a list")
                    metrics["records_returned"] += len(rows)
                    stopped_mid_page = False
                    for root in rows:
                        if isinstance(root, dict):
                            if self._process_root(
                                root, parent_type, parent_id, stage.name, metrics, expected
                            ):
                                stopped_mid_page = True
                                break
                    if stopped_mid_page:
                        metrics["archived"] = self.state.archived_parent_count(
                            parent_type, parent_id
                        )
                        metrics["status"] = self._terminal_status(
                            parent_type, parent_id, expected
                        )
                        break
                    paging = data.get("paging") or {}
                    is_end = bool(paging.get("is_end", not rows))
                    candidate = "" if is_end else str(paging.get("next") or "")
                    self._save_checkpoint(
                        parent_type,
                        parent_id,
                        stage.name,
                        endpoint,
                        stage.order_mode,
                        candidate,
                        is_end,
                    )
                    metrics["archived"] = self.state.archived_parent_count(
                        parent_type, parent_id
                    )
                    self._save_parent_metrics(metrics)
                    if self._stop_reached(parent_type, parent_id, expected):
                        metrics["status"] = self._terminal_status(
                            parent_type, parent_id, expected
                        )
                        break
                    if is_end or not candidate:
                        break
                    if candidate in seen_urls:
                        raise ValueError("Pagination loop detected")
                    seen_urls.add(candidate)
                    next_url = candidate
            except self.error_type as exc:
                metrics["errors"].append(str(exc))
                status = getattr(exc, "status", None)
                if status is not None:
                    key = str(status)
                    metrics["endpoint_http_status_counts"][key] = (
                        metrics["endpoint_http_status_counts"].get(key, 0) + 1
                    )
                if status == 404:
                    self.state.mark_endpoint(
                        parent_type, endpoint, "unavailable", getattr(exc, "status", None), str(exc)
                    )
                else:
                    setattr(exc, "comment_metrics", metrics)
                    raise
            finally:
                metrics["elapsed_seconds_by_stage"][stage.name] = (
                    metrics["elapsed_seconds_by_stage"].get(stage.name, 0.0)
                    + time.monotonic() - stage_started
                )
            if metrics["status"] == "capped" or (
                metrics["status"] == "complete" and self.options.strategy != "exhaustive"
            ):
                break

        metrics["archived"] = self.state.archived_parent_count(parent_type, parent_id)
        metrics["status"] = self._terminal_status(parent_type, parent_id, expected)
        self._save_parent_metrics(metrics)
        return metrics

    def _save_parent_metrics(self, metrics: dict[str, Any]) -> None:
        from archive_state import ParentTask

        self.state.save_parent_task(
            ParentTask(
                metrics["parent_type"],
                metrics["parent_id"],
                metrics["expected"],
                metrics["archived"],
                metrics["stage"],
                metrics["status"],
                "; ".join(metrics["errors"]),
            )
        )
