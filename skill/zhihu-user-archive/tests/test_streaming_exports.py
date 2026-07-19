from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import archive_zhihu_user as archive


class StreamingExportTests(unittest.TestCase):
    def write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

    def mixed_rows(self) -> list[dict]:
        return [
            {
                "archive_type": "answer",
                "id": "a1",
                "title": "中文问题",
                "url": "https://example.test/a1",
                "text": "回答正文",
                "excerpt": "回答摘要",
                "created_time": 1,
                "updated_time": 2,
                "author_name": "甲",
                "author_token": "author-a",
                "comment_count": 3,
                "voteup_count": 4,
                "parent_type": "",
                "parent_id": "",
                "raw": {"large": "not exported"},
            },
            {
                "archive_type": "article",
                "id": "p1",
                "title": "文章标题",
                "url": "https://example.test/p1",
                "text": "文章正文",
                "excerpt": "",
                "created_time": 5,
                "updated_time": 6,
                "author_name": "乙",
                "author_token": "author-b",
                "comment_count": 0,
                "voteup_count": 7,
                "parent_type": "",
                "parent_id": "",
            },
            {
                "archive_type": "comment_received",
                "id": "c1",
                "title": "",
                "url": "",
                "text": "评论内容",
                "excerpt": "评论摘要",
                "created_time": 7,
                "updated_time": "",
                "author_name": "丙",
                "author_token": "author-c",
                "comment_count": 0,
                "voteup_count": 0,
                "parent_type": "answer",
                "parent_id": "a1",
            },
        ]

    def test_iter_jsonl_yields_source_line_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.jsonl"
            path.write_text(
                json.dumps(self.mixed_rows()[0], ensure_ascii=False)
                + "\n\n"
                + json.dumps(self.mixed_rows()[1], ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )

            yielded = list(archive.iter_jsonl(path))

            self.assertEqual([line for line, _ in yielded], [1, 3])
            self.assertEqual([row["id"] for _, row in yielded], ["a1", "p1"])

    def test_iter_jsonl_reports_the_malformed_line_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.jsonl"
            path.write_text('{"archive_type":"answer","id":"a1"}\nnot-json\n', encoding="utf-8")

            with self.assertRaisesRegex(archive.ArchiveError, r"line 2"):
                list(archive.iter_jsonl(path))

    def test_verify_jsonl_reports_bad_lines_duplicates_and_type_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.jsonl"
            path.write_text(
                '{"archive_type":"answer","id":"a1"}\n'
                "malformed\n"
                '{"archive_type":"answer","id":"a1"}\n'
                '{"archive_type":"article","id":"p1"}\n'
                '{"archive_type":"answer"}\n',
                encoding="utf-8",
            )

            report = archive.verify_jsonl(path)

            self.assertEqual(
                report,
                {
                    "valid_records": 3,
                    "bad_lines": 2,
                    "duplicate_keys": 1,
                    "counts_by_type": {"answer": 2, "article": 1},
                    "unique_counts_by_type": {"answer": 1, "article": 1},
                },
            )

    def test_duplicate_keys_keep_first_record_in_exports_and_unique_manifest_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [
                {
                    "archive_type": "answer", "id": "a1", "title": "First title",
                    "text": "first body", "created_time": "",
                },
                {
                    "archive_type": "answer", "id": "a1", "title": "Duplicate title",
                    "text": "duplicate body", "created_time": "",
                },
                {
                    "archive_type": "article", "id": "p1", "title": "Article title",
                    "text": "article body", "created_time": "",
                },
            ]
            self.write_jsonl(root / "records.jsonl", rows)

            with patch.object(archive, "load_existing", side_effect=AssertionError("must stream")):
                result = archive.rebuild_exports(str(root))

            self.assertEqual(result, 0)
            with (root / "records.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertEqual([(row["archive_type"], row["id"]) for row in csv_rows], [("answer", "a1"), ("article", "p1")])
            self.assertEqual(csv_rows[0]["title"], "First title")
            answer_markdown = (root / "markdown" / "answer.md").read_text(encoding="utf-8")
            self.assertTrue(answer_markdown.startswith("# answer (1)\n\n"))
            self.assertIn("## First title", answer_markdown)
            self.assertNotIn("Duplicate title", answer_markdown)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["total_unique_records"], 2)
            self.assertEqual(manifest["counts_by_type"], {"answer": 1, "article": 1})
            self.assertEqual(sum(manifest["counts_by_type"].values()), manifest["total_unique_records"])
            self.assertEqual(manifest["record_verification"]["valid_records"], 3)
            self.assertEqual(manifest["record_verification"]["duplicate_keys"], 1)

    def test_streaming_csv_and_markdown_preserve_counts_format_and_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records_path = root / "records.jsonl"
            self.write_jsonl(records_path, self.mixed_rows())

            with patch.object(archive, "load_existing", side_effect=AssertionError("must stream")):
                archive.export_csv_stream(root / "records.csv", records_path)
                archive.export_markdown_stream(root / "markdown", records_path)

            with (root / "records.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertEqual(len(csv_rows), 3)
            self.assertEqual(csv_rows[0]["title"], "中文问题")
            self.assertNotIn("raw", csv_rows[0])

            markdown_files = sorted((root / "markdown").glob("*.md"))
            self.assertEqual([path.name for path in markdown_files], ["answer.md", "article.md", "comment_received.md"])
            answer_text = (root / "markdown" / "answer.md").read_text(encoding="utf-8")
            comment_text = (root / "markdown" / "comment_received.md").read_text(encoding="utf-8")
            self.assertTrue(answer_text.startswith("# answer (1)\n\n"))
            self.assertIn("## 中文问题", answer_text)
            self.assertIn("[原文](https://example.test/a1)", answer_text)
            self.assertTrue(comment_text.startswith("# comment_received (1)\n\n"))
            self.assertIn("parent: answer a1", comment_text)
            self.assertFalse(list((root / "markdown").glob("*.tmp")))

    def test_markdown_stream_matches_historical_newlines_and_final_terminator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records_path = root / "records.jsonl"
            rows = [
                {"archive_type": "answer", "id": "a1", "title": "一", "text": "甲", "created_time": ""},
                {"archive_type": "answer", "id": "a2", "title": "二", "text": "乙", "created_time": ""},
            ]
            self.write_jsonl(records_path, rows)

            archive.export_markdown_stream(root / "markdown", records_path)

            historical_text = "\n".join([
                "# answer (2)", "",
                "## 一", "", "甲", "", "---", "",
                "## 二", "", "乙", "", "---", "",
            ])
            expected = historical_text.replace("\n", os.linesep).encode("utf-8")
            self.assertEqual((root / "markdown" / "answer.md").read_bytes(), expected)

    def test_rebuild_exports_streams_and_includes_verification_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_jsonl(root / "records.jsonl", self.mixed_rows())
            (root / "manifest.json").write_text(
                json.dumps({"version": archive.VERSION, "categories": {}}), encoding="utf-8"
            )

            with patch.object(archive, "load_existing", side_effect=AssertionError("must stream")):
                result = archive.rebuild_exports(str(root))

            self.assertEqual(result, 0)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["total_unique_records"], 3)
            self.assertEqual(
                manifest["counts_by_type"],
                {"answer": 1, "article": 1, "comment_received": 1},
            )
            self.assertEqual(manifest["record_verification"]["valid_records"], 3)
            self.assertEqual(manifest["record_verification"]["bad_lines"], 0)
            self.assertEqual(manifest["record_verification"]["duplicate_keys"], 0)

    def test_network_archive_exports_once_only_after_collection_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            network_finished = False
            export_calls: list[str] = []

            def fake_pages(*_args, **_kwargs):
                nonlocal network_finished
                try:
                    yield {"id": "a1", "question": {"title": "Q"}, "content": "body"}
                finally:
                    network_finished = True

            def csv_export(_path, records_path):
                self.assertTrue(network_finished)
                self.assertEqual(Path(records_path).name, "records.jsonl")
                export_calls.append("csv")

            def markdown_export(_path, records_path):
                self.assertTrue(network_finished)
                self.assertEqual(Path(records_path).name, "records.jsonl")
                export_calls.append("markdown")

            args = archive.build_parser().parse_args([
                "--user", "test", "--output", tmp, "--types", "answers",
                "--auth-mode", "public", "--delay", "0",
                "--base-url", "http://127.0.0.1/api/v4/",
            ])
            with patch.object(archive, "resolve_auth", return_value=("", None)), patch.object(
                archive, "iter_pages", side_effect=fake_pages
            ), patch.object(archive, "export_csv", side_effect=AssertionError("list export used")), patch.object(
                archive, "export_markdown", side_effect=AssertionError("list export used")
            ), patch.object(archive, "export_csv_stream", side_effect=csv_export), patch.object(
                archive, "export_markdown_stream", side_effect=markdown_export
            ):
                result = archive.run_archive(args)

            self.assertEqual(result, 0)
            self.assertEqual(export_calls, ["csv", "markdown"])


if __name__ == "__main__":
    unittest.main()
