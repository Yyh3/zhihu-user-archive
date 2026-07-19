from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import archive_zhihu_user as archive
from archive_state import ArchiveState, PageCheckpoint, ParentTask


class FixtureClient:
    def __init__(self, comment_pages: list[list[dict]], expected: int | None = None) -> None:
        self.comment_pages = comment_pages
        self.expected = expected
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, path: str, params=None):
        copied = dict(params) if params is not None else None
        self.calls.append((path, copied))
        if path == "members/test/answers":
            expected = self.expected if self.expected is not None else sum(len(page) for page in self.comment_pages)
            return {
                "data": [{"id": "a1", "comment_count": expected, "content": "answer"}],
                "paging": {"is_end": True},
            }
        if path == "comment_v5/answers/a1/root_comment":
            page_number = 0
        elif path.startswith("COMMENT-NEXT-"):
            page_number = int(path.rsplit("-", 1)[1])
        else:
            return {"data": [], "paging": {"is_end": True}}
        rows = self.comment_pages[page_number]
        is_end = page_number + 1 == len(self.comment_pages)
        return {
            "data": rows,
            "paging": {
                "is_end": is_end,
                "next": "" if is_end else f"COMMENT-NEXT-{page_number + 1}",
            },
        }


class ArchiveIntegrationTests(unittest.TestCase):
    def archive_args(self, output: str) -> argparse.Namespace:
        args = archive.build_parser().parse_args([
            "--user", "test", "--output", output, "--types", "answers",
            "--content-comments", "all", "--auth-mode", "public", "--delay", "0",
            "--base-url", "http://127.0.0.1/api/v4/",
        ])
        return args

    def run_fixture(self, output: str, pages: list[list[dict]]):
        client = FixtureClient(pages)
        with patch.object(archive, "resolve_auth", return_value=("", None)), patch.object(
            archive, "ApiClient", return_value=client
        ):
            code = archive.run_archive(self.archive_args(output))
        manifest = json.loads((Path(output) / "manifest.json").read_text(encoding="utf-8"))
        return code, client, manifest

    def test_jsonl_append_is_indexed_before_a_missing_checkpoint_can_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "records.jsonl"
            state = ArchiveState(Path(tmp) / "state.sqlite3")
            record = {"archive_type": "answer", "id": "a1", "parent_type": "", "parent_id": ""}
            with archive.JsonlWriter(jsonl, state) as writer:
                self.assertTrue(writer.append(record))
            self.assertIsNone(state.load_checkpoint("answer", "a1", "stage", "endpoint", "score"))
            with archive.JsonlWriter(jsonl, state) as resumed:
                self.assertFalse(resumed.append(record))
            state.close()
            self.assertEqual(len(jsonl.read_text(encoding="utf-8").splitlines()), 1)

    def test_truncated_tail_is_backed_up_and_valid_prefix_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.jsonl"
            valid = b'{"archive_type":"answer","id":"a1"}\n'
            original = valid + b'{"archive_type":"answer","id":'
            path.write_bytes(original)
            self.assertTrue(archive.repair_jsonl_tail(path))
            self.assertEqual(path.read_bytes(), valid)
            backups = list(Path(tmp).glob("records.jsonl.corrupt-tail-backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)

    def test_adaptive_completion_uses_no_legacy_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, client, manifest = self.run_fixture(tmp, [[{"id": "r1", "content": "root"}]])
            self.assertEqual(code, 0)
            called_paths = [path for path, _ in client.calls]
            self.assertNotIn("answers/a1/comments", called_paths)
            self.assertNotIn("answers/a1/root_comments", called_paths)
            performance = manifest["categories"]["content-comments"]["performance"]
            self.assertEqual(performance["parents_completed_by_count"], 1)
            self.assertEqual(performance["parents_partial_after_all_stages"], 0)

    def test_long_parent_progress_exposes_stage_pages_skips_and_additions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pages = [[{"id": f"r{number}", "content": "root"}] for number in range(3)]
            code, _, manifest = self.run_fixture(tmp, pages)
            self.assertEqual(code, 0)
            category = manifest["categories"]["content-comments"]
            self.assertEqual(category["progress"], {
                "current_stage": "v5_score",
                "page_count": 3,
                "roots_skipped_known": 0,
                "records_added": 3,
            })
            expected_keys = {
                "pages_requested", "records_returned", "records_added", "roots_seen",
                "roots_skipped_known", "child_trees_skipped_complete", "child_tree_traversals",
                "child_pages_requested",
                "parents_completed_by_count", "parents_partial_after_all_stages",
                "endpoint_http_status_counts", "elapsed_seconds_by_stage", "mismatch_count",
                "missing_sum", "top_mismatches",
            }
            self.assertEqual(set(category["performance"]), expected_keys)

    def test_cli_defaults_and_boolean_optional_checkpoint_flag(self) -> None:
        parser = archive.build_parser()
        defaults = parser.parse_args([])
        self.assertEqual(defaults.comment_strategy, "adaptive")
        self.assertTrue(defaults.checkpoint_every_page)
        self.assertIsNone(defaults.state_db)
        self.assertEqual(defaults.legacy_fallback, "auto")
        self.assertEqual(defaults.legacy_root_threshold, 1)
        self.assertFalse(defaults.rebuild_state_index)
        self.assertFalse(parser.parse_args(["--no-checkpoint-every-page"]).checkpoint_every_page)

    def test_rebuild_state_index_is_offline_and_reports_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / "records.jsonl").write_text(
                '{"archive_type":"answer","id":"a1"}\n', encoding="utf-8"
            )
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["archive", "--rebuild-state-index", "--output", tmp]), patch.object(
                archive, "resolve_auth", side_effect=AssertionError("network/auth path used")
            ), redirect_stdout(stdout):
                self.assertEqual(archive.main(), 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["rebuilt_record_count"], 1)
            state = ArchiveState(output / "archive_state.sqlite3")
            self.assertTrue(state.has_record("answer", "a1"))
            state.close()

    def test_rebuild_state_index_preserves_progress_and_capability_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / "records.jsonl").write_text(
                '{"archive_type":"answer","id":"a1"}\n', encoding="utf-8"
            )
            state = ArchiveState(output / "archive_state.sqlite3")
            task = ParentTask("answer", "a1", 2, 1, "v5_ts", "short")
            checkpoint = PageCheckpoint(
                "answer", "a1", "v5_ts", "endpoint", "ts", "NEXT", 20, False
            )
            state.save_parent_task(task)
            state.save_checkpoint(checkpoint)
            state.mark_endpoint("answer", "missing-endpoint", "unavailable", 404, "missing")
            state.upsert_record("answer", "stale")
            state.close()

            self.assertEqual(archive.rebuild_state_index(tmp), 0)

            state = ArchiveState(output / "archive_state.sqlite3")
            self.assertTrue(state.has_record("answer", "a1"))
            self.assertFalse(state.has_record("answer", "stale"))
            self.assertEqual(state.load_parent_task("answer", "a1"), task)
            self.assertEqual(
                state.load_checkpoint("answer", "a1", "v5_ts", "endpoint", "ts"),
                checkpoint,
            )
            self.assertEqual(
                state.endpoint_status("answer", "missing-endpoint"), "unavailable"
            )
            state.close()

    def test_comment_cap_marks_category_partial_and_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.archive_args(tmp)
            args.max_comments_per_parent = 1
            client = FixtureClient(
                [[{"id": "r1", "content": "one"}, {"id": "r2", "content": "two"}]],
                expected=2,
            )
            with patch.object(archive, "resolve_auth", return_value=("", None)), patch.object(
                archive, "ApiClient", return_value=client
            ):
                code = archive.run_archive(args)
            manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
            category = manifest["categories"]["content-comments"]
            self.assertEqual(code, 2)
            self.assertEqual(category["status"], "partial")
            self.assertFalse(manifest["complete"])
            self.assertEqual(category["performance"]["parents_completed_by_count"], 0)
            self.assertEqual(category["performance"]["parents_partial_after_all_stages"], 1)
            self.assertEqual(category["performance"]["mismatch_count"], 1)
            self.assertEqual(category["performance"]["missing_sum"], 1)
            self.assertEqual(category["performance"]["top_mismatches"][0]["status"], "capped")

    def test_final_count_mismatch_records_top_mismatch_and_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FixtureClient([[{"id": "r1", "content": "one"}]], expected=2)
            with patch.object(archive, "resolve_auth", return_value=("", None)), patch.object(
                archive, "ApiClient", return_value=client
            ):
                code = archive.run_archive(self.archive_args(tmp))
            manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
            category = manifest["categories"]["content-comments"]
            performance = category["performance"]
            self.assertEqual(code, 2)
            self.assertEqual(category["status"], "partial")
            self.assertEqual(performance["mismatch_count"], 1)
            self.assertEqual(performance["missing_sum"], 1)
            self.assertEqual(performance["top_mismatches"], [{
                "parent_type": "answer",
                "parent_id": "a1",
                "expected": 2,
                "archived": 1,
                "missing": 1,
                "stage": "legacy_root_reverse",
                "status": "short",
                "title": "",
            }])

    def test_top_mismatches_is_bounded_to_fifty_while_totals_cover_all_parents(self) -> None:
        class ManyParentsClient(FixtureClient):
            def get(self, path: str, params=None):
                self.calls.append((path, dict(params) if params is not None else None))
                if path == "members/test/answers":
                    return {
                        "data": [
                            {"id": f"a{index:02d}", "comment_count": 1, "content": "answer"}
                            for index in range(51)
                        ],
                        "paging": {"is_end": True},
                    }
                return {"data": [], "paging": {"is_end": True}}

        with tempfile.TemporaryDirectory() as tmp:
            args = self.archive_args(tmp)
            args.comment_strategy = "single-pass"
            client = ManyParentsClient([])
            with patch.object(archive, "resolve_auth", return_value=("", None)), patch.object(
                archive, "ApiClient", return_value=client
            ):
                self.assertEqual(archive.run_archive(args), 2)
            performance = json.loads(
                (Path(tmp) / "manifest.json").read_text(encoding="utf-8")
            )["categories"]["content-comments"]["performance"]
            self.assertEqual(performance["mismatch_count"], 51)
            self.assertEqual(performance["missing_sum"], 51)
            self.assertEqual(len(performance["top_mismatches"]), 50)
            self.assertTrue(all(item["missing"] == 1 for item in performance["top_mismatches"]))

    def test_real_zhihu_run_rejects_delay_below_minimum_before_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.archive_args(tmp)
            args.base_url = "https://www.zhihu.com/api/v4/"
            args.delay = 0
            with patch.object(
                archive, "resolve_auth", side_effect=AssertionError("auth must not start")
            ):
                with self.assertRaisesRegex(archive.ArchiveError, "at least 1.5"):
                    archive.run_archive(args)

    def test_real_zhihu_detail_enrichment_rejects_delay_below_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.archive_args(tmp)
            args.delay = 0
            with patch.object(
                archive, "load_existing", side_effect=AssertionError("archive read must not start")
            ):
                with self.assertRaisesRegex(archive.ArchiveError, "at least 1.5"):
                    archive.enrich_details(args)

    def test_local_fixture_parse_and_run_allows_zero_delay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.archive_args(tmp)
            self.assertEqual(args.delay, 0)
            args.content_comments = "none"
            client = FixtureClient([])
            with patch.object(archive, "resolve_auth", return_value=("", None)), patch.object(
                archive, "ApiClient", return_value=client
            ):
                self.assertEqual(archive.run_archive(args), 0)

    def test_429_pauses_archive_instead_of_continuing_to_next_parent(self) -> None:
        class RateLimitedClient(FixtureClient):
            def get(self, path: str, params=None):
                if path == "comment_v5/answers/a1/root_comment":
                    raise archive.ArchiveError("rate limited", 429, True)
                return super().get(path, params)

        with tempfile.TemporaryDirectory() as tmp:
            client = RateLimitedClient([], expected=1)
            with patch.object(archive, "resolve_auth", return_value=("", None)), patch.object(
                archive, "ApiClient", return_value=client
            ):
                with self.assertRaises(archive.ArchiveError) as raised:
                    archive.run_archive(self.archive_args(tmp))
            self.assertEqual(raised.exception.status, 429)
            manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
            category = manifest["categories"]["content-comments"]
            performance = category["performance"]
            self.assertEqual(category["status"], "partial")
            self.assertFalse(manifest["complete"])
            self.assertEqual(performance["endpoint_http_status_counts"], {"429": 1})
            self.assertEqual(performance["parents_partial_after_all_stages"], 1)
            self.assertEqual(performance["mismatch_count"], 1)
            self.assertEqual(len(category["errors"]), 1)
            self.assertEqual(category["errors"][0]["parent_id"], "a1")
            self.assertEqual(category["errors"][0]["http_status"], 429)
            self.assertIn("rate limited", category["errors"][0]["error"])

    def test_later_stage_429_aggregates_manifest_once_before_pausing(self) -> None:
        class LaterRateLimitedClient(FixtureClient):
            def get(self, path: str, params=None):
                if (
                    path == "comment_v5/answers/a1/root_comment"
                    and (params or {}).get("order_by") == "ts"
                ):
                    raise archive.ArchiveError("later rate limited", 429, True)
                return super().get(path, params)

        with tempfile.TemporaryDirectory() as tmp:
            client = LaterRateLimitedClient([[]], expected=1)
            with patch.object(archive, "resolve_auth", return_value=("", None)), patch.object(
                archive, "ApiClient", return_value=client
            ):
                with self.assertRaises(archive.ArchiveError) as raised:
                    archive.run_archive(self.archive_args(tmp))
            self.assertEqual(raised.exception.status, 429)
            manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
            category = manifest["categories"]["content-comments"]
            performance = category["performance"]
            self.assertEqual(category["status"], "partial")
            self.assertFalse(manifest["complete"])
            self.assertEqual(performance["pages_requested"], 1)
            self.assertEqual(performance["endpoint_http_status_counts"], {"429": 1})
            self.assertEqual(performance["parents_partial_after_all_stages"], 1)
            self.assertEqual(performance["mismatch_count"], 1)
            self.assertEqual(len(category["errors"]), 1)
            self.assertEqual(category["errors"][0]["http_status"], 429)
            self.assertIn("later rate limited", category["errors"][0]["error"])

    def test_exhaustive_strategy_runs_every_stage_after_count_is_reached(self) -> None:
        from comment_pipeline import AdaptiveCommentPipeline, CommentOptions

        with tempfile.TemporaryDirectory() as tmp:
            state = ArchiveState(Path(tmp) / "state.sqlite3")
            client = FixtureClient([[{"id": "r1", "content": "root"}]])
            written: list[dict] = []
            pipeline = AdaptiveCommentPipeline(
                client,
                state,
                lambda record: written.append(record) or True,
                CommentOptions(strategy="exhaustive"),
                error_type=RuntimeError,
            )
            result = pipeline.run_parent(
                {"archive_type": "answer", "id": "a1", "comment_count": 1}
            )
            state.close()
            root_calls = [path for path, _ in client.calls if "child_comment" not in path]
            self.assertEqual(root_calls, [
                "comment_v5/answers/a1/root_comment",
                "comment_v5/answers/a1/root_comment",
                "answers/a1/comments",
                "answers/a1/comments",
                "answers/a1/root_comments",
                "answers/a1/root_comments",
            ])
            self.assertEqual(result["status"], "complete")

    def test_partial_parent_with_existing_comment_resumes_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            state = ArchiveState(output / "archive_state.sqlite3")
            answer = archive.normalize("answer", {"id": "a1", "comment_count": 2})
            existing = archive.normalize("comment_received", {"id": "r1"}, "answer", "a1")
            with archive.JsonlWriter(output / "records.jsonl", state) as writer:
                writer.append(answer)
                writer.append(existing)
            state.save_parent_task(ParentTask("answer", "a1", 2, 1, "v5_score", "short"))
            state.close()

            client = FixtureClient([[{"id": "r2", "content": "new"}]], expected=2)
            args = self.archive_args(tmp)
            args.resume = True
            with patch.object(archive, "resolve_auth", return_value=("", None)), patch.object(
                archive, "ApiClient", return_value=client
            ):
                code = archive.run_archive(args)

            rows = [json.loads(line) for line in (output / "records.jsonl").read_text(encoding="utf-8").splitlines()]
            comments = [row for row in rows if row["archive_type"] == "comment_received"]
            self.assertEqual(code, 0)
            self.assertEqual({row["id"] for row in comments}, {"r1", "r2"})
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["categories"]["content-comments"]["status"], "complete")

    def test_resume_with_more_archived_than_fresh_expected_is_partial_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            state = ArchiveState(output / "archive_state.sqlite3")
            answer = archive.normalize("answer", {"id": "a1", "comment_count": 2})
            first = archive.normalize("comment_received", {"id": "r1"}, "answer", "a1")
            second = archive.normalize("comment_received", {"id": "r2"}, "answer", "a1")
            with archive.JsonlWriter(output / "records.jsonl", state) as writer:
                writer.append(answer)
                writer.append(first)
                writer.append(second)
            state.save_parent_task(ParentTask("answer", "a1", 2, 2, "v5_score", "complete"))
            state.close()

            client = FixtureClient([], expected=1)
            args = self.archive_args(tmp)
            args.resume = True
            with patch.object(archive, "resolve_auth", return_value=("", None)), patch.object(
                archive, "ApiClient", return_value=client
            ):
                self.assertEqual(archive.run_archive(args), 2)

            performance = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )["categories"]["content-comments"]["performance"]
            self.assertEqual(performance["parents_completed_by_count"], 0)
            self.assertEqual(performance["parents_partial_after_all_stages"], 1)
            self.assertEqual(performance["mismatch_count"], 1)
            self.assertEqual(performance["missing_sum"], 0)
            self.assertEqual(performance["top_mismatches"][0]["archived"], 2)

    def test_writer_separates_append_after_valid_tail_without_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.jsonl"
            first = {"archive_type": "answer", "id": "a1"}
            path.write_text(json.dumps(first), encoding="utf-8")
            self.assertFalse(archive.repair_jsonl_tail(path))
            state = ArchiveState(Path(tmp) / "state.sqlite3")
            state.rebuild_from_jsonl(path)
            with archive.JsonlWriter(path, state) as writer:
                writer.append({"archive_type": "answer", "id": "a2"})
            state.close()
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["id"] for row in rows], ["a1", "a2"])

    def test_resume_reconciles_stale_record_index_without_losing_progress_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            record = archive.normalize("answer", {"id": "a1", "comment_count": 0})
            (output / "records.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
            state = ArchiveState(output / "archive_state.sqlite3")
            state.save_parent_task(ParentTask("answer", "a1", 3, 1, "v5_ts", "short"))
            checkpoint = PageCheckpoint("answer", "a1", "v5_ts", "endpoint", "ts", "NEXT", 20, False)
            state.save_checkpoint(checkpoint)
            state.mark_endpoint("answer", "endpoint", "unavailable", 404, "missing")
            state.close()

            client = FixtureClient([], expected=0)
            args = self.archive_args(tmp)
            args.resume = True
            args.content_comments = "none"
            with patch.object(archive, "resolve_auth", return_value=("", None)), patch.object(
                archive, "ApiClient", return_value=client
            ):
                self.assertEqual(archive.run_archive(args), 0)

            rows = (output / "records.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 1)
            state = ArchiveState(output / "archive_state.sqlite3")
            self.assertTrue(state.has_record("answer", "a1"))
            self.assertEqual(state.load_parent_task("answer", "a1").stage, "v5_ts")
            self.assertEqual(state.load_checkpoint("answer", "a1", "v5_ts", "endpoint", "ts"), checkpoint)
            self.assertEqual(state.endpoint_status("answer", "endpoint"), "unavailable")
            state.close()

    def test_pipeline_failure_keeps_completed_page_metrics_in_manifest(self) -> None:
        class FaultClient(FixtureClient):
            def get(self, path: str, params=None):
                if path == "BROKEN-NEXT":
                    raise archive.ArchiveError("fixture failure", 500, False)
                if path == "comment_v5/answers/a1/root_comment":
                    self.calls.append((path, dict(params or {})))
                    return {
                        "data": [{"id": "r1", "content": "root"}],
                        "paging": {"is_end": False, "next": "BROKEN-NEXT"},
                    }
                return super().get(path, params)

        with tempfile.TemporaryDirectory() as tmp:
            client = FaultClient([], expected=2)
            with patch.object(archive, "resolve_auth", return_value=("", None)), patch.object(
                archive, "ApiClient", return_value=client
            ):
                code = archive.run_archive(self.archive_args(tmp))
            self.assertEqual(code, 2)
            manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
            category = manifest["categories"]["content-comments"]
            self.assertEqual(category["status"], "partial")
            self.assertEqual(category["performance"]["pages_requested"], 1)
            self.assertEqual(category["performance"]["records_returned"], 1)
            self.assertEqual(category["performance"]["records_added"], 1)
            self.assertEqual(category["performance"]["roots_seen"], 1)
            self.assertEqual(category["performance"]["endpoint_http_status_counts"], {"500": 1})
            self.assertIn("v5_score", category["performance"]["elapsed_seconds_by_stage"])

    def test_refresh_uses_fresh_parent_comment_count_without_rewriting_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            state = ArchiveState(output / "archive_state.sqlite3")
            old_answer = archive.normalize("answer", {"id": "a1", "comment_count": 1})
            existing = archive.normalize("comment_received", {"id": "r1"}, "answer", "a1")
            with archive.JsonlWriter(output / "records.jsonl", state) as writer:
                writer.append(old_answer)
                writer.append(existing)
            state.save_parent_task(ParentTask("answer", "a1", 1, 1, "v5_score", "complete"))
            state.close()

            client = FixtureClient([[{"id": "r2", "content": "new"}]], expected=2)
            args = self.archive_args(tmp)
            args.resume = True
            args.refresh_existing_comments = True
            with patch.object(archive, "resolve_auth", return_value=("", None)), patch.object(
                archive, "ApiClient", return_value=client
            ):
                self.assertEqual(archive.run_archive(args), 0)

            rows = [json.loads(line) for line in (output / "records.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sum(row["archive_type"] == "answer" for row in rows), 1)
            self.assertEqual(
                {row["id"] for row in rows if row["archive_type"] == "comment_received"},
                {"r1", "r2"},
            )
            self.assertTrue(any(path == "comment_v5/answers/a1/root_comment" for path, _ in client.calls))
            state = ArchiveState(output / "archive_state.sqlite3")
            task = state.load_parent_task("answer", "a1")
            self.assertEqual((task.expected_comments, task.archived_comments, task.status), (2, 2, "complete"))
            state.close()

    def test_run_archive_resumes_real_page_checkpoint_from_next_cursor(self) -> None:
        class NextClient(FixtureClient):
            def get(self, path: str, params=None):
                if path == "SAVED-NEXT":
                    self.calls.append((path, dict(params) if params is not None else None))
                    return {"data": [{"id": "r2", "content": "next"}], "paging": {"is_end": True}}
                return super().get(path, params)

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            state = ArchiveState(output / "archive_state.sqlite3")
            answer = archive.normalize("answer", {"id": "a1", "comment_count": 2})
            existing = archive.normalize("comment_received", {"id": "r1"}, "answer", "a1")
            with archive.JsonlWriter(output / "records.jsonl", state) as writer:
                writer.append(answer)
                writer.append(existing)
            state.save_parent_task(ParentTask("answer", "a1", 2, 1, "v5_score", "short"))
            state.save_checkpoint(PageCheckpoint(
                "answer", "a1", "v5_score", "comment_v5/answers/a1/root_comment",
                "score", "SAVED-NEXT", 20, False,
            ))
            state.close()

            client = NextClient([], expected=2)
            args = self.archive_args(tmp)
            args.resume = True
            with patch.object(archive, "resolve_auth", return_value=("", None)), patch.object(
                archive, "ApiClient", return_value=client
            ):
                self.assertEqual(archive.run_archive(args), 0)

            comment_calls = [path for path, _ in client.calls if path != "members/test/answers"]
            self.assertEqual(comment_calls[0], "SAVED-NEXT")
            rows = [json.loads(line) for line in (output / "records.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(
                {row["id"] for row in rows if row["archive_type"] == "comment_received"},
                {"r1", "r2"},
            )


if __name__ == "__main__":
    unittest.main()
