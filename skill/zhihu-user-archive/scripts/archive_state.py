from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Any


@dataclass(frozen=True)
class ParentTask:
    parent_type: str
    parent_id: str
    expected_comments: int
    archived_comments: int
    stage: str
    status: str
    last_error: str = ""


@dataclass(frozen=True)
class PageCheckpoint:
    parent_type: str
    parent_id: str
    stage: str
    endpoint: str
    order_mode: str
    cursor: str
    offset: int
    is_end: bool


class ArchiveState:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self._create_schema()

    def _create_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS record_index(
              archive_type TEXT NOT NULL,
              record_id TEXT NOT NULL,
              parent_type TEXT NOT NULL DEFAULT '',
              parent_id TEXT NOT NULL DEFAULT '',
              root_id TEXT NOT NULL DEFAULT '',
              child_count INTEGER NOT NULL DEFAULT 0,
              jsonl_offset INTEGER,
              PRIMARY KEY(archive_type, record_id)
            );
            CREATE INDEX IF NOT EXISTS idx_record_parent
              ON record_index(parent_type, parent_id);
            CREATE INDEX IF NOT EXISTS idx_record_root ON record_index(root_id);
            CREATE TABLE IF NOT EXISTS parent_tasks(
              parent_type TEXT NOT NULL,
              parent_id TEXT NOT NULL,
              expected_comments INTEGER NOT NULL,
              archived_comments INTEGER NOT NULL,
              stage TEXT NOT NULL,
              status TEXT NOT NULL,
              last_error TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(parent_type, parent_id)
            );
            CREATE TABLE IF NOT EXISTS page_checkpoints(
              parent_type TEXT NOT NULL,
              parent_id TEXT NOT NULL,
              stage TEXT NOT NULL,
              endpoint TEXT NOT NULL,
              order_mode TEXT NOT NULL,
              cursor TEXT NOT NULL DEFAULT '',
              offset_value INTEGER NOT NULL DEFAULT 0,
              is_end INTEGER NOT NULL DEFAULT 0,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY(parent_type, parent_id, stage, endpoint, order_mode)
            );
            CREATE TABLE IF NOT EXISTS endpoint_capabilities(
              parent_type TEXT NOT NULL,
              endpoint TEXT NOT NULL,
              status TEXT NOT NULL,
              http_status INTEGER,
              error TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(parent_type, endpoint)
            );
            """
        )
        self.db.commit()

    def upsert_record(
        self,
        archive_type: str,
        record_id: str,
        parent_type: str = "",
        parent_id: str = "",
        root_id: str = "",
        child_count: int = 0,
        jsonl_offset: int | None = None,
    ) -> None:
        self._upsert_record_no_commit(
            archive_type,
            record_id,
            parent_type,
            parent_id,
            root_id,
            child_count,
            jsonl_offset,
        )
        self.db.commit()

    def _upsert_record_no_commit(
        self,
        archive_type: str,
        record_id: str,
        parent_type: str = "",
        parent_id: str = "",
        root_id: str = "",
        child_count: int = 0,
        jsonl_offset: int | None = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO record_index
            (archive_type, record_id, parent_type, parent_id, root_id,
             child_count, jsonl_offset)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(archive_type, record_id) DO UPDATE SET
              parent_type=excluded.parent_type,
              parent_id=excluded.parent_id,
              root_id=excluded.root_id,
              child_count=MAX(record_index.child_count, excluded.child_count),
              jsonl_offset=COALESCE(record_index.jsonl_offset,
                                    excluded.jsonl_offset)""",
            (
                str(archive_type),
                str(record_id),
                str(parent_type),
                str(parent_id),
                str(root_id),
                int(child_count or 0),
                jsonl_offset,
            ),
        )

    def has_record(self, archive_type: str, record_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM record_index WHERE archive_type=? AND record_id=?",
            (str(archive_type), str(record_id)),
        ).fetchone()
        return row is not None

    def archived_parent_count(self, parent_type: str, parent_id: str) -> int:
        row = self.db.execute(
            """SELECT COUNT(*) FROM record_index
            WHERE archive_type='comment_received'
              AND parent_type=? AND parent_id=?""",
            (str(parent_type), str(parent_id)),
        ).fetchone()
        return int(row[0])

    def archived_child_count(self, root_id: str) -> int:
        row = self.db.execute(
            """SELECT COUNT(*) FROM record_index
            WHERE archive_type='comment_received' AND root_id=?""",
            (str(root_id),),
        ).fetchone()
        return int(row[0])

    def save_parent_task(self, task: ParentTask) -> None:
        self.db.execute(
            """INSERT INTO parent_tasks
            (parent_type, parent_id, expected_comments, archived_comments,
             stage, status, last_error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(parent_type, parent_id) DO UPDATE SET
              expected_comments=excluded.expected_comments,
              archived_comments=excluded.archived_comments,
              stage=excluded.stage,
              status=excluded.status,
              last_error=excluded.last_error""",
            (
                task.parent_type,
                task.parent_id,
                task.expected_comments,
                task.archived_comments,
                task.stage,
                task.status,
                task.last_error,
            ),
        )
        self.db.commit()

    def load_parent_task(
        self, parent_type: str, parent_id: str
    ) -> ParentTask | None:
        row = self.db.execute(
            """SELECT parent_type, parent_id, expected_comments,
                      archived_comments, stage, status, last_error
               FROM parent_tasks WHERE parent_type=? AND parent_id=?""",
            (str(parent_type), str(parent_id)),
        ).fetchone()
        return ParentTask(*row) if row is not None else None

    def save_checkpoint(self, checkpoint: PageCheckpoint) -> None:
        self.db.execute(
            """INSERT INTO page_checkpoints
            (parent_type, parent_id, stage, endpoint, order_mode, cursor,
             offset_value, is_end, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(parent_type, parent_id, stage, endpoint, order_mode)
            DO UPDATE SET
              cursor=excluded.cursor,
              offset_value=excluded.offset_value,
              is_end=excluded.is_end,
              updated_at=excluded.updated_at""",
            (
                checkpoint.parent_type,
                checkpoint.parent_id,
                checkpoint.stage,
                checkpoint.endpoint,
                checkpoint.order_mode,
                checkpoint.cursor,
                checkpoint.offset,
                int(checkpoint.is_end),
                int(time.time()),
            ),
        )
        self.db.commit()

    def load_checkpoint(
        self,
        parent_type: str,
        parent_id: str,
        stage: str,
        endpoint: str,
        order_mode: str,
    ) -> PageCheckpoint | None:
        row = self.db.execute(
            """SELECT parent_type, parent_id, stage, endpoint, order_mode,
                      cursor, offset_value, is_end
               FROM page_checkpoints
               WHERE parent_type=? AND parent_id=? AND stage=?
                 AND endpoint=? AND order_mode=?""",
            (
                str(parent_type),
                str(parent_id),
                str(stage),
                str(endpoint),
                str(order_mode),
            ),
        ).fetchone()
        if row is None:
            return None
        return PageCheckpoint(*row[:-1], bool(row[-1]))

    def mark_endpoint(
        self,
        parent_type: str,
        endpoint: str,
        status: str,
        http_status: int | None,
        error: str,
    ) -> None:
        if status != "unavailable" or http_status != 404:
            self.db.execute(
                "DELETE FROM endpoint_capabilities WHERE parent_type=? AND endpoint=?",
                (str(parent_type), str(endpoint)),
            )
            self.db.commit()
            return
        self.db.execute(
            """INSERT INTO endpoint_capabilities
            (parent_type, endpoint, status, http_status, error)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(parent_type, endpoint) DO UPDATE SET
              status=excluded.status,
              http_status=excluded.http_status,
              error=excluded.error""",
            (str(parent_type), str(endpoint), str(status), http_status, str(error)),
        )
        self.db.commit()

    def endpoint_status(self, parent_type: str, endpoint: str) -> str | None:
        row = self.db.execute(
            """SELECT status FROM endpoint_capabilities
               WHERE parent_type=? AND endpoint=?
                 AND status='unavailable' AND http_status=404""",
            (str(parent_type), str(endpoint)),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def rebuild_from_jsonl(self, path: Path) -> int:
        unique_keys: set[tuple[str, str]] = set()
        with self.db:
            self.db.execute("DELETE FROM record_index")
            with Path(path).open("rb") as handle:
                line_no = 0
                while True:
                    offset = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    line_no += 1
                    if not line.strip():
                        continue
                    try:
                        decoded: Any = json.loads(line.decode("utf-8"))
                        if not isinstance(decoded, dict):
                            raise TypeError("top-level JSON value must be an object")
                        record: dict[str, Any] = decoded
                        archive_type = self._required_string(
                            record, "archive_type", string_only=True
                        )
                        record_id = self._required_string(record, "id")
                        parent_type = self._optional_string(record, "parent_type")
                        parent_id = self._optional_string(record, "parent_id")
                        root_id = self._optional_string(record, "root_id")
                        if not root_id:
                            root_id = self._optional_string(
                                record, "reply_comment_id"
                            )
                        child_count = self._child_count(record)
                    except (
                        UnicodeError,
                        json.JSONDecodeError,
                        KeyError,
                        TypeError,
                        ValueError,
                    ) as exc:
                        raise ValueError(
                            f"Invalid JSONL line {line_no}: {exc}"
                        ) from exc

                    unique_keys.add((archive_type, record_id))
                    self._upsert_record_no_commit(
                        archive_type,
                        record_id,
                        parent_type,
                        parent_id,
                        root_id,
                        child_count,
                        offset,
                    )
        return len(unique_keys)

    @staticmethod
    def _required_string(
        record: dict[str, Any], field: str, *, string_only: bool = False
    ) -> str:
        value = record[field]
        if value is None or isinstance(value, bool):
            raise TypeError(f"field {field!r} must be a non-null scalar")
        if string_only:
            if not isinstance(value, str):
                raise TypeError(f"field {field!r} must be a string")
        elif not isinstance(value, (str, int)):
            raise TypeError(f"field {field!r} must be a string or integer")
        return str(value)

    @staticmethod
    def _optional_string(record: dict[str, Any], field: str) -> str:
        value = record.get(field)
        if value is None:
            return ""
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise TypeError(f"field {field!r} must be a string, integer, or null")
        return str(value)

    @staticmethod
    def _child_count(record: dict[str, Any]) -> int:
        value = record.get("child_comment_count")
        if value is None or value == "":
            return 0
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise TypeError("field 'child_comment_count' must be an integer")
        count = int(value)
        if count < 0:
            raise ValueError("field 'child_comment_count' must not be negative")
        if count > 2**63 - 1:
            raise ValueError("field 'child_comment_count' exceeds SQLite INTEGER")
        return count

    def close(self) -> None:
        self.db.close()
