import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from archive_state import ArchiveState, PageCheckpoint, ParentTask


class ArchiveStateTests(unittest.TestCase):
    def test_record_and_child_counts_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = ArchiveState(Path(tmp) / "state.sqlite3")

            state.upsert_record("comment_received", "r1", "answer", "a1", "", 2)
            state.upsert_record("comment_received", "c1", "answer", "a1", "r1", 0)
            state.upsert_record("comment_received", "c1", "answer", "a1", "r1", 0)

            self.assertTrue(state.has_record("comment_received", "r1"))
            self.assertFalse(state.has_record("comment_received", "missing"))
            self.assertEqual(state.archived_parent_count("answer", "a1"), 2)
            self.assertEqual(state.archived_child_count("r1"), 1)
            state.close()

    def test_rebuild_from_jsonl_counts_unique_primary_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [
                {
                    "archive_type": "comment_received",
                    "id": "r1",
                    "parent_type": "answer",
                    "parent_id": "a1",
                    "child_comment_count": 1,
                },
                {
                    "archive_type": "comment_received",
                    "id": "r1",
                    "parent_type": "answer",
                    "parent_id": "a1",
                    "child_comment_count": 2,
                },
                {
                    "archive_type": "comment_received",
                    "id": "c1",
                    "parent_type": "answer",
                    "parent_id": "a1",
                    "reply_comment_id": "r1",
                },
            ]
            jsonl = root / "records.jsonl"
            jsonl.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )
            state = ArchiveState(root / "state.sqlite3")
            state.upsert_record("stale", "old")

            rebuilt = state.rebuild_from_jsonl(jsonl)

            self.assertEqual(rebuilt, 2)
            self.assertFalse(state.has_record("stale", "old"))
            self.assertTrue(state.has_record("comment_received", "r1"))
            self.assertEqual(state.archived_parent_count("answer", "a1"), 2)
            self.assertEqual(state.archived_child_count("r1"), 1)
            state.close()

    def test_rebuild_reports_invalid_jsonl_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "records.jsonl"
            jsonl.write_text(
                json.dumps({"archive_type": "answer", "id": "a1"})
                + "\nnot-json\n",
                encoding="utf-8",
            )
            state = ArchiveState(root / "state.sqlite3")

            with self.assertRaisesRegex(ValueError, "Invalid JSONL line 2"):
                state.rebuild_from_jsonl(jsonl)
            state.close()

    def test_empty_rebuild_commits_deleted_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "records.jsonl"
            jsonl.write_text("", encoding="utf-8")
            database = root / "state.sqlite3"
            state = ArchiveState(database)
            state.upsert_record("answer", "old")

            self.assertEqual(state.rebuild_from_jsonl(jsonl), 0)
            state.close()

            reopened = ArchiveState(database)
            self.assertFalse(reopened.has_record("answer", "old"))
            reopened.close()

    def test_failed_rebuild_rolls_back_the_complete_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "records.jsonl"
            jsonl.write_text(
                json.dumps({"archive_type": "answer", "id": "new"})
                + "\nnot-json\n",
                encoding="utf-8",
            )
            database = root / "state.sqlite3"
            state = ArchiveState(database)
            state.upsert_record("answer", "old")

            with self.assertRaisesRegex(ValueError, "Invalid JSONL line 2"):
                state.rebuild_from_jsonl(jsonl)
            state.close()

            reopened = ArchiveState(database)
            self.assertTrue(reopened.has_record("answer", "old"))
            self.assertFalse(reopened.has_record("answer", "new"))
            reopened.close()

    def test_rebuild_wraps_all_record_format_errors_with_line_number(self) -> None:
        invalid_rows = [
            [],
            {"archive_type": "answer"},
            {"archive_type": "answer", "id": "a1", "child_comment_count": {}},
            {
                "archive_type": "answer",
                "id": "a1",
                "child_comment_count": 2**63,
            },
        ]
        for row in invalid_rows:
            with self.subTest(row=row), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                jsonl = root / "records.jsonl"
                jsonl.write_text(json.dumps(row) + "\n", encoding="utf-8")
                state = ArchiveState(root / "state.sqlite3")

                with self.assertRaisesRegex(ValueError, "^Invalid JSONL line 1:"):
                    state.rebuild_from_jsonl(jsonl)
                state.close()

    def test_parent_task_round_trips_and_is_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = ArchiveState(Path(tmp) / "state.sqlite3")
            original = ParentTask("answer", "a1", 10, 2, "v5_score", "running")
            updated = ParentTask(
                "answer", "a1", 10, 9, "legacy", "partial", "HTTP 403"
            )

            self.assertIsNone(state.load_parent_task("answer", "a1"))
            state.save_parent_task(original)
            self.assertEqual(state.load_parent_task("answer", "a1"), original)
            state.save_parent_task(updated)
            self.assertEqual(state.load_parent_task("answer", "a1"), updated)
            state.close()

    def test_checkpoint_round_trips_bool_and_is_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = ArchiveState(Path(tmp) / "state.sqlite3")
            original = PageCheckpoint(
                "answer", "a1", "v5_score", "root_comment", "score", "next-1", 20, False
            )
            updated = PageCheckpoint(
                "answer", "a1", "v5_score", "root_comment", "score", "", 40, True
            )

            self.assertIsNone(
                state.load_checkpoint(
                    "answer", "a1", "v5_score", "root_comment", "score"
                )
            )
            state.save_checkpoint(original)
            self.assertEqual(
                state.load_checkpoint(
                    "answer", "a1", "v5_score", "root_comment", "score"
                ),
                original,
            )
            state.save_checkpoint(updated)
            loaded = state.load_checkpoint(
                "answer", "a1", "v5_score", "root_comment", "score"
            )
            self.assertEqual(loaded, updated)
            self.assertIs(loaded.is_end, True)
            state.close()

    def test_only_stable_404_endpoint_unavailability_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = ArchiveState(Path(tmp) / "state.sqlite3")

            self.assertIsNone(state.endpoint_status("answer", "root_comment"))
            state.mark_endpoint("answer", "root_comment", "available", 200, "")
            self.assertIsNone(state.endpoint_status("answer", "root_comment"))
            state.mark_endpoint(
                "answer", "root_comment", "unavailable", 403, "forbidden"
            )
            self.assertIsNone(state.endpoint_status("answer", "root_comment"))
            state.mark_endpoint(
                "answer", "root_comment", "unavailable", 429, "rate limited"
            )
            self.assertIsNone(state.endpoint_status("answer", "root_comment"))
            state.mark_endpoint(
                "answer", "root_comment", "unavailable", 404, "missing"
            )
            self.assertEqual(
                state.endpoint_status("answer", "root_comment"), "unavailable"
            )
            state.close()


if __name__ == "__main__":
    unittest.main()
