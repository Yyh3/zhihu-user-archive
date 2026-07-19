# Archive schema and coverage

## Files

### `records.jsonl`

Each line is one UTF-8 JSON object:

This append-oriented JSONL is the canonical archive. CSV, Markdown, summaries, and the SQLite state index are derived or rebuildable outputs.

- `archive_type`: `answer`, `article`, `pin`, `column`, `activity`, `comment_authored`, `comment_received`, `question`, or `zvideo`.
- `id`: stable Zhihu object ID when exposed; otherwise a deterministic hash.
- `title`, `url`, `text`, `excerpt`: normalized readable fields.
- `created_time`, `updated_time`: Unix timestamps when exposed.
- `author_name`, `author_token`: author metadata.
- `comment_count`, `voteup_count`: numeric counters when exposed.
- `parent_type`, `parent_id`: set for comments or nested records.
- `raw`: the original API object for loss-minimizing preservation.

### `records.csv`

A flat UTF-8 BOM CSV containing the normalized fields above except `raw`. Newlines are preserved inside quoted CSV cells.

### `markdown/<type>.md`

Readable documents grouped by archive type. HTML is converted to plain text; the JSONL retains original markup inside `raw`.

### `manifest.json`

- `target`: requested user and resolved URL token.
- `requested_types`: requested top-level categories.
- `content_comments`: `none` or `all`.
- `authentication`: selected authentication mode and whether isolated-browser fallback was available; it never contains cookie values.
- `categories`: per-category request count, unique record count, HTTP/error details, and status.
- `total_unique_records`: deduplicated total.
- `complete`: true only when every requested category completed without a coverage warning.

### `archive_state.sqlite3`

A disposable runtime database used for record deduplication, parent/root counts, stable-404 endpoint availability, and per-page resume checkpoints. It is not the source archive. Authentication failures and rate limits are not persisted as endpoint unavailability. If the database is missing, rebuild its record index offline from canonical `records.jsonl`:

```text
python scripts/archive_zhihu_user.py --output <directory> --rebuild-state-index
```

An in-place rebuild transactionally clears and reconstructs only `record_index`, while deliberately retaining parent progress, page checkpoints, and stable-404 endpoint capability state. If the database itself is suspect, preserve or remove it from the active path first, then run the command to create a fresh database. JSONL cannot reconstruct those transient progress tables.

SQLite `-wal` and `-shm` companions may exist while the archive is running and are runtime files too.

## Comment performance metrics

When `content_comments` is `all`, `categories.content-comments.performance` contains:

- `pages_requested`: parent/root-list pages requested across comment stages.
- `records_returned`: raw root and child rows returned, including overlap between stages.
- `records_added`: unique comment records appended to canonical JSONL during this run.
- `roots_seen`: root rows processed, including roots repeated by overlapping orders.
- `roots_skipped_known`: already indexed roots whose child trees were complete.
- `child_trees_skipped_complete`: repeated child trees avoided because indexed child count met `child_comment_count`.
- `child_tree_traversals`: incomplete child trees entered for child-endpoint traversal.
- `child_pages_requested`: pages requested from child-comment endpoints.
- `parents_completed_by_count`: parents whose archived count reached their visible `comment_count`; reaching a lower configured cap does not increment this field.
- `parents_partial_after_all_stages`: parents still short or stopped at a configured cap when the selected strategy finished.
- `endpoint_http_status_counts`: HTTP failures grouped by status code.
- `elapsed_seconds_by_stage`: cumulative local elapsed seconds grouped by stage.
- `mismatch_count`: parents whose final archived count differs from `comment_count`.
- `missing_sum`: sum of positive `comment_count - archived_count` differences.
- `top_mismatches`: up to 50 mismatched parents, ordered by missing count, with `parent_type`, `parent_id`, `expected`, `archived`, `missing`, `stage`, `status`, and `title`.

Inspect `mismatch_count` and `missing_sum` before reporting coverage. Use the page, traversal, skip, and child-page metrics to spot request amplification; high overlap should increase complete-tree skips rather than repeatedly paginate the same child tree.

## Coverage meanings

- `complete`: pagination reached the endpoint's end and no known count conflict exists.
- `partial`: some records were collected, but pagination, child comments, or visible-count comparison indicates a gap.
- `unavailable`: authentication, security verification, privacy, endpoint removal, or unsupported access prevented collection.
- `failed`: an unexpected transport or parsing error prevented collection.

Authored comments deserve special caution: Zhihu may not expose a public historical-comments tab or API for a profile. A zero-record response proves only that the tested endpoint returned zero records, not that the user never commented.

## Endpoint families

The script uses currently observed `/api/v4` endpoint families and follows server-provided pagination links. These are implementation details rather than a stable public API contract. Keep endpoint mappings in the script centralized and treat changes as expected drift.
