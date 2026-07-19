from __future__ import annotations

from copy import deepcopy
from typing import Any


def child_comment(cid: str, root_id: str) -> dict[str, Any]:
    return {"id": cid, "reply_comment_id": root_id, "content": f"child-{cid}"}


def root_comment(rid: str, child_ids: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "id": rid,
        "content": f"root-{rid}",
        "child_comment_count": len(child_ids),
        "child_comments": [child_comment(cid, rid) for cid in child_ids],
    }


def repeated_root_routes(count: int = 100) -> dict[tuple[str, str], list[dict[str, Any]]]:
    roots = []
    routes: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for index in range(count):
        root_id = f"r{index}"
        child_id = f"c{index}"
        root = root_comment(root_id)
        root["child_comment_count"] = 1
        roots.append(root)
        routes[(f"comment_v5/comment/{root_id}/child_comment", "")] = [
            child_comment(child_id, root_id)
        ]

    for order in ("score", "ts"):
        routes[("comment_v5/answers/a1/root_comment", order)] = roots
    for order in ("normal", "reverse"):
        routes[("answers/a1/comments", order)] = roots
    return routes


class FakeClient:
    def __init__(self, routes: dict[tuple[str, str], list[dict[str, Any]]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        order = str((params or {}).get("order_by") or (params or {}).get("order") or "")
        self.calls.append((path, order))
        rows = deepcopy(self.routes.get((path, order), []))
        return {"data": rows, "paging": {"is_end": True}}
