import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

if not __package__:
    sys.path.insert(0, str(TESTS_DIR.parent))
    __package__ = "tests"

from .fixtures import FakeClient, repeated_root_routes, root_comment


class InjectedStop(RuntimeError):
    pass


class Http404(RuntimeError):
    status = 404
    unavailable = True


class Http500(RuntimeError):
    status = 500


class Http401(RuntimeError):
    status = 401
    unavailable = True


class Http403(RuntimeError):
    status = 403
    unavailable = True


class Http429(RuntimeError):
    status = 429
    unavailable = True


class PagingClient(FakeClient):
    def get(self, path: str, params=None):
        order = str((params or {}).get("order_by") or (params or {}).get("order") or "")
        self.calls.append((path, order))
        route = self.routes.get((path, order), [])
        if isinstance(route, BaseException):
            raise route
        if isinstance(route, dict):
            return route
        return {"data": route, "paging": {"is_end": True}}


class ExactClient:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, path: str, params=None):
        copied = dict(params) if params is not None else None
        self.calls.append((path, copied))
        return self.routes.get(path, {"data": [], "paging": {"is_end": True}})


class CommentPipelineTests(unittest.TestCase):
    def make_pipeline_fixture(self, expected: int = 1):
        from archive_state import ArchiveState

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = ArchiveState(Path(tmp.name) / "state.sqlite3")
        self.addCleanup(state.close)
        client = PagingClient({
            ("comment_v5/answers/a1/root_comment", "score"): [root_comment("r1")],
        })
        written: list[dict] = []
        return state, client, written, expected

    def run_pipeline(self, state, client, written, expected: int = 1, **kwargs):
        from comment_pipeline import AdaptiveCommentPipeline

        pipeline = AdaptiveCommentPipeline(
            client,
            state,
            lambda record: written.append(record) or True,
            error_type=kwargs.pop("error_type", RuntimeError),
            **kwargs,
        )
        return pipeline.run_parent(
            {"archive_type": "answer", "id": "a1", "comment_count": expected}
        )

    def test_normal_completion_skips_reverse_and_root_comments(self) -> None:
        state, client, written, expected = self.make_pipeline_fixture(expected=1)
        result = self.run_pipeline(state, client, written, expected)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["stage"], "v5_score")
        self.assertEqual(state.archived_parent_count("answer", "a1"), 1)
        self.assertFalse(any(order == "reverse" for _, order in client.calls))
        self.assertFalse(any("root_comments" in path for path, _ in client.calls))

    def test_404_endpoint_is_cached_and_not_retried(self) -> None:
        from archive_state import ArchiveState

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = ArchiveState(Path(tmp.name) / "state.sqlite3")
        self.addCleanup(state.close)
        routes = {
            ("comment_v5/answers/a1/root_comment", order): []
            for order in ("score", "ts", "normal", "reverse")
        }
        routes[("answers/a1/root_comments", "normal")] = Http404("missing")
        client = PagingClient(routes)
        written: list[dict] = []
        first = self.run_pipeline(state, client, written, expected=1, error_type=Http404)
        first_calls = len(client.calls)
        second = self.run_pipeline(state, client, written, expected=1, error_type=Http404)
        self.assertEqual(len(client.calls), first_calls)
        self.assertEqual(state.endpoint_status("answer", "answers/a1/root_comments"), "unavailable")
        self.assertEqual(first["stage"], "legacy_root_normal")
        self.assertEqual(second["stage"], "legacy_root_normal")
        self.assertEqual(second["status"], "short")
        self.assertEqual([record["id"] for record in written], [])
        self.assertEqual(state.archived_parent_count("answer", "a1"), 0)

    def test_401_is_raised_and_not_persisted_as_endpoint_unavailable(self) -> None:
        state, _, written, _ = self.make_pipeline_fixture()
        client = PagingClient({
            ("comment_v5/answers/a1/root_comment", "score"): Http401("login required"),
        })
        with self.assertRaises(Http401):
            self.run_pipeline(state, client, written, error_type=Http401)
        self.assertIsNone(
            state.endpoint_status("answer", "comment_v5/answers/a1/root_comment")
        )

    def test_403_is_raised_and_not_persisted_as_endpoint_unavailable(self) -> None:
        state, _, written, _ = self.make_pipeline_fixture()
        client = PagingClient({
            ("comment_v5/answers/a1/root_comment", "score"): Http403("forbidden"),
        })
        with self.assertRaises(Http403):
            self.run_pipeline(state, client, written, error_type=Http403)
        self.assertIsNone(
            state.endpoint_status("answer", "comment_v5/answers/a1/root_comment")
        )

    def test_429_is_raised_and_not_persisted_as_endpoint_unavailable(self) -> None:
        state, _, written, _ = self.make_pipeline_fixture()
        client = PagingClient({
            ("comment_v5/answers/a1/root_comment", "score"): Http429("rate limited"),
        })
        with self.assertRaises(Http429):
            self.run_pipeline(state, client, written, error_type=Http429)
        self.assertIsNone(
            state.endpoint_status("answer", "comment_v5/answers/a1/root_comment")
        )

    def test_only_missing_children_are_written(self) -> None:
        from archive_state import ArchiveState

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = ArchiveState(Path(tmp.name) / "state.sqlite3")
        self.addCleanup(state.close)
        state.upsert_record("comment_received", "r1", "answer", "a1", "", 2)
        state.upsert_record("comment_received", "c1", "answer", "a1", "r1", 0)
        root = root_comment("r1", ("c1", "c2"))
        client = PagingClient({
            ("comment_v5/answers/a1/root_comment", "score"): [root],
        })
        written: list[dict] = []
        result = self.run_pipeline(state, client, written, expected=3)
        self.assertEqual([record["id"] for record in written], ["c2"])
        self.assertEqual(result["records_added"], 1)
        self.assertEqual(result["stage"], "v5_score")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["roots_skipped_known"], 0)
        self.assertEqual(state.archived_parent_count("answer", "a1"), 3)

    def test_checkpoint_resumes_from_saved_next_url(self) -> None:
        from archive_state import ArchiveState
        from comment_pipeline import AdaptiveCommentPipeline

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = ArchiveState(Path(tmp.name) / "state.sqlite3")
        self.addCleanup(state.close)
        client = PagingClient({
            ("comment_v5/answers/a1/root_comment", "score"): {
                "data": [root_comment("r1")],
                "paging": {"is_end": False, "next": "NEXT"},
            },
            ("NEXT", ""): InjectedStop("stop"),
        })
        written: list[dict] = []
        pipeline = AdaptiveCommentPipeline(client, state, lambda record: written.append(record) or True)
        with self.assertRaises(InjectedStop):
            pipeline.run_parent({"archive_type": "answer", "id": "a1", "comment_count": 2})
        checkpoint = state.load_checkpoint(
            "answer", "a1", "v5_score", "comment_v5/answers/a1/root_comment", "score"
        )
        self.assertEqual(checkpoint.cursor, "NEXT")
        client.routes[("NEXT", "")] = {
            "data": [root_comment("r2")], "paging": {"is_end": True}
        }
        resumed = pipeline.run_parent({"archive_type": "answer", "id": "a1", "comment_count": 2})
        self.assertEqual(client.calls[-1][0], "NEXT")
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(resumed["stage"], "v5_score")
        self.assertEqual([record["id"] for record in written], ["r1", "r2"])
        self.assertEqual(state.archived_parent_count("answer", "a1"), 2)

    def test_all_six_stages_use_distinct_real_endpoints_and_parameter_names(self) -> None:
        from archive_state import ArchiveState
        from comment_pipeline import AdaptiveCommentPipeline

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = ArchiveState(Path(tmp.name) / "state.sqlite3")
        self.addCleanup(state.close)
        client = ExactClient()
        result = AdaptiveCommentPipeline(client, state, lambda record: True).run_parent(
            {"archive_type": "answer", "id": "a1", "comment_count": 1}
        )
        self.assertEqual(client.calls, [
            ("comment_v5/answers/a1/root_comment", {"order_by": "score"}),
            ("comment_v5/answers/a1/root_comment", {"order_by": "ts"}),
            ("answers/a1/comments", {"order": "normal"}),
            ("answers/a1/comments", {"order": "reverse"}),
            ("answers/a1/root_comments", {"order": "normal"}),
            ("answers/a1/root_comments", {"order": "reverse"}),
        ])
        self.assertEqual(result["stage"], "legacy_root_reverse")
        self.assertEqual(result["status"], "short")

    def test_v5_404_does_not_suppress_legacy_comments_endpoint(self) -> None:
        from archive_state import ArchiveState
        from comment_pipeline import AdaptiveCommentPipeline

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = ArchiveState(Path(tmp.name) / "state.sqlite3")
        self.addCleanup(state.close)

        class EndpointClient(ExactClient):
            def get(self, path, params=None):
                copied = dict(params) if params is not None else None
                self.calls.append((path, copied))
                if path.startswith("comment_v5/"):
                    raise Http404("v5 missing")
                if path == "answers/a1/comments":
                    return {"data": [root_comment("r1")], "paging": {"is_end": True}}
                return {"data": [], "paging": {"is_end": True}}

        client = EndpointClient()
        written: list[dict] = []
        result = AdaptiveCommentPipeline(
            client, state, lambda record: written.append(record) or True, error_type=Http404
        ).run_parent({"archive_type": "answer", "id": "a1", "comment_count": 1})
        self.assertEqual([record["id"] for record in written], ["r1"])
        self.assertIn(("answers/a1/comments", {"order": "normal"}), client.calls)
        self.assertEqual(result["stage"], "legacy_comments_normal")
        self.assertEqual(result["status"], "complete")

    def test_child_http_failure_is_counted_once_when_it_bubbles(self) -> None:
        from archive_state import ArchiveState
        from comment_pipeline import AdaptiveCommentPipeline

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = ArchiveState(Path(tmp.name) / "state.sqlite3")
        self.addCleanup(state.close)
        root = root_comment("r1")
        root["child_comment_count"] = 1
        client = PagingClient({
            ("comment_v5/answers/a1/root_comment", "score"): [root],
            ("comment_v5/comment/r1/child_comment", ""): Http500("server error"),
        })
        pipeline = AdaptiveCommentPipeline(
            client,
            state,
            lambda record: True,
            error_type=Http500,
        )
        with self.assertRaises(Http500) as raised:
            pipeline.run_parent(
                {"archive_type": "answer", "id": "a1", "comment_count": 2}
            )
        self.assertEqual(
            raised.exception.comment_metrics["endpoint_http_status_counts"],
            {"500": 1},
        )

    def test_expected_count_stops_before_later_roots_in_same_page(self) -> None:
        state, client, written, _ = self.make_pipeline_fixture(expected=1)
        client.routes[("comment_v5/answers/a1/root_comment", "score")] = [
            root_comment("r1"), root_comment("r2")
        ]
        result = self.run_pipeline(state, client, written, expected=1)
        self.assertEqual([record["id"] for record in written], ["r1"])
        self.assertEqual(result["roots_seen"], 1)
        self.assertEqual(result["archived"], 1)
        self.assertEqual(result["status"], "complete")

    def test_comment_cap_stops_even_exhaustive_strategy_before_later_roots(self) -> None:
        from comment_pipeline import CommentOptions

        state, client, written, _ = self.make_pipeline_fixture(expected=2)
        client.routes[("comment_v5/answers/a1/root_comment", "score")] = [
            root_comment("r1"), root_comment("r2")
        ]
        result = self.run_pipeline(
            state,
            client,
            written,
            expected=2,
            options=CommentOptions(strategy="exhaustive", max_comments_per_parent=1),
        )
        self.assertEqual([record["id"] for record in written], ["r1"])
        self.assertEqual(result["archived"], 1)
        self.assertEqual(result["status"], "capped")

    def test_expected_count_stops_inside_child_page_without_finishing_tree(self) -> None:
        from archive_state import ArchiveState

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = ArchiveState(Path(tmp.name) / "state.sqlite3")
        self.addCleanup(state.close)
        root = root_comment("r1")
        root["child_comment_count"] = 2
        client = PagingClient({
            ("comment_v5/answers/a1/root_comment", "score"): [root],
            ("comment_v5/comment/r1/child_comment", ""): [
                {"id": "c1", "reply_comment_id": "r1"},
                {"id": "c2", "reply_comment_id": "r1"},
            ],
        })
        written: list[dict] = []
        result = self.run_pipeline(state, client, written, expected=2)
        self.assertEqual([record["id"] for record in written], ["r1", "c1"])
        self.assertEqual(result["archived"], 2)
        self.assertEqual(result["child_pages_requested"], 1)
        self.assertEqual(result["status"], "complete")
        self.assertIsNone(state.load_checkpoint(
            "answer", "a1", "v5_score", "comment_v5/comment/r1/child_comment", ""
        ))

    def test_known_complete_root_skips_child_endpoint_in_later_stage(self) -> None:
        from archive_state import ArchiveState
        from comment_pipeline import AdaptiveCommentPipeline

        with tempfile.TemporaryDirectory() as tmp:
            state = ArchiveState(Path(tmp) / "state.sqlite3")
            state.upsert_record("comment_received", "r1", "answer", "a1", "", 2)
            state.upsert_record("comment_received", "c1", "answer", "a1", "r1", 0)
            state.upsert_record("comment_received", "c2", "answer", "a1", "r1", 0)
            client = FakeClient({
                ("comment_v5/answers/a1/root_comment", "ts"): [root_comment("r1")],
            })
            pipeline = AdaptiveCommentPipeline(client, state, lambda record: False)
            result = pipeline.run_parent({"archive_type": "answer", "id": "a1", "comment_count": 3})
            self.assertEqual(result["child_pages_requested"], 0)
            state.close()

    def test_parent_reaching_expected_count_stops_before_legacy(self) -> None:
        from archive_state import ArchiveState
        from comment_pipeline import AdaptiveCommentPipeline

        with tempfile.TemporaryDirectory() as tmp:
            state = ArchiveState(Path(tmp) / "state.sqlite3")
            client = FakeClient({
                ("comment_v5/answers/a1/root_comment", "score"): [root_comment("r1")],
            })
            written: list[dict] = []
            pipeline = AdaptiveCommentPipeline(client, state, lambda record: written.append(record) or True)
            result = pipeline.run_parent({"archive_type": "answer", "id": "a1", "comment_count": 1})
            self.assertEqual(result["stage"], "v5_score")
            self.assertFalse(any("root_comments" in path for path, _ in client.calls))
            state.close()

    def test_repeated_orders_write_unique_roots_and_traverse_each_child_tree_once(self) -> None:
        from archive_state import ArchiveState
        from comment_pipeline import AdaptiveCommentPipeline

        with tempfile.TemporaryDirectory() as tmp:
            state = ArchiveState(Path(tmp) / "state.sqlite3")
            client = FakeClient(repeated_root_routes())
            written: list[dict] = []
            pipeline = AdaptiveCommentPipeline(client, state, lambda record: written.append(record) or True)
            pipeline.run_parent({"archive_type": "answer", "id": "a1", "comment_count": 201})

            root_ids = [str(record["id"]) for record in written if not record.get("reply_comment_id")]
            self.assertEqual(len(root_ids), 100)
            self.assertEqual(len(set(root_ids)), 100)

            root_route_calls = [
                (path, order)
                for path, order in client.calls
                if path in {
                    "comment_v5/answers/a1/root_comment",
                    "answers/a1/comments",
                }
            ]
            self.assertEqual(root_route_calls, [
                ("comment_v5/answers/a1/root_comment", "score"),
                ("comment_v5/answers/a1/root_comment", "ts"),
                ("answers/a1/comments", "normal"),
                ("answers/a1/comments", "reverse"),
            ])

            child_tree_calls = Counter(
                path for path, _ in client.calls if path.endswith("/child_comment")
            )
            self.assertLessEqual(sum(child_tree_calls.values()), 100)
            self.assertTrue(all(count <= 1 for count in child_tree_calls.values()))
            state.close()

    def test_overlap_fixture_bounds_request_amplification_and_adaptive_pages(self) -> None:
        from archive_state import ArchiveState
        from comment_pipeline import AdaptiveCommentPipeline, CommentOptions

        def run(strategy: str) -> dict:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            state = ArchiveState(Path(tmp.name) / "state.sqlite3")
            self.addCleanup(state.close)
            for index in range(100):
                state.upsert_record(
                    "comment_received", f"c{index}", "answer", "a1", f"r{index}", 0
                )
            client = FakeClient(repeated_root_routes())
            written: list[dict] = []
            pipeline = AdaptiveCommentPipeline(
                client,
                state,
                lambda record: written.append(record) or True,
                options=CommentOptions(strategy=strategy),
            )
            metrics = pipeline.run_parent(
                {"archive_type": "answer", "id": "a1", "comment_count": 200}
            )
            self.assertEqual(metrics["records_added"], 100)
            self.assertLessEqual(metrics["child_tree_traversals"], 100)
            return metrics

        adaptive = run("adaptive")
        exhaustive = run("exhaustive")
        self.assertEqual(adaptive["pages_requested"], 1)
        self.assertEqual(adaptive["stage"], "v5_score")
        self.assertGreaterEqual(exhaustive["child_trees_skipped_complete"], 300)
        self.assertLess(adaptive["pages_requested"], exhaustive["pages_requested"])


if __name__ == "__main__":
    unittest.main()
